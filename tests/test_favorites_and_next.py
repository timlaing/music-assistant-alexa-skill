import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import pytest
from music_assistant_models.enums import MediaType, QueueOption
from skill import voice_music as v
from test_radio import handler_input, queue, run_async


@pytest.fixture
def connected(monkeypatch):
    from contextlib import asynccontextmanager

    def use(client):
        @asynccontextmanager
        async def connection():
            yield client

        monkeypatch.setattr(v, "connection", connection)

    return use


def track(name, provider="library", item_id=None, favorite=False, available=True):
    item_id = item_id or name.lower()
    return NS(
        name=name,
        item_id=item_id,
        provider=provider,
        uri=f"{provider}://track/{item_id}",
        media_type=MediaType.TRACK,
        favorite=favorite,
        available=available,
        artists=[],
    )


def client(current=None, library=None, favorites=(), found=()):
    q = queue(current) if current is not None else NS(queue_id="echo-queue")
    if current is None:
        q.current_item = None
    return NS(
        music=NS(
            add_item_to_favorites=AsyncMock(),
            remove_item_from_favorites=AsyncMock(),
            add_item_to_library=AsyncMock(),
            remove_item_from_library=AsyncMock(),
            get_library_item_by_prov_id=AsyncMock(return_value=library),
            get_library_tracks=AsyncMock(return_value=list(favorites)),
            search=AsyncMock(return_value=NS(tracks=list(found))),
        ),
        player_queues=NS(
            get_active_queue=AsyncMock(return_value=q), play_media=AsyncMock()
        ),
        send_command=AsyncMock(),
    )


async def apply(collection, add):
    """Answer like the handler, then make the change it schedules."""
    message, change = await v.plan_song_change("echo", collection, add)
    if change is not None:
        await change()
    return message


@run_async
async def test_add_to_favorites_hearts_the_current_song(connected):
    c = client(track("Teardrop", "ytmusic", "abc"))
    connected(c)
    message = await apply("favorites", True)
    assert message == "Adding Teardrop to your favorites."
    c.music.add_item_to_favorites.assert_awaited_once_with("ytmusic://track/abc")


@run_async
async def test_remove_from_favorites_clears_the_heart_of_the_library_copy(connected):
    library = track("Teardrop", item_id="12", favorite=True)
    c = client(track("Teardrop", "ytmusic", "abc"), library=library)
    connected(c)
    message = await apply("favorites", False)
    assert message == "Removing Teardrop from your favorites."
    # Checked when answering, and again when the change runs.
    c.music.get_library_item_by_prov_id.assert_awaited_with(
        MediaType.TRACK, "abc", "ytmusic"
    )
    c.music.remove_item_from_favorites.assert_awaited_once_with(MediaType.TRACK, "12")
    c.music.remove_item_from_library.assert_not_awaited()


@pytest.mark.parametrize(
    "collection, add, current, message",
    [
        (
            "favorites",
            True,
            track("A", favorite=True),
            "A is already in your favorites.",
        ),
        ("favorites", False, track("A"), "A is not in your favorites."),
        ("library", True, track("A"), "A is already in your library."),
        ("library", False, track("A", "ytmusic"), "A is not in your library."),
    ],
)
@run_async
async def test_nothing_changes_when_already_in_that_state(
    connected, collection, add, current, message
):
    c = client(current)
    connected(c)
    assert await apply(collection, add) == message
    for action in (
        "add_item_to_favorites",
        "remove_item_from_favorites",
        "add_item_to_library",
        "remove_item_from_library",
    ):
        getattr(c.music, action).assert_not_awaited()


@run_async
async def test_add_to_library_saves_a_provider_song(connected):
    c = client(track("Teardrop", "ytmusic", "abc"))
    connected(c)
    message = await apply("library", True)
    assert message == "Adding Teardrop to your library."
    c.music.add_item_to_library.assert_awaited_once_with("ytmusic://track/abc")
    c.music.add_item_to_favorites.assert_not_awaited()


@run_async
async def test_remove_from_library_removes_the_library_copy(connected):
    library = track("Teardrop", item_id="12")
    c = client(track("Teardrop", "ytmusic", "abc"), library=library)
    connected(c)
    message = await apply("library", False)
    assert message == "Removing Teardrop from your library."
    c.music.remove_item_from_library.assert_awaited_once_with(MediaType.TRACK, "12")


@run_async
async def test_unknown_library_copy_counts_as_not_saved(connected):
    from music_assistant_models.errors import MediaNotFoundError

    c = client(track("Teardrop", "ytmusic", "abc"))
    c.music.get_library_item_by_prov_id.side_effect = MediaNotFoundError("gone")
    connected(c)
    message = await apply("library", False)
    assert message == "Teardrop is not in your library."


@run_async
async def test_song_commands_need_a_song_playing(connected):
    connected(client(NS(media_type=MediaType.RADIO, name="BBC")))
    with pytest.raises(ValueError, match="play a song first"):
        await v.plan_song_change("echo", "favorites", True)


@run_async
async def test_favorites_play_shuffled_once_as_a_finite_queue(connected):
    favorites = [track("A"), track("B"), track("Gone", available=False)]
    c = client(favorites=favorites)
    connected(c)
    with patch.object(v.random, "shuffle", side_effect=lambda xs: xs.reverse()):
        selection = await v.prepare_music("echo", kind="favorites")
    assert selection["media"] == ["library://track/b", "library://track/a"]
    assert selection["radio"] is False
    c.music.get_library_tracks.assert_awaited_once_with(favorite=True, limit=500)


@run_async
async def test_no_favorites_is_spoken(connected):
    connected(client())
    with pytest.raises(ValueError, match="no favorite songs"):
        await v.prepare_music("echo", kind="favorites")


@run_async
async def test_play_next_inserts_the_matched_song_after_the_current_one(connected):
    c = client(
        track("Playing"),
        found=[
            NS(
                **{
                    **vars(track("Teardrop", "ytmusic", "t1")),
                    "artists": [NS(name="Massive Attack")],
                }
            ),
            NS(
                **{
                    **vars(track("Teardrop", "ytmusic", "t2")),
                    "artists": [NS(name="Someone Else")],
                }
            ),
        ],
    )
    connected(c)
    assert await v.play_next("echo", "Teardrop by Massive Attack") == "Teardrop"
    c.player_queues.play_media.assert_awaited_once_with(
        "echo-queue", "ytmusic://track/t1", option=QueueOption.NEXT
    )


@run_async
async def test_play_next_needs_something_playing(connected):
    c = client(found=[track("Teardrop")])
    connected(c)
    with pytest.raises(ValueError, match="Nothing is playing"):
        await v.play_next("echo", "Teardrop")
    c.player_queues.play_media.assert_not_awaited()


def test_play_next_answers_with_the_song_and_keeps_pending_starts():
    hi = handler_input("PlayNextIntent", "Teardrop")
    with (
        patch.object(v.device_mapping, "get_player_for_device", return_value="echo"),
        patch.object(v, "play_next", AsyncMock(return_value="Teardrop")),
        patch.object(v.playback_requests, "begin") as begin,
        patch.object(v.threading, "Thread") as thread,
    ):
        assert v.VoiceMusicHandler().can_handle(hi)
        v.VoiceMusicHandler().handle(hi)
    hi.response_builder.speak.assert_called_once_with("Teardrop will play next.")
    begin.assert_not_called()
    thread.assert_not_called()


@pytest.mark.parametrize(
    "intent, args",
    [
        ("AddFavoriteIntent", ("favorites", True)),
        ("RemoveFavoriteIntent", ("favorites", False)),
        ("AddToLibraryIntent", ("library", True)),
        ("RemoveFromLibraryIntent", ("library", False)),
    ],
)
def test_song_changes_answer_first_and_finish_in_the_background(intent, args):
    hi = handler_input(intent)
    change = AsyncMock()
    with (
        patch.object(v.device_mapping, "get_player_for_device", return_value="echo"),
        patch.object(
            v, "plan_song_change", AsyncMock(return_value=("Adding A.", change))
        ) as plan,
        patch.object(v.playback_requests, "begin") as begin,
        patch.object(v.threading, "Thread") as thread,
    ):
        assert v.VoiceMusicHandler().can_handle(hi)
        v.VoiceMusicHandler().handle(hi)
        # The slow Music Assistant call has not run when Alexa gets the answer.
        change.assert_not_awaited()
        thread.call_args.kwargs["target"]()
    plan.assert_awaited_once_with("echo", *args)
    hi.response_builder.speak.assert_called_once_with("Adding A.")
    change.assert_awaited_once()
    begin.assert_not_called()


def test_song_already_in_that_state_starts_no_background_work():
    hi = handler_input("AddToLibraryIntent")
    with (
        patch.object(v.device_mapping, "get_player_for_device", return_value="echo"),
        patch.object(
            v,
            "plan_song_change",
            AsyncMock(return_value=("A is already in your library.", None)),
        ),
        patch.object(v.threading, "Thread") as thread,
    ):
        v.VoiceMusicHandler().handle(hi)
    hi.response_builder.speak.assert_called_once_with("A is already in your library.")
    thread.assert_not_called()


def test_favorites_start_in_the_background():
    hi = handler_input("PlayFavoritesIntent")
    selection = {
        "queue_id": "q",
        "media": ["a"],
        "radio": False,
        "announcement": "Playing your favorites.",
    }
    with (
        patch.object(v.device_mapping, "get_player_for_device", return_value="echo"),
        patch.object(v, "prepare_music", AsyncMock(return_value=selection)) as prep,
        patch.object(v.threading, "Thread") as thread,
    ):
        v.VoiceMusicHandler().handle(hi)
    prep.assert_awaited_once_with("echo", "", "favorites", recommended=False)
    thread.return_value.start.assert_called_once()
    hi.response_builder.speak.assert_called_once_with("Playing your favorites.")


def test_model_has_the_new_phrases():
    model = json.loads(
        (Path(__file__).resolve().parents[1] / "app/models/en-US.json").read_text()
    )
    intents = {
        i["name"]: i["samples"]
        for i in model["interactionModel"]["languageModel"]["intents"]
    }
    assert "play {query} next" in intents["PlayNextIntent"]
    assert "add this song to my favorites" in intents["AddFavoriteIntent"]
    assert "remove this song from my favorites" in intents["RemoveFavoriteIntent"]
    assert "add this song to my library" in intents["AddToLibraryIntent"]
    assert "remove this song from my library" in intents["RemoveFromLibraryIntent"]
    # "like" and "unlike" sound too alike for Alexa to tell apart.
    samples = [sample for phrases in intents.values() for sample in phrases]
    assert "like this song" not in samples and "unlike this song" not in samples
    assert "play my favorites" in intents["PlayFavoritesIntent"]


@run_async
async def test_remove_right_after_add_wins_before_the_add_has_run(connected):
    c = client(track("Teardrop", "ytmusic", "abc"), library=None)
    connected(c)
    _, add = await v.plan_song_change("echo", "favorites", True)
    message, remove = await v.plan_song_change("echo", "favorites", False)
    assert message == "Removing Teardrop from your favorites."
    await add()
    await remove()
    c.music.add_item_to_favorites.assert_not_awaited()
    c.music.remove_item_from_favorites.assert_not_awaited()


@run_async
async def test_change_rechecks_the_song_when_it_runs(connected):
    c = client(track("Teardrop", "ytmusic", "abc"), library=None)
    connected(c)
    _, add = await v.plan_song_change("echo", "favorites", True)
    # Hearted elsewhere meanwhile: nothing left to do.
    c.music.get_library_item_by_prov_id.return_value = track(
        "Teardrop", item_id="12", favorite=True
    )
    await add()
    c.music.add_item_to_favorites.assert_not_awaited()


def test_redelivered_play_next_does_not_queue_the_song_twice():
    hi = handler_input("PlayNextIntent", "Teardrop")
    hi.request_envelope.request.request_id = "amzn1.echo-api.request.1"
    play_next = AsyncMock(return_value="Teardrop")
    with (
        patch.object(v.device_mapping, "get_player_for_device", return_value="echo"),
        patch.object(v, "play_next", play_next),
    ):
        v.VoiceMusicHandler().handle(hi)
        v.VoiceMusicHandler().handle(hi)
    play_next.assert_awaited_once()
    assert hi.response_builder.speak.call_count == 2
