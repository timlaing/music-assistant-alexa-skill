# -*- coding: utf-8 -*-
import gettext
import os
import re
import sys
from contextvars import ContextVar

# Fuege /app/src/ zum Python-Pfad hinzu
_app_src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _app_src not in sys.path:
    sys.path.insert(0, _app_src)

# shared_store lives beside this package, so the sys.path insert above must
# run first (E402 is deliberate here).
import shared_store  # noqa: E402

_ = gettext.gettext

WELCOME_MSG = _("")
HELP_MSG = _(
    "Welcome to {}. You can play, stop, resume listening.  How can I help you ?"
)
UNHANDLED_MSG = _("Sorry, I could not understand what you've just said.")
CANNOT_SKIP_MSG = _(
    "This is radio, you have to wait for previous or next track to play."
)
RESUME_MSG = _("Resuming {}")
NOT_POSSIBLE_MSG = _(
    "This is radio, you can not do that.  You can ask me to stop or pause to stop listening."
)
STOP_MSG = _("")
DEVICE_NOT_SUPPORTED = _("Sorry, this skill is not supported on this device")
DEVICE_NOT_MAPPED_MSG = _(
    "This device is not paired with a Music Assistant player yet. Please configure it on the status page."
)
MA_COMMAND_FAILED_MSG = _("Sorry, I could not reach Music Assistant to skip the track.")

_request_info = ContextVar("playback_metadata", default=None)


def _metadata(payload):
    payload = payload or {}
    secondary = " - ".join(
        str(value) for value in (payload.get("artist"), payload.get("album")) if value
    )
    image = payload.get("imageUrl") or ""
    url = re.sub(r"(?i)\.flac(?=$|\?)", ".mp3", payload.get("streamUrl") or "")
    return {
        "audioSources": url,
        "backgroundImageSource": image,
        "coverImageSource": image,
        "headerAttributionImage": "",
        "headerTitle": "",
        "headerSubtitle": "",
        "primaryText": payload.get("title") or "",
        "secondaryText": secondary,
    }


def select_stream(payload):
    """Pin one player's stream and metadata to this Alexa response."""
    _request_info.set(_metadata(payload))


def begin_request(device_id=None):
    """Pin this request's stream metadata (issue #33).

    When the requesting Echo is mapped to an MA player (/devices), pin that
    player's own push: several Echoes can play different queues at once, and
    MA pushes every player's URL within a fraction of a second. Unmapped
    devices - and players without a push of their own - pin the latest push,
    exactly as before.
    """
    player = None
    if device_id:
        from . import device_mapping

        player = device_mapping.get_player_for_device(device_id)
    select_stream(
        shared_store.get_player_snapshot(player) if player else shared_store.get_ma()
    )


def get_info():
    snapshot = _request_info.get()
    return dict(snapshot) if snapshot is not None else _metadata(shared_store.get_ma())


def get_latest(device_id=None):
    """Read this process directly; an empty store is normal before the first push.

    `device_id` scopes "latest" to the requesting Echo's own player so a
    refresh from one Echo never shows another player's stream (issue #33).
    """
    begin_request(device_id)
    return {"changed": bool(get_info().get("audioSources"))}
