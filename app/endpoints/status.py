import json
import re
import shutil
import subprocess
import urllib.parse
from pathlib import Path

from app_settings import get_setting
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
from setup_helpers import has_functional_cli_config
from skill_deployment import DeploymentError, manager, settings

status_bp = Blueprint("status_bp", __name__)

TEST_AUDIO_FILE = Path(__file__).resolve().parents[2] / "assets" / "test-tone.mp3"

# Repeated display fragments kept as constants.
TESTING_ENABLED = "testing enabled"
TESTING_DISABLED = "testing not enabled"
NO_RECENT_INVOCATIONS_HTML = '<span class="muted">No recent invocations</span>'
OPEN_SETUP_BUTTON = (
    " <button onclick=\"window.location='/setup'\" "
    'style="margin-left:8px">Open Setup</button>'
)


def _configured_host(skill_host):
    try:
        if skill_host.startswith(("http://", "https://")):
            return urllib.parse.urlparse(skill_host).netloc
        return skill_host
    except Exception:
        return skill_host


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
    """HTML for a connected (managed) skill deployment."""
    instance = manager()
    deployment = instance.public_status()
    current = instance.state.get("deployed_settings")
    try:
        matches = current == settings() if current else False
    except DeploymentError:
        matches = False
    ready = deployment.get("phase") == "complete" and matches
    color = "green" if ready else "yellow"
    note = (
        "Amazon verified deployment"
        if ready
        else (deployment.get("message") or "Select your personal skill")
    )
    html = f'<span class="led {color}"></span> {escape(note)} <a href="/setup">Open Setup</a>'
    if ready:
        html += f" (verified at {escape(str(deployment.get('verified_at')))}, saved ID: {escape(deployment.get('skill_id'))})"
    return html


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
    """ASK CLI / deployment status fragment for the status page."""
    try:
        skill_host = get_setting("SKILL_HOSTNAME", "").strip()
        if manager().public_status().get("connected"):
            return _deployment_status_html()
        if shutil.which("ask") and skill_host:
            return _ask_cli_status_html(skill_host)
        if not shutil.which("ask"):
            return '<span class="muted">ask CLI not available in container</span>'
        return '<span class="muted">SKILL_HOSTNAME not configured</span>'
    except Exception as e:
        return f'<span class="muted">ASK check error: {escape(str(e))}</span>'


def _render_metadata_html(metadata_info):
    """Pretty-print current APL metadata as a status fragment."""
    pretty_metadata = json.dumps(metadata_info, indent=2, ensure_ascii=False)
    content_preview = escape(pretty_metadata)
    if metadata_info.get("audioSources") or metadata_info.get("primaryText"):
        return (
            f'<span class="led green"></span> APL Metadata Refresh (current data)'
            f"<pre class='status-box' tabindex='0' style='white-space:pre-wrap;background:#f6f6f6;padding:8px;border-radius:4px;max-height:200px;overflow:auto;user-select:text'>"
            f"{content_preview}</pre>"
        )
    return (
        f'<span class="led yellow"></span> APL Metadata Refresh (no data loaded yet)'
        f"<pre class='status-box' tabindex='0' style='white-space:pre-wrap;background:#fff9e6;padding:8px;border-radius:4px;max-height:200px;overflow:auto;user-select:text'>"
        f"{content_preview}</pre>"
    )


def _metadata_status_html():
    """Metadata-refresh status fragment (raw skill data)."""
    try:
        from skill import data as skill_data

        return _render_metadata_html(dict(skill_data.get_info()))
    except Exception as e:
        return f'<span class="led red"></span> Error loading metadata: {escape(str(e))}'


def _invocations_link_html(count):
    if count:
        return f'<a href="/invocations" target="_blank" rel="noopener noreferrer">View {count} invocations</a>'
    return NO_RECENT_INVOCATIONS_HTML


def _invocations_count():
    intent_logs = current_app.config.get("INTENT_LOGS", [])
    return len(intent_logs) if intent_logs else 0


def _build_status_json():
    return {
        "skill_html": '<span class="led green"></span> Skill running',
        "skill_ask_html": _skill_ask_html(),
        "ma_api_html": _compute_ma_api_html(),
        "alexa_api_html": _compute_alexa_api_html(),
        "metadata_html": _metadata_status_html(),
        "invocations_html": _invocations_link_html(_invocations_count()),
        "created": False,
    }


def _store_html(payload, label):
    if not payload:
        return f'<span class="led yellow"></span> {label} idle — no stream pushed yet'
    preview = escape(json.dumps(payload, indent=2, ensure_ascii=False))
    return f'<span class="led green"></span> {label} ready<pre>{preview}</pre>'


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
        return f'<span class="led red"></span> Error loading metadata: {escape(str(e))}'


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
        tpl = render_template("status.html")
        tpl = tpl.replace("__NAV__", nav_html("status"))
        tpl = tpl.replace("__URLS_HTML__", "Checking public URLs…")
        tpl = tpl.replace(
            "__SKILL_HTML__", '<span class="led green"></span> Skill running'
        )
        tpl = tpl.replace(
            "__SKILL_ASK_HTML__",
            '<span class="muted">Checking ASK CLI status...</span>',
        )
        tpl = tpl.replace(
            "__MA_API_HTML__",
            '<span class="muted">Checking Music Assistant API...</span>',
        )
        tpl = tpl.replace(
            "__ALEXA_API_HTML__", '<span class="muted">Checking Alexa API...</span>'
        )
        tpl = tpl.replace(
            "__METADATA_HTML__", '<span class="muted">Loading APL metadata...</span>'
        )
        tpl = tpl.replace(
            "__INVOCATIONS_HTML__", _invocations_link_html(_invocations_count())
        )
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
    """Return the current invocation count and invocation HTML so the UI can refresh it live."""
    count = _invocations_count()
    return jsonify({"count": count, "invocations_html": _invocations_link_html(count)})


@status_bp.route("/status/urls", methods=["GET"])
def status_urls():
    from reachability import html, snapshot

    result = snapshot()
    return jsonify(**result, urls_html=html(result))
