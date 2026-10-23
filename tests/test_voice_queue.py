import json
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from skill import voice_music as v


def manager_for(client):
    manager = AsyncMock()
    manager.__aenter__.return_value = client
    return manager


def track(name, artist="Artist", album=None, index=0):
    return NS(
        queue_item_id=name,
        name=name,
        media_item=NS(name=name, artists=[NS(name=artist)], album=album),
        index=index,
    )


class VoiceQueueTests(unittest.IsolatedAsyncioTestCase):
    async def run_with(self, client, callback):
        with (
            patch.object(v, "MusicAssistantClient", return_value=manager_for(client)),
            patch.object(v, "get_env_secret", return_value="configured"),
        ):
            return await callback()

    async def test_current_and_next_include_artist_without_modifying_queue(self):
        q = NS(
            queue_id="echo",
            current_index=1,
            items=3,
            current_item=track("Now"),
            next_item=track("Next"),
        )
        client = NS(player_queues=NS(get_active_queue=AsyncMock(return_value=q)))
        self.assertEqual(
            await self.run_with(client, lambda: v.queue_information("echo", "current")),
            "This is Now by Artist.",
        )
        self.assertEqual(
            await self.run_with(client, lambda: v.queue_information("echo", "next")),
            "Next is Next by Artist.",
        )

    async def test_previous_uses_queue_index(self):
        q = NS(
            queue_id="echo",
            current_index=2,
            items=4,
            current_item=track("Now"),
            next_item=None,
        )
        client = NS(
            player_queues=NS(
                get_active_queue=AsyncMock(return_value=q),
                get_queue_items=AsyncMock(return_value=[track("Before")]),
            )
        )
        self.assertEqual(
            await self.run_with(
                client, lambda: v.queue_information("echo", "previous")
            ),
            "Before this was Before by Artist.",
        )
        client.player_queues.get_queue_items.assert_awaited_once_with(
            "echo", limit=1, offset=1
        )

    async def test_no_previous_or_next_at_queue_edges(self):
        q = NS(
            queue_id="echo",
            current_index=0,
            items=1,
            current_item=track("Only"),
            next_item=None,
        )
        client = NS(player_queues=NS(get_active_queue=AsyncMock(return_value=q)))
        for which in ("previous", "next"):
            self.assertIn(
                "There is no",
                await self.run_with(
                    client, lambda which=which: v.queue_information("echo", which)
                ),
            )

    async def test_shuffle_preserves_current_buffer_and_metadata(self):
        album = NS(uri="provider://album/1", media_type=v.MediaType.ALBUM)
        items = [track(str(i), album=album, index=i) for i in range(5)]
        q = NS(
            queue_id="echo",
            current_index=0,
            index_in_buffer=1,
            items=5,
            current_item=items[0],
            sources=[album],
            is_dynamic=False,
            shuffle_enabled=False,
        )
        stored = items[:]

        async def move(queue_id, item_id, pos_shift):
            old = next(i for i, x in enumerate(stored) if x.queue_item_id == item_id)
            self.assertGreater(old, 1)
            self.assertGreater(old + pos_shift, 1)
            stored.insert(old + pos_shift, stored.pop(old))

        pq = NS(
            get_active_queue=AsyncMock(return_value=q),
            get_queue_items=AsyncMock(return_value=items[2:]),
            move_item=AsyncMock(side_effect=move),
        )
        with patch.object(v.random, "shuffle", side_effect=lambda xs: xs.reverse()):
            await self.run_with(NS(player_queues=pq), lambda: v.shuffle_album("echo"))
        self.assertEqual([i.name for i in stored], ["0", "1", "4", "3", "2"])
        self.assertIs(stored[0], items[0])
        self.assertFalse(q.shuffle_enabled)
        self.assertEqual(q.sources, [album])

    async def test_shuffle_refuses_non_album_and_dynamic_queues(self):
        album = NS(uri="library://album/1", name="Album", media_type=v.MediaType.ALBUM)
        non_album = NS(
            queue_id="echo",
            current_index=0,
            current_item=track("Now"),
            is_dynamic=False,
        )
        # A valid album queue except that it is dynamic, so only that guard rejects it.
        dynamic_album = NS(
            queue_id="echo",
            current_index=0,
            current_item=track("Now", album=album),
            is_dynamic=True,
            sources=[album],
            shuffle_enabled=False,
        )
        for q in (non_album, dynamic_album):
            pq = NS(get_active_queue=AsyncMock(return_value=q), move_item=AsyncMock())
            with self.assertRaisesRegex(ValueError, "start an album"):
                await self.run_with(
                    NS(player_queues=pq), lambda: v.shuffle_album("echo")
                )
            pq.move_item.assert_not_awaited()

    async def test_explicit_artist_chooses_correct_song_or_album(self):
        for kind in ("track", "album"):
            wrong = NS(name="One", artists=[NS(name="Wrong")])
            right = NS(name="One", artists=[NS(name="Metallica")])
            result = NS(tracks=[wrong, right], albums=[wrong, right])
            client = NS(music=NS(search=AsyncMock(return_value=result)))
            self.assertIs(await v.find_music(client, "One by Metallica", kind), right)

    async def test_artist_mismatch_never_plays_wrong_artist(self):
        client = NS(
            music=NS(
                search=AsyncMock(
                    return_value=NS(tracks=[NS(name="One", artists=[NS(name="Wrong")])])
                )
            )
        )
        with self.assertRaisesRegex(ValueError, "by that artist"):
            await v.find_music(client, "One by Metallica", "track")

    def test_title_without_artist_and_titles_containing_by(self):
        self.assertEqual(v.title_and_artist("By the Way"), ("By the Way", None))
        self.assertEqual(
            v.title_and_artist("By the Way by Red Hot Chili Peppers"),
            ("By the Way", "Red Hot Chili Peppers"),
        )

    def test_no_ambiguous_sample_utterances(self):
        model = json.loads(
            (Path(__file__).resolve().parents[1] / "app/models/en-US.json").read_text()
        )
        seen = {}
        for intent in model["interactionModel"]["languageModel"]["intents"]:
            for sample in intent.get("samples", []):
                self.assertNotIn(
                    sample, seen, (sample, intent["name"], seen.get(sample))
                )
                seen[sample] = intent["name"]

    async def test_shuffle_stops_before_mutation_if_playback_advances(self):
        album = NS(uri="provider://album/1", media_type=v.MediaType.ALBUM)
        items = [track(str(i), album=album) for i in range(4)]
        q = NS(
            queue_id="echo",
            current_index=0,
            index_in_buffer=0,
            items=4,
            current_item=items[0],
            sources=[album],
            is_dynamic=False,
            shuffle_enabled=False,
        )
        advanced = NS(queue_id="echo", current_item=items[1], index_in_buffer=1)
        pq = NS(
            get_active_queue=AsyncMock(side_effect=[q, advanced]),
            get_queue_items=AsyncMock(return_value=items[1:]),
            move_item=AsyncMock(),
        )
        with (
            patch.object(v.random, "shuffle", side_effect=lambda xs: xs.reverse()),
            self.assertRaisesRegex(ValueError, "Playback advanced"),
        ):
            await self.run_with(NS(player_queues=pq), lambda: v.shuffle_album("echo"))
        pq.move_item.assert_not_awaited()

    async def test_failed_shuffle_puts_the_order_back(self):
        album = NS(uri="provider://album/1", media_type=v.MediaType.ALBUM)
        items = [track(str(i), album=album, index=i) for i in range(5)]
        q = NS(
            queue_id="echo",
            current_index=0,
            index_in_buffer=0,
            items=5,
            current_item=items[0],
            sources=[album],
            is_dynamic=False,
            shuffle_enabled=False,
        )
        stored = items[:]
        moves = []

        async def move(queue_id, item_id, pos_shift):
            moves.append(item_id)
            if len(moves) == 2:
                raise v.MusicAssistantError("lost")
            old = next(i for i, x in enumerate(stored) if x.queue_item_id == item_id)
            stored.insert(old + pos_shift, stored.pop(old))

        pq = NS(
            get_active_queue=AsyncMock(return_value=q),
            get_queue_items=AsyncMock(return_value=items[1:]),
            move_item=AsyncMock(side_effect=move),
        )
        with (
            patch.object(v.random, "shuffle", side_effect=lambda xs: xs.reverse()),
            self.assertRaises(v.MusicAssistantError),
        ):
            await self.run_with(NS(player_queues=pq), lambda: v.shuffle_album("echo"))
        self.assertEqual([i.name for i in stored], ["0", "1", "2", "3", "4"])

    async def test_title_containing_by_is_not_mistaken_for_artist(self):
        song = NS(name="Stand by Me", artists=[NS(name="Ben E King")])
        client = NS(music=NS(search=AsyncMock(return_value=NS(tracks=[song]))))
        self.assertIs(await v.find_music(client, "Stand by Me", "track"), song)

    async def test_playlist_shuffle_preserves_current_with_mixed_albums(self):
        source = NS(uri="provider://playlist/1", media_type=v.MediaType.PLAYLIST)
        items = [
            track(str(i), album=NS(uri="provider://album/" + str(i))) for i in range(4)
        ]
        q = NS(
            queue_id="echo",
            current_index=0,
            index_in_buffer=0,
            items=4,
            current_item=items[0],
            sources=[source],
            is_dynamic=False,
            shuffle_enabled=False,
        )
        stored = items[:]

        async def move(queue_id, item_id, pos_shift):
            old = next(i for i, x in enumerate(stored) if x.queue_item_id == item_id)
            self.assertGreater(old + pos_shift, 0)
            stored.insert(old + pos_shift, stored.pop(old))

        pq = NS(
            get_active_queue=AsyncMock(return_value=q),
            get_queue_items=AsyncMock(return_value=items[1:]),
            move_item=AsyncMock(side_effect=move),
        )
        with patch.object(v.random, "shuffle", side_effect=lambda xs: xs.reverse()):
            await self.run_with(
                NS(player_queues=pq), lambda: v.shuffle_playlist("echo")
            )
        self.assertEqual([i.name for i in stored], ["0", "3", "2", "1"])
        self.assertFalse(q.shuffle_enabled)
        self.assertEqual(q.sources, [source])
