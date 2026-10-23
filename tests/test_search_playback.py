from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.enums import MediaType, QueueOption
from skill import voice_music as v


def media(name, kind, artist="Artist", available=True, suffix="chosen"):
    return NS(
        name=name,
        uri=f"provider://{kind}/{suffix}",
        item_id=suffix,
        provider="provider",
        media_type=MediaType(kind),
        available=available,
        artists=[NS(name=artist)],
    )


@pytest.fixture
def playback_client(monkeypatch):
    client = NS(
        music=NS(
            search=AsyncMock(),
            get_library_playlists=AsyncMock(),
            get_artist_tracks=AsyncMock(),
        ),
        player_queues=NS(
            get_active_queue=AsyncMock(return_value=NS(queue_id="echo")),
            play_media=AsyncMock(),
            stop=AsyncMock(),
        ),
        send_command=AsyncMock(),
    )

    @asynccontextmanager
    async def connection():
        yield client

    monkeypatch.setattr(v, "connection", connection)
    return client


def run_async(function):
    import asyncio
    from functools import wraps

    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


@pytest.mark.parametrize(
    "kind,query,name,artist",
    [
        ("track", "Sweater Weather", "Sweater Weather", "The Neighbourhood"),
        (
            "track",
            "Sweater Weather by The Neighbourhood",
            "Sweater Weather",
            "The Neighbourhood",
        ),
        ("track", "Stand by Me", "Stand by Me", "Ben E. King"),
        ("track", "Stand by Me by Ben E. King", "Stand by Me", "Ben E. King"),
        ("album", "Hybrid Theory", "Hybrid Theory", "Linkin Park"),
        ("album", "Hybrid Theory by Linkin Park", "Hybrid Theory", "Linkin Park"),
        (
            "album",
            "By the Way by Red Hot Chili Peppers",
            "By the Way",
            "Red Hot Chili Peppers",
        ),
        ("artist", "Linkin Park", "Linkin Park", "Linkin Park"),
    ],
)
@run_async
async def test_resolved_request_reaches_exact_initial_playback(
    playback_client, kind, query, name, artist
):
    client = playback_client
    chosen = media(name, kind, artist)
    # Exact names must beat unrelated provider results. With an artist
    # qualifier, the exact-title cover must also lose to the requested artist.
    wrong = media(
        name if query != name and kind in ("track", "album") else "Unrelated",
        kind,
        "Wrong Artist",
        suffix="wrong",
    )
    client.music.search.return_value = NS(
        **{
            {"track": "tracks", "album": "albums", "artist": "artists"}[kind]: [
                wrong,
                chosen,
            ]
        }
    )
    artist_tracks = [
        media("Artist track 1", "track", artist, suffix="one"),
        media("Artist track 2", "track", artist, suffix="two"),
    ]
    client.music.get_artist_tracks.return_value = artist_tracks
    prepared = await v.prepare_music("echo", query, kind=kind)
    client.send_command.assert_not_awaited()
    client.player_queues.stop.assert_not_awaited()
    await v.play_music_selection(prepared)
    initial = client.send_command.call_args_list[0].kwargs
    assert initial["media"] == (
        [item.uri for item in artist_tracks] if kind == "artist" else chosen.uri
    )
    assert initial["option"] == QueueOption.REPLACE
    assert initial["radio_mode"] is False
    assert initial["shuffle"] is False
    if kind == "track":
        client.player_queues.play_media.assert_awaited_once_with(
            "echo", chosen.uri, option=QueueOption.REPLACE_NEXT, radio_mode=True
        )
    else:
        # Finite artist/album requests never route through mix generation.
        client.player_queues.play_media.assert_not_awaited()
        client.send_command.assert_awaited_once()


@run_async
async def test_exact_playlist_starts_selected_playlist_without_mix(playback_client):
    client = playback_client
    chosen = media("Favorites", "playlist")
    client.music.get_library_playlists.return_value = [
        media("Favorites 2", "playlist", suffix="wrong"),
        chosen,
    ]
    prepared = await v.prepare_music("echo", "Favorites", kind="playlist")
    await v.play_music_selection(prepared)
    client.send_command.assert_awaited_once_with(
        "player_queues/play_media",
        queue_id="echo",
        media=chosen.uri,
        option=QueueOption.REPLACE,
        radio_mode=False,
        shuffle=False,
    )
    client.player_queues.play_media.assert_not_awaited()


@run_async
async def test_unavailable_exact_song_is_not_substituted_for_wrong_artist(
    playback_client,
):
    client = playback_client
    client.music.search.return_value = NS(
        tracks=[
            media("Song", "track", "Requested", available=False),
            media("Song", "track", "Wrong"),
        ]
    )
    with pytest.raises(ValueError, match="by that artist"):
        await v.prepare_music("echo", "Song by Requested")
    client.send_command.assert_not_awaited()
    client.player_queues.play_media.assert_not_awaited()
    client.player_queues.stop.assert_not_awaited()


@run_async
async def test_recommendation_choice_is_the_opening_song(playback_client, monkeypatch):
    client = playback_client
    monkeypatch.setattr(
        v,
        "recommended_seed",
        AsyncMock(return_value=("provider://track/recommended", "Song by Artist")),
    )
    prepared = await v.prepare_music("echo", recommended=True)
    await v.play_music_selection(prepared)
    assert (
        client.send_command.call_args_list[0].kwargs["media"]
        == "provider://track/recommended"
    )
    assert client.send_command.call_args_list[0].kwargs["radio_mode"] is False
    client.player_queues.play_media.assert_awaited_once_with(
        "echo",
        "provider://track/recommended",
        option=QueueOption.REPLACE_NEXT,
        radio_mode=True,
    )
