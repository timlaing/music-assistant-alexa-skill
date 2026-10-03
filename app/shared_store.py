"""Locked, independent snapshots for MA streams and Alexa playback metadata."""

import time
from copy import deepcopy
from threading import RLock

_lock = RLock()
_ma = None
_alexa = None
_version = 0


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
        _ma = deepcopy(payload)
        _ma.update(version=_version, timestamp=time.time())
        return _version


def set_alexa(payload):
    global _alexa
    with _lock:
        _alexa = deepcopy(payload)
