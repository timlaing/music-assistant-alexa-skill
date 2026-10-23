import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import pytest
from skill import voice_music as v
from test_radio import handler_input, run_async


@pytest.fixture
def connected(monkeypatch):
    from contextlib import asynccontextmanager

    def use(client):
        @asynccontextmanager
        async def connection():
            yield client

        monkeypatch.setattr(v, "connection", connection)

    return use


def song(name="Teardrop", artist="Massive Attack"):
    return NS(name=name, media_item=NS(name=name, artists=[NS(name=artist)]))


def client(items=12, state="paused", current=None):
    queue = NS(queue_id="echo-queue", items=items, state=state, current_item=current)
    return NS(
        player_queues=NS(
            get_active_queue=AsyncMock(return_value=queue), resume=AsyncMock()
        )
    )


@run_async
async def test_a_stopped_or_paused_queue_can_be_resumed(connected):
    current = song()
    connected(client(state="idle", current=current))
    assert await v.prepare_resume("echo") == ("echo-queue", current)


@pytest.mark.parametrize(
    ("queue", "spoken"),
    [
        (client(items=0), "nothing to resume"),
        (client(state="playing", current=song()), "already playing"),
    ],
)
@run_async
async def test_empty_or_playing_queues_are_not_resumed(connected, queue, spoken):
    connected(queue)
    with pytest.raises(ValueError, match=spoken):
        await v.prepare_resume("echo")


@run_async
async def test_resume_continues_the_queue_in_music_assistant(connected):
    c = client()
    connected(c)
    await v.resume_queue("echo-queue")
    c.player_queues.resume.assert_awaited_once_with("echo-queue")


def test_resume_names_the_song_and_starts_in_the_background():
    hi = handler_input("ResumeMusicIntent")
    resume = AsyncMock()
    with (
        patch.object(v.device_mapping, "get_player_for_device", return_value="echo"),
        patch.object(
            v, "prepare_resume", AsyncMock(return_value=("echo-queue", song()))
        ),
        patch.object(v, "resume_queue", resume),
        patch.object(v.asyncio, "sleep", AsyncMock()),
        patch.object(v.threading, "Thread") as thread,
    ):
        assert v.VoiceMusicHandler().can_handle(hi)
        v.VoiceMusicHandler().handle(hi)
        resume.assert_not_awaited()
        thread.call_args.kwargs["target"]()
    hi.response_builder.speak.assert_called_once_with(
        "Resuming Teardrop by Massive Attack."
    )
    resume.assert_awaited_once_with("echo-queue")


def test_nothing_to_resume_is_spoken_without_starting_anything():
    hi = handler_input("ResumeMusicIntent")
    with (
        patch.object(v.device_mapping, "get_player_for_device", return_value="echo"),
        patch.object(
            v,
            "prepare_resume",
            AsyncMock(side_effect=ValueError("There is nothing to resume.")),
        ),
        patch.object(v.threading, "Thread") as thread,
    ):
        v.VoiceMusicHandler().handle(hi)
    hi.response_builder.speak.assert_called_once_with("There is nothing to resume.")
    thread.assert_not_called()


def test_model_has_the_resume_music_phrases():
    model = json.loads(
        (Path(__file__).resolve().parents[1] / "app/models/en-US.json").read_text()
    )
    intents = {
        i["name"]: i["samples"]
        for i in model["interactionModel"]["languageModel"]["intents"]
    }
    assert "resume music" in intents["ResumeMusicIntent"]
    assert "resume my music" in intents["ResumeMusicIntent"]
