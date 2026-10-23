import asyncio
import json
from functools import wraps
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType, PlayerState
from skill import voice_music as v


def run_async(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


def station(name, item_id=None, provider="library", available=True, mappings=()):
    item_id = item_id or name
    return NS(
        name=name,
        item_id=item_id,
        provider=provider,
        uri=f"{provider}://radio/{item_id}",
        media_type=MediaType.RADIO,
        available=available,
        provider_mappings=mappings,
    )


def queue(media=None, state=PlayerState.PLAYING, metadata=None):
    return NS(
        queue_id="echo-queue",
        state=state,
        current_index=0,
        current_item=NS(
            media_item=media,
            queue_item_id="current",
            streamdetails=NS(stream_metadata=metadata),
        ),
    )


def client_for(stations, q=None, history=()):
    return NS(
        music=NS(
            get_library_radios=AsyncMock(return_value=stations),
            recently_played=AsyncMock(return_value=history),
        ),
        player_queues=NS(
            get_active_queue=AsyncMock(return_value=q or queue()), stop=AsyncMock()
        ),
        send_command=AsyncMock(),
    )


@pytest.fixture
def connected(monkeypatch):
    from contextlib import asynccontextmanager

    def use(client):
        @asynccontextmanager
        async def connection():
            yield client

        monkeypatch.setattr(v, "connection", connection)

    return use


@run_async
async def test_resume_uses_per_echo_radio_history_not_music_or_global_history(
    connected,
):
    a, b = station("Alpha"), station("Bravo")
    client = client_for([a, b], queue(NS(media_type=MediaType.TRACK)), [b])
    connected(client)
    selected, queue_id, expected = await v.prepare_radio("echo", "resume")
    assert selected is b
    assert queue_id == "echo-queue"
    assert expected is None
    client.music.recently_played.assert_awaited_once_with(
        limit=50,
        media_types=[MediaType.RADIO],
        queue_id="echo-queue",
        fully_played_only=False,
    )


@run_async
async def test_current_station_takes_precedence_over_history(connected):
    a, b = station("Alpha"), station("Bravo")
    client = client_for([a, b], queue(b, PlayerState.PAUSED), [a])
    connected(client)
    assert (await v.prepare_radio("echo", "resume"))[0] is b
    client.music.recently_played.assert_not_awaited()


@run_async
async def test_history_provider_reference_matches_library_station(connected):
    mapping = NS(
        provider_instance="radiobrowser-123",
        provider_domain="radiobrowser",
        item_id="uuid",
    )
    saved = station("Station", "42", mappings=[mapping])
    client = client_for(
        [saved], history=[station("Old name", "uuid", "radiobrowser-123")]
    )
    connected(client)
    assert (await v.prepare_radio("echo", "resume"))[0] is saved


@run_async
async def test_unknown_history_uses_random_available_saved_station(connected):
    a, b = station("Alpha"), station("Bravo")
    client = client_for(
        [station("Offline", available=False), a, b], history=[station("Removed")]
    )
    connected(client)
    with patch.object(v.random, "choice", return_value=b) as choose:
        assert (await v.prepare_radio("echo", "resume"))[0] is b
    choose.assert_called_once_with([a, b])


@run_async
async def test_pagination_and_alphabetical_wrap(connected):
    a, b = station("alpha"), station("Bravo")
    client = client_for([b, a], queue(a))
    connected(client)
    assert (await v.prepare_radio("echo", "previous"))[0] is b
    client.player_queues.get_active_queue.return_value = queue(b)
    assert (await v.prepare_radio("echo", "next"))[0] is a
    first = [station(f"Z{i}") for i in range(500)]
    client.music.get_library_radios.side_effect = [first, [a]]
    assert len(await v.saved_stations(client)) == 501
    assert client.music.get_library_radios.call_args.kwargs["offset"] == 500


@run_async
@pytest.mark.parametrize("action", ["next", "previous"])
@pytest.mark.parametrize(
    "q",
    [
        queue(NS(media_type=MediaType.TRACK)),
        queue(),
        queue(station("A"), PlayerState.IDLE),
        queue(station("A"), PlayerState.PAUSED),
    ],
)
async def test_navigation_never_changes_music_idle_or_paused_playback(
    connected, action, q
):
    client = client_for([station("A")], q)
    connected(client)
    with pytest.raises(ValueError, match="playback is unchanged"):
        await v.prepare_radio("echo", action)
    client.send_command.assert_not_awaited()
    client.music.get_library_radios.assert_not_awaited()
    client.player_queues.stop.assert_not_awaited()


@run_async
async def test_unsaved_current_station_does_not_guess_navigation(connected):
    client = client_for([station("A")], queue(station("Other")))
    connected(client)
    with pytest.raises(ValueError, match="not in your saved"):
        await v.prepare_radio("echo", "next")
    client.send_command.assert_not_awaited()


def test_name_matching_requires_unambiguous_saved_station():
    a, b = station("BBC Radio 1"), station("BBC Radio 2")
    assert v.named_station([a, b], "bbc radio 1") is a
    assert v.named_station([a, b], "radio 2") is b
    with pytest.raises(ValueError, match="multiple stations"):
        v.named_station([a, b], "BBC")
    with pytest.raises(ValueError, match="could not find"):
        v.named_station([a, b], "Other")
    with pytest.raises(ValueError, match="station name"):
        v.named_station([a, b], "")
    with pytest.raises(ValueError, match="multiple stations"):
        v.named_station([a, station(a.name, "duplicate")], a.name)


@run_async
async def test_empty_library_explains_what_to_do(connected):
    client = client_for([])
    connected(client)
    with pytest.raises(ValueError, match="add some radio stations"):
        await v.prepare_radio("echo", "resume")


@run_async
async def test_station_playback_is_direct_radio_not_generated_mix(connected):
    a = station("A")
    client = client_for([a])
    connected(client)
    await v.play_station("echo", a, "echo-queue")
    client.send_command.assert_awaited_once_with(
        "player_queues/play_media",
        queue_id="echo-queue",
        media=a.uri,
        option=v.QueueOption.REPLACE,
        radio_mode=False,
        shuffle=False,
    )


@run_async
async def test_navigation_cancels_if_music_starts_during_preparation(connected):
    a = station("A")
    client = client_for([a], queue(NS(media_type=MediaType.TRACK)))
    connected(client)
    await v.play_station("echo", a, "echo-queue", "current")
    client.send_command.assert_not_awaited()


@run_async
async def test_current_information_includes_optional_live_song_metadata(connected):
    q = queue(station("Station"), metadata=NS(title="Song", artist="Artist"))
    client = client_for([], q)
    connected(client)
    assert (
        await v.queue_information("echo", "current")
        == "You are listening to Station. This is Song by Artist."
    )
    q.current_item.streamdetails = None
    assert (
        await v.queue_information("echo", "current") == "You are listening to Station."
    )
    q.current_item.streamdetails = NS(stream_metadata=NS(title="Station", artist=None))
    assert v.describe_station(q, True) == "You are listening to Station."
    q.state = PlayerState.IDLE
    assert v.describe_station(q) == "No radio station is playing."
    q.current_item.media_item = NS(media_type=MediaType.TRACK)
    assert v.describe_station(q) == "No radio station is playing."


def handler_input(intent, query=""):
    response = MagicMock()
    response.speak.return_value = response
    response.set_should_end_session.return_value = response
    return NS(
        response_builder=response,
        request_envelope=NS(
            request=NS(intent=NS(name=intent, slots={"query": NS(value=query)})),
            context=NS(system=NS(device=NS(device_id="device"))),
        ),
    )


def test_ambiguity_is_spoken_and_does_not_launch_background_thread():
    hi = handler_input("StartStationIntent", "BBC")
    with (
        patch.object(v.device_mapping, "get_player_for_device", return_value="echo"),
        patch.object(
            v, "prepare_radio", side_effect=ValueError("Which station? BBC 1 & BBC 2.")
        ),
        patch.object(v.threading, "Thread") as thread,
    ):
        assert v.VoiceMusicHandler().can_handle(hi)
        v.VoiceMusicHandler().handle(hi)
    hi.response_builder.speak.assert_called_once_with(
        "Which station? BBC 1 &amp; BBC 2."
    )
    thread.assert_not_called()


def test_named_radio_acknowledges_escaped_station_and_starts_worker():
    hi = handler_input("StartStationIntent", "Rock")
    with (
        patch.object(v.device_mapping, "get_player_for_device", return_value="echo"),
        patch.object(
            v, "prepare_radio", return_value=(station("Rock & Roll"), "queue", None)
        ),
        patch.object(v.threading, "Thread") as thread,
    ):
        v.VoiceMusicHandler().handle(hi)
    hi.response_builder.speak.assert_called_once_with("Starting Rock &amp; Roll.")
    thread.return_value.start.assert_called_once()


def test_model_reserves_requested_radio_phrases_without_changing_song_next():
    model = json.loads(
        (Path(__file__).resolve().parents[1] / "app/models/en-US.json").read_text()
    )
    intents = {
        i["name"]: i for i in model["interactionModel"]["languageModel"]["intents"]
    }
    for intent, phrase in [
        ("StartRadioIntent", "start radio"),
        ("StartStationIntent", "start {query}"),
        ("NextStationIntent", "change to next station"),
        ("PreviousStationIntent", "change to previous station"),
    ]:
        assert phrase in intents[intent]["samples"]
    assert "AMAZON.NextIntent" in intents
    assert "AMAZON.PreviousIntent" in intents


def test_station_question_is_covered_by_whats_playing_only():
    model = json.loads(
        (Path(__file__).resolve().parents[1] / "app/models/en-US.json").read_text()
    )
    names = {i["name"] for i in model["interactionModel"]["languageModel"]["intents"]}
    assert "WhatsStationIntent" not in names
    assert "WhatsPlayingIntent" in names


@pytest.mark.parametrize(
    "intent",
    [
        "StartRadioIntent",
        "StartStationIntent",
        "NextStationIntent",
        "PreviousStationIntent",
    ],
)
def test_station_playback_waits_for_announcement_in_background(intent):
    hi = handler_input(intent, "Rock")
    events = []

    async def sleep(delay):
        events.append(("wait", delay))

    async def play(*args):
        events.append(("play", args))

    with (
        patch.object(v.device_mapping, "get_player_for_device", return_value="echo"),
        patch.object(
            v, "prepare_radio", return_value=(station("Rock"), "queue", "current")
        ),
        patch.object(v.asyncio, "sleep", side_effect=sleep),
        patch.object(v, "play_station", side_effect=play),
        patch.object(v.threading, "Thread") as thread,
    ):
        v.VoiceMusicHandler().handle(hi)
        assert events == []
        hi.response_builder.speak.assert_called_once_with("Starting Rock.")
        thread.call_args.kwargs["target"]()
    assert events[0] == ("wait", v.speaking_time("Starting Rock."))
    assert events[1][0] == "play"
    assert events[1][1][0] == "echo"
    assert events[1][1][1].name == "Rock"
    assert events[1][1][2:] == ("queue", "current")


@run_async
async def test_radio_preparation_never_calls_ma_stop(connected):
    a, b = station("A"), station("B")
    client = client_for([a, b], queue(a))
    connected(client)
    events = []
    client.music.get_library_radios.side_effect = lambda **kw: (
        events.append("lookup") or [a, b]
    )
    client.player_queues.stop.side_effect = lambda *args: events.append("stop")
    selected, queue_id, expected = await v.prepare_radio("echo", "next")
    assert events == ["lookup"]
    client.player_queues.stop.assert_not_awaited()
    assert selected is b
    client.send_command.assert_not_awaited()
    await v.play_station("echo", selected, queue_id, expected)
    client.send_command.assert_awaited_once()


@run_async
async def test_invalid_station_does_not_stop_playback(connected):
    a = station("A")
    client = client_for([a], queue(a))
    connected(client)
    with pytest.raises(ValueError, match="could not find"):
        await v.prepare_radio("echo", "named", "Missing")
    client.player_queues.stop.assert_not_awaited()


@run_async
async def test_station_navigation_does_not_stop_music_started_during_lookup(connected):
    a, b = station("A"), station("B")
    client = client_for([a, b], queue(a))
    client.player_queues.get_active_queue.side_effect = [
        queue(a),
        queue(NS(media_type=MediaType.TRACK)),
    ]
    connected(client)
    with pytest.raises(ValueError, match="Playback changed"):
        await v.prepare_radio("echo", "next")
    client.player_queues.stop.assert_not_awaited()


@run_async
async def test_stopped_radio_without_current_pointer_cancels_station_navigation(
    connected,
):
    a = station("A")
    q = queue(state=PlayerState.IDLE)
    q.current_item = None
    client = client_for([a], q)
    connected(client)
    await v.play_station("echo", a, "echo-queue", "old")
    client.send_command.assert_not_awaited()


@pytest.mark.parametrize(
    "intent",
    [
        "PlaySongIntent",
        "PlayAlbumIntent",
        "PlayArtistIntent",
        "PlayPlaylistIntent",
        "PlayRecommendedIntent",
    ],
)
def test_music_resolves_before_starting_worker(intent):
    hi = handler_input(intent, "Music")
    events = []

    async def prepare(*args, **kwargs):
        events.append("resolve")
        return {"media": "chosen", "announcement": "Playing Music."}

    with (
        patch.object(v.device_mapping, "get_player_for_device", return_value="echo"),
        patch.object(v, "prepare_music", side_effect=prepare),
        patch.object(v.threading, "Thread") as thread,
    ):
        thread.return_value.start.side_effect = lambda: events.append("worker")
        v.VoiceMusicHandler().handle(hi)
    assert events == ["resolve", "worker"]


def test_similar_songs_preserves_current_track_without_music_preparation():
    hi = handler_input("PlaySimilarIntent")
    with (
        patch.object(v.device_mapping, "get_player_for_device", return_value="echo"),
        patch.object(v, "prepare_music") as prepare,
        patch.object(v.threading, "Thread"),
    ):
        v.VoiceMusicHandler().handle(hi)
    prepare.assert_not_called()


@pytest.mark.parametrize(
    "error, spoken",
    [
        (ValueError("I could not find that track."), "could not find"),
        (ValueError("Please use its full name."), "full name"),
        (TimeoutError(), "took too long"),
        (RuntimeError("offline"), "could not prepare"),
    ],
)
def test_selection_failure_is_spoken_without_starting_worker(error, spoken):
    hi = handler_input("PlaySongIntent", "Song")
    with (
        patch.object(v.device_mapping, "get_player_for_device", return_value="echo"),
        patch.object(v, "prepare_music", side_effect=error),
        patch.object(v.threading, "Thread") as thread,
    ):
        v.VoiceMusicHandler().handle(hi)
    thread.assert_not_called()
    assert spoken in hi.response_builder.speak.call_args.args[0]


@run_async
async def test_track_resolution_never_calls_ma_stop_or_repeats_search(connected):
    client = client_for([])
    connected(client)
    events = []

    async def search(*args, **kwargs):
        events.append("search")
        return NS(tracks=[NS(name="Song", uri="provider://track/song", available=True)])

    client.music.search = AsyncMock(side_effect=search)
    client.player_queues.stop.side_effect = lambda *args: events.append("stop")
    client.send_command.side_effect = lambda *args, **kw: events.append("play")
    client.player_queues.play_media = AsyncMock(
        side_effect=lambda *args, **kw: events.append("mix")
    )
    selection = await v.prepare_music("echo", "Song")
    assert events == ["search"]
    client.player_queues.stop.assert_not_awaited()
    await v.play_music_selection(selection)
    assert events == ["search", "play", "mix"]
    client.music.search.assert_awaited_once()
    assert selection["radio"]


@run_async
@pytest.mark.parametrize("kind", ["track", "album", "artist", "playlist"])
async def test_failed_search_leaves_current_playback_untouched(connected, kind):
    client = client_for([])
    client.music.search = AsyncMock(return_value=NS(tracks=[], albums=[], artists=[]))
    client.music.get_library_playlists = AsyncMock(return_value=[])
    connected(client)
    with pytest.raises(ValueError):
        await v.prepare_music("echo", "Missing", kind=kind)
    client.player_queues.stop.assert_not_awaited()
    client.send_command.assert_not_awaited()


@run_async
async def test_artist_without_available_tracks_does_not_stop(connected):
    client = client_for([])
    client.music.search = AsyncMock(
        return_value=NS(
            artists=[
                NS(
                    name="Artist",
                    uri="provider://artist/1",
                    item_id="1",
                    provider="provider",
                )
            ]
        )
    )
    client.music.get_artist_tracks = AsyncMock(
        return_value=[NS(available=False, uri="offline")]
    )
    connected(client)
    with pytest.raises(ValueError, match="tracks for that artist"):
        await v.prepare_music("echo", "Artist", kind="artist")
    client.player_queues.stop.assert_not_awaited()


@run_async
async def test_empty_recommendations_leave_current_playback_untouched(connected):
    client = client_for([])
    client.music.recommendations = AsyncMock(return_value=[])
    client.music.get_library_tracks = AsyncMock(return_value=[])
    connected(client)
    with pytest.raises(ValueError, match="no recommended songs"):
        await v.prepare_music("echo", recommended=True)
    client.player_queues.stop.assert_not_awaited()


@run_async
async def test_ambiguous_playlist_leaves_playback_untouched(connected):
    client = client_for([])
    client.music.get_library_playlists = AsyncMock(
        return_value=[NS(name="Morning One"), NS(name="Morning Two")]
    )
    connected(client)
    with pytest.raises(ValueError, match="one playlist"):
        await v.prepare_music("echo", "Morning", kind="playlist")
    client.player_queues.stop.assert_not_awaited()


@pytest.mark.parametrize(
    "intent",
    [
        "StartRadioIntent",
        "StartStationIntent",
        "NextStationIntent",
        "PreviousStationIntent",
        "PlaySongIntent",
        "PlayAlbumIntent",
        "PlayArtistIntent",
        "PlayPlaylistIntent",
        "PlayRecommendedIntent",
    ],
)
def test_success_announces_without_explicit_stop_directive(intent):
    from ask_sdk_core.response_helper import ResponseFactory
    from ask_sdk_core.serialize import DefaultSerializer

    hi = handler_input(intent, "Music")
    hi.response_builder = ResponseFactory()
    with (
        patch.object(v.device_mapping, "get_player_for_device", return_value="echo"),
        patch.object(v, "prepare_radio", return_value=(station("Jazz"), "queue", None)),
        patch.object(
            v,
            "prepare_music",
            return_value={"media": "chosen", "announcement": "Playing Song."},
        ),
        patch.object(v.threading, "Thread"),
    ):
        response = v.VoiceMusicHandler().handle(hi)
    payload = DefaultSerializer().serialize(response)
    assert not payload.get("directives")
    expected = (
        "Starting Jazz."
        if "Station" in intent or "Radio" in intent
        else "Playing Song."
    )
    assert expected in payload["outputSpeech"]["ssml"]
    assert payload["shouldEndSession"] is True


@pytest.mark.parametrize("intent", ["StartStationIntent", "PlaySongIntent"])
def test_failed_search_speaks_without_stop_directive(intent):
    from ask_sdk_core.response_helper import ResponseFactory
    from ask_sdk_core.serialize import DefaultSerializer

    hi = handler_input(intent, "Missing")
    hi.response_builder = ResponseFactory()
    with (
        patch.object(v.device_mapping, "get_player_for_device", return_value="echo"),
        patch.object(v, "prepare_radio", side_effect=ValueError("Not found.")),
        patch.object(v, "prepare_music", side_effect=ValueError("Not found.")),
        patch.object(v.threading, "Thread") as thread,
    ):
        response = v.VoiceMusicHandler().handle(hi)
    payload = DefaultSerializer().serialize(response)
    assert not payload.get("directives")
    assert "Not found." in payload["outputSpeech"]["ssml"]
    thread.assert_not_called()


def test_similar_request_never_stops_audio_in_response():
    hi = handler_input("PlaySimilarIntent")
    with (
        patch.object(v.device_mapping, "get_player_for_device", return_value="echo"),
        patch.object(v.threading, "Thread"),
    ):
        v.VoiceMusicHandler().handle(hi)
    hi.response_builder.add_directive.assert_not_called()


def test_music_worker_waits_for_original_response_before_ma_play_request():
    hi = handler_input("PlaySongIntent", "Song")
    events = []

    async def wait(delay):
        events.append(("wait", delay))

    async def play(selection):
        events.append(("play", selection))

    with (
        patch.object(v.device_mapping, "get_player_for_device", return_value="echo"),
        patch.object(
            v,
            "prepare_music",
            return_value={"media": "chosen", "announcement": "Playing Song."},
        ),
        patch.object(v.asyncio, "sleep", side_effect=wait),
        patch.object(v, "play_music_selection", side_effect=play),
        patch.object(v.threading, "Thread") as thread,
    ):
        v.VoiceMusicHandler().handle(hi)
        assert events == []
        thread.call_args.kwargs["target"]()
    assert events == [
        ("wait", v.speaking_time("Playing Song.")),
        ("play", {"media": "chosen", "announcement": "Playing Song."}),
    ]


@run_async
async def test_exact_song_starts_before_dynamic_mix_and_mix_never_replaces_it(
    connected,
):
    client = client_for([])
    connected(client)
    current = None
    events = []

    async def play_direct(command, **kwargs):
        nonlocal current
        assert kwargs["radio_mode"] is False
        assert kwargs["shuffle"] is False
        assert kwargs["option"] == v.QueueOption.REPLACE
        current = kwargs["media"]
        events.append(("current", current))

    async def dynamic(queue_id, seed, option, radio_mode):
        nonlocal current
        # MA's dynamic pool may put a different track first. Only replacing
        # the upcoming tail must preserve the specifically requested song.
        assert current == seed
        assert radio_mode
        if option == v.QueueOption.REPLACE:
            current = "provider://track/something-else"
        events.append(("mix", option))

    client.send_command.side_effect = play_direct
    client.player_queues.play_media = AsyncMock(side_effect=dynamic)
    await v.play_music_selection(
        {
            "queue_id": "echo-queue",
            "media": "provider://track/sweater-weather",
            "radio": True,
            "recommended": False,
        }
    )
    assert current == "provider://track/sweater-weather"
    assert events == [("current", current), ("mix", v.QueueOption.REPLACE_NEXT)]


@run_async
async def test_unavailable_mix_does_not_replace_or_stop_requested_song(connected):
    from music_assistant_models.errors import UnsupportedFeaturedException

    client = client_for([])
    connected(client)
    client.player_queues.play_media = AsyncMock(
        side_effect=UnsupportedFeaturedException("No similar tracks")
    )
    await v.play_music_selection(
        {
            "queue_id": "echo-queue",
            "media": "provider://track/song",
            "radio": True,
            "recommended": False,
        }
    )
    client.send_command.assert_awaited_once_with(
        "player_queues/play_media",
        queue_id="echo-queue",
        media="provider://track/song",
        option=v.QueueOption.REPLACE,
        radio_mode=False,
        shuffle=False,
    )
    client.player_queues.stop.assert_not_awaited()
