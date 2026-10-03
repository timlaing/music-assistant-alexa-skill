# -*- coding: utf-8 -*-

import logging
import os
import threading

import requests
from ask_sdk_model.interfaces.alexa.presentation.apl import (
    ControlMediaCommand,
    ExecuteCommandsDirective,
    MediaCommandType,
)
from ask_sdk_model.interfaces.audioplayer import (
    AudioItem,
    ClearBehavior,
    ClearQueueDirective,
    PlayBehavior,
    PlayDirective,
    StopDirective,
    Stream,
)

from . import data
from .apl import add_apl


def apl_enabled():
    return os.environ.get('ENABLE_APL', 'false').lower() in ('true', '1', 'yes')


# Last known playback-stopped position per device, so Resume can continue
# from where it left off instead of always restarting at 0. MA's players/cmd/play
# (used for AMAZON.ResumeIntent) doesn't push a fresh stream URL, so on resume
# we're replaying the same stream - the offset only makes sense for that same URL.
_last_stopped = {}
_last_stopped_lock = threading.Lock()


def record_stopped_position(device_id, url, offset_ms):
    if not device_id or not url:
        return
    with _last_stopped_lock:
        _last_stopped[device_id] = {"url": url, "offset": offset_ms or 0}


def get_resume_offset(device_id, url):
    """Return the offset (ms) to resume at for this device+url, or 0 if unknown/stale."""
    if not device_id:
        return 0
    with _last_stopped_lock:
        entry = _last_stopped.get(device_id)
    if entry and entry.get("url") == url:
        return entry.get("offset", 0)
    return 0


def get_ma_hostname(raise_on_http_scheme=True):
    from public_urls import public_base
    try:
        return public_base(os.environ.get('MA_HOSTNAME', ''))
    except ValueError:
        if raise_on_http_scheme:
            raise
        return ''


def replace_ip_in_url(url, hostname):
    from public_urls import rewrite_url
    return rewrite_url(url, hostname)

def audio_data(request):
    try:
        return data.get_info()
    except Exception:
        return


def push_alexa_metadata(url):
    payload = {
        'streamUrl': url,
        'title': data.get_info().get("primaryText"),
        'secondary': data.get_info().get("secondaryText"),
        'imageUrl': data.get_info().get("coverImageSource")
    }

    import shared_store
    shared_store.set_alexa(payload)


def play(url, offset, text, response_builder, supports_apl=False):
    if supports_apl and apl_enabled():
        add_apl(response_builder)
    else:
        try:
            hostname = get_ma_hostname(raise_on_http_scheme=True)
        except ValueError:
            response_builder.speak(
                "The domain uses an unsupported scheme (http). Please check your environment variable MA_HOSTNAME.").set_should_end_session(True)
            return response_builder.response

        if not hostname:
            response_builder.speak(
                "You did not specify a valid hostname. Please check your environment variable MA_HOSTNAME.").set_should_end_session(True)
            return response_builder.response

        url = replace_ip_in_url(url, hostname)

        skip_validation = os.environ.get('SKIP_URL_VALIDATION', 'false').lower() in ('true', '1', 'yes')

        if skip_validation:
            logging.info('Stream URL (validation skipped via SKIP_URL_VALIDATION): %s', url)
        else:
            try:
                head_resp = requests.head(url, allow_redirects=True, timeout=5)
                resp = head_resp
                if head_resp.status_code >= 400:
                    head_resp.close()
                    resp = requests.get(url, stream=True, allow_redirects=True, timeout=5)

                status_code = resp.status_code
                resp.close()
                if status_code >= 400:
                    logging.error('Audio URL returned HTTP %s: %s', resp.status_code, url)
                    response_builder.speak(
                        "Sorry, I can't reach the audio file. Please check that your stream URL is internet accessible via HTTPS at the MA_HOSTNAME variable you provided.")
                    response_builder.set_should_end_session(True)
                    return response_builder.response
            except requests.RequestException:
                logging.exception('Play Function URL: %s', url)
                response_builder.speak(
                    "Sorry, I can't reach the audio file. Please check that your stream URL is internet accessible via HTTPS at the MA_HOSTNAME variable you provided.")
                response_builder.set_should_end_session(True)
                return response_builder.response

        response_builder.add_directive(
            PlayDirective(
                play_behavior=PlayBehavior.REPLACE_ALL,
                audio_item=AudioItem(
                    stream=Stream(
                        token=url,
                        url=url,
                        offset_in_milliseconds=offset,
                        expected_previous_token=None
                    )
                )
            )
        )
        response_builder.set_should_end_session(True)

    if text:
        response_builder.speak(text)

    try:
        push_alexa_metadata(url)
    except Exception:
        logging.exception('Error while preparing Alexa API push payload')

    return response_builder.response


def stop(text, response_builder, supports_apl=False):
    response_builder.add_directive(StopDirective())

    if text:
        response_builder.speak(text)

    response_builder.set_should_end_session(True)

    return response_builder.response


def pause(text, response_builder, supports_apl=False, session_new=False):
    if supports_apl and apl_enabled():
        try:
            if session_new:
                try:
                    add_apl(response_builder, start_paused=True)
                except Exception:
                    logging.exception('Failed to re-render APL on session new')
                response_builder.set_should_end_session(False)
            else:
                cmd = ControlMediaCommand(command=MediaCommandType.pause, component_id="videoPlayer")
                response_builder.add_directive(
                    ExecuteCommandsDirective(
                        commands=[cmd],
                        token="playbackToken"
                    )
                ).set_should_end_session(False)
        except Exception:
            logging.exception('Failed to add APL pause command; falling back to Stop')
            response_builder.add_directive(StopDirective())
            response_builder.set_should_end_session(True)
    else:
        response_builder.add_directive(StopDirective())
        response_builder.set_should_end_session(True)

    if text:
        response_builder.speak(text)

    return response_builder.response

def clear(response_builder):
    response_builder.add_directive(ClearQueueDirective(
        clear_behavior=ClearBehavior.CLEAR_ENQUEUED))
    return response_builder.response


def update_apl_metadata(response_builder):
    """Update the APL document with the latest metadata without interrupting playback.

    This function sends ExecuteCommands directives to update only the text and image
    components, avoiding a full document re-render that would restart audio playback.
    This is called in response to UserEvent requests from the APL document.
    """
    if not apl_enabled():
        return
    try:
        cover_image = data.get_info().get("coverImageSource", "")
        background_image = data.get_info().get("backgroundImageSource", "")

        # Build SetValue commands to update individual components
        commands = []

        # Update primary text (song title)
        if data.get_info().get("primaryText"):
            commands.append({
                "type": "SetValue",
                "componentId": "Audio_PrimaryText",
                "property": "text",
                "value": data.get_info()["primaryText"]
            })

        # Update secondary text (artist/album)
        if data.get_info().get("secondaryText"):
            commands.append({
                "type": "SetValue",
                "componentId": "Audio_SecondaryText",
                "property": "text",
                "value": data.get_info()["secondaryText"]
            })

        # Update cover image and bound data so conditional rendering refreshes.
        if cover_image:
            commands.append({
                "type": "SetValue",
                "componentId": "AudioPlayerRoot",
                "property": "coverImageSource",
                "value": cover_image
            })
            commands.append({
                "type": "SetValue",
                "componentId": "Audio_CoverArt",
                "property": "imageSource",
                "value": cover_image
            })

        # Update background image and bound data so layouts recompute.
        if background_image:
            commands.append({
                "type": "SetValue",
                "componentId": "AudioPlayerRoot",
                "property": "backgroundImageSource",
                "value": background_image
            })
            commands.append({
                "type": "SetValue",
                "componentId": "AlexaBackground",
                "property": "backgroundImageSource",
                "value": background_image
            })

        # Send ExecuteCommands directive if we have any commands
        if commands:
            response_builder.add_directive(
                ExecuteCommandsDirective(
                    commands=commands,
                    token="playbackToken"
                )
            )
        else:
            logging.warning("No SetValue commands generated - no metadata to update")

    except Exception:
        logging.exception('Error while updating APL metadata')


def schedule_apl_refresh(response_builder, delay_ms=1000):
    """Schedule the next APL metadata refresh via a UserEvent.

    This keeps refreshes alive even if the onMount loop does not repeat.
    """
    if not apl_enabled():
        return
    try:
        commands = [
            {
                "type": "Idle",
                "delay": int(delay_ms)
            },
            {
                "type": "SendEvent",
                "arguments": [
                    "MetadataRefresh",
                    "${refreshTick}"
                ]
            }
        ]

        response_builder.add_directive(
            ExecuteCommandsDirective(
                commands=commands,
                token="playbackToken"
            )
        )
    except Exception:
        logging.exception('Error while scheduling APL refresh')
