from datetime import datetime
from urllib.parse import unquote, urlparse

from flask import (
    Blueprint,
    Response,
    current_app,
    redirect,
    render_template,
    request,
    url_for,
)
from markupsafe import escape
from page_nav import nav_html
from shared_store import get_ma
from skill import device_mapping

devices_bp = Blueprint("devices_bp", __name__)


def _extract_device_id(payload):
    try:
        return (
            payload.get("context", {})
            .get("System", {})
            .get("device", {})
            .get("deviceId")
        )
    except Exception:
        return None


def _extract_intent_name(payload):
    try:
        req = payload.get("request", {})
        intent = req.get("intent")
        if isinstance(intent, dict):
            return intent.get("name")
        return req.get("type")
    except Exception:
        return None


def _seen_devices():
    """Return {device_id: {'last_seen': ts, 'last_request': name}} from intent logs."""
    intent_logs = current_app.config.get("INTENT_LOGS", [])
    seen = {}
    for entry in intent_logs:
        payload = entry.get("incoming")
        if not isinstance(payload, dict):
            continue
        device_id = _extract_device_id(payload)
        if not device_id:
            continue
        ts = entry.get("ts")
        name = _extract_intent_name(payload)
        prev = seen.get(device_id)
        if prev is None or (ts and (prev.get("last_seen") or 0) <= ts):
            seen[device_id] = {"last_seen": ts, "last_request": name}
    return seen


def _format_ts(ts):
    if not ts:
        return "(unknown)"
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%H:%M:%S %Y-%m-%d")
    except Exception:
        return str(ts)


def _ma_stream_player_name():
    """Player name parsed from the current Music Assistant stream URL path.

    MA stream URLs look like ``/source/{hash}/{Player Name}/{file}``; the parent
    directory segment (URL-decoded) is the player's display name. Returns an
    empty string when no stream is available.
    """
    try:
        ma = get_ma() or {}
        url = ma.get("streamUrl") or ma.get("url") or ""
        if not url:
            return ""
        parts = [p for p in urlparse(url).path.split("/") if p]
        if len(parts) >= 2:
            return unquote(parts[-2])
        return unquote(parts[-1]) if parts else ""
    except Exception:
        return ""


@devices_bp.route("/devices", methods=["GET"])
def devices_page():
    mapping = device_mapping.load_mapping()
    seen = _seen_devices()

    all_device_ids = sorted(
        set(mapping.keys()) | set(seen.keys()),
        key=lambda d: -(seen.get(d, {}).get("last_seen") or 0),
    )

    # The stream URL's player segment is a display NAME, not a player ID, and
    # a single stream snapshot cannot be associated with a specific Echo —
    # show it as a page-level hint only and never prefill player_id with it.
    stream_hint = _ma_stream_player_name()
    devices = []
    for device_id in all_device_ids:
        info = seen.get(device_id, {})
        saved_player = mapping.get(device_id, "")
        devices.append(
            {
                "device_id": device_id,
                "short_id": device_id[-12:],
                "last_seen": _format_ts(info.get("last_seen")),
                "last_request": info.get("last_request") or "(not seen this session)",
                "player_id": saved_player,
                "mapped": bool(saved_player),
            }
        )

    try:
        page = render_template("devices.html", devices=devices, stream_hint=stream_hint)
        page = page.replace("__NAV__", nav_html("devices"))
        return Response(page, status=200, mimetype="text/html")
    except Exception:
        if devices:
            items = "".join(
                f"<li><code>{escape(device['short_id'])}</code> — "
                f"{escape(device['last_seen'])} — {escape(device['last_request'])} — "
                f"<form method='POST' action='/devices' style='display:inline'>"
                f"<input type='hidden' name='device_id' "
                f"value='{escape(device['device_id'])}'>"
                f"<input type='text' name='player_id' "
                f"value='{escape(device['player_id'])}' placeholder='MA player_id'>"
                "<button type='submit'>Save</button></form></li>"
                for device in devices
            )
        else:
            items = "<li>No devices seen yet.</li>"
        html = f"<!doctype html><html><head><meta charset='utf-8'><title>Device mappings</title></head><body><h1>Device mappings</h1><ul>{items}</ul></body></html>"
        return Response(html, status=200, mimetype="text/html")


@devices_bp.route("/devices", methods=["POST"])
def devices_save():
    device_id = (request.form.get("device_id") or "").strip()
    player_id = (request.form.get("player_id") or "").strip()
    if device_id:
        device_mapping.set_player_for_device(device_id, player_id or None)
    return redirect(url_for("devices_bp.devices_page"))
