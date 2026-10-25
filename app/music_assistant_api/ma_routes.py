"""Music Assistant stream push and snapshot endpoints."""

from urllib.parse import urlsplit

import shared_store
from app_settings import get_setting
from flask import jsonify, request
from public_urls import rewrite_url


def register_routes(bp):
    bp.add_url_rule("/push-url", "push_url", push_url, methods=["POST"])
    bp.add_url_rule("/latest-url", "latest_url", latest_url, methods=["GET"])


def stream_error(payload):
    """Why a push payload cannot name a stream, or None when it can."""
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("streamUrl"), str)
        or not payload["streamUrl"]
    ):
        return "Missing required fields"
    return None


def pushed_image(payload, raw_url, base):
    """Artwork rewritten like the stream, unless it is hosted with its own push."""
    image_url = payload.get("imageUrl")
    if not image_url:
        return image_url
    if not isinstance(image_url, str):
        raise ValueError("imageUrl must be a string")
    # Provider artwork already hosted elsewhere must retain its own host.
    image_base = (
        base if urlsplit(image_url).hostname == urlsplit(raw_url).hostname else ""
    )
    return rewrite_url(image_url, image_base)


def push_url():
    payload = request.get_json(silent=True)
    error = stream_error(payload)
    if error:
        return jsonify({"error": error}), 400
    raw_url = payload["streamUrl"]
    base = get_setting("MA_HOSTNAME", "").strip()
    try:
        stream_url = rewrite_url(raw_url, base)
        image_url = pushed_image(payload, raw_url, base)
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    version = shared_store.set_ma(
        {
            "streamUrl": stream_url,
            "title": payload.get("title"),
            "artist": payload.get("artist"),
            "album": payload.get("album"),
            "imageUrl": image_url,
            # The scheme://host Music Assistant actually pushed from — the
            # observable truth of its configured api_url. host_url (not
            # url_root) excludes the /ma mount prefix. Redacted to a
            # host placeholder in the diagnostics bundle.
            "push_base": request.host_url.rstrip("/"),
            "playerId": payload.get("playerId")
            if isinstance(payload.get("playerId"), str)
            else None,
        }
    )
    return jsonify({"status": "ok", "version": version})


def latest_url():
    payload = shared_store.get_ma()
    if not payload:
        return jsonify({"error": "No URL available"}), 404
    return jsonify(payload)
