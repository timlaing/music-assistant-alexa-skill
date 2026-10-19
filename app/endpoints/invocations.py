from flask import Blueprint, Response, current_app
from markupsafe import escape
from page_nav import nav_html
from pathlib import Path
import json
from datetime import datetime, timezone

invocations_bp = Blueprint("invocations_bp", __name__)

# Repeated empty-value placeholders kept as constants.
NO_PAYLOAD = "(no payload)"
NO_RESPONSE = "(no response)"
NO_TIMESTAMP = "(no timestamp)"
NO_REQUEST_NAME = "(no request name)"


def _entry_value(entry, *keys):
    """Return the first present key's value from entry, else None."""
    for key in keys:
        if key in entry:
            return entry.get(key)
    return None


def _entry_timestamp(entry, incoming):
    """Prefer explicit time fields, otherwise incoming.request.timestamp."""
    t = entry.get("time") or entry.get("ts")
    if t or not isinstance(incoming, dict):
        return t
    try:
        return incoming.get("request", {}).get("timestamp")
    except Exception:
        return t


def _entry_name(incoming):
    """Derive a friendly name: intent name, otherwise request type."""
    try:
        if isinstance(incoming, dict):
            req = incoming.get("request")
            if isinstance(req, dict):
                intent = req.get("intent")
                name = intent.get("name") if isinstance(intent, dict) else None
                return name or req.get("type")
    except Exception:
        return None
    return None


def _format_time_string(t):
    """Parse an ISO-ish timestamp string for display, else show it raw."""
    s = t.strip()
    try:
        if s.endswith("Z") and "T" in s:
            # Parse as UTC then convert to local timezone
            dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
            dt = dt.replace(tzinfo=timezone.utc).astimezone()
            return dt.strftime("%H:%M:%S %Y-%m-%d")
        dt = datetime.fromisoformat(s)
        # If timezone-aware, convert to local; otherwise assume it's local
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.strftime("%H:%M:%S %Y-%m-%d")
    except Exception:
        return escape(s)


def _format_time(t):
    """Render a timestamp value for display."""
    if t is None:
        return NO_TIMESTAMP
    try:
        if isinstance(t, (int, float)):
            return datetime.fromtimestamp(float(t)).strftime("%H:%M:%S %Y-%m-%d")
        if isinstance(t, str):
            return _format_time_string(t)
        return escape(str(t))
    except Exception:
        return escape(str(t))


def _format_value(obj, empty):
    """Pretty-print a payload/response value; `empty` marks missing data."""
    if obj is None:
        return empty
    if isinstance(obj, str):
        s = obj.strip()
        if s == "" or s.lower() == "null":
            return empty
        try:
            parsed = json.loads(s)
        except Exception:
            return s
        if parsed is None:
            return empty
        return json.dumps(parsed, indent=2, ensure_ascii=False)
    try:
        return json.dumps(obj, indent=2, ensure_ascii=False)
    except Exception:
        return str(obj)


def _render_entry(entry_id, entry):
    incoming = _entry_value(entry, "incoming", "payload")
    raw_response = _entry_value(entry, "response", "response_body")

    display_time = _format_time(_entry_timestamp(entry, incoming))
    name = _entry_name(incoming)
    display_name = escape(name) if name else NO_REQUEST_NAME

    pretty_payload = _format_value(incoming, NO_PAYLOAD)
    pretty_response = _format_value(raw_response, NO_RESPONSE)

    # Collapsible structure: header (with toggle) followed by hidden intent-pair
    item_html = (
        f"<div class='invocation'><div class='invocation-header'><h3>Invocation {entry_id} - {display_time} - {display_name}</h3>"
        f"<button class='intent-toggle' aria-expanded='false' style='margin-left:12px'>Show</button></div>"
    )
    item_html += (
        f"<div class='intent-pair' style='display:none; margin-top:8px'><div><strong>Payload</strong>"
        f"<pre class='payload'>{escape(pretty_payload)}</pre></div>"
        f"<div><strong>Response</strong><pre class='response'>{escape(pretty_response)}</pre></div></div>"
    )
    item_html += "</div>"
    return item_html


def _invocations_response(body):
    try:
        tpl_path = Path(__file__).parent.parent / "templates" / "invocations.html"
        tpl = tpl_path.read_text()
        tpl = tpl.replace("__NAV__", nav_html("invocations"))
        tpl = tpl.replace("__INVOCATIONS_BODY__", body)
        return Response(tpl, status=200, mimetype="text/html")
    except Exception:
        html = f"""<!doctype html>
            <html>
            <head><meta charset="utf-8"><title>Invocations</title></head>
            <body>
            <h1>Invocations</h1>
            {body}
            </body>
            </html>"""
        return Response(html, status=200, mimetype="text/html")


@invocations_bp.route("/invocations", methods=["GET"])
def invocations():
    intent_logs = current_app.config.get("INTENT_LOGS", [])
    body_items = [
        _render_entry(len(intent_logs) - idx, entry)
        for idx, entry in enumerate(reversed(intent_logs))
    ]
    body = "\n".join(body_items) or '<div class="muted">No invocations logged</div>'
    return _invocations_response(body)
