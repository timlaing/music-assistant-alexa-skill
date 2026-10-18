"""Additional tests for skill/ma_control.py command dispatch."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from music_assistant_client.exceptions import (
    CannotConnect,
    ConnectionFailed,
    InvalidServerVersion,
)
from music_assistant_models.errors import MusicAssistantError
from skill import ma_control


def _fake_client_context(client):
    context = AsyncMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=False)
    return lambda *a, **kw: context


def test_mark_and_is_echo_edge_cases():
    # Falsy device ids are ignored on both sides.
    ma_control.mark_ma_triggered("", "pause")
    assert ma_control.is_echo_of_ma_command("", "pause") is False
    assert ma_control.is_echo_of_ma_command(None, "pause") is False
    # An unset marker never reports an echo.
    assert ma_control.is_echo_of_ma_command("dev-x", "pause") is False


def test_echo_marker_is_consumed_once():
    ma_control.mark_ma_triggered("dev-y", "stop")
    assert ma_control.is_echo_of_ma_command("dev-y", "stop") is True
    assert ma_control.is_echo_of_ma_command("dev-y", "stop") is False


def test_expired_echo_marker_is_not_an_echo(monkeypatch):
    monkeypatch.setattr(ma_control.time, "time", lambda: 100)
    ma_control.mark_ma_triggered("dev-z", "resume")
    monkeypatch.setattr(ma_control.time, "time", lambda: 100 + 9)
    assert ma_control.is_echo_of_ma_command("dev-z", "resume") is False


def test_send_player_command_rejects_unknown_command():
    with pytest.raises(ValueError, match="Unsupported MA command"):
        ma_control.send_player_command("player", "seek")


def test_send_command_rejects_unknown_command(monkeypatch):
    client = SimpleNamespace(players=SimpleNamespace())
    monkeypatch.setattr(
        ma_control, "MusicAssistantClient", _fake_client_context(client)
    )
    with pytest.raises(ValueError, match="Unsupported MA command"):
        asyncio.run(ma_control._send_command("http://ma:8095", "tok", "player", "seek"))


def test_send_player_command_success(monkeypatch):
    monkeypatch.setenv("MA_API_URL", "http://ma:8095")
    sent = AsyncMock()
    monkeypatch.setattr(ma_control, "_send_command", sent)
    assert ma_control.send_player_command("player", "next") is True
    sent.assert_awaited_once_with("http://ma:8095", None, "player", "next")


@pytest.mark.parametrize(
    "error",
    [
        CannotConnect("down"),
        ConnectionFailed("down"),
        InvalidServerVersion("old"),
        MusicAssistantError("rejected"),
        RuntimeError("unexpected"),
    ],
)
def test_send_player_command_failure_returns_false(monkeypatch, error):
    monkeypatch.setenv("MA_API_URL", "http://ma:8095")
    monkeypatch.setattr(ma_control, "_send_command", AsyncMock(side_effect=error))
    assert ma_control.send_player_command("player", "next") is False


def test_play_relative_index_requires_active_track():
    client = SimpleNamespace(
        player_queues=SimpleNamespace(
            get_active_queue=AsyncMock(return_value=None),
            play_index=AsyncMock(),
        )
    )
    with pytest.raises(ValueError, match="No active queue"):
        asyncio.run(ma_control._play_relative_index(client, "player", -1))
    client.player_queues.get_active_queue = AsyncMock(
        return_value=SimpleNamespace(queue_id="q", current_index=None)
    )
    with pytest.raises(ValueError, match="No active queue"):
        asyncio.run(ma_control._play_relative_index(client, "player", -1))


def test_send_command_previous_and_start_over_use_queue_index(monkeypatch):
    client = SimpleNamespace(
        players=SimpleNamespace(
            next_track=AsyncMock(),
            pause=AsyncMock(),
            stop=AsyncMock(),
            play=AsyncMock(),
        ),
        player_queues=SimpleNamespace(
            get_active_queue=AsyncMock(
                return_value=SimpleNamespace(queue_id="q", current_index=3)
            ),
            play_index=AsyncMock(),
        ),
    )
    monkeypatch.setattr(
        ma_control, "MusicAssistantClient", _fake_client_context(client)
    )
    asyncio.run(ma_control._send_command("http://ma:8095", "tok", "player", "previous"))
    client.player_queues.play_index.assert_awaited_once_with("q", 2)
    client.player_queues.play_index.reset_mock()
    asyncio.run(
        ma_control._send_command("http://ma:8095", "tok", "player", "start_over")
    )
    client.player_queues.play_index.assert_awaited_once_with("q", 3)


def test_send_command_resume_uses_players_play(monkeypatch):
    client = SimpleNamespace(
        players=SimpleNamespace(
            next_track=AsyncMock(),
            pause=AsyncMock(),
            stop=AsyncMock(),
            play=AsyncMock(),
        )
    )
    monkeypatch.setattr(
        ma_control, "MusicAssistantClient", _fake_client_context(client)
    )
    asyncio.run(ma_control._send_command("http://ma:8095", "tok", "player", "resume"))
    client.players.play.assert_awaited_once_with("player")
