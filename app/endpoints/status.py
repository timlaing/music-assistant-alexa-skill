import json
import os
import re
import shutil
import subprocess
import urllib.parse
from pathlib import Path

from flask import Blueprint, Response, current_app, jsonify, request
from markupsafe import escape
from setup_helpers import has_functional_cli_config
from skill_deployment import manager, settings

status_bp = Blueprint('status_bp', __name__)


def _build_status_json():
    skill_html = '<span class="led green"></span> Skill running'

    skill_ask_html = '<span class="muted">ASK CLI check unavailable</span>'
    try:
        skill_host = os.environ.get('SKILL_HOSTNAME', '').strip()
        deployment = manager().public_status()
        if deployment.get('connected'):
            instance = manager()
            current = instance.state.get('deployed_settings')
            matches = current == settings() if current else False
            ready = deployment.get('phase') == 'complete' and matches
            color = 'green' if ready else 'yellow'
            note = 'Amazon verified deployment' if ready else (deployment.get('message') or 'Select your personal skill')
            skill_ask_html = f'<span class="led {color}"></span> {escape(note)} <a href="/setup">Open Setup</a>'
            if ready:
                skill_ask_html += f' (verified at {escape(str(deployment.get("verified_at")))}, saved ID: {escape(deployment.get("skill_id"))})'
        elif shutil.which('ask') and skill_host:
            if not has_functional_cli_config(profile='default'):
                skill_ask_html = '<span class="led yellow"></span> ASK CLI credentials are not configured for profile default'
                try:
                    skill_ask_html += ' <button onclick="window.location=\'/setup\'" style="margin-left:8px">Open Setup</button>'
                except Exception:
                    pass
            else:
                ls = subprocess.run(['ask', 'smapi', 'list-skills-for-vendor', '--profile', 'default'], capture_output=True, text=True)
                out = ls.stdout or ls.stderr or ''
                m = re.search(r'amzn1\.ask\.skill\.[0-9a-fA-F\-]+', out)
                if not m:
                    skill_ask_html = '<span class="led red"></span> Music Assistant Skill interaction model not found via ASK CLI'
                else:
                    sid = m.group(0)
                    mf = subprocess.run(['ask', 'smapi', 'get-skill-manifest', '--skill-id', sid, '--profile', 'default'], capture_output=True, text=True)
                    mf_out = mf.stdout or mf.stderr or ''
                    mm = re.search(r'https?://[^"\s\)\]]+', mf_out)
                    try:
                        if skill_host.startswith('http://') or skill_host.startswith('https://'):
                            cfg_host = urllib.parse.urlparse(skill_host).netloc
                        else:
                            cfg_host = skill_host
                    except Exception:
                        cfg_host = skill_host

                    testing_enabled = False
                    try:
                        en = subprocess.run(['ask', 'smapi', 'get-skill-enablement-status', '--skill-id', sid, '--stage', 'development', '--profile', 'default'], capture_output=True, text=True)
                        en_out = en.stdout or en.stderr or ''
                        if en.returncode == 0 or 'Command executed successfully' in en_out:
                            testing_enabled = True
                        else:
                            if re.search(r'\[Error\]:\s*\{', en_out) or re.search(r'404', en_out):
                                testing_enabled = False
                            elif re.search(r'"isEnabled"\s*:\s*true', en_out, re.IGNORECASE) or re.search(r'"enabled"\s*:\s*true', en_out, re.IGNORECASE):
                                testing_enabled = True
                    except Exception:
                        testing_enabled = False

                    is_green = False
                    if not mm:
                        testing_msg = 'testing enabled' if testing_enabled else 'testing not enabled'
                        skill_ask_html = f'<span class="led yellow"></span> Music Assistant Skill interaction model {escape(sid)} found; endpoint not set ({testing_msg})'
                    else:
                        uri = mm.group(0)
                        try:
                            parsed = urllib.parse.urlparse(uri)
                            manifest_host = parsed.netloc
                            locale_list = []
                            try:
                                mf_json = None
                                try:
                                    mf_json = json.loads(mf_out)
                                except Exception:
                                    idx = mf_out.find('{')
                                    if idx != -1:
                                        try:
                                            mf_json = json.loads(mf_out[idx:])
                                        except Exception:
                                            mf_json = None
                                if mf_json:
                                    locales_obj = mf_json.get('manifest', {}).get('publishingInformation', {}).get('locales', {})
                                    if isinstance(locales_obj, dict):
                                        locale_list = list(locales_obj.keys())
                            except Exception:
                                locale_list = []

                            locale_display = ','.join(locale_list) if locale_list else 'unknown'

                            if manifest_host == cfg_host:
                                if testing_enabled:
                                    skill_ask_html = f'<span class="led green"></span> Music Assistant Skill interaction model found; endpoint matches ({escape(manifest_host)}); testing enabled; locale: {escape(locale_display)}'
                                    is_green = True
                                else:
                                    skill_ask_html = f'<span class="led yellow"></span> Music Assistant Skill interaction model found and endpoint matches ({escape(manifest_host)}); testing NOT enabled'
                            else:
                                testing_note = 'testing enabled' if testing_enabled else 'testing not enabled'
                                skill_ask_html = f'<span class="led red"></span> Music Assistant Skill interaction model endpoint mismatch (manifest: {escape(manifest_host)} vs configured: {escape(cfg_host)}); {testing_note}'
                        except Exception:
                            testing_msg = 'testing enabled' if testing_enabled else 'testing not enabled'
                            skill_ask_html = f'<span class="led yellow"></span> Music Assistant Skill interaction model found; endpoint parse failed ({testing_msg})'

                    try:
                        if not is_green:
                            skill_ask_html += ' <button onclick="window.location=\'/setup\'" style="margin-left:8px">Open Setup</button>'
                    except Exception:
                        pass
        else:
            if not shutil.which('ask'):
                skill_ask_html = '<span class="muted">ask CLI not available in container</span>'
            else:
                skill_ask_html = '<span class="muted">SKILL_HOSTNAME not configured</span>'
    except Exception as e:
        skill_ask_html = f'<span class="muted">ASK check error: {escape(str(e))}</span>'

    ma_api_html = _compute_ma_api_html()
    alexa_api_html = _compute_alexa_api_html()

    # Metadata Refresh display (APL updates)
    try:
        from skill import data as skill_data
        metadata_info = dict(skill_data.get_info())  # Create a copy
        pretty_metadata = json.dumps(metadata_info, indent=2, ensure_ascii=False)
        content_preview = escape(pretty_metadata)
        
        if metadata_info.get('audioSources') or metadata_info.get('primaryText'):
            metadata_html = (
                f'<span class="led green"></span> APL Metadata Refresh (current data)'
                f"<pre class='status-box' tabindex='0' style='white-space:pre-wrap;background:#f6f6f6;padding:8px;border-radius:4px;max-height:200px;overflow:auto;user-select:text'>"
                f"{content_preview}</pre>"
            )
        else:
            metadata_html = (
                f'<span class="led yellow"></span> APL Metadata Refresh (no data loaded yet)'
                f"<pre class='status-box' tabindex='0' style='white-space:pre-wrap;background:#fff9e6;padding:8px;border-radius:4px;max-height:200px;overflow:auto;user-select:text'>"
                f"{content_preview}</pre>"
            )
    except Exception as e:
        metadata_html = f'<span class="led red"></span> Error loading metadata: {escape(str(e))}'

    # lightweight invocations link
    intent_logs = current_app.config.get('INTENT_LOGS', [])
    count = len(intent_logs) if intent_logs else 0
    if count:
        invocations_html = f'<a href="/invocations" target="_blank" rel="noopener noreferrer">View {count} invocations</a>'
    else:
        invocations_html = '<span class="muted">No recent invocations</span>'

    return {'skill_html': skill_html, 'skill_ask_html': skill_ask_html, 'ma_api_html': ma_api_html, 'alexa_api_html': alexa_api_html, 'metadata_html': metadata_html, 'invocations_html': invocations_html, 'created': False}


def _store_html(payload, label):
    if not payload:
        return f'<span class="led yellow"></span> {label} idle — no stream pushed yet'
    preview = escape(json.dumps(payload, indent=2, ensure_ascii=False))
    return f'<span class="led green"></span> {label} ready<pre>{preview}</pre>'


def _compute_ma_api_html(api_user=None, api_pass=None):
    import shared_store
    return _store_html(shared_store.get_ma(), 'Music Assistant API')


def _compute_alexa_api_html(api_user=None, api_pass=None):
    import shared_store
    return _store_html(shared_store.get_alexa(), 'Alexa API')


@status_bp.route('/status/ma', methods=['GET'])
def status_ma():
    return jsonify({'ma_api_html': _compute_ma_api_html()})


@status_bp.route('/status/alexa', methods=['GET'])
def status_alexa():
    return jsonify({'alexa_api_html': _compute_alexa_api_html()})


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
                if metadata_info.get('coverImageSource'):
                    metadata_info['coverImageSource'] = replace_ip_in_url(metadata_info['coverImageSource'], hostname)
                if metadata_info.get('backgroundImageSource'):
                    metadata_info['backgroundImageSource'] = replace_ip_in_url(metadata_info['backgroundImageSource'], hostname)
        except Exception:
            pass  # If hostname replacement fails, show original URLs
        
        pretty_metadata = json.dumps(metadata_info, indent=2, ensure_ascii=False)
        content_preview = escape(pretty_metadata)
        
        if metadata_info.get('audioSources') or metadata_info.get('primaryText'):
            return (
                f'<span class="led green"></span> APL Metadata Refresh (current data)'
                f"<pre class='status-box' tabindex='0' style='white-space:pre-wrap;background:#f6f6f6;padding:8px;border-radius:4px;max-height:200px;overflow:auto;user-select:text'>"
                f"{content_preview}</pre>"
            )
        else:
            return (
                f'<span class="led yellow"></span> APL Metadata Refresh (no data loaded yet)'
                f"<pre class='status-box' tabindex='0' style='white-space:pre-wrap;background:#fff9e6;padding:8px;border-radius:4px;max-height:200px;overflow:auto;user-select:text'>"
                f"{content_preview}</pre>"
            )
    except Exception as e:
        return f'<span class="led red"></span> Error loading metadata: {escape(str(e))}'


@status_bp.route('/status/metadata', methods=['GET'])
def status_metadata():
    return jsonify({'metadata_html': _compute_metadata_html()})

@status_bp.route('/status', methods=['GET'])
def status():
    # If client requested JSON, return the aggregated checks
    want_json = request.args.get('format') == 'json' or 'application/json' in (request.headers.get('Accept') or '')
    if want_json:
        return jsonify(_build_status_json())

    # Non-JSON: render status template
    try:
        tpl_path = Path(__file__).parent.parent / 'templates' / 'status.html'
        tpl = tpl_path.read_text()
        tpl = tpl.replace('__SKILL_HTML__', '<span class="led green"></span> Skill running')
        tpl = tpl.replace('__SKILL_ASK_HTML__', '<span class="muted">Checking ASK CLI status...</span>')
        tpl = tpl.replace('__MA_API_HTML__', '<span class="muted">Checking Music Assistant API...</span>')
        tpl = tpl.replace('__ALEXA_API_HTML__', '<span class="muted">Checking Alexa API...</span>')
        tpl = tpl.replace('__METADATA_HTML__', '<span class="muted">Loading APL metadata...</span>')
        intent_logs = current_app.config.get('INTENT_LOGS', [])
        count = len(intent_logs) if intent_logs else 0
        if count:
            invocations_html = f'<a href="/invocations" target="_blank" rel="noopener noreferrer">View {count} invocations</a>'
        else:
            invocations_html = '<span class="muted">No recent invocations</span>'
        tpl = tpl.replace('__INVOCATIONS_HTML__', invocations_html)
        return Response(tpl, status=200, mimetype='text/html')
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
        return Response(html, status=200, mimetype='text/html')


@status_bp.route('/status/api', methods=['GET'])
def status_api():
    """Lightweight API used by the status UI to fetch aggregated checks."""
    return jsonify(_build_status_json())


@status_bp.route('/status/ask', methods=['GET'])
def status_ask():
    """Return only the ASK CLI check fragment used by the client UI."""
    data = _build_status_json()
    return jsonify({'skill_ask_html': data.get('skill_ask_html')})


@status_bp.route('/status/invocations', methods=['GET'])
def status_invocations():
    """Return the current invocation count and invocation HTML so the UI can refresh it live."""
    intent_logs = current_app.config.get('INTENT_LOGS', [])
    count = len(intent_logs) if intent_logs else 0
    if count:
        invocations_html = f'<a href="/invocations" target="_blank" rel="noopener noreferrer">View {count} invocations</a>'
    else:
        invocations_html = '<span class="muted">No recent invocations</span>'
    return jsonify({'count': count, 'invocations_html': invocations_html})
