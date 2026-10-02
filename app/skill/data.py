# -*- coding: utf-8 -*-
import gettext

_ = gettext.gettext

import os
import re
import sys

# Fuege /app/src/ zum Python-Pfad hinzu
_app_src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _app_src not in sys.path:
    sys.path.insert(0, _app_src)

WELCOME_MSG = _("")
HELP_MSG = _("Welcome to {}. You can play, stop, resume listening.  How can I help you ?")
UNHANDLED_MSG = _("Sorry, I could not understand what you've just said.")
CANNOT_SKIP_MSG = _("This is radio, you have to wait for previous or next track to play.")
RESUME_MSG = _("Resuming {}")
NOT_POSSIBLE_MSG = _("This is radio, you can not do that.  You can ask me to stop or pause to stop listening.")
STOP_MSG = _("")
DEVICE_NOT_SUPPORTED = _("Sorry, this skill is not supported on this device")
DEVICE_NOT_MAPPED_MSG = _("This device is not paired with a Music Assistant player yet. Please configure it on the status page.")
MA_COMMAND_FAILED_MSG = _("Sorry, I could not reach Music Assistant to skip the track.")

from contextvars import ContextVar

import shared_store

_request_info = ContextVar('playback_metadata', default=None)


def _metadata(payload):
    payload = payload or {}
    secondary = ' - '.join(str(value) for value in
                           (payload.get('artist'), payload.get('album')) if value)
    image = payload.get('imageUrl') or ''
    url = re.sub(r'(?i)\.flac(?=$|\?)', '.mp3', payload.get('streamUrl') or '')
    return {
        'audioSources': url, 'backgroundImageSource': image,
        'coverImageSource': image, 'headerAttributionImage': '',
        'headerTitle': '', 'headerSubtitle': '',
        'primaryText': payload.get('title') or '', 'secondaryText': secondary,
    }


def begin_request():
    _request_info.set(_metadata(shared_store.get_ma()))


def get_info():
    snapshot = _request_info.get()
    return dict(snapshot) if snapshot is not None else _metadata(shared_store.get_ma())


def get_latest(*args, **kwargs):
    """Read this process directly; an empty store is normal before the first push."""
    begin_request()
    return {'changed': bool(get_info().get('audioSources'))}
