import asyncio
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from music_assistant_models.errors import (
    ProviderUnavailableError,
    UnsupportedFeaturedException,
)
from skill import voice_music as v


class RecommendationTests(unittest.IsolatedAsyncioTestCase):
    async def test_recommendation_uses_any_provider_and_random_track(self):
        rows = [NS(provider="spotify--1", item_id="recommendations")]
        c = NS(
            music=NS(recommendations=AsyncMock(return_value=rows)),
            send_command=AsyncMock(
                return_value=[
                    {"media_type": "album", "uri": "album"},
                    {"media_type": "track", "uri": "one"},
                    {"media_type": "track", "uri": "two"},
                ]
            ),
        )
        with patch.object(v.random, "choice", side_effect=lambda xs: xs[-1]):
            self.assertEqual((await v.recommended_seed(c))[0], "two")
        self.assertEqual(c.send_command.call_args.kwargs["provider"], "spotify--1")

    async def test_failed_row_falls_back_to_favorites(self):
        c = NS(
            music=NS(
                recommendations=AsyncMock(
                    return_value=[NS(provider="broken", item_id="row")]
                ),
                get_library_tracks=AsyncMock(
                    return_value=[NS(uri="local://track/1", available=True)]
                ),
            ),
            send_command=AsyncMock(side_effect=ProviderUnavailableError("offline")),
        )
        self.assertEqual((await v.recommended_seed(c))[0], "local://track/1")
        self.assertTrue(c.music.get_library_tracks.call_args.kwargs["favorite"])

    async def test_empty_recommendations_and_library_fail(self):
        c = NS(
            music=NS(
                recommendations=AsyncMock(return_value=[]),
                get_library_tracks=AsyncMock(return_value=[]),
            )
        )
        with self.assertRaisesRegex(ValueError, "no recommended songs"):
            await v.recommended_seed(c)

    async def test_empty_provider_cache_still_requests_radio_on_requesting_player(self):
        c = NS(
            providers=[],
            send_command=AsyncMock(),
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
            patch.object(
                v,
                "recommended_seed",
                AsyncMock(return_value=("spotify://track/seed", None)),
            ),
        ):
            await v.play_recommended("Echo Dot 3")
        c.player_queues.play_media.assert_awaited_once_with(
            "dot3",
            "spotify://track/seed",
            option=v.QueueOption.REPLACE_NEXT,
            radio_mode=True,
        )

    async def test_actual_radio_failure_plays_normal_selection(self):
        c = NS(
            providers=[],
            send_command=AsyncMock(),
            player_queues=NS(
                get_active_queue=AsyncMock(return_value=NS(queue_id="dot3")),
                stop=AsyncMock(),
                play_media=AsyncMock(
                    side_effect=UnsupportedFeaturedException("No similar tracks")
                ),
            ),
        )
        manager = AsyncMock()
        manager.__aenter__.return_value = c
        with (
            patch.object(v, "MusicAssistantClient", return_value=manager),
            patch.object(v, "get_env_secret", return_value="configured"),
            patch.object(
                v,
                "recommended_seed",
                AsyncMock(return_value=("local://track/seed", None)),
            ),
            patch.object(
                v,
                "library_selection",
                AsyncMock(return_value=["local://track/seed", "local://track/other"]),
            ),
        ):
            await v.play_recommended("Echo Dot 3")
        self.assertEqual(
            c.send_command.call_args_list[0].kwargs["media"], "local://track/seed"
        )
        self.assertEqual(
            c.send_command.call_args.kwargs["media"], ["local://track/other"]
        )
        self.assertEqual(
            c.send_command.call_args.kwargs["option"], v.QueueOption.REPLACE_NEXT
        )
        self.assertFalse(c.send_command.call_args.kwargs["radio_mode"])
        c.player_queues.play_media.assert_awaited_once()


class RecommendationPoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_rows_combined_and_each_song_has_one_selection_slot(self):
        rows = [
            NS(provider="one", item_id="large"),
            NS(provider="two", item_id="small"),
            NS(provider="one", item_id="duplicate"),
            NS(provider="broken", item_id="offline"),
        ]

        def track(uri, **kwargs):
            return {"media_type": "track", "uri": uri, **kwargs}

        async def items(command, provider, item_id):
            if item_id == "offline":
                raise ProviderUnavailableError("offline")
            return {
                "large": [
                    track("a"),
                    track("b"),
                    track("c"),
                    {"media_type": "album", "uri": "album"},
                    track("unavailable", available=False),
                ],
                "small": [track("d")],
                "duplicate": [track("a"), track("b")],
            }[item_id]

        c = NS(
            music=NS(recommendations=AsyncMock(return_value=rows)),
            send_command=AsyncMock(side_effect=items),
        )
        with patch.object(
            v.random, "choice", side_effect=lambda pool: pool[-1]
        ) as choice:
            self.assertEqual((await v.recommended_seed(c))[0], "d")
        self.assertEqual(choice.call_args.args[0], ["a", "b", "c", "d"])
        self.assertEqual(choice.call_count, 1)
        self.assertEqual(c.send_command.await_count, 4)

    def test_library_and_provider_aliases_merge_transitively(self):
        items = [
            {"uri": "library://track/42"},
            {"uri": "ytmusic://track/abc"},
            {
                "uri": "spotify://track/xyz",
                "provider_mappings": [
                    {"provider_domain": "library", "item_id": "42"},
                    {"provider_domain": "ytmusic", "item_id": "abc"},
                ],
            },
            {"uri": "different"},
        ]
        self.assertEqual(len(v.unique_recommendations(items)), 2)

    def test_same_title_artist_version_merges_but_covers_and_versions_remain_separate(
        self,
    ):
        def song(uri, artist="Original", version=""):
            return {
                "uri": uri,
                "name": "Song",
                "artists": [{"name": artist}],
                "version": version,
            }

        items = [
            song("a"),
            song("b"),
            song("cover", "Cover Artist"),
            song("live", version="Live"),
        ]
        self.assertEqual(v.unique_recommendations(items), ["a", "cover", "live"])

    def test_recording_identifier_links_different_provider_metadata(self):
        self.assertEqual(
            len(
                v.unique_recommendations(
                    [
                        {"uri": "a", "external_ids": [["isrc", "recording"]]},
                        {"uri": "b", "external_ids": [["isrc", "RECORDING"]]},
                    ]
                )
            ),
            1,
        )

    async def test_short_lived_client_never_receives_concurrent_row_requests(self):
        in_flight = False
        rows = [NS(provider="provider", item_id=str(i)) for i in range(8)]

        async def items(command, provider, item_id):
            nonlocal in_flight
            if in_flight:
                raise RuntimeError("Concurrent call to receive() is not allowed")
            in_flight = True
            try:
                await asyncio.sleep(0)
                return [{"media_type": "track", "uri": item_id}]
            finally:
                in_flight = False

        c = NS(
            music=NS(recommendations=AsyncMock(return_value=rows)),
            send_command=AsyncMock(side_effect=items),
        )
        with patch.object(
            v.random, "choice", side_effect=lambda pool: pool[-1]
        ) as choice:
            self.assertEqual((await v.recommended_seed(c))[0], "7")
        self.assertEqual(choice.call_args.args[0], [str(i) for i in range(8)])
        self.assertEqual(c.send_command.await_count, 8)
