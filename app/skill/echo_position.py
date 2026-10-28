"""Where each Echo really is in its stream, as it reports with every request.

Alexa pauses skill audio for every voice request, so Music Assistant's clock runs ahead
of the Echo. Every request carries the Echo's AudioPlayer state, and the playback events
carry the offset at which playback stopped or resumed. Music Assistant's Alexa player
reads the latest report per player from /alexa/playback-state to correct itself.
"""

import threading
import time
from urllib.parse import unquote, urlsplit

import shared_store

from . import device_mapping

# Activity implied by AudioPlayer events, which report the offset in the request itself.
EVENT_ACTIVITY = {
    "AudioPlayer.PlaybackStarted": "PLAYING",
    "AudioPlayer.PlaybackStopped": "STOPPED",
    "AudioPlayer.PlaybackFinished": "FINISHED",
}
# Alexa's player activity values that denote the Echo is still holding the stream.
# BUFFER_UNDERRUN means there's a temporary buffer underrun but the Echo still
# holds the stream and will resume.
_ENGAGED = {"PLAYING", "PAUSED", "BUFFERING", "BUFFER_UNDERRUN"}

_lock = threading.Lock()
_reports = {}
# The device that sends each player's playback events, i.e. the one actually playing.
_playing_devices = {}


def record(envelope):
    """Remember the Echo's stream offset and activity for its Music Assistant player."""
    request = envelope.request
    device = getattr(getattr(envelope.context, "system", None), "device", None)
    device_id = getattr(device, "device_id", None)
    player = device_mapping.get_player_for_device(device_id)
    activity = EVENT_ACTIVITY.get(getattr(request, "object_type", None))
    is_event = activity is not None
    if is_event:
        token = getattr(request, "token", None)
        offset = getattr(request, "offset_in_milliseconds", None)
        # An Alexa speaker group sends its playback events under its own device ID,
        # which is not mapped; the stream URL names the player it was pushed for.
        player = player or shared_store.player_from_stream(token)
    else:
        audio_player = getattr(envelope.context, "audio_player", None)
        token = getattr(audio_player, "token", None)
        offset = getattr(audio_player, "offset_in_milliseconds", None)
        activity = getattr(
            getattr(audio_player, "player_activity", None), "value", None
        )
    if not player or not token or offset is None or not activity:
        return
    # Alexa's own send time orders reports that arrive out of order.
    sent_at = getattr(request, "timestamp", None)
    with _lock:
        latest = _reports.get(player)
        if latest and sent_at and latest["sentAt"] and sent_at < latest["sentAt"]:
            return
        if is_event:
            _playing_devices[player] = device_id
        elif _playing_devices.get(player, device_id) != device_id:
            # A member of an Alexa speaker group reports its own idle player, not the
            # group's playback, and the group sends no events around a voice request.
            return
        _reports[player] = {
            "token": token,
            "offsetMs": offset,
            "activity": activity,
            "observedAt": time.time(),
            "sentAt": sent_at,
        }


def latest_reports():
    """Latest report per player with its current-stream flag (read-only)."""
    with _lock:
        players = list(_reports)
    return {player: report(player) for player in players}


def _stream_key(url):
    # The Echo gets the pushed URL rewritten to MP3; compare the path without extension.
    path = unquote(urlsplit(url or "").path)
    return path.rsplit(".", 1)[0] if path else ""


def report(player):
    """Return the latest report for the player, and whether it is for its current stream."""
    with _lock:
        latest = _reports.get(player)
    if latest is None:
        return {"playerId": player, "current": False}
    snapshot = shared_store.get_player_snapshot(player) or {}
    pushed = _stream_key(snapshot.get("streamUrl"))
    return {
        "playerId": player,
        "current": bool(pushed) and _stream_key(latest["token"]) == pushed,
        "offsetMs": latest["offsetMs"],
        "activity": latest["activity"],
        "observedAt": latest["observedAt"],
        "ageSeconds": max(0.0, time.time() - latest["observedAt"]),
    }
