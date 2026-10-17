from flask import Blueprint, Response, current_app
from markupsafe import escape
from pathlib import Path
import json
from datetime import datetime, timezone

invocations_bp = Blueprint('invocations_bp', __name__)

# Repeated empty-value placeholders kept as constants.
NO_PAYLOAD = '(no payload)'
NO_RESPONSE = '(no response)'


@invocations_bp.route('/invocations', methods=['GET'])
def invocations():
    intent_logs = current_app.config.get('INTENT_LOGS', [])
    body_items = []
    for idx, entry in enumerate(reversed(intent_logs)):
        entry_id = len(intent_logs) - idx

        # Support multiple entry shapes: modern entries use 'incoming'/'response_body'/'ts'
        if 'incoming' in entry:
            incoming = entry.get('incoming')
        elif 'payload' in entry:
            incoming = entry.get('payload')
        else:
            incoming = None
        if 'response' in entry:
            raw_response = entry.get('response')
        elif 'response_body' in entry:
            raw_response = entry.get('response_body')
        else:
            raw_response = None

        # Prefer explicit time fields, otherwise try incoming.request.timestamp
        t = entry.get('time') or entry.get('ts')
        try:
            if not t and isinstance(incoming, dict):
                t = incoming.get('request', {}).get('timestamp')
        except Exception:
            pass

        # Derive a friendly name: prefer intent name, otherwise use request type
        name = None
        try:
            if isinstance(incoming, dict):
                req = incoming.get('request')
                if isinstance(req, dict):
                    intent = req.get('intent')
                    if isinstance(intent, dict):
                        name = intent.get('name')
                    if not name:
                        name = req.get('type')
        except Exception:
            name = None

        # Friendly defaults for missing metadata
        if t is None:
            display_time = '(no timestamp)'
        else:
            try:
                # If numeric epoch, format time first then date; else try ISO parse and reformat
                if isinstance(t, (int, float)):
                    display_time = datetime.fromtimestamp(float(t)).strftime('%H:%M:%S %Y-%m-%d')
                elif isinstance(t, str):
                    s = t.strip()
                    try:
                        if s.endswith('Z') and 'T' in s:
                            # Parse as UTC then convert to local timezone
                            dt = datetime.strptime(s, '%Y-%m-%dT%H:%M:%SZ')
                            dt = dt.replace(tzinfo=timezone.utc).astimezone()
                            display_time = dt.strftime('%H:%M:%S %Y-%m-%d')
                        else:
                            dt = datetime.fromisoformat(s)
                            # If timezone-aware, convert to local; otherwise assume it's local
                            if dt.tzinfo is not None:
                                dt = dt.astimezone()
                            display_time = dt.strftime('%H:%M:%S %Y-%m-%d')
                    except Exception:
                        display_time = escape(s)
                else:
                    display_time = escape(str(t))
            except Exception:
                display_time = escape(str(t))

        display_name = escape(name) if name else '(no request name)'

        def _format_payload(obj):
            # Treat None, empty string, or literal 'null' as no payload
            if obj is None:
                return NO_PAYLOAD
            if isinstance(obj, str):
                s = obj.strip()
                if s == '' or s.lower() == 'null':
                    return NO_PAYLOAD
                # try to parse JSON string
                try:
                    parsed = json.loads(s)
                    if parsed is None:
                        return NO_PAYLOAD
                    return json.dumps(parsed, indent=2, ensure_ascii=False)
                except Exception:
                    return s
            # obj is likely a dict/list
            try:
                return json.dumps(obj, indent=2, ensure_ascii=False)
            except Exception:
                return str(obj)

        def _format_response(obj):
            if obj is None:
                return NO_RESPONSE
            if isinstance(obj, str):
                s = obj.strip()
                if s == '' or s.lower() == 'null':
                    return NO_RESPONSE
                # attempt to parse JSON body
                try:
                    parsed = json.loads(s)
                    if parsed is None:
                        return NO_RESPONSE
                    return json.dumps(parsed, indent=2, ensure_ascii=False)
                except Exception:
                    return s
            try:
                return json.dumps(obj, indent=2, ensure_ascii=False)
            except Exception:
                return str(obj)

        pretty_payload = _format_payload(incoming)
        pretty_response = _format_response(raw_response)

        # Collapsible structure: header (with toggle) followed by hidden intent-pair
        item_html = (f"<div class='invocation'><div class='invocation-header'><h3>Invocation {entry_id} - {display_time} - {display_name}</h3>"
                 f"<button class='intent-toggle' aria-expanded='false' style='margin-left:12px'>Show</button></div>")
        item_html += (f"<div class='intent-pair' style='display:none; margin-top:8px'><div><strong>Payload</strong>"
                  f"<pre class='payload'>{escape(pretty_payload)}</pre></div>"
                  f"<div><strong>Response</strong><pre class='response'>{escape(pretty_response)}</pre></div></div>")
        item_html += "</div>"
        body_items.append(item_html)

    body = '\n'.join(body_items) or '<div class="muted">No invocations logged</div>'
    try:
        tpl_path = Path(__file__).parent.parent / 'templates' / 'invocations.html'
        tpl = tpl_path.read_text()
        tpl = tpl.replace('__INVOCATIONS_BODY__', body)
        return Response(tpl, status=200, mimetype='text/html')
    except Exception:
        html = f"""<!doctype html>
            <html>
            <head><meta charset="utf-8"><title>Invocations</title></head>
            <body>
            <h1>Invocations</h1>
            {body}
            </body>
            </html>"""
        return Response(html, status=200, mimetype='text/html')
