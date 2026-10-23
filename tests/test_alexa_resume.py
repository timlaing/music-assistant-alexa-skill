from unittest.mock import AsyncMock, patch

import pytest
import shared_store
from music_assistant_client.exceptions import CannotConnect
from music_assistant_models.errors import InvalidToken, MusicAssistantError
from skill import device_mapping, ma_control
from skill import voice_music as v
from test_playback import event, invoke


@pytest.fixture
def mapped(monkeypatch):
    monkeypatch.setenv("MA_API_URL", "http://ma:8095")
    monkeypatch.setenv("MA_API_TOKEN", "token")
    device_mapping.set_player_for_device("echo-1", "Echo Dot 1")


def resume():
    return invoke(event("IntentRequest", intent="AMAZON.ResumeIntent"))


def test_resume_continues_the_music_assistant_queue_silently(mapped):
    prepare = AsyncMock(return_value=("echo-queue", None))
    resumed = AsyncMock()
    with (
        patch.object(v, "prepare_resume", prepare),
        patch.object(v, "resume_queue", resumed),
        patch.object(v.threading, "Thread") as thread,
        patch.object(ma_control, "send_player_command") as old_sync,
    ):
        response = resume()
        thread.call_args.kwargs["target"]()
    # MA's own play button also arrives as a resume, while MA reports it as playing.
    prepare.assert_awaited_once_with("Echo Dot 1", refuse_playing=False)
    resumed.assert_awaited_once_with("echo-queue")
    old_sync.assert_not_called()
    assert "outputSpeech" not in response
    assert not response.get("directives")
    assert response["shouldEndSession"] is True


def test_resume_with_an_empty_queue_says_so(mapped):
    with (
        patch.object(
            v,
            "prepare_resume",
            AsyncMock(side_effect=ValueError("There is nothing to resume.")),
        ),
        patch.object(v.threading, "Thread") as thread,
    ):
        response = resume()
    assert "There is nothing to resume." in response["outputSpeech"]["ssml"]
    thread.assert_not_called()


def test_resume_without_music_assistant_keeps_the_stream_replay(monkeypatch):
    device_mapping.set_player_for_device("echo-1", "Echo Dot 1")
    with patch.object(v, "prepare_resume") as prepare:
        resume()
    prepare.assert_not_called()


@pytest.mark.parametrize(
    "error",
    [CannotConnect("ma:8095"), InvalidToken("rotated"), TimeoutError()],
)
def test_resume_replays_the_stored_stream_when_music_assistant_is_unreachable(
    mapped, error
):
    shared_store.set_ma(
        {
            "streamUrl": "https://streams.example.com/flow/s/q/i/Echo%20Dot%201.mp3",
            "playerId": "Echo Dot 1",
        }
    )
    with (
        patch.object(v, "prepare_resume", AsyncMock(side_effect=error)),
        patch.object(ma_control, "send_player_command", return_value=False),
        patch.object(v.threading, "Thread") as thread,
    ):
        response = resume()
    thread.assert_not_called()
    play = response["directives"][0]
    assert play["type"] == "AudioPlayer.Play"
    assert play["audioItem"]["stream"]["url"].endswith("/Echo%20Dot%201.mp3")


def test_resume_refused_by_music_assistant_is_not_replayed(mapped):
    with patch.object(
        v, "prepare_resume", AsyncMock(side_effect=MusicAssistantError("busy"))
    ):
        response = resume()
    assert "could not resume" in response["outputSpeech"]["ssml"]
    assert not response.get("directives")
