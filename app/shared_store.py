"""Locked, independent snapshots for MA streams and Alexa playback metadata."""

import time
from copy import deepcopy
from threading import RLock
from urllib.parse import unquote, urlsplit

_lock = RLock()
_ma = None
_alexa = None
_version = 0
_player_snapshots = {}


def get_ma():
    with _lock:
        return deepcopy(_ma)


def get_alexa():
    with _lock:
        return deepcopy(_alexa)


def set_ma(payload):
    global _ma, _version
    with _lock:
        _version += 1
        player = payload.get("playerId") or player_from_stream(payload.get("streamUrl"))

        # Get the previous snapshot for this player (if any) to preserve streamFirstSeen
        old_snapshot = _player_snapshots.get(player) if player else None

        # Extract player-specific old stream for comparison
        if old_snapshot:
            old_snapshot.get("streamUrl")
        elif _ma:
            _ma.get("streamUrl")

        # Extract player-specific old first_seen for comparison
        if old_snapshot:
            old_snapshot.get("streamFirstSeen")
        elif _ma:
            _ma.get("streamFirstSeen")

        new_stream = payload.get("streamUrl")
        _ma = deepcopy(payload)

        # Track when this stream URL was first pushed. For each new push, reset
        # streamFirstSeen to handle long-running streams and metadata re-pushes
        # correctly. A new playback time is tracked even when the same URL is reused.
        stream_first_seen = time.time()
        _ma.update(
            playerId=player,
            version=_version,
            timestamp=time.time(),
            streamFirstSeen=stream_first_seen,
        )
        if player and new_stream:
            _player_snapshots[player] = deepcopy(_ma)
        elif not player:
            # The push may replace any player's stream; serve it to all, as upstream
            # does, rather than an older per-player stream.
            _player_snapshots.clear()
        return _version


def set_alexa(payload):
    global _alexa
    with _lock:
        _alexa = deepcopy(payload)


def player_from_stream(url):
    """Stock MA stream URLs identify the output player in the final filename.

    Queue streams are /flow|single/<session>/<queue>/<item>/<player>.<fmt>; plugin
    sources (e.g. line-in) are /source/<session>/<source player>/<player>.<fmt>.
    """
    if not isinstance(url, str):
        return None
    parts = urlsplit(url).path.strip("/").split("/")
    queue_stream = len(parts) >= 5 and parts[-5] in ("flow", "single")
    source_stream = len(parts) >= 4 and parts[-4] == "source"
    if not queue_stream and not source_stream:
        return None
    filename, separator, extension = parts[-1].rpartition(".")
    if not separator or extension not in ("mp3", "flac", "wav", "aac", "pcm", "ogg"):
        return None
    return unquote(filename) or None


def get_player_snapshot(player):
    with _lock:
        snapshot = _player_snapshots.get(player)
        if snapshot is None and _ma and not _ma.get("playerId"):
            snapshot = _ma  # stream URLs that do not name their player
        return deepcopy(snapshot)
