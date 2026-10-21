import asyncio
import json
import platform
import re
import shutil
import subprocess
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from app_settings import get_setting, store
from env_secrets import get_env_secret
from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    jsonify,
    render_template,
    request,
    send_file,
)
from markupsafe import escape
from page_nav import nav_html
from locale_names import LOCALE_NAMES
from setup_helpers import has_functional_cli_config
from skill_deployment import DeploymentError, manager, settings
from ui_markup import banner, banner_icon, row, timeline

status_bp = Blueprint("status_bp", __name__)

TEST_AUDIO_FILE = Path(__file__).resolve().parents[2] / "assets" / "test-tone.mp3"

# Repeated display fragments kept as constants.
TESTING_ENABLED = "testing enabled"
TESTING_DISABLED = "testing not enabled"
RECENT_ACTIVITY_EMPTY = '<p class="muted">No recent activity. Speak a command on your Echo to get started.</p>'
CHECKING_URLS_HTML = '<span class="muted">Checking public URLs…</span>'
CHECKING_ACCOUNT_HTML = '<span class="muted">Checking account status…</span>'
CHECKING_MA_API_HTML = '<span class="muted">Checking Music Assistant API…</span>'
CHECKING_ALEXA_API_HTML = '<span class="muted">Checking Alexa API…</span>'
OPEN_SETUP_BUTTON = (
    " <button onclick=\"window.location='/setup'\" "
    'style="margin-left:8px">Open Setup</button>'
)


def _apl_enabled():
    """True when the add-on's Echo Show display support (ENABLE_APL) is on."""
    return str(get_setting("ENABLE_APL", "false")).lower() in ("true", "1", "yes", "on")


# Display names for bundled locales come from locale_names.LOCALE_NAMES (imported).


def _configured_host(skill_host):
    try:
        if skill_host.startswith(("http://", "https://")):
            return urllib.parse.urlparse(skill_host).netloc
        return skill_host
    except Exception:
        return skill_host


def _parse_epoch(value):
    """Best-effort epoch seconds from a float, numeric string or ISO timestamp."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _local_time(value):
    epoch = _parse_epoch(value)
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(epoch)
    except (OverflowError, OSError, ValueError):
        return None


def _activity_time_label(value):
    """Timeline timestamp: 'Today, 10:42' / 'Yesterday, 10:42' / '19 Oct, 10:42'."""
    when = _local_time(value)
    if when is None:
        return str(value) if value is not None else "(no timestamp)"
    now = datetime.now()
    yesterday = datetime.fromordinal(now.toordinal() - 1)
    if when.date() == now.date():
        return f"Today, {when:%H:%M}"
    if when.date() == yesterday.date():
        return f"Yesterday, {when:%H:%M}"
    return f"{when.day} {when:%b}, {when:%H:%M}"


def _deployment_facts():
    """(deployment status, deployed settings match, ready-to-serve)."""
    instance = manager()
    deployment = instance.public_status()
    current = instance.state.get("deployed_settings")
    try:
        matches = current == settings() if current else False
    except DeploymentError:
        matches = False
    ready = deployment.get("phase") == "complete" and matches
    return deployment, matches, ready


def _skill_row_html():
    """'Music Assistant' row of the Alexa skill panel: the deployment state."""
    try:
        deployment, matches, ready = _deployment_facts()
    except Exception as e:
        return row(
            "Music Assistant",
            f"Deployment status unavailable: {escape(str(e))}",
            "Unknown",
            "warning",
        )
    # Only claim the skill was never linked when there is genuinely no deployed
    # skill to report: an expired Amazon connection must not hide a completed,
    # verified deployment (which is summarised under the account row).
    if not deployment.get("connected") and not deployment.get("skill_id"):
        return row(
            "Music Assistant",
            "The personal skill has not been linked to Amazon yet.",
            "Not deployed",
            "warning",
        )
    locale = (get_setting("LOCALE", "") or "en-US").strip() or "en-US"
    if ready:
        return row(
            "Music Assistant",
            f"Development skill · {escape(LOCALE_NAMES.get(locale, locale))}",
            "Deployed",
            "success",
        )
    if deployment.get("phase") == "complete" and not matches:
        return row(
            "Music Assistant",
            "Settings changed since the last deployment.",
            "Out of date",
            "warning",
        )
    message = str(deployment.get("message") or "Deployment in progress.")
    label = "Deploying" if deployment.get("phase") == "running" else "Not deployed"
    return row("Music Assistant", escape(message), label, "warning")


def _ask_testing_enabled(sid):
    """Best-effort check whether the skill's development testing is enabled."""
    try:
        en = subprocess.run(
            [
                "ask",
                "smapi",
                "get-skill-enablement-status",
                "--skill-id",
                sid,
                "--stage",
                "development",
                "--profile",
                "default",
            ],
            capture_output=True,
            text=True,
        )
        en_out = en.stdout or en.stderr or ""
        if en.returncode == 0 or "Command executed successfully" in en_out:
            return True
        if re.search(r"\[Error\]:\s*\{", en_out) or re.search(r"404", en_out):
            return False
        if re.search(r'"isEnabled"\s*:\s*true', en_out, re.IGNORECASE) or re.search(
            r'"enabled"\s*:\s*true', en_out, re.IGNORECASE
        ):
            return True
    except Exception:
        return False
    return False


def _manifest_locales(mf_out):
    """Extract publishing locales from an ASK manifest listing, if parseable."""
    try:
        try:
            mf_json = json.loads(mf_out)
        except Exception:
            idx = mf_out.find("{")
            if idx == -1:
                return []
            try:
                mf_json = json.loads(mf_out[idx:])
            except Exception:
                return []
        if not mf_json:
            return []
        locales_obj = (
            mf_json.get("manifest", {})
            .get("publishingInformation", {})
            .get("locales", {})
        )
        return list(locales_obj.keys()) if isinstance(locales_obj, dict) else []
    except Exception:
        return []


def _ask_manifest_html(sid, mf_out, skill_host, testing_enabled):
    """HTML for the found-skill branch; returns (html, is_green)."""
    testing_msg = TESTING_ENABLED if testing_enabled else TESTING_DISABLED
    mm = re.search(r'https?://[^"\s\)\]]+', mf_out)
    if not mm:
        return (
            f'<span class="led yellow"></span> Music Assistant Skill interaction model {escape(sid)} found; endpoint not set ({testing_msg})',
            False,
        )
    cfg_host = _configured_host(skill_host)
    try:
        parsed = urllib.parse.urlparse(mm.group(0))
        manifest_host = parsed.netloc
        locale_list = _manifest_locales(mf_out)
        locale_display = ",".join(locale_list) if locale_list else "unknown"
        if manifest_host == cfg_host:
            if testing_enabled:
                return (
                    f'<span class="led green"></span> Music Assistant Skill interaction model found; endpoint matches ({escape(manifest_host)}); testing enabled; locale: {escape(locale_display)}',
                    True,
                )
            return (
                f'<span class="led yellow"></span> Music Assistant Skill interaction model found and endpoint matches ({escape(manifest_host)}); testing NOT enabled',
                False,
            )
        return (
            f'<span class="led red"></span> Music Assistant Skill interaction model endpoint mismatch (manifest: {escape(manifest_host)} vs configured: {escape(cfg_host)}); {testing_msg}',
            False,
        )
    except Exception:
        return (
            f'<span class="led yellow"></span> Music Assistant Skill interaction model found; endpoint parse failed ({testing_msg})',
            False,
        )


def _deployment_status_html():
    """Last-deployment summary; shown only for a verified deployment."""
    instance = manager()
    deployment = instance.public_status()
    current = instance.state.get("deployed_settings")
    try:
        matches = current == settings() if current else False
    except DeploymentError:
        matches = False
    if not (deployment.get("phase") == "complete" and matches):
        return ""
    return (
        '<span class="led green"></span> Amazon verified deployment'
        f" (verified at {escape(str(deployment.get('verified_at')))},"
        f" saved ID: {escape(deployment.get('skill_id'))})"
    )


def _ask_cli_status_html(skill_host):
    """HTML for the ASK-CLI-inspection branch of the skill status."""
    if not has_functional_cli_config(profile="default"):
        return (
            '<span class="led yellow"></span> ASK CLI credentials are not configured for profile default'
            + OPEN_SETUP_BUTTON
        )
    ls = subprocess.run(
        ["ask", "smapi", "list-skills-for-vendor", "--profile", "default"],
        capture_output=True,
        text=True,
    )
    out = ls.stdout or ls.stderr or ""
    m = re.search(r"amzn1\.ask\.skill\.[0-9a-fA-F\-]+", out)
    if not m:
        return '<span class="led red"></span> Music Assistant Skill interaction model not found via ASK CLI'
    sid = m.group(0)
    mf = subprocess.run(
        [
            "ask",
            "smapi",
            "get-skill-manifest",
            "--skill-id",
            sid,
            "--profile",
            "default",
        ],
        capture_output=True,
        text=True,
    )
    mf_out = mf.stdout or mf.stderr or ""
    testing_enabled = _ask_testing_enabled(sid)
    html, is_green = _ask_manifest_html(sid, mf_out, skill_host, testing_enabled)
    if not is_green:
        html += OPEN_SETUP_BUTTON
    return html


def _skill_ask_html():
    """Amazon account row plus the last-deployment summary fragment."""
    try:
        skill_host = get_setting("SKILL_HOSTNAME", "").strip()
        deployment, _matches, _ready = _deployment_facts()
        if deployment.get("connected"):
            account = row(
                "Amazon account", "Personal developer account.", "Connected", "success"
            )
        elif shutil.which("ask") and skill_host:
            if has_functional_cli_config(profile="default"):
                account = row(
                    "Amazon account",
                    "Personal developer account.",
                    "Connected",
                    "success",
                )
            else:
                account = row(
                    "Amazon account",
                    "ASK CLI credentials are not configured for profile default.",
                    "Not connected",
                    "warning",
                )
        elif not shutil.which("ask"):
            account = row(
                "Amazon account",
                "ask CLI not available in container",
                "Not available",
                "warning",
            )
        else:
            account = row(
                "Amazon account",
                "SKILL_HOSTNAME not configured",
                "Not configured",
                "warning",
            )
        summary_fragment = _deployment_status_html()
        if not summary_fragment:
            return account
        return account + (
            '<div class="ma-small ma-muted" style="margin-top:14px">'
            f"{summary_fragment}</div>"
        )
    except Exception as e:
        return row(
            "Amazon account", f"ASK check error: {escape(str(e))}", "Unknown", "warning"
        )


def _render_metadata_html(metadata_info):
    """Diagnostics content for the Echo Show display panel."""
    pretty_metadata = json.dumps(metadata_info, indent=2, ensure_ascii=False)
    if metadata_info.get("audioSources") or metadata_info.get("primaryText"):
        return f'<pre class="ma-code" tabindex="0">{escape(pretty_metadata)}</pre>'
    return '<pre class="ma-code" tabindex="0">No track metadata received yet.</pre>'


def _metadata_status_html():
    """Metadata-refresh status fragment (raw skill data)."""
    try:
        from skill import data as skill_data

        return _render_metadata_html(dict(skill_data.get_info()))
    except Exception as e:
        return (
            f'<p class="ma-small ma-error">Error loading metadata: {escape(str(e))}</p>'
        )


def _activity_items():
    """(epoch, icon, title, time label, description) newest first, at most five."""
    items = []
    try:
        deployment = manager().public_status()
        verified_at = deployment.get("verified_at")
        if deployment.get("phase") == "complete" and verified_at:
            items.append(
                (
                    _parse_epoch(verified_at) or 0.0,
                    "cloud_upload",
                    "Skill deployment verified",
                    _activity_time_label(verified_at),
                    "Amazon confirmed the development deployment",
                )
            )
    except Exception:
        pass
    from endpoints.invocations import _entry_name, _entry_timestamp

    logs = current_app.config.get("INTENT_LOGS", []) or []
    for entry in list(logs)[-5:]:
        if not isinstance(entry, dict):
            continue
        incoming = entry.get("incoming") or entry.get("payload")
        ts = _entry_timestamp(entry, incoming)
        name = _entry_name(incoming) or "Alexa request"
        code = entry.get("response_status")
        description = (
            f"Response HTTP {code}" if code is not None else "Response recorded"
        )
        items.append(
            (
                _parse_epoch(ts) or 0.0,
                "message_square",
                str(name),
                _activity_time_label(ts),
                description,
            )
        )
    items.sort(key=lambda item: item[0], reverse=True)
    return items[:5]


def _activity_fragment():
    items = _activity_items()
    if not items:
        return RECENT_ACTIVITY_EMPTY
    return "".join(
        timeline(icon_name, title, when, description)
        for _epoch, icon_name, title, when, description in items
    )


def _invocations_count():
    intent_logs = current_app.config.get("INTENT_LOGS", [])
    return len(intent_logs) if intent_logs else 0


def _build_status_json():
    return {
        "skill_html": _skill_row_html(),
        "skill_ask_html": _skill_ask_html(),
        "ma_api_html": _compute_ma_api_html(),
        "alexa_api_html": _compute_alexa_api_html(),
        "metadata_html": _metadata_status_html(),
        "invocations_html": _activity_fragment(),
        "created": False,
    }


_EMPTY_STORE_DESC = {
    "Music Assistant API": "No stream has been sent yet.",
    "Alexa API": "Ready for the first playback request.",
}


def _store_html(payload, label):
    if not payload:
        return row(
            label,
            _EMPTY_STORE_DESC.get(label, "No stream has been sent yet."),
            "Waiting",
            "neutral",
        )
    return row(label, "Stream payload received.", "Active", "success")


def _compute_ma_api_html():
    import shared_store

    return _store_html(shared_store.get_ma(), "Music Assistant API")


def _compute_alexa_api_html():
    import shared_store

    return _store_html(shared_store.get_alexa(), "Alexa API")


@status_bp.route("/status/ma", methods=["GET"])
def status_ma():
    return jsonify({"ma_api_html": _compute_ma_api_html()})


@status_bp.route("/status/alexa", methods=["GET"])
def status_alexa():
    return jsonify({"alexa_api_html": _compute_alexa_api_html()})


def _compute_metadata_html():
    """Compute HTML showing the current APL metadata being sent in refreshes."""
    try:
        from skill import data as skill_data
        from skill.util import get_ma_hostname, replace_ip_in_url

        metadata_info = dict(skill_data.get_info())  # Create a copy

        # Apply MA_HOSTNAME replacement to image URLs for display
        try:
            hostname = get_ma_hostname(raise_on_http_scheme=False)
            if hostname:
                if metadata_info.get("coverImageSource"):
                    metadata_info["coverImageSource"] = replace_ip_in_url(
                        metadata_info["coverImageSource"], hostname
                    )
                if metadata_info.get("backgroundImageSource"):
                    metadata_info["backgroundImageSource"] = replace_ip_in_url(
                        metadata_info["backgroundImageSource"], hostname
                    )
        except Exception:
            pass  # If hostname replacement fails, show original URLs

        return _render_metadata_html(metadata_info)
    except Exception as e:
        return (
            f'<p class="ma-small ma-error">Error loading metadata: {escape(str(e))}</p>'
        )


@status_bp.route("/status/metadata", methods=["GET"])
def status_metadata():
    return jsonify({"metadata_html": _compute_metadata_html()})


@status_bp.route("/status", methods=["GET"])
def status():
    # If client requested JSON, return the aggregated checks
    want_json = request.args.get("format") == "json" or "application/json" in (
        request.headers.get("Accept") or ""
    )
    if want_json:
        return jsonify(_build_status_json())

    # Non-JSON: render status template
    try:
        from reachability import diagnostics, snapshot, summary as reachability_summary

        reachability = snapshot()
        state = reachability_summary(reachability)
        tpl = render_template(
            "status.html",
            banner_html=banner(state["kind"], state["title"], state["description"]),
            conn_diag=diagnostics(reachability),
            urls_html=CHECKING_URLS_HTML,
            skill_html=_skill_row_html(),
            skill_ask_html=CHECKING_ACCOUNT_HTML,
            ma_api_html=CHECKING_MA_API_HTML,
            alexa_api_html=CHECKING_ALEXA_API_HTML,
            metadata_html=_metadata_status_html(),
            activity_html=_activity_fragment(),
            apl_enabled=_apl_enabled(),
        )
        tpl = tpl.replace("__NAV__", nav_html("status"))
        return Response(tpl, status=200, mimetype="text/html")
    except Exception:
        html = """<!doctype html>
            <html>
            <head><meta charset="utf-8"><title>Service Status</title></head>
            <body>
                <h1>Service Status</h1>
                <div><span class=\"led green\"></span> Skill running</div>
                <div><span class=\"muted\">Checking ASK CLI status...</span></div>
                <div><span class=\"muted\">Checking Music Assistant API...</span></div>
            </body>
            </html>"""
        return Response(html, status=200, mimetype="text/html")


@status_bp.route("/test-audio.mp3", methods=["GET"])
def test_audio():
    """Serve a small self-generated tone for public-delivery diagnostics."""
    if not TEST_AUDIO_FILE.is_file():
        abort(404)
    return send_file(TEST_AUDIO_FILE, mimetype="audio/mpeg", max_age=86400)


@status_bp.route("/status/api", methods=["GET"])
def status_api():
    """Lightweight API used by the status UI to fetch aggregated checks."""
    return jsonify(_build_status_json())


@status_bp.route("/status/ask", methods=["GET"])
def status_ask():
    """Return only the ASK CLI check fragment used by the client UI."""
    data = _build_status_json()
    return jsonify({"skill_ask_html": data.get("skill_ask_html")})


@status_bp.route("/status/invocations", methods=["GET"])
def status_invocations():
    """Return the current invocation count and activity timeline HTML so the UI can refresh it live."""
    return jsonify(
        {"count": _invocations_count(), "invocations_html": _activity_fragment()}
    )


@status_bp.route("/status/urls", methods=["GET"])
def status_urls():
    from reachability import diagnostics, html, snapshot, summary

    result = snapshot()
    state = summary(result)
    return jsonify(
        **result,
        urls_html=html(result),
        conn_diag_html=diagnostics(result),
        summary_title=state["title"],
        summary_description=state["description"],
        summary_kind=state["kind"],
        summary_icon=banner_icon(state["kind"]),
    )


@status_bp.route("/status/verify", methods=["GET"])
def status_verify():
    """Run the read-only Music Assistant channel checks (V1–V6)."""
    from verification import checks_html, run_checks

    result = run_checks()
    return jsonify({**result, "checks_html": checks_html(result)})


def _provider_error(cfg):
    """Last provider error as a plain message (dict/object/string tolerant)."""
    error = getattr(cfg, "last_error", None)
    if error is None:
        return None
    if isinstance(error, dict):
        return error.get("message") or str(error)
    return str(getattr(error, "message", None) or error)


def _provider_status(configs):
    """Provider status list plus the alexa provider's configuration.

    Config values come from config/providers?include_values=True: passwords
    are SECURE_STRING entries that Music Assistant substitutes server-side,
    and the diagnostics redact pass masks username/email-like keys anyway.
    """
    instances = []
    alexa = []
    for cfg in configs:
        instances.append(
            {
                "domain": getattr(cfg, "domain", None),
                "enabled": bool(getattr(cfg, "enabled", False)),
                "status": str(getattr(cfg, "status", "") or ""),
                "last_error": _provider_error(cfg),
            }
        )
        if getattr(cfg, "domain", None) != "alexa":
            continue
        values = getattr(cfg, "values", None) or {}
        alexa.append(
            {
                "instance_id": getattr(cfg, "instance_id", None),
                "name": getattr(cfg, "name", None),
                "enabled": bool(getattr(cfg, "enabled", False)),
                "status": str(getattr(cfg, "status", "") or ""),
                "last_error": _provider_error(cfg),
                "config": {
                    key: getattr(entry, "value", None) for key, entry in values.items()
                },
                "secure_keys": [
                    key
                    for key, entry in values.items()
                    if str(getattr(entry, "type", "")) == "secure_string"
                ],
            }
        )
    return {"instances": instances, "alexa": alexa}


async def _read_ma_status(server_url, token):
    import aiohttp
    from music_assistant_client import MusicAssistantClient

    async with (
        aiohttp.ClientSession() as session,
        MusicAssistantClient(server_url, session, token=token) as client,
        asyncio.timeout(8),
    ):
        # server_info is a property set by connect(), not a coroutine. The
        # players cache (client.players.players) is only filled by
        # start_listening(), which this skill never runs — players must come
        # from the players/all command or the list is always empty.
        info = client.server_info
        players = await client.send_command("players/all")
        try:
            provider_configs = await client.config.get_provider_configs(
                include_values=True
            )
        except Exception as exc:
            providers = {"error": f"{type(exc).__name__}: {exc}"}
        else:
            providers = _provider_status(provider_configs)
    return {
        "configured": True,
        "server_version": getattr(info, "server_version", None),
        "players": [
            {
                "player_id": player.get("player_id"),
                "name": player.get("name"),
                "state": str(player.get("playback_state") or ""),
                "available": bool(player.get("available")),
            }
            for player in players
        ],
        "providers": providers,
    }


def _ma_status_probe():
    """Read-only Music Assistant status via the stored API token.

    Uses the same client and credentials the skill already uses for player
    commands. Failures are recorded as data — the error itself is diagnostic.
    """
    server_url = get_env_secret("MA_API_URL")
    token = get_env_secret("MA_API_TOKEN")
    if not server_url or not token:
        return {"configured": False}
    try:
        return asyncio.run(_read_ma_status(server_url, token))
    except Exception as exc:
        return {"configured": True, "error": f"{type(exc).__name__}: {exc}"}


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_HOST_RE = re.compile(
    r"\b(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,}\b"
)
_SENSITIVE_KEY_PARTS = (
    "password",
    "secret",
    "token",
    "username",
    "serial",
    "accountid",
    "login",
    "email",
)
_SENSITIVE_KEYS = {"user_id", "userid", "lwa_client_id"}


def _is_sensitive_key(key):
    lowered = (key or "").lower()
    return lowered in _SENSITIVE_KEYS or any(
        part in lowered for part in _SENSITIVE_KEY_PARTS
    )


def _diagnostics_redact(value):
    """Recursively redact domains, IPs, emails and username-like values.

    Placeholders are stable within one bundle (<host-1> is the same host
    everywhere), so correlations survive redaction — e.g. whether the stream
    host matches the callback host stays visible. URL paths are kept: they
    carry structure that is often the actual bug. Query strings are dropped:
    they can carry tokens.
    """
    hosts, ips, emails = {}, {}, {}

    def host_placeholder(name):
        return hosts.setdefault(name, f"<host-{len(hosts) + 1}>")

    def strip_text(text):
        text = _EMAIL_RE.sub(
            lambda m: emails.setdefault(m.group(0), f"<email-{len(emails) + 1}>"),
            text,
        )

        def url_sub(match):
            parts = urllib.parse.urlsplit(match.group(0))
            host = host_placeholder(parts.hostname or parts.netloc)
            return f"{parts.scheme}://{host}{parts.path}"

        text = _URL_RE.sub(url_sub, text)
        text = _IPV4_RE.sub(
            lambda m: ips.setdefault(m.group(0), f"<ip-{len(ips) + 1}>"), text
        )
        return _HOST_RE.sub(lambda m: host_placeholder(m.group(0)), text)

    def walk(item, key=None):
        if isinstance(item, dict):
            return {k: walk(v, k) for k, v in item.items()}
        if isinstance(item, list):
            return [walk(entry, key) for entry in item]
        if isinstance(item, str):
            if _is_sensitive_key(key):
                return "<redacted>"
            return strip_text(item)
        return item

    return walk(value)


def _verification_section():
    """Read-only channel verification for the bundle (failures recorded as data)."""
    try:
        from verification import run_checks

        return run_checks()
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _diagnostics_bundle():
    """Sanitized diagnostic snapshot for bug reports.

    Secrets (API password, MA token, LWA secret, OAuth tokens) are never
    included: settings come from the redacted public() view, and the
    deployment status copies only non-secret state keys. Domains, IPs,
    emails and usernames are additionally masked by _diagnostics_redact().
    """
    import shared_store

    bundle = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "app": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "script_root": request.script_root,
            "ingress_enabled": str(get_setting("HA_INGRESS_ENABLED", "false")).lower()
            in ("true", "1", "yes", "on"),
            "enable_apl": _apl_enabled(),
        },
        "settings": store().public(),
        "music_assistant": _ma_status_probe(),
        "verification": _verification_section(),
        "stores": {
            "music_assistant": shared_store.get_ma(),
            "alexa": shared_store.get_alexa(),
        },
        "recent_invocations": list(current_app.config.get("INTENT_LOGS", []))[-20:],
    }
    try:
        deployment, matches, ready = _deployment_facts()
        bundle["deployment"] = {**deployment, "settings_match": matches, "ready": ready}
    except Exception as exc:
        bundle["deployment"] = {"error": f"{type(exc).__name__}: {exc}"}
    return _diagnostics_redact(bundle)


@status_bp.route("/status/diagnostics", methods=["GET"])
def diagnostics_download():
    """Download a sanitized diagnostics bundle for bug reports."""
    payload = json.dumps(
        _diagnostics_bundle(), indent=2, ensure_ascii=False, default=str
    )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    response = Response(payload, mimetype="application/json")
    response.headers["Content-Disposition"] = (
        f'attachment; filename="ma-alexa-skill-diagnostics-{stamp}.json"'
    )
    return response
