from flask import Blueprint, Response, current_app, render_template
from markupsafe import escape
from page_nav import nav_html
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
            return dt.strftime("%H:%M:%S")
        dt = datetime.fromisoformat(s)
        # If timezone-aware, convert to local; otherwise assume it's local
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.strftime("%H:%M:%S")
    except Exception:
        return escape(s)


def _format_time(t):
    """Render a timestamp value for display."""
    if t is None:
        return NO_TIMESTAMP
    try:
        if isinstance(t, (int, float)):
            return datetime.fromtimestamp(float(t)).strftime("%H:%M:%S")
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
        f"<div class='ma-invocation'><div class='invocation-header'><h3>Invocation {entry_id} - {display_time} - {display_name}</h3>"
        f"<button class='intent-toggle' type='button' aria-expanded='false'>Show</button></div>"
    )
    item_html += (
        f"<div class='intent-pair' style='display:none'><div><strong>Payload</strong>"
        f"<pre class='payload'>{escape(pretty_payload)}</pre></div>"
        f"<div><strong>Response</strong><pre class='response'>{escape(pretty_response)}</pre></div></div>"
    )
    item_html += "</div>"
    return item_html


def _entry_device(incoming):
    """Return the short device id for an incoming payload, else None."""
    try:
        if not isinstance(incoming, dict):
            return None
        device_id = (
            incoming.get("context", {})
            .get("System", {})
            .get("device", {})
            .get("deviceId")
        )
        return device_id if isinstance(device_id, str) and device_id else None
    except Exception:
        return None


def _render_row(entry_id, entry, detail_id):
    """One desktop table row: Time, Request, Device, Result and a Details toggle."""
    incoming = _entry_value(entry, "incoming", "payload")
    display_time = _format_time(_entry_timestamp(entry, incoming))
    display_name = _entry_name(incoming) or NO_REQUEST_NAME
    device = _entry_device(incoming)
    device_cell = escape("…" + device[-6:]) if device else "—"
    # The intent log records no Alexa-side result, so it stays unknown rather
    # than being inferred from an accepted HTTP response.
    return (
        f"<tr><td>{escape(display_time)}</td>"
        f"<td>{escape(display_name)}<br>"
        f"<span class='ma-small ma-muted'>Invocation {entry_id}</span></td>"
        f"<td><code>{device_cell}</code></td>"
        f"<td><span class='ma-state'><span class='ma-dot' aria-hidden='true'></span>"
        f"Unknown</span></td>"
        f"<td><button class='ma-button intent-toggle' type='button' "
        f"aria-expanded='false' aria-controls='{detail_id}'>Details</button></td></tr>"
    )


def _render_record(entry_id, entry, detail_id):
    """One mobile card mirroring a table row."""
    incoming = _entry_value(entry, "incoming", "payload")
    display_time = _format_time(_entry_timestamp(entry, incoming))
    display_name = _entry_name(incoming) or NO_REQUEST_NAME
    device = _entry_device(incoming)
    device_text = escape("Device …" + device[-6:]) if device else "Device —"
    return (
        f"<article class='ma-record'><div class='ma-panelhead'><h3>"
        f"{escape(display_name)}</h3>"
        f"<span class='ma-state'><span class='ma-dot' aria-hidden='true'></span>"
        f"Unknown</span></div>"
        f"<div class='ma-small ma-muted'>{escape(display_time)} · Invocation {entry_id}"
        f" · {device_text}</div>"
        f"<button class='ma-button intent-toggle' type='button' "
        f"aria-expanded='false' aria-controls='{detail_id}'>Details</button></article>"
    )


def _render_detail(entry_id, entry, detail_id):
    """Shared expandable payload/response block for one invocation.

    Hidden by default; the row/card Details button toggles it open. Renders the
    payload/response directly (no nested header), keeping the test-pinned
    _render_entry output untouched for its own callers.
    """
    incoming = _entry_value(entry, "incoming", "payload")
    raw_response = _entry_value(entry, "response", "response_body")
    name = _entry_name(incoming) or NO_REQUEST_NAME
    pretty_payload = _format_value(incoming, NO_PAYLOAD)
    pretty_response = _format_value(raw_response, NO_RESPONSE)
    return (
        f"<div id='{detail_id}' class='ma-requestdetail' style='display:none'>"
        f"<div><strong>{escape(name)} · Request Details</strong>"
        f"<pre class='payload'>{escape(pretty_payload)}</pre></div>"
        f"<div><strong>Response</strong><pre class='response'>{escape(pretty_response)}</pre></div>"
        f"</div>"
    )


def _invocations_body(intent_logs):
    """Assemble the toolbar, desktop table, mobile cards and detail blocks."""
    rev = list(reversed(intent_logs))
    # Number newest highest, matching the previous newest-first numbering.
    numbered = [(len(intent_logs) - idx, entry) for idx, entry in enumerate(rev)]
    rows, cards, details = [], [], []
    for entry_id, entry in numbered:
        detail_id = f"invocation-detail-{entry_id}"
        rows.append(_render_row(entry_id, entry, detail_id))
        cards.append(_render_record(entry_id, entry, detail_id))
        details.append(_render_detail(entry_id, entry, detail_id))
    toolbar = (
        f"<div class='ma-toolbar'><span class='ma-muted'>"
        f"{len(numbered)} invocations logged</span></div>"
    )
    table = (
        "<table class='ma-table'><thead><tr><th>Time</th><th>Request</th>"
        "<th>Device</th><th>Result</th><th></th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )
    return toolbar + table + "".join(cards) + "".join(details)


def _invocations_response(body):
    try:
        page = render_template("invocations.html")
        page = page.replace("__NAV__", nav_html("invocations"))
        page = page.replace("__INVOCATIONS_BODY__", body)
        return Response(page, status=200, mimetype="text/html")
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
    if intent_logs:
        body = _invocations_body(intent_logs)
    else:
        body = '<div class="muted">No invocations logged</div>'
    return _invocations_response(body)
