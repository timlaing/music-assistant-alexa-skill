"""Read-only verification of the Music Assistant <-> skill channel.

Answers "is the Alexa player (Music Assistant's alexa provider) correctly
configured to talk to this skill, and does it send correct information?"
with six flow-free checks — Music Assistant is only ever READ (never
written to, no flow started), and the only outbound request is a GET
against the skill's own /ma/latest-url endpoint:

  V1   provider health      alexa provider enabled, loaded, no last error
  V2a  setup keys           setup_data carries api_url/api_username/api_password
                            (presence only — values are encrypted at rest)
  V2b  push observed        the scheme://host Music Assistant actually pushed
                            from, recorded on each authenticated /ma/push-url
  V3   endpoint reachable   GET <base>/ma/latest-url answers
  V4   basic auth           the same fetch is accepted (401 = rejected)
  V5   push freshness       how long ago the last push arrived
  V6   now playing          what MA says is playing vs the last pushed stream

Failures become check states, never exceptions.
"""

import asyncio
import time
from datetime import datetime, timezone

import shared_store
from app_settings import get_setting
from env_secrets import get_env_secret
from markupsafe import escape
from ui_markup import row

FETCH_TIMEOUT_SECONDS = 5
MA_TIMEOUT_SECONDS = 8
FRESH_PUSH_SECONDS = 3600

_PILL = {
    "pass": ("Passed", "success"),
    "fail": ("Failed", "error"),
    "warning": ("Warning", "warning"),
    "unknown": ("Unknown", "neutral"),
    "info": ("Info", "neutral"),
}


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


async def _read_channel_state(server_url, token):
    """One MA connection for V1 (health), V2a (setup keys) and V6 (players)."""
    import aiohttp
    from music_assistant_client import MusicAssistantClient

    async with (
        aiohttp.ClientSession() as session,
        MusicAssistantClient(server_url, session, token=token) as client,
        asyncio.timeout(MA_TIMEOUT_SECONDS),
    ):
        # server_info is a property set by connect(), not a coroutine.
        info = client.server_info
        try:
            configs = await client.config.get_provider_configs(include_values=False)
            providers_error = None
        except Exception as exc:
            configs, providers_error = [], f"{type(exc).__name__}: {exc}"
        alexa_cfg = next(
            (cfg for cfg in configs if getattr(cfg, "domain", None) == "alexa"), None
        )

        # V2a: key presence only — setup_data values are encrypted at rest and
        # never carried out of this function.
        setup_keys = None
        setup_error = None
        try:
            setup = await client.config.get_provider_config_value(
                instance_id="alexa", key="setup_data", default=None
            )
        except Exception as exc:
            if "not found" in str(exc):
                setup_keys = []  # provider setup has never run
            else:
                setup_error = f"{type(exc).__name__}: {exc}"
        else:
            if isinstance(setup, dict):
                setup_keys = sorted(setup)
            elif setup is None:
                setup_keys = []  # no setup data recorded at all
            else:
                setup_error = f"unexpected setup_data type {type(setup).__name__}"

        players_raw = await client.send_command("players/all")
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
                    "title": media.get("title") if isinstance(media, dict) else None,
                    "artist": media.get("artist") if isinstance(media, dict) else None,
                }
            )
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
            "Alexa provider",
            "unknown",
            "Music Assistant API is not configured (MA_API_URL / MA_API_TOKEN).",
        )
    if channel.get("error"):
        return _check(
            "v1",
            "Alexa provider",
            "fail",
            f"Music Assistant is unreachable: {channel['error']}",
        )
    if channel.get("providers_error"):
        return _check(
            "v1",
            "Alexa provider",
            "fail",
            f"Provider list unreadable: {channel['providers_error']}",
        )
    alexa = channel.get("alexa")
    if not alexa:
        return _check(
            "v1",
            "Alexa provider",
            "fail",
            "The alexa provider is not configured in Music Assistant.",
        )
    if alexa.get("last_error"):
        return _check(
            "v1", "Alexa provider", "fail", f"Provider error: {alexa['last_error']}"
        )
    if not alexa.get("enabled"):
        return _check("v1", "Alexa provider", "fail", "The alexa provider is disabled.")
    if (alexa.get("status") or "").lower() != "loaded":
        return _check(
            "v1",
            "Alexa provider",
            "fail",
            f"Provider status is '{alexa.get('status')}' (expected 'loaded').",
        )
    return _check("v1", "Alexa provider", "pass", "Enabled, loaded, no errors.")


def _v2a_setup_keys(channel):
    if not channel.get("configured"):
        return _check(
            "v2a", "Provider setup keys", "unknown", "Not tested — MA not configured."
        )
    if channel.get("error"):
        return _check(
            "v2a",
            "Provider setup keys",
            "unknown",
            "Not tested — Music Assistant is unreachable (see provider health).",
        )
    if channel.get("setup_error"):
        return _check(
            "v2a",
            "Provider setup keys",
            "unknown",
            f"setup_data could not be read: {channel['setup_error']}",
        )
    keys = channel.get("setup_keys")
    if keys is None:
        return _check(
            "v2a", "Provider setup keys", "unknown", "setup_data was not read."
        )
    missing = [
        key for key in ("api_url", "api_username", "api_password") if key not in keys
    ]
    if missing:
        return _check(
            "v2a",
            "Provider setup keys",
            "fail",
            f"setup_data is missing {', '.join(missing)} — re-run the provider "
            "setup in Music Assistant.",
        )
    return _check(
        "v2a",
        "Provider setup keys",
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
    base = push_base or skill_host
    if not base:
        return [
            _check(
                "v3",
                "Endpoint reachable",
                "unknown",
                "No push recorded and no SKILL_HOSTNAME configured — no URL to test.",
            ),
            _check("v4", "Basic auth", "unknown", "Not tested — no endpoint URL."),
        ]
    via = (
        "the address Music Assistant pushed from"
        if push_base
        else "the configured skill hostname"
    )
    fetched = _self_fetch(base)
    if fetched["error"] is not None:
        return [
            _check(
                "v3",
                "Endpoint reachable",
                "fail",
                f"GET {fetched['url']} failed via {via}: {fetched['error']}",
            ),
            _check(
                "v4",
                "Basic auth",
                "unknown",
                "Not tested — the endpoint was unreachable.",
            ),
        ]
    status = fetched["status"]
    v3 = _check(
        "v3",
        "Endpoint reachable",
        "pass",
        f"GET {fetched['url']} answered HTTP {status} (via {via}).",
    )
    if status == 401:
        v4 = _check(
            "v4",
            "Basic auth",
            "fail",
            "The endpoint rejected the configured username/password (HTTP 401).",
        )
    elif status in (200, 404):
        if not fetched["auth"]:
            v4 = _check(
                "v4",
                "Basic auth",
                "unknown",
                "No APP_USERNAME/APP_PASSWORD are configured — basic auth is "
                "not enforced, nothing to verify.",
            )
        else:
            note = "no stream stored yet" if status == 404 else "stream stored"
            v4 = _check(
                "v4",
                "Basic auth",
                "pass",
                f"The endpoint accepted the configured credentials "
                f"({note}, HTTP {status}).",
            )
    else:
        v4 = _check(
            "v4",
            "Basic auth",
            "unknown",
            f"Unexpected HTTP {status} — credentials not verified.",
        )
    return [v3, v4]


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
            "v5", "Push freshness", "unknown", "No stream push has been stored yet."
        )
    try:
        age = max(0.0, time.time() - float(timestamp))
    except (TypeError, ValueError):
        return _check(
            "v5",
            "Push freshness",
            "unknown",
            "The stored push timestamp is unreadable.",
        )
    label = _age_label(age)
    if age <= FRESH_PUSH_SECONDS:
        return _check("v5", "Push freshness", "pass", f"The last push arrived {label}.")
    return _check(
        "v5",
        "Push freshness",
        "warning",
        f"The last push arrived {label} — either nothing has played since, "
        "or pushes stopped.",
    )


def _v6_now_playing(channel, store):
    if not channel.get("configured") or channel.get("error"):
        return _check(
            "v6",
            "Now playing",
            "unknown",
            "Not tested — Music Assistant state is unavailable.",
        )
    players = channel.get("alexa_players") or []
    if not players:
        return _check(
            "v6", "Now playing", "fail", "Music Assistant reports no Alexa players."
        )
    playing = [player for player in players if player.get("state") == "playing"]
    if not playing:
        return _check(
            "v6",
            "Now playing",
            "unknown",
            "Nothing is playing on an Alexa player right now — nothing to compare.",
        )
    store_title = _norm(store.get("title"))
    if not store_title:
        return _check(
            "v6",
            "Now playing",
            "unknown",
            "Alexa is playing but the last push carried no title to compare.",
        )
    for player in playing:
        if _norm(player.get("title")) == store_title:
            where = player.get("name") or player.get("player_id")
            return _check(
                "v6",
                "Now playing",
                "pass",
                f"'{player.get('title')}' on {where} matches the last push.",
            )
    titles = ", ".join(f"'{p.get('title') or 'untitled'}'" for p in playing)
    return _check(
        "v6",
        "Now playing",
        "fail",
        f"Music Assistant is playing {titles} but the last push was "
        f"'{store.get('title')}'.",
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
    """Run V1–V6. Never raises — every failure becomes a check state."""
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


def _run_checks():
    store = shared_store.get_ma() or {}
    channel = _read_channel()
    checks = [
        _v1_provider(channel),
        _v2a_setup_keys(channel),
        _v2b_push_base(store),
        *_v3_v4_fetch(store),
        _v5_freshness(store),
        _v6_now_playing(channel, store),
    ]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "checks": checks,
        "summary": _summary(checks),
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
