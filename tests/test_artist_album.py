import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from skill import voice_music as v


class ArtistAlbumTests(unittest.IsolatedAsyncioTestCase):
    async def check_selection(self, kind):
        item = NS(
            name="Linkin Park" if kind == "artist" else "Hybrid Theory",
            uri="ytmusic://" + kind + "/123",
            item_id="123",
            provider="ytmusic",
        )
        c = NS(
            send_command=AsyncMock(),
            music=NS(
                search=AsyncMock(return_value=NS(artists=[item], albums=[item])),
                get_artist_tracks=AsyncMock(
                    return_value=[
                        NS(uri="ytmusic://track/one"),
                        NS(uri="ytmusic://track/two"),
                    ]
                ),
            ),
            player_queues=NS(
                get_active_queue=AsyncMock(return_value=NS(queue_id="dot3")),
                stop=AsyncMock(),
                play_media=AsyncMock(),
            ),
        )
        manager = AsyncMock()
        manager.__aenter__.return_value = c
        with (
            patch.object(v, "MusicAssistantClient", return_value=manager),
            patch.object(v, "get_env_secret", return_value="configured"),
        ):
            await v.select_and_play("Echo Dot 3", item.name, kind=kind)
        args = c.send_command.call_args
        self.assertFalse(args.kwargs["radio_mode"])
        self.assertFalse(args.kwargs["shuffle"])
        self.assertEqual(
            args.kwargs["media"],
            ["ytmusic://track/one", "ytmusic://track/two"]
            if kind == "artist"
            else item.uri,
        )

    async def test_artist_queues_artist_tracks(self):
        await self.check_selection("artist")

    async def test_album_queues_album_without_mix(self):
        await self.check_selection("album")

    def test_audio_events_not_music_requests(self):
        self.assertFalse(
            v.VoiceMusicHandler().can_handle(NS(request_envelope=NS(request=NS())))
        )

    async def test_similar_replaces_future_with_current_track_radio(self):
        media = NS(uri="ytmusic://track/current", media_type=v.MediaType.TRACK)
        c = NS(
            player_queues=NS(
                get_active_queue=AsyncMock(
                    return_value=NS(queue_id="dot3", current_item=NS(media_item=media))
                ),
                stop=AsyncMock(),
                play_media=AsyncMock(),
            )
        )
        manager = AsyncMock()
        manager.__aenter__.return_value = c
        with (
            patch.object(v, "MusicAssistantClient", return_value=manager),
            patch.object(v, "get_env_secret", return_value="configured"),
        ):
            await v.play_similar("Echo Dot 3")
        c.player_queues.play_media.assert_awaited_once_with(
            "dot3", media.uri, option=v.QueueOption.REPLACE_NEXT, radio_mode=True
        )

    async def test_similar_without_track_does_not_change_queue(self):
        c = NS(
            player_queues=NS(
                get_active_queue=AsyncMock(return_value=NS(current_item=None)),
                stop=AsyncMock(),
                play_media=AsyncMock(),
            )
        )
        manager = AsyncMock()
        manager.__aenter__.return_value = c
        with (
            patch.object(v, "MusicAssistantClient", return_value=manager),
            patch.object(v, "get_env_secret", return_value="configured"),
            self.assertRaisesRegex(ValueError, "play a song first"),
        ):
            await v.play_similar("Echo Dot 3")
        c.player_queues.play_media.assert_not_awaited()
