import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import shared_store
from public_urls import public_base, rewrite_url
from skill import data, device_mapping, ma_control, util
from skill import lambda_function as handlers

AUTH = {"Authorization": "Basic dGVzdC11c2VyOnRlc3QtcGFzc3dvcmQ="}


def event(
    request_type="LaunchRequest",
    device="echo-1",
    intent=None,
    apl=False,
    token=None,
    offset=0,
):
    request = {
        "type": request_type,
        "requestId": "test-request",
        "timestamp": "2026-10-02T20:00:00Z",
        "locale": "en-US",
    }
    if intent:
        request["intent"] = {"name": intent, "slots": {}}
    if token:
        request.update(token=token, offsetInMilliseconds=offset)
    return {
        "version": "1.0",
        "context": {
            "System": {
                "application": {"applicationId": "test-skill"},
                "user": {"userId": "test-user"},
                "device": {
                    "deviceId": device,
                    "supportedInterfaces": {
                        "AudioPlayer": {},
                        **({"Alexa.Presentation.APL": {}} if apl else {}),
                    },
                },
            },
            "AudioPlayer": {"token": token or "", "offsetInMilliseconds": offset},
        },
        "request": request,
    }


def invoke(payload):
    return handlers.lambda_handler(payload, None)["response"]


def push(client, **extra):
    payload = {
        "streamUrl": "http://ma.local:8097/flow/queue/song.mp3",
        "title": "Track",
        "artist": "Artist",
        "album": "Album",
        "imageUrl": "http://ma.local:8097/imageproxy/image",
    }
    payload.update(extra)
    return client.post("/ma/push-url", json=payload, headers=AUTH)


@pytest.mark.parametrize(
    "source",
    [
        "http://192.168.1.10:8097/flow/track%20one.mp3?key=a%2Fb#part",
        "http://ma.local:8097/flow/track%20one.mp3?key=a%2Fb#part",
        "http://[::1]:8097/flow/track%20one.mp3?key=a%2Fb#part",
    ],
)
def test_proxy_url_rewriting(source):
    expected = (
        "https://streams.example.com:8443/music/flow/track%20one.mp3?key=a%2Fb#part"
    )
    assert rewrite_url(source, "streams.example.com:8443/music") == expected
    assert rewrite_url(expected, "https://streams.example.com:8443/music") == expected


def test_url_validation_and_unicode():
    assert public_base(' "streams.example.com/" ') == "https://streams.example.com"
    assert "B%C3%BCro%20Echo.mp3" in rewrite_url(
        "http://ma/flow/Büro Echo.mp3", "streams.example.com"
    )
    for value in [
        "http://streams.example.com",
        "https://user:password@host",
        "https://host?query=1",
    ]:
        with pytest.raises(ValueError):
            public_base(value)


def test_auth_health_and_idle_status(client):
    assert client.get("/health").json == {"status": "ok"}
    assert client.get("/ma/latest-url").status_code == 401
    assert client.get("/alexa/intents").status_code == 401
    assert client.get("/ma/latest-url", headers=AUTH).status_code == 404
    assert client.get("/alexa/intents", headers=AUTH).status_code == 200
    assert "idle" in client.get("/status/ma", headers=AUTH).json["ma_api_html"]
    assert "idle" in client.get("/status/alexa", headers=AUTH).json["alexa_api_html"]
    response = client.post("/", json=event())
    assert response.status_code != 200  # Missing Alexa signature must not be accepted.


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        ["url"],
        {"streamUrl": 42},
        {"streamUrl": ""},
        {"streamUrl": "file:///tmp/file"},
    ],
)
def test_invalid_push(client, payload):
    assert client.post("/ma/push-url", json=payload, headers=AUTH).status_code == 400


def test_push_play_does_not_overwrite_ma(client):
    assert push(client).status_code == 200
    before = client.get("/ma/latest-url", headers=AUTH).json
    response = invoke(event())
    assert (
        response["directives"][0]["audioItem"]["stream"]["url"] == before["streamUrl"]
    )
    assert shared_store.get_ma() == before
    assert shared_store.get_alexa()["secondary"] == "Artist - Album"
    assert client.get("/alexa/latest-url", headers=AUTH).status_code == 200


def test_external_artwork_preserved(client):
    assert push(client, imageUrl="https://cdn.example.com/cover.jpg").status_code == 200
    assert shared_store.get_ma()["imageUrl"] == "https://cdn.example.com/cover.jpg"


@pytest.mark.parametrize(
    "url",
    [
        "http://ma.local:8097/announcement/echo.mp3",
        "http://ma.local:8097/flow/finite/song.mp3",
        "http://ma.local:8097/pluginsource/radio.mp3",
    ],
)
def test_completion_never_requeues_current_url(client, url):
    push(client, streamUrl=url)
    token = shared_store.get_ma()["streamUrl"]
    for request_type in [
        "AudioPlayer.PlaybackNearlyFinished",
        "AudioPlayer.PlaybackFinished",
        "AudioPlayer.PlaybackFailed",
    ]:
        payload = event(request_type, token=token)
        if request_type.endswith("Failed"):
            payload["request"]["error"] = {
                "type": "MEDIA_ERROR_SERVICE_UNAVAILABLE",
                "message": "unavailable",
            }
        assert not invoke(payload).get("directives")


def test_resume_only_for_matching_device_and_url(client):
    push(client)
    url = shared_store.get_ma()["streamUrl"]
    invoke(event("AudioPlayer.PlaybackStopped", token=url, offset=12345))
    response = invoke(event("IntentRequest", intent="AMAZON.ResumeIntent"))
    assert (
        response["directives"][0]["audioItem"]["stream"]["offsetInMilliseconds"]
        == 12345
    )
    assert util.get_resume_offset("other-device", url) == 0
    assert util.get_resume_offset("echo-1", url + "?new=1") == 0


def test_apl_and_metadata_request_isolation(monkeypatch):
    monkeypatch.setenv("ENABLE_APL", "true")
    barrier = Barrier(2)

    def capture(apl, title):
        shared_store.set_ma(
            {"streamUrl": "https://streams.example.com/flow/song.mp3", "title": title}
        )
        envelope = SimpleNamespace(
            context=SimpleNamespace(
                system=SimpleNamespace(
                    device=SimpleNamespace(
                        supported_interfaces=SimpleNamespace(
                            alexa_presentation_apl=object() if apl else None
                        )
                    )
                )
            )
        )
        handler = SimpleNamespace(
            request_envelope=envelope,
            attributes_manager=SimpleNamespace(request_attributes={}),
        )
        handlers.APLSupportRequestInterceptor().process(handler)
        barrier.wait(timeout=3)
        return handlers._supports_apl(handler), data.get_info()["primaryText"]

    # Snapshot A before B updates the shared store.
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(capture, True, "A")
        # Wait until first reaches barrier before changing the store.
        import time

        deadline = time.monotonic() + 3
        while barrier.n_waiting != 1 and time.monotonic() < deadline:
            time.sleep(0.001)
        second = pool.submit(capture, False, "B")
        assert first.result() == (True, "A")
        assert second.result() == (False, "B")


def test_atomic_mapping_updates_and_reload():
    with ThreadPoolExecutor(8) as pool:
        assert all(
            pool.map(
                lambda n: device_mapping.set_player_for_device(
                    str(n), "player-" + str(n)
                ),
                range(32),
            )
        )
    assert len(device_mapping.load_mapping()) == 32
    assert device_mapping.get_player_for_device("3") == "player-3"
    assert device_mapping.set_player_for_device("3", None)
    assert device_mapping.get_player_for_device("3") is None


@pytest.mark.parametrize(
    "intent,command",
    [
        ("AMAZON.NextIntent", "next"),
        ("AMAZON.PreviousIntent", "previous"),
        ("AMAZON.StartOverIntent", "start_over"),
    ],
)
def test_voice_controls_route_to_mapped_player(monkeypatch, intent, command):
    device_mapping.set_player_for_device("echo-1", "ma-player")
    called = []
    monkeypatch.setattr(
        ma_control,
        "send_player_command",
        lambda player, cmd: called.append((player, cmd)) or True,
    )
    invoke(event("IntentRequest", intent=intent))
    assert called == [("ma-player", command)]


def test_missing_and_invalid_ma_credentials(monkeypatch):
    assert ma_control.send_player_command("player", "next") is False
    monkeypatch.setenv("MA_API_URL", "http://ma:8095")
    from music_assistant_client.exceptions import CannotConnect

    monkeypatch.setattr(
        ma_control, "_send_command", AsyncMock(side_effect=CannotConnect("unreachable"))
    )
    assert ma_control.send_player_command("player", "next") is False


def test_pause_stop_resume_echo_is_consumed_once(monkeypatch):
    device_mapping.set_player_for_device("echo-1", "player")
    called = []
    monkeypatch.setattr(
        ma_control,
        "send_player_command",
        lambda player, command: called.append(command) or True,
    )
    invoke(event("IntentRequest", intent="AMAZON.PauseIntent"))
    invoke(event("IntentRequest", intent="AMAZON.PauseIntent"))
    assert called == ["pause"]
    assert not ma_control.is_echo_of_ma_command("echo-1", "pause")


def test_options_redaction_and_signal_ownership():
    # Use a fresh subprocess because importing the app in the test suite has already occurred.
    code = "import signal; old=signal.getsignal(signal.SIGTERM); import app; assert signal.getsignal(signal.SIGTERM)==old; assert app._safe_options_for_log({'api_password':'private','MA_API_TOKEN':'private'})=={'api_password':'set','MA_API_TOKEN':'set'}"
    subprocess.run(
        [sys.executable, "-c", code],
        cwd=os.environ.get(
            "SKILL_APP_PATH", str(Path(__file__).resolve().parents[1] / "app")
        ),
        check=True,
    )


def test_apl_opt_in_uses_request_snapshot(client, monkeypatch):
    push(client)
    assert invoke(event(apl=True))["directives"][0]["type"] == "AudioPlayer.Play"
    monkeypatch.setenv("ENABLE_APL", "true")
    assert (
        invoke(event(apl=True))["directives"][0]["type"]
        == "Alexa.Presentation.APL.RenderDocument"
    )


def test_internal_artwork_on_separate_port(client):
    assert (
        push(client, imageUrl="http://ma.local:8095/imageproxy/art").status_code == 200
    )
    assert (
        shared_store.get_ma()["imageUrl"]
        == "https://streams.example.com/music/imageproxy/art"
    )


def test_stream_snapshot_is_independent_of_updates():
    shared_store.set_ma(
        {"streamUrl": "https://streams.example.com/flow/a.mp3", "title": "A"}
    )
    snapshot = shared_store.get_ma()
    snapshot["title"] = "Changed by caller"
    assert shared_store.get_ma()["title"] == "A"
    shared_store.set_alexa({"streamUrl": "https://streams.example.com/flow/b.mp3"})
    assert shared_store.get_ma()["title"] == "A"


@pytest.mark.parametrize(
    "command,method",
    [("next", "next_track"), ("pause", "pause"), ("stop", "stop"), ("resume", "play")],
)
def test_ma_client_command_interface(monkeypatch, command, method):
    import asyncio
    from unittest.mock import MagicMock

    client = MagicMock()
    client.players = SimpleNamespace(
        **{name: AsyncMock() for name in ("next_track", "pause", "stop", "play")}
    )
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(
        ma_control, "MusicAssistantClient", lambda *args, **kwargs: context
    )
    asyncio.run(ma_control._send_command("http://ma:8095", "token", "player", command))
    getattr(client.players, method).assert_awaited_once_with("player")


@pytest.mark.parametrize("offset,index,expected", [(-1, 0, 0), (-1, 3, 2), (0, 3, 3)])
def test_relative_queue_index(offset, index, expected):
    import asyncio

    client = SimpleNamespace(
        player_queues=SimpleNamespace(
            get_active_queue=AsyncMock(
                return_value=SimpleNamespace(queue_id="queue", current_index=index)
            ),
            play_index=AsyncMock(),
        )
    )
    asyncio.run(ma_control._play_relative_index(client, "player", offset))
    client.player_queues.play_index.assert_awaited_once_with("queue", expected)


def test_ma_command_echo_expires(monkeypatch):
    monkeypatch.setattr(ma_control.time, "time", lambda: 100)
    ma_control.mark_ma_triggered("echo", "stop")
    monkeypatch.setattr(ma_control.time, "time", lambda: 109)
    assert not ma_control.is_echo_of_ma_command("echo", "stop")


@pytest.mark.parametrize("header", ["X-Simulator-Bypass", "X-Simulator-Signature"])
def test_simulator_bypass_requires_api_auth(client, header):
    push(client)
    assert client.post("/", json=event(), headers={header: "true"}).status_code == 403
    response = client.post("/", json=event(), headers={**AUTH, header: "true"})
    assert response.status_code == 200
    assert response.json["response"]["directives"][0]["type"] == "AudioPlayer.Play"


def test_apl_preserves_provider_artwork_on_render_and_refresh(client, monkeypatch):
    from ask_sdk_core.response_helper import ResponseFactory
    monkeypatch.setenv("ENABLE_APL", "true")
    artwork = "https://art.provider.example/cover%20one.jpg"
    assert push(client, imageUrl=artwork).status_code == 200
    response = invoke(event(apl=True))
    document = response["directives"][0]["document"]
    assert document["mainTemplate"]["items"][0]["coverImageSource"] == artwork
    builder = ResponseFactory()
    util.update_apl_metadata(builder)
    commands = builder.response.directives[0].commands
    images = [command["value"] for command in commands
              if command["property"] in ("coverImageSource", "imageSource", "backgroundImageSource")]
    assert images and all(value == artwork for value in images)
