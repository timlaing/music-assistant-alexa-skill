import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from skill import voice_music


class SelectionTests(unittest.IsolatedAsyncioTestCase):
    def test_playlist_ambiguity(self):
        a, b = NS(name="Evening"), NS(name="Evening Jazz")
        self.assertIs(voice_music.choose_playlist([a, b], "evening"), a)
        with self.assertRaises(ValueError):
            voice_music.choose_playlist([a, b], "eve")

    async def check_play(self, playlist):
        item = NS(name="Evening" if playlist else "Teardrop", uri="ytmusic://track/123")
        client = NS(
            send_command=AsyncMock(),
            music=NS(
                search=AsyncMock(return_value=NS(tracks=[item])),
                get_library_playlists=AsyncMock(return_value=[item]),
            ),
            player_queues=NS(
                get_active_queue=AsyncMock(return_value=NS(queue_id="echo-queue")),
                stop=AsyncMock(),
                play_media=AsyncMock(),
            ),
        )
        manager = AsyncMock()
        manager.__aenter__.return_value = client
        with (
            patch.object(voice_music, "MusicAssistantClient", return_value=manager),
            patch.object(voice_music, "get_env_secret", return_value="configured"),
        ):
            await voice_music.select_and_play("echo", item.name, playlist)
        if playlist:
            args = client.send_command.call_args
            self.assertEqual(args.kwargs["media"], item.uri)
            self.assertFalse(args.kwargs["radio_mode"])
            self.assertFalse(args.kwargs["shuffle"])
            client.music.search.assert_not_called()
        else:
            args = client.player_queues.play_media.call_args
            self.assertEqual(args.args, ("echo-queue", item.uri))
            self.assertTrue(args.kwargs["radio_mode"])

    async def test_song_radio(self):
        await self.check_play(False)

    async def test_library_playlist(self):
        await self.check_play(True)


if __name__ == "__main__":
    unittest.main()
