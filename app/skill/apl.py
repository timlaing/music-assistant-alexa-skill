# -*- coding: utf-8 -*-

import json
import logging
import os
import sys

from ask_sdk_model.interfaces.alexa.presentation.apl import RenderDocumentDirective

from . import data

# Ensure /app/src is on the Python path so shared_store can be imported
_app_src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _app_src not in sys.path:
    sys.path.insert(0, _app_src)


def _load_apl_template():
    # type: () -> dict
    """Load the APL document template from JSON file."""
    template_path = os.path.join(os.path.dirname(__file__), 'apl_document.json')
    with open(template_path, 'r') as f:
        return json.load(f)


def add_apl(response_builder, start_paused=False):
    # type: (ResponseFactory, bool) -> None
    """Add the RenderDocumentDirective to the response with APL document."""
    metadata = _get_metadata()
    if not metadata:
        logging.warning("No metadata available for APL rendering")
        return

    # The MA API already normalizes local artwork and preserves provider hosts.
    cover_image = metadata.get("coverImageSource", "")
    background_image = metadata.get("backgroundImageSource", "")

    # Load the APL document template
    apl_document = _load_apl_template()

    # Set the dynamic autoplay value based on start_paused
    autoplay = not start_paused

    # Update autoplay in Video component and AlexaTransportControls
    try:
        video_component = apl_document["layouts"]["AudioPlayer"]["item"][0]["items"][2]["items"][1]["items"][0]
        video_component["autoplay"] = autoplay
    except (KeyError, IndexError):
        logging.debug("Could not set video autoplay in APL template")

    try:
        transport_controls = apl_document["layouts"]["AudioPlayer"]["item"][0]["items"][2]["items"][1]["items"][1]["items"][0]["item"][1]
        transport_controls["autoplay"] = autoplay
    except (KeyError, IndexError):
        logging.debug("Could not set transport autoplay in APL template")

    # Update mainTemplate with metadata values
    try:
        main_template_item = apl_document["mainTemplate"]["items"][0]
        main_template_item.update({
            "audioSources": metadata.get("audioSources", ""),
            "backgroundImageSource": background_image,
            "coverImageSource": cover_image,
            "headerAttributionImage": metadata.get("headerAttributionImage", ""),
            "headerTitle": metadata.get("headerTitle", ""),
            "headerSubtitle": metadata.get("headerSubtitle", ""),
            "primaryText": metadata.get("primaryText", ""),
            "secondaryText": metadata.get("secondaryText", "")
        })
    except (KeyError, IndexError):
        logging.warning("Could not update mainTemplate in APL document")

    response_builder.add_directive(
        RenderDocumentDirective(
            token="playbackToken",
            document=apl_document,
            datasources={}
        )
    )


def _get_metadata():
    """Use the same request snapshot as AudioPlayer playback."""
    return data.get_info()
