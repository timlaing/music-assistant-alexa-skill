import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import pytest
from skill import device_mapping, ma_control, playback_requests
from test_playback import event, invoke


def test_newer_request_and_stop_cancel_pending_start():
    first = playback_requests.begin("group")
    other = playback_requests.begin("other")
    playback_requests.activate(first)
    playback_requests.check()
    playback_requests.begin("group")
    with pytest.raises(playback_requests.SupersededPlayback):
        playback_requests.check()
    assert playback_requests.current(other)


def test_stop_and_pause_preserve_queue():
    queues = NS(
        get_active_queue=AsyncMock(return_value=NS(queue_id="active-group")),
        clear=AsyncMock(),
    )
    players = NS(stop=AsyncMock(), pause=AsyncMock())
    client = NS(player_queues=queues, players=players)
    context = AsyncMock()
    context.__aenter__.return_value = client
    with patch("skill.ma_control.MusicAssistantClient", return_value=context):
        asyncio.run(ma_control._send_command("http://ma", "token", "group", "stop"))
        players.stop.assert_awaited_once_with("group")
        asyncio.run(ma_control._send_command("http://ma", "token", "group", "pause"))
        players.pause.assert_awaited_once_with("group")
        queues.clear.assert_not_awaited()


def test_waiting_old_write_is_cancelled_before_it_can_mutate_queue():
    async def scenario():
        ticket = playback_requests.begin("group")
        playback_requests.activate(ticket)
        operation = AsyncMock()
        lock = playback_requests.write_lock("group")
        lock.acquire()
        task = asyncio.create_task(playback_requests.write(operation))
        await asyncio.sleep(0)
        playback_requests.begin("group")
        lock.release()
        with pytest.raises(playback_requests.SupersededPlayback):
            await task
        operation.assert_not_awaited()

    asyncio.run(scenario())


@pytest.mark.parametrize("intent", ["AMAZON.StopIntent", "AMAZON.PauseIntent"])
def test_spoken_stop_or_pause_cancels_a_pending_start(intent):
    device_mapping.set_player_for_device("echo-1", "Echo Dot 1")
    pending = playback_requests.begin("Echo Dot 1")
    with patch.object(ma_control, "send_player_command", return_value=True):
        invoke(event("IntentRequest", intent=intent))
    assert not playback_requests.current(pending)
