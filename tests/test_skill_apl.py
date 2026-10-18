"""Tests for skill/apl.py document rendering."""

import shared_store
from ask_sdk_core.response_helper import ResponseFactory
from skill import apl


def _directive(response_builder):
    directives = response_builder.response.directives
    return directives[0] if directives else None


def test_add_apl_without_metadata_is_noop(monkeypatch):
    # get_info() always returns a dict, so the no-metadata guard is reached
    # only via a falsy snapshot (e.g. an empty dict from a fresh store).
    monkeypatch.setattr(apl, "_get_metadata", dict)
    builder = ResponseFactory()
    apl.add_apl(builder)
    assert not builder.response.directives


def test_add_apl_injects_metadata_into_document():
    shared_store.set_ma(
        {
            "streamUrl": "https://streams.example.com/music/song.mp3",
            "title": "Song",
            "artist": "Artist",
            "album": "Album",
            "imageUrl": "https://cdn.example.com/cover.jpg",
        }
    )
    builder = ResponseFactory()
    apl.add_apl(builder)
    directive = _directive(builder)
    assert directive.token == "playbackToken"
    assert directive.datasources == {}
    item = directive.document["mainTemplate"]["items"][0]
    assert item["audioSources"] == "https://streams.example.com/music/song.mp3"
    assert item["primaryText"] == "Song"
    assert item["secondaryText"] == "Artist - Album"
    assert item["coverImageSource"] == "https://cdn.example.com/cover.jpg"
    assert item["backgroundImageSource"] == "https://cdn.example.com/cover.jpg"
    # Autoplay defaults to true.
    video = directive.document["layouts"]["AudioPlayer"]["item"][0]["items"][2][
        "items"
    ][1]["items"][0]
    assert video["autoplay"] is True


def test_add_apl_start_paused_disables_autoplay():
    shared_store.set_ma(
        {"streamUrl": "https://streams.example.com/music/song.mp3", "title": "Song"}
    )
    builder = ResponseFactory()
    apl.add_apl(builder, start_paused=True)
    directive = _directive(builder)
    video = directive.document["layouts"]["AudioPlayer"]["item"][0]["items"][2][
        "items"
    ][1]["items"][0]
    assert video["autoplay"] is False
    transport = directive.document["layouts"]["AudioPlayer"]["item"][0]["items"][2][
        "items"
    ][1]["items"][1]["items"][0]["item"][1]
    assert transport["autoplay"] is False


def test_add_apl_survives_unexpected_template_shape(monkeypatch):
    shared_store.set_ma(
        {"streamUrl": "https://streams.example.com/music/song.mp3", "title": "Song"}
    )
    # A template missing the expected layout/mainTemplate keys exercises the
    # KeyError fallbacks; the directive is still emitted with the empty doc.
    monkeypatch.setattr(apl, "_load_apl_template", dict)
    builder = ResponseFactory()
    apl.add_apl(builder)
    directive = _directive(builder)
    assert directive.document == {}


def test_get_metadata_uses_request_snapshot():
    shared_store.set_ma({"streamUrl": "https://x/song.mp3", "title": "Snap"})
    assert apl._get_metadata()["primaryText"] == "Snap"
