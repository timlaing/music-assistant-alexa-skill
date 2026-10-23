from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from skill import playback_requests
from skill import voice_music as v
from test_radio import handler_input, run_async


def handle(intent, query="", **patches):
    hi = handler_input(intent, query)
    with (
        patch.object(v.device_mapping, "get_player_for_device", return_value="echo"),
        patch.object(v.threading, "Thread") as thread,
    ):
        with patch.multiple(v, **patches):
            v.VoiceMusicHandler().handle(hi)
    return hi.response_builder.speak.call_args.args[0], thread


def test_failed_search_keeps_the_pending_start():
    pending = playback_requests.begin("echo")
    spoken, thread = handle(
        "PlaySongIntent",
        "Missing",
        prepare_music=AsyncMock(side_effect=ValueError("I could not find that track.")),
    )
    assert spoken == "I could not find that track."
    thread.assert_not_called()
    assert playback_requests.current(pending)


def test_ready_request_replaces_the_pending_start():
    pending = playback_requests.begin("echo")
    handle(
        "PlaySongIntent",
        "Song",
        prepare_music=AsyncMock(return_value={"announcement": "Playing Song."}),
    )
    assert not playback_requests.current(pending)


def test_similar_songs_without_a_song_is_answered_without_starting_anything():
    spoken, thread = handle(
        "PlaySimilarIntent",
        check_similar=AsyncMock(side_effect=ValueError("Please play a song first.")),
    )
    assert spoken == "Please play a song first."
    thread.assert_not_called()


def test_similar_songs_are_confirmed_after_the_check():
    spoken, thread = handle("PlaySimilarIntent", check_similar=AsyncMock())
    assert spoken == "Switching to similar songs after this track."
    thread.return_value.start.assert_called_once()


def test_longer_answers_wait_longer_before_the_music_starts():
    short = v.speaking_time("Starting your music.")
    long = v.speaking_time("Resuming Teardrop by Massive Attack.")
    assert 1 < short < long < 3


def item(name, artists=()):
    return SimpleNamespace(
        name=name, artists=[SimpleNamespace(name=a) for a in artists]
    )


@pytest.mark.parametrize(
    ("kind", "found", "spoken"),
    [
        (
            "track",
            item("Teardrop", ["Massive Attack"]),
            "Playing Teardrop by Massive Attack.",
        ),
        ("track", item("Something", ["A", "B"]), "Playing Something by A, B."),
        ("track", item("Untitled"), "Playing Untitled."),
        (
            "album",
            item("Hybrid Theory", ["Linkin Park"]),
            "Playing the album Hybrid Theory by Linkin Park.",
        ),
        ("artist", item("Linkin Park"), "Playing songs by Linkin Park."),
        ("playlist", item("Favorites"), "Playing your playlist Favorites."),
    ],
)
def test_answers_name_what_was_found(kind, found, spoken):
    assert v.announce(kind, found) == spoken


def test_a_ready_selection_is_announced_by_name():
    selection = {"announcement": "Playing Teardrop by Massive Attack."}
    spoken, thread = handle(
        "PlaySongIntent", "Teardrop", prepare_music=AsyncMock(return_value=selection)
    )
    assert spoken == "Playing Teardrop by Massive Attack."
    thread.return_value.start.assert_called_once()


@run_async
async def test_play_music_names_the_recommended_song(monkeypatch):
    from contextlib import asynccontextmanager

    client = SimpleNamespace(
        player_queues=SimpleNamespace(
            get_active_queue=AsyncMock(return_value=SimpleNamespace(queue_id="q"))
        ),
        music=SimpleNamespace(
            recommendations=AsyncMock(
                return_value=[SimpleNamespace(provider="ytmusic", item_id="row")]
            )
        ),
        send_command=AsyncMock(
            return_value=[
                {
                    "media_type": "track",
                    "uri": "ytmusic://track/t1",
                    "name": "Teardrop",
                    "artists": [{"name": "Massive Attack"}],
                }
            ]
        ),
    )

    @asynccontextmanager
    async def connection():
        yield client

    monkeypatch.setattr(v, "connection", connection)
    selection = await v.prepare_music("echo", recommended=True)
    assert selection["media"] == "ytmusic://track/t1"
    assert selection["announcement"] == "Playing Teardrop by Massive Attack."
