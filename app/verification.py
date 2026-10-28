"""Read-only verification of the Music Assistant <-> skill channel.

Answers "is the Alexa player (Music Assistant's alexa provider) correctly
configured to talk to this skill, and does it send correct information?"
with flow-free checks — Music Assistant is only ever READ (never
written to, no flow started), and the only outbound request is a GET
against the skill's own /ma/latest-url endpoint:

  V1   provider health      alexa provider enabled, loaded, no last error
  V2a  setup keys           setup_data carries api_url/api_username/api_password
                            (presence only — values are encrypted at rest)
  V2b  push observed        the scheme://host Music Assistant actually pushed
                            from, recorded on each authenticated /ma/push-url
  V3   endpoint reachable   GET <skill endpoint>/ma/latest-url answers
                            (the configured origin + path prefix, so a
                            prefixed deployment is never judged by the
                            bare host)
  V4   basic auth           the same fetch is accepted (401 = rejected)
  V5   push freshness       how long ago the last push arrived
  V6   now playing          what MA says is playing vs the last pushed stream
  V7   skill invocations    the last request that reached the skill (the
                            Alexa -> skill leg; MA pushes arrive here too)
  V8   stream host          stored stream host vs the configured MA hostname
                            (unvalidated mode is flagged, not assumed fine)
  V9   player inventory     which alexa players MA has, with states/availability
  V10  Amazon account link  stored LWA refresh/access token health (read-only;
                            token values are never carried out)
  V11  public reachability  folds the cached add-on reachability results into
                            the summary (reuses the existing probe cache)
  V12  invocation name      the invocation name the skill deploys is the name
                            Music Assistant addresses in its start command and
                            in the control-command prefix (issue #71 class)
  V13  MA to Amazon channel correlates the stream push with the Echo's own
                            playback reports: a fresh push that its player
                            never reports as current means MA's Amazon
                            session is dead (issue #86 class — every other
                            check stays green)

Failures become check states, never exceptions.
"""

import asyncio
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import shared_store
from app_settings import get_setting
from env_secrets import get_env_secret
from markupsafe import escape
from public_urls import public_base
from ui_markup import row

FETCH_TIMEOUT_SECONDS = 5
MA_TIMEOUT_SECONDS = 8
FRESH_PUSH_SECONDS = 3600
FRESH_INTENT_SECONDS = 86400
# MA's play sequence is push URL -> ask Amazon -> Alexa invokes the skill ->
# the Echo reports AudioPlayer.PlaybackStarted. The last leg takes seconds;
# a shorter window would warn on a playback that is still starting.
CHANNEL_GRACE_SECONDS = 60

_PILL = {
    "pass": ("Passed", "success"),
    "fail": ("Failed", "error"),
    "warning": ("Warning", "warning"),
    "unknown": ("Unknown", "neutral"),
    "info": ("Info", "neutral"),
}

# The MA API credentials are set in the web Setup (or the add-on's
# Configuration tab when running as an add-on), so "not configured" must
# say where they are set (issue #47 follow-up).
_CONFIG_HINT = (
    " Set MA_API_URL and MA_API_TOKEN in the web Setup "
    "(Settings), or in the add-on's Configuration tab."
)

# Check titles — one definition each; every state branch of a check shares
# its title.
_TITLE_PROVIDER = "Alexa provider"
_TITLE_SETUP = "Provider setup keys"
_TITLE_ENDPOINT = "Endpoint reachable"
_TITLE_AUTH = "Basic auth"
_TITLE_FRESHNESS = "Push freshness"
_TITLE_PLAYING = "Now playing"
_TITLE_INVOCATIONS = "Skill invocations"
_TITLE_STREAM = "Stream host"
_TITLE_PLAYERS = "Alexa players"
_TITLE_AMAZON = "Amazon account link"
_TITLE_REACH = "Public reachability"
_TITLE_INVOCATION = "Invocation name"
_TITLE_CHANNEL = "MA to Amazon channel"

# Music Assistant's start commands (ALEXA_LANGUAGE_COMMANDS in
# music-assistant/server music_assistant/providers/alexa/__init__.py;
# identical in the 2.10.7 tag and dev). Locales absent from the table fall
# back to MA's English default — correct for en-AU/en-GB/en-IN, an upstream
# gap for es-MX.
MA_DEFAULT_START_COMMAND = "ask music assistant to play audio"
MA_START_COMMANDS = {
    "de-DE": "sag music assistant spiele audio",
    "en-CA": MA_DEFAULT_START_COMMAND,
    "en-US": MA_DEFAULT_START_COMMAND,
    "es-ES": "pídele a music assistant que reproduzca audio",
    "fr-CA": "music assistant",
    "fr-FR": "music assistant",
    "it-IT": "chiedi a music assistant di riprodurre audio",
    "pt-BR": "peça ao music assistant para reproduzir áudio",
    "nl-NL": "speel music assistant",
}


def _origin(url):
    """Normalized (scheme, host, port) of a URL, or None when unusable."""
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    if port is None:
        port = 443 if parts.scheme.lower() == "https" else 80
    return (parts.scheme.lower(), (parts.hostname or "").lower(), port)


def _skill_base_url(url):
    """scheme://host[:port][/prefix] form of the skill endpoint, or "".

    The API is served wherever the skill is mounted (DispatcherMiddleware
    mounts it at /ma below the app root), so a skill endpoint carrying a
    path prefix (e.g. https://host/ma-alexa-skill/) is probed WITH the
    prefix — the bare origin may be answered by a different service (for
    example the Music Assistant stream server), and a 404 from that
    service would be a false pass.
    """
    origin = _origin(url)
    if origin is None:
        return ""
    scheme, host, port = origin
    if ":" in host:  # IPv6 literal from urlsplit needs its brackets back.
        host = f"[{host}]"
    default_port = 443 if scheme == "https" else 80
    netloc = host if port == default_port else f"{host}:{port}"
    path = urlsplit(url).path.rstrip("/")
    return f"{scheme}://{netloc}{path}"


def _check(check_id, title, state, detail):
    return {"id": check_id, "title": title, "state": state, "detail": detail}


def _norm(text):
    """Case/whitespace-insensitive form for title comparisons."""
    return " ".join(str(text or "").casefold().split())


def _last_error(cfg):
    """Provider last error as a plain message (dict/object/string tolerant)."""
    error = getattr(cfg, "last_error", None)
    if error is None:
        return None
    if isinstance(error, dict):
        return error.get("message") or str(error)
    return str(getattr(error, "message", None) or error)


async def _read_providers(client):
    """Provider configs plus a readable error string (or None)."""
    try:
        configs = await client.config.get_provider_configs(include_values=False)
        return configs, None
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"


async def _read_setup_keys(client, alexa_cfg):
    """(keys, error) for V2a — key PRESENCE only; values never leave here.

    The provider instance ID can differ from its domain, so the lookup uses
    the discovered config's ID; with no alexa config at all the lookup is
    skipped (V1 already reports that).
    """
    if alexa_cfg is None:
        return None, None  # setup_keys None → V2a "not read", V1 fails instead
    instance_id = getattr(alexa_cfg, "instance_id", None) or "alexa"
    try:
        setup = await client.config.get_provider_config_value(
            instance_id=instance_id, key="setup_data", default=None
        )
    except Exception as exc:
        if "not found" in str(exc):
            return [], None  # provider setup has never run
        return None, f"{type(exc).__name__}: {exc}"
    if isinstance(setup, dict):
        return sorted(setup), None
    if setup is None:
        return [], None  # no setup data recorded at all
    return None, f"unexpected setup_data type {type(setup).__name__}"


def _read_alexa_players(players_raw):
    """Shape raw players/all dicts into the store's player records (V6/V9)."""
    players = []
    for item in players_raw:
        if item.get("provider") != "alexa":
            continue
        media = item.get("current_media")
        players.append(
            {
                "player_id": item.get("player_id"),
                "name": item.get("name"),
                "state": str(item.get("playback_state") or ""),
                "available": bool(item.get("available")),
                "powered": item.get("powered"),
                "title": media.get("title") if isinstance(media, dict) else None,
                "artist": media.get("artist") if isinstance(media, dict) else None,
            }
        )
    return players


async def _read_channel_state(server_url, token):
    """One MA connection for V1 (health), V2a (setup keys) and V6 (players)."""
    import aiohttp
    from music_assistant_client import MusicAssistantClient

    async with (
        # Timeout first: `async with` enters left to right, so the deadline
        # must start before the client connects (handshake included).
        asyncio.timeout(MA_TIMEOUT_SECONDS),
        aiohttp.ClientSession() as session,
        MusicAssistantClient(server_url, session, token=token) as client,
    ):
        # server_info is a property set by connect(), not a coroutine.
        info = client.server_info
        configs, providers_error = await _read_providers(client)
        alexa_cfg = next(
            (cfg for cfg in configs if getattr(cfg, "domain", None) == "alexa"), None
        )
        setup_keys, setup_error = await _read_setup_keys(client, alexa_cfg)
        players = _read_alexa_players(await client.send_command("players/all"))
    return {
        "configured": True,
        "server_version": getattr(info, "server_version", None),
        "providers_error": providers_error,
        "alexa": None
        if alexa_cfg is None
        else {
            "enabled": bool(getattr(alexa_cfg, "enabled", False)),
            "status": str(getattr(alexa_cfg, "status", "") or ""),
            "last_error": _last_error(alexa_cfg),
        },
        "setup_keys": setup_keys,
        "setup_error": setup_error,
        "alexa_players": players,
    }


def _read_channel():
    """Sync wrapper: one MA connection, failures recorded as data."""
    server_url = get_env_secret("MA_API_URL")
    token = get_env_secret("MA_API_TOKEN")
    if not server_url or not token:
        return {"configured": False}
    try:
        return asyncio.run(_read_channel_state(server_url, token))
    except Exception as exc:
        return {"configured": True, "error": f"{type(exc).__name__}: {exc}"}


def _self_fetch(base):
    """GET <base>/ma/latest-url with the app's own basic auth.

    Returns {"url", "status", "error", "auth"}. trust_env=False so a stray
    proxy env never routes the LAN self-fetch away from the target.
    """
    import requests

    url = f"{base.rstrip('/')}/ma/latest-url"
    username = get_env_secret("APP_USERNAME")
    password = get_env_secret("APP_PASSWORD")
    auth = (username, password) if username and password else None
    with requests.Session() as session:
        session.trust_env = False
        try:
            response = session.get(url, auth=auth, timeout=FETCH_TIMEOUT_SECONDS)
        except Exception as exc:
            return {
                "url": url,
                "status": None,
                "error": f"{type(exc).__name__}: {exc}",
                "auth": auth is not None,
            }
    return {
        "url": url,
        "status": response.status_code,
        "error": None,
        "auth": auth is not None,
    }


def _v1_provider(channel):
    if not channel.get("configured"):
        return _check(
            "v1",
            _TITLE_PROVIDER,
            "unknown",
            "Music Assistant API is not configured." + _CONFIG_HINT,
        )
    if channel.get("error"):
        return _check(
            "v1",
            _TITLE_PROVIDER,
            "fail",
            f"Music Assistant is unreachable: {channel['error']}",
        )
    if channel.get("providers_error"):
        return _check(
            "v1",
            _TITLE_PROVIDER,
            "fail",
            f"Provider list unreadable: {channel['providers_error']}",
        )
    alexa = channel.get("alexa")
    if not alexa:
        return _check(
            "v1",
            _TITLE_PROVIDER,
            "fail",
            "The alexa provider is not configured in Music Assistant.",
        )
    if alexa.get("last_error"):
        return _check(
            "v1", _TITLE_PROVIDER, "fail", f"Provider error: {alexa['last_error']}"
        )
    if not alexa.get("enabled"):
        return _check("v1", _TITLE_PROVIDER, "fail", "The alexa provider is disabled.")
    if (alexa.get("status") or "").lower() != "loaded":
        return _check(
            "v1",
            _TITLE_PROVIDER,
            "fail",
            f"Provider status is '{alexa.get('status')}' (expected 'loaded').",
        )
    return _check("v1", _TITLE_PROVIDER, "pass", "Enabled, loaded, no errors.")


def _v2a_setup_keys(channel):
    if not channel.get("configured"):
        return _check(
            "v2a",
            _TITLE_SETUP,
            "unknown",
            "Not tested — MA not configured." + _CONFIG_HINT,
        )
    if channel.get("error"):
        return _check(
            "v2a",
            _TITLE_SETUP,
            "unknown",
            "Not tested — Music Assistant is unreachable (see provider health).",
        )
    if channel.get("setup_error"):
        return _check(
            "v2a",
            _TITLE_SETUP,
            "unknown",
            f"setup_data could not be read: {channel['setup_error']}",
        )
    keys = channel.get("setup_keys")
    if keys is None:
        return _check("v2a", _TITLE_SETUP, "unknown", "setup_data was not read.")
    missing = [
        key for key in ("api_url", "api_username", "api_password") if key not in keys
    ]
    if missing:
        return _check(
            "v2a",
            _TITLE_SETUP,
            "fail",
            f"setup_data is missing {', '.join(missing)} — re-run the provider "
            "setup in Music Assistant.",
        )
    return _check(
        "v2a",
        _TITLE_SETUP,
        "pass",
        "api_url, api_username and api_password are set "
        "(values are encrypted in Music Assistant).",
    )


def _v2b_push_base(store):
    base = store.get("push_base")
    if not base:
        return _check(
            "v2b",
            "Push endpoint observed",
            "unknown",
            "No push has arrived yet — play something in Music Assistant to "
            "record the URL it uses.",
        )
    return _check(
        "v2b",
        "Push endpoint observed",
        "pass",
        f"Music Assistant last pushed from {base}.",
    )


def _v3_v4_fetch(store):
    push_base = (store.get("push_base") or "").strip()
    skill_host = (get_setting("SKILL_HOSTNAME", "") or "").strip()
    # The recorded push address (request.host_url — origin only) selects the
    # "via" wording when BOTH addresses are usable and share their origin
    # (scheme+host+port): /ma/push-url records the request-derived host, and
    # the basic-auth credentials must never be sent to an origin the
    # deployment did not configure. Comparing raw origins would let two
    # unusable values (None == None) select the unvalidated push address,
    # so unusable values never match.
    push_origin = _origin(push_base) if push_base else None
    skill_origin = _origin(skill_host) if skill_host else None
    use_push_base = push_origin is not None and push_origin == skill_origin
    # Probe the configured skill endpoint (origin + path prefix): the API is
    # served wherever the skill is mounted, so a prefixed endpoint must be
    # probed WITH its prefix — the bare origin may be answered by a
    # different service (e.g. the Music Assistant stream server) and its
    # 404 would be a false pass (issue #47). The recorded push address
    # never carries the prefix, so it can only ever agree on the origin.
    base = _skill_base_url(skill_host)
    if not base:
        return [
            _check(
                "v3",
                _TITLE_ENDPOINT,
                "unknown",
                "No usable SKILL_HOSTNAME and no same-origin push recorded"
                " — no URL to test.",
            ),
            _check("v4", _TITLE_AUTH, "unknown", "Not tested — no endpoint URL."),
        ]
    via = (
        "the address Music Assistant pushed from"
        if use_push_base
        else "the configured skill hostname"
    )
    fetched = _self_fetch(base)
    if fetched["error"] is not None:
        return [
            _check(
                "v3",
                _TITLE_ENDPOINT,
                "fail",
                f"GET {fetched['url']} failed via {via}: {fetched['error']}",
            ),
            _check(
                "v4",
                _TITLE_AUTH,
                "unknown",
                "Not tested — the endpoint was unreachable.",
            ),
        ]
    status = fetched["status"]
    v3 = _check(
        "v3",
        _TITLE_ENDPOINT,
        "pass",
        f"GET {fetched['url']} answered HTTP {status} (via {via}).",
    )
    return [v3, _v4_auth_check(status, fetched["auth"])]


def _v4_auth_check(status, auth):
    """Map the self-fetch HTTP status onto the V4 basic-auth check."""
    if status == 401:
        return _check(
            "v4",
            _TITLE_AUTH,
            "fail",
            "The endpoint rejected the configured username/password (HTTP 401).",
        )
    if status not in (200, 404):
        return _check(
            "v4",
            _TITLE_AUTH,
            "unknown",
            f"Unexpected HTTP {status} — credentials not verified.",
        )
    if not auth:
        return _check(
            "v4",
            _TITLE_AUTH,
            "unknown",
            "No APP_USERNAME/APP_PASSWORD are configured — basic auth is "
            "not enforced, nothing to verify.",
        )
    note = "no stream stored yet" if status == 404 else "stream stored"
    return _check(
        "v4",
        _TITLE_AUTH,
        "pass",
        f"The endpoint accepted the configured credentials ({note}, HTTP {status}).",
    )


def _age_label(seconds):
    if seconds < 120:
        return "moments ago"
    if seconds < 3600:
        return f"{int(seconds // 60)} minutes ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} hours ago"
    return f"{int(seconds // 86400)} days ago"


def _v5_freshness(store):
    timestamp = store.get("timestamp")
    if not timestamp:
        return _check(
            "v5", _TITLE_FRESHNESS, "unknown", "No stream push has been stored yet."
        )
    try:
        age = max(0.0, time.time() - float(timestamp))
    except (TypeError, ValueError):
        return _check(
            "v5",
            _TITLE_FRESHNESS,
            "unknown",
            "The stored push timestamp is unreadable.",
        )
    label = _age_label(age)
    if age <= FRESH_PUSH_SECONDS:
        return _check("v5", _TITLE_FRESHNESS, "pass", f"The last push arrived {label}.")
    return _check(
        "v5",
        _TITLE_FRESHNESS,
        "warning",
        f"The last push arrived {label} — either nothing has played since, "
        "or pushes stopped.",
    )


def _v6_now_playing(channel, store):
    if not channel.get("configured"):
        return _check(
            "v6",
            _TITLE_PLAYING,
            "unknown",
            "Not tested — MA not configured." + _CONFIG_HINT,
        )
    if channel.get("error"):
        return _check(
            "v6",
            _TITLE_PLAYING,
            "unknown",
            "Not tested — Music Assistant state is unavailable.",
        )
    players = channel.get("alexa_players") or []
    if not players:
        return _check(
            "v6", _TITLE_PLAYING, "fail", "Music Assistant reports no Alexa players."
        )
    playing = [player for player in players if player.get("state") == "playing"]
    if not playing:
        return _check(
            "v6",
            _TITLE_PLAYING,
            "unknown",
            "Nothing is playing on an Alexa player right now — nothing to compare.",
        )
    store_title = _norm(store.get("title"))
    if not store_title:
        return _check(
            "v6",
            _TITLE_PLAYING,
            "unknown",
            "Alexa is playing but the last push carried no title to compare.",
        )
    for player in playing:
        if _norm(player.get("title")) == store_title:
            where = player.get("name") or player.get("player_id")
            return _check(
                "v6",
                _TITLE_PLAYING,
                "pass",
                f"'{player.get('title')}' on {where} matches the last push.",
            )
    titles = ", ".join(f"'{p.get('title') or 'untitled'}'" for p in playing)
    return _check(
        "v6",
        _TITLE_PLAYING,
        "fail",
        f"Music Assistant is playing {titles} but the last push was "
        f"'{store.get('title')}'.",
    )


def _entry_age(entry):
    """Seconds since an invocation entry, else None if no usable timestamp."""
    if not isinstance(entry, dict):
        return None
    from endpoints.invocations import _entry_timestamp

    stamp = _entry_timestamp(entry, entry.get("incoming"))
    if isinstance(stamp, (int, float)):
        return max(0.0, time.time() - float(stamp))
    if isinstance(stamp, str):
        try:
            # fromisoformat handles the Alexa Z form and explicit offsets;
            # naive values are assumed UTC (Alexa timestamps are UTC).
            moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return max(0.0, time.time() - moment.timestamp())
    return None


def _v7_invocations(logs):
    if not logs:
        return _check(
            "v7",
            _TITLE_INVOCATIONS,
            "unknown",
            "No requests have reached the skill yet — talk to Alexa or play "
            "something to record one.",
        )
    # Entries are appended when a request FINISHES, so out-of-order completion
    # means logs[-1] is not necessarily the newest request — pick by age.
    usable = [(age, entry) for entry in logs if (age := _entry_age(entry)) is not None]
    if not usable:
        return _check(
            "v7",
            _TITLE_INVOCATIONS,
            "unknown",
            "The last invocation has no usable timestamp.",
        )
    age, latest = min(usable, key=lambda pair: pair[0])
    from endpoints.invocations import _entry_name

    name = _entry_name(latest.get("incoming")) or "request"
    label = _age_label(age)
    if age <= FRESH_INTENT_SECONDS:
        return _check(
            "v7", _TITLE_INVOCATIONS, "pass", f"Last request ({name}) arrived {label}."
        )
    return _check(
        "v7",
        _TITLE_INVOCATIONS,
        "warning",
        f"Last request ({name}) arrived {label} — nothing has reached the skill "
        "recently.",
    )


def _v8_stream_host(store):
    stream = (store.get("streamUrl") or "").strip()
    if not stream:
        return _check("v8", _TITLE_STREAM, "unknown", "No stream has been stored yet.")
    try:
        source = urlsplit(stream)
    except ValueError:
        source = None
    stored_host = (source.netloc or "").lower() if source else ""
    base = get_setting("MA_HOSTNAME", "").strip()
    if not base:
        return _check(
            "v8",
            _TITLE_STREAM,
            "warning",
            "MA_HOSTNAME is not configured — pushes are stored without host "
            f"validation (stored host: {stored_host or 'unknown'}).",
        )
    try:
        base_host = urlsplit(public_base(base)).netloc.lower()
    except ValueError:
        return _check(
            "v8",
            _TITLE_STREAM,
            "unknown",
            f"The configured MA hostname is not a valid public URL ({base}).",
        )
    if stored_host and stored_host == base_host:
        return _check(
            "v8",
            _TITLE_STREAM,
            "pass",
            f"Stored stream host {stored_host} matches the configured MA hostname.",
        )
    return _check(
        "v8",
        _TITLE_STREAM,
        "fail",
        f"Stored stream host {stored_host or 'unknown'} does not match the "
        f"configured MA hostname ({base_host}) — the stored stream may not be "
        "the one Music Assistant pushes.",
    )


def _v9_players(channel):
    if not channel.get("configured"):
        return _check(
            "v9",
            _TITLE_PLAYERS,
            "unknown",
            "Not available — MA not configured." + _CONFIG_HINT,
        )
    if channel.get("error"):
        return _check(
            "v9",
            _TITLE_PLAYERS,
            "unknown",
            "Not available — Music Assistant state is unavailable.",
        )
    players = channel.get("alexa_players") or []
    if not players:
        return _check(
            "v9",
            _TITLE_PLAYERS,
            "fail",
            "No Alexa players are registered in Music Assistant.",
        )
    states = {}
    unavailable = 0
    for player in players:
        state = player.get("state") or "unknown"
        states[state] = states.get(state, 0) + 1
        if player.get("available") is False:
            unavailable += 1
    state_summary = ", ".join(
        f"{count} {state}" for state, count in sorted(states.items())
    )
    names = [player.get("name") or player.get("player_id") or "?" for player in players]
    shown = ", ".join(names[:6]) + (
        f" (+{len(names) - 6} more)" if len(names) > 6 else ""
    )
    detail = f"{len(players)} registered ({state_summary}): {shown}."
    if unavailable:
        detail += f" {unavailable} unavailable."
    return _check("v9", _TITLE_PLAYERS, "info", detail)


def _v10_alexa_link(state):
    """Amazon LWA link health from the deployment state (tokens never shown)."""
    if not isinstance(state, dict):
        return _check(
            "v10",
            _TITLE_AMAZON,
            "unknown",
            "No deployment state yet — connect Amazon in Setup.",
        )
    tokens = state.get("tokens")
    if not isinstance(tokens, dict) or not tokens.get("refresh_token"):
        if isinstance(tokens, dict) and tokens.get("access_token"):
            return _check(
                "v10",
                _TITLE_AMAZON,
                "warning",
                "An access token is stored but no refresh token — it cannot "
                "renew; reconnect in Setup.",
            )
        return _check(
            "v10",
            _TITLE_AMAZON,
            "unknown",
            "Amazon is not connected yet — finish the Setup connect step.",
        )
    try:
        from skill_deployment import config_key

        current_key = config_key()
    except Exception as exc:
        return _check(
            "v10",
            _TITLE_AMAZON,
            "unknown",
            f"OAuth configuration unreadable: {type(exc).__name__}: {exc}",
        )
    stored_key = state.get("client_key")
    if stored_key and current_key and stored_key != current_key:
        return _check(
            "v10",
            _TITLE_AMAZON,
            "warning",
            "The stored Amazon link belongs to a different Login with Amazon "
            "client — reconnect in Setup.",
        )
    expires = tokens.get("expires")
    if isinstance(expires, (int, float)) and expires >= time.time():
        hours = max(1, int((expires - time.time()) / 3600))
        return _check(
            "v10",
            _TITLE_AMAZON,
            "pass",
            f"Amazon connected; access token valid for about {hours} more hours "
            "(refresh token stored — renewal not revalidated by this check).",
        )
    return _check(
        "v10",
        _TITLE_AMAZON,
        "pass",
        "Amazon connected; refresh token stored (renewal is not revalidated "
        "by this check — a revoked token fails only when it is next used).",
    )


def _v11_reachability(snap):
    """Fold the cached add-on reachability results into one check."""
    if not isinstance(snap, dict):
        return _check("v11", _TITLE_REACH, "unknown", "Reachability data unavailable.")
    if not snap.get("checks"):
        if snap.get("checking"):
            return _check(
                "v11",
                _TITLE_REACH,
                "unknown",
                "Reachability checks are still running.",
            )
        return _check(
            "v11",
            _TITLE_REACH,
            "unknown",
            "No public URLs are configured — nothing to check.",
        )
    from reachability import summary

    result = summary(snap)
    kind = {"good": "pass", "warning": "warning", "error": "fail"}.get(
        result.get("kind"), "unknown"
    )
    return _check(
        "v11",
        _TITLE_REACH,
        kind,
        f"{result.get('title')} — {result.get('description')}",
    )


def _model_language(relative):
    """languageModel dict of a bundled app/models file, else None."""
    import json
    from pathlib import Path

    try:
        data = json.loads(
            (Path(__file__).parent / "models" / relative).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    language = (
        data.get("interactionModel", {}).get("languageModel")
        if isinstance(data, dict)
        else None
    )
    return language if isinstance(language, dict) else None


def _names_invocation(phrase, name):
    """True when a phrase references a skill name as whole words (not a fragment)."""
    phrase = " ".join(str(phrase or "").casefold().split())
    name = " ".join(str(name or "").casefold().split())
    return bool(name) and f" {name} " in f" {phrase} "


def _deployment_locale_problem(state, locale):
    """Fail text when Amazon serves a model for another locale, else ``""``."""
    deployed = state.get("deployed_settings") if isinstance(state, dict) else None
    deployed_locale = deployed.get("locale") if isinstance(deployed, dict) else ""
    if not deployed_locale or deployed_locale == locale:
        return ""
    return (
        f"Amazon serves the {deployed_locale} model but this add-on is "
        f"configured for {locale}; deploy this locale before trusting "
        "the invocation check."
    )


def _invocation_notes(live, bundled_name, locale, command):
    """(stale-deployment note, Music Assistant fallback note) for V12."""
    stale = ""
    if live and bundled_name and live != bundled_name:
        stale = (
            f" The deployed model still uses '{live}'; deploy to update it "
            f"to '{bundled_name}'."
        )
    fallback = ""
    if locale not in MA_START_COMMANDS and not locale.startswith("en-"):
        fallback = (
            f" Music Assistant has no start command for {locale} and falls "
            f"back to English ('{command}') — a known Music Assistant gap."
        )
    return stale, fallback


def _invocation_problems(state, locale, source, command, effective, built_in, prefix):
    """Every reason the deployed invocation cannot work, in message order."""
    problems = []
    locale_problem = _deployment_locale_problem(state, locale)
    if locale_problem:
        problems.append(locale_problem)
    if not effective:
        problems.append(f"The {source} model declares no invocation name.")
    elif not _names_invocation(command, effective):
        problems.append(
            f"Music Assistant starts playback with '{command}' for {locale}, "
            f"which does not address the invocation name '{effective}' — the "
            "command can never reach this skill (issue #71)."
        )
    if built_in is None:
        problems.append(f"No built-in intents file is bundled for {locale}.")
    elif not prefix:
        problems.append(
            "Music Assistant receives an empty control-command prefix for "
            "this locale — its pause/resume/stop commands will be incomplete."
        )
    elif effective and not _names_invocation(prefix, effective):
        problems.append(
            f"The control-command prefix '{prefix}' does not include the "
            f"invocation name '{effective}' — pause, resume and stop will "
            "not route to this skill."
        )
    return problems


def _v12_invocation(state):
    """The deployed invocation name is the name Music Assistant addresses.

    MA composes both its start command (ALEXA_LANGUAGE_COMMANDS) and the
    pause/resume/stop prefix (from /alexa/intents) around this name; any
    disagreement means MA-initiated commands never reach the skill — the
    issue #71 failure class, self-diagnosed here.
    """
    locale = get_setting("LOCALE", "en-US")
    bundled = _model_language(f"{locale}.json")
    if bundled is None:
        return _check(
            "v12",
            _TITLE_INVOCATION,
            "unknown",
            f"No voice model is bundled for {locale}.",
        )
    live = None if not isinstance(state, dict) else state.get("live_invocation")
    effective = live or bundled.get("invocationName") or ""
    source = "deployed" if live else "bundled"
    command = MA_START_COMMANDS.get(locale, MA_DEFAULT_START_COMMAND)
    built_in = _model_language(f"built-in/{locale}.json")
    prefix = (built_in or {}).get("invocationName") or ""

    problems = _invocation_problems(
        state, locale, source, command, effective, built_in, prefix
    )
    stale, fallback = _invocation_notes(
        live, bundled.get("invocationName") or "", locale, command
    )
    if problems:
        return _check(
            "v12", _TITLE_INVOCATION, "fail", " ".join(problems) + stale + fallback
        )
    if not live:
        # Absent is not proof: state written before live_invocation existed
        # cannot show what Amazon serves right now.
        return _check(
            "v12",
            _TITLE_INVOCATION,
            "unknown",
            f"No deployment has recorded the live invocation name yet; the "
            f"bundled {locale} model declares '{effective}'. Review and deploy "
            f"once to record what Amazon serves, then re-check.{fallback}",
        )
    detail = (
        f"Music Assistant's start command '{command}' addresses "
        f"'{effective}'; the control-command prefix '{prefix}' agrees."
        f"{stale}{fallback}"
    )
    if fallback:
        return _check("v12", _TITLE_INVOCATION, "warning", detail)
    return _check("v12", _TITLE_INVOCATION, "pass", detail)


def _v13_channel(store):
    """The pushed stream was acknowledged by its Echo — the MA->Amazon leg works.

    MA's play_media pushes the stream URL here FIRST, then asks Amazon to
    invoke the skill (issue #71). With a dead MA->Amazon session the push
    still lands but no Echo ever reports the pushed stream as current, while
    every other check stays green (issue #86). A metadata re-push of the SAME
    stream keeps the Echo's report current, so the current-stream flag — not
    recency — is what is compared; only the pushed player's report counts, so
    a second Echo happily playing cannot mask a dead channel.
    """
    from skill import echo_position

    timestamp = store.get("timestamp")
    if not timestamp:
        return _check(
            "v13",
            _TITLE_CHANNEL,
            "unknown",
            "No stream push has been stored yet — press play in Music "
            "Assistant to exercise the channel.",
        )
    try:
        age = max(0.0, time.time() - float(timestamp))
    except (TypeError, ValueError):
        return _check(
            "v13",
            _TITLE_CHANNEL,
            "unknown",
            "The stored push timestamp is unreadable.",
        )
    label = _age_label(age)
    player = store.get("playerId")
    reports = echo_position.latest_reports()
    if player:
        acknowledged = bool(reports.get(player, {}).get("current"))
    else:
        # The push named no player; any Echo reporting the stream proves the leg.
        acknowledged = any(report.get("current") for report in reports.values())
    if acknowledged:
        where = player or "An Alexa player"
        return _check(
            "v13",
            _TITLE_CHANNEL,
            "pass",
            f"{where} is playing the stream pushed {label}.",
        )
    if age <= CHANNEL_GRACE_SECONDS:
        # MA pushes, then asks Amazon; the Echo's report takes seconds to land.
        return _check(
            "v13",
            _TITLE_CHANNEL,
            "pass",
            f"The last push arrived {label} — too early to expect the Echo's report.",
        )
    if age > FRESH_PUSH_SECONDS:
        return _check(
            "v13",
            _TITLE_CHANNEL,
            "unknown",
            f"The last push arrived {label}; too old to judge the channel "
            "now. Press play in Music Assistant to test it live.",
        )
    where = player or "its Alexa player"
    return _check(
        "v13",
        _TITLE_CHANNEL,
        "warning",
        f"The stream pushed {label} has not been acknowledged by {where}. "
        "Music Assistant's Amazon session looks dead — re-authenticate its "
        "Alexa provider with Amazon (separate from this add-on's 'Connect to "
        "Amazon' step). Every other check can stay green through this fault.",
    )


def _summary(checks):
    counts = {"pass": 0, "fail": 0, "warning": 0, "unknown": 0, "info": 0}
    for check in checks:
        counts[check["state"]] = counts.get(check["state"], 0) + 1
    if counts["fail"]:
        kind, title = "error", "Problems detected"
    elif counts["warning"]:
        kind, title = "warning", "Warnings found"
    elif checks and all(c["state"] in ("pass", "info") for c in checks):
        kind, title = "good", "All checks passed"
    else:
        kind, title = "neutral", "Partially verified"
    return {"kind": kind, "title": title, "counts": counts}


def run_checks():
    """Run V1–V13. Never raises — every failure becomes a check state."""
    try:
        return _run_checks()
    except Exception as exc:  # pragma: no cover - defensive
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "checks": [
                _check(
                    "v0",
                    "Verification",
                    "unknown",
                    f"Checks could not run: {type(exc).__name__}: {exc}",
                )
            ],
            "summary": _summary([]),
        }


def _intent_logs():
    """Logged Alexa invocations (empty outside a Flask app context)."""
    try:
        from flask import current_app

        return list(current_app.config.get("INTENT_LOGS") or [])
    except Exception:
        return []


def _deployment_state():
    """Parsed deployment state file (read-only), else None."""
    import json
    import os

    path = os.environ.get("SKILL_DEPLOYMENT_PATH", "/data/skill-deployment.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _reachability_snapshot():
    """Cached reachability snapshot, else None outside a Flask app context."""
    try:
        from reachability import snapshot

        return snapshot()
    except Exception:
        return None


def _run_checks():
    store = shared_store.get_ma() or {}
    channel = _read_channel()
    state = _deployment_state()
    checks = [
        _v1_provider(channel),
        _v2a_setup_keys(channel),
        _v2b_push_base(store),
        *_v3_v4_fetch(store),
        _v5_freshness(store),
        _v6_now_playing(channel, store),
        _v7_invocations(_intent_logs()),
        _v8_stream_host(store),
        _v9_players(channel),
        _v10_alexa_link(state),
        _v11_reachability(_reachability_snapshot()),
        _v12_invocation(state),
        _v13_channel(store),
    ]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "checks": checks,
        "summary": _summary(checks),
        # Player inventory for the bundle (states + availability flags) so a
        # downloaded diagnostics file shows the fleet without a live click.
        "players": channel.get("alexa_players") or [],
    }


def checks_html(result):
    """Render checks as approved status rows (details pre-escaped here)."""
    rows = []
    for check in result.get("checks", []):
        label, kind = _PILL.get(check.get("state"), ("Unknown", "neutral"))
        rows.append(
            row(
                check.get("title", "Check"),
                escape(check.get("detail", "")),
                label,
                kind,
            )
        )
    return "".join(rows)
