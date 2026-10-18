"""Tests for skill/util.py response-building helpers."""

import pytest
import requests
from ask_sdk_core.response_helper import ResponseFactory
from skill import data, util


def _builder():
    return ResponseFactory()


def _as_dict(response):
    return response.to_dict()


def _directive_types(response):
    return [d["object_type"] for d in _as_dict(response)["directives"]]


def test_apl_enabled_reads_setting(monkeypatch):
    for value, expected in [
        ("true", True),
        ("1", True),
        ("yes", True),
        ("false", False),
        ("", False),
    ]:
        monkeypatch.setenv("ENABLE_APL", value)
        assert util.apl_enabled() is expected


def test_stopped_position_roundtrip_and_stale_entries():
    util.record_stopped_position("dev-1", "https://x/song.mp3", 4200)
    assert util.get_resume_offset("dev-1", "https://x/song.mp3") == 4200
    # Stale: same device, different URL -> 0.
    assert util.get_resume_offset("dev-1", "https://x/other.mp3") == 0
    # Unknown device -> 0.
    assert util.get_resume_offset("dev-9", "https://x/song.mp3") == 0
    # Offset of None is normalized to 0.
    util.record_stopped_position("dev-2", "https://x/song.mp3", None)
    assert util.get_resume_offset("dev-2", "https://x/song.mp3") == 0


def test_record_stopped_position_ignores_missing_identity():
    util.record_stopped_position("", "https://x/song.mp3", 100)
    util.record_stopped_position("dev", "", 100)
    assert util.get_resume_offset("", "https://x/song.mp3") == 0
    assert util.get_resume_offset("dev", "https://x/song.mp3") == 0


def test_get_ma_hostname_passthrough_and_error_handling(monkeypatch):
    monkeypatch.setenv("MA_HOSTNAME", "https://streams.example.com/music")
    assert util.get_ma_hostname() == "https://streams.example.com/music"
    # http:// raises by default, returns '' when raise_on_http_scheme=False.
    monkeypatch.setenv("MA_HOSTNAME", "http://insecure.example.com")
    with pytest.raises(ValueError):
        util.get_ma_hostname()
    assert util.get_ma_hostname(raise_on_http_scheme=False) == ""


def test_replace_ip_in_url_delegates_to_rewrite():
    assert (
        util.replace_ip_in_url(
            "http://192.168.1.5:8095/flow/a.mp3", "streams.example.com"
        )
        == "https://streams.example.com/flow/a.mp3"
    )


def test_audio_data_returns_snapshot_and_swallows_errors(monkeypatch):
    import shared_store

    shared_store.set_ma({"streamUrl": "https://x/song.mp3", "title": "Song"})
    assert util.audio_data()["primaryText"] == "Song"

    def boom():
        raise RuntimeError("no data")

    monkeypatch.setattr(data, "get_info", boom)
    assert util.audio_data() is None


def test_push_alexa_metadata_captures_stream_info():
    import shared_store

    data._request_info.set(
        {
            "audioSources": "https://x/song.mp3",
            "primaryText": "Song",
            "secondaryText": "Artist - Album",
            "coverImageSource": "https://x/cover.jpg",
        }
    )
    util.push_alexa_metadata("https://x/song.mp3")
    payload = shared_store.get_alexa()
    assert payload["streamUrl"] == "https://x/song.mp3"
    assert payload["title"] == "Song"
    assert payload["secondary"] == "Artist - Album"
    assert payload["imageUrl"] == "https://x/cover.jpg"


def test_play_rejects_http_scheme_hostname(monkeypatch):
    monkeypatch.setenv("MA_HOSTNAME", "http://insecure.example.com")
    response = util.play("https://x/song.mp3", 0, "hello", _builder())
    assert "unsupported scheme" in response.output_speech.ssml
    assert response.should_end_session is True


def test_play_requires_hostname(monkeypatch):
    monkeypatch.setenv("MA_HOSTNAME", "")
    response = util.play("https://x/song.mp3", 0, None, _builder())
    assert "did not specify a valid hostname" in response.output_speech.ssml


def test_play_skips_validation_and_emits_play_directive(monkeypatch):
    monkeypatch.setenv("MA_HOSTNAME", "https://streams.example.com/music")
    monkeypatch.setenv("SKIP_URL_VALIDATION", "true")

    def unexpected_head(*a, **kw):
        raise AssertionError("validation should be skipped")

    monkeypatch.setattr(util.requests, "head", unexpected_head)
    response = util.play(
        "http://192.168.1.5:8095/flow/song.mp3", 1500, None, _builder()
    )
    stream = _as_dict(response)["directives"][0]["audio_item"]["stream"]
    assert stream["url"] == "https://streams.example.com/music/flow/song.mp3"
    assert stream["offset_in_milliseconds"] == 1500
    assert response.should_end_session is True


class _FakeResp:
    def __init__(self, status_code):
        self.status_code = status_code
        self.closed = False

    def close(self):
        self.closed = True


def test_play_validation_head_ok(monkeypatch):
    monkeypatch.setenv("MA_HOSTNAME", "https://streams.example.com/music")
    monkeypatch.setenv("SKIP_URL_VALIDATION", "false")
    heads = []
    monkeypatch.setattr(
        util.requests, "head", lambda url, **kw: heads.append(url) or _FakeResp(200)
    )
    response = util.play(
        "https://streams.example.com/music/song.mp3", 0, None, _builder()
    )
    assert heads  # head was attempted
    assert _directive_types(response) == ["AudioPlayer.Play"]


def test_play_validation_head_fails_get_recovers(monkeypatch):
    monkeypatch.setenv("MA_HOSTNAME", "https://streams.example.com/music")
    monkeypatch.setenv("SKIP_URL_VALIDATION", "false")
    gets = []
    monkeypatch.setattr(util.requests, "head", lambda url, **kw: _FakeResp(404))
    monkeypatch.setattr(
        util.requests, "get", lambda url, **kw: gets.append(url) or _FakeResp(200)
    )
    response = util.play(
        "https://streams.example.com/music/song.mp3", 0, None, _builder()
    )
    assert gets
    assert _directive_types(response) == ["AudioPlayer.Play"]


def test_play_validation_unreachable_get_fails(monkeypatch):
    monkeypatch.setenv("MA_HOSTNAME", "https://streams.example.com/music")
    monkeypatch.setenv("SKIP_URL_VALIDATION", "false")
    monkeypatch.setattr(util.requests, "head", lambda url, **kw: _FakeResp(500))
    monkeypatch.setattr(util.requests, "get", lambda url, **kw: _FakeResp(500))
    response = util.play(
        "https://streams.example.com/music/song.mp3", 0, None, _builder()
    )
    assert "can't reach the audio file" in response.output_speech.ssml
    assert response.should_end_session is True
    assert not response.directives


def test_play_validation_request_exception(monkeypatch):
    monkeypatch.setenv("MA_HOSTNAME", "https://streams.example.com/music")
    monkeypatch.setenv("SKIP_URL_VALIDATION", "false")

    def boom(url, **kw):
        raise requests.RequestException("dns down")

    monkeypatch.setattr(util.requests, "head", boom)
    response = util.play(
        "https://streams.example.com/music/song.mp3", 0, None, _builder()
    )
    assert "can't reach the audio file" in response.output_speech.ssml


def test_play_with_apl_adds_document_and_speaks(monkeypatch):
    import shared_store

    monkeypatch.setenv("ENABLE_APL", "true")
    shared_store.set_ma(
        {
            "streamUrl": "https://streams.example.com/music/song.mp3",
            "title": "Song",
        }
    )
    response = util.play(
        "https://streams.example.com/music/song.mp3",
        0,
        "Playing Song",
        _builder(),
        supports_apl=True,
    )
    assert _directive_types(response) == ["Alexa.Presentation.APL.RenderDocument"]
    assert "Playing Song" in response.output_speech.ssml


def test_play_metadata_push_failure_is_non_fatal(monkeypatch):
    monkeypatch.setenv("MA_HOSTNAME", "https://streams.example.com/music")
    monkeypatch.setenv("SKIP_URL_VALIDATION", "true")

    def boom(url):
        raise RuntimeError("store down")

    monkeypatch.setattr(util, "push_alexa_metadata", boom)
    response = util.play(
        "https://streams.example.com/music/song.mp3", 0, None, _builder()
    )
    assert _directive_types(response) == ["AudioPlayer.Play"]


def test_stop_sends_stop_directive_and_ends_session():
    response = util.stop("Goodbye", _builder())
    assert _directive_types(response) == ["AudioPlayer.Stop"]
    assert response.should_end_session is True
    assert "Goodbye" in response.output_speech.ssml
    # Without text there is no speech.
    response = util.stop(None, _builder())
    assert response.output_speech is None


def test_pause_without_apl_falls_back_to_stop(monkeypatch):
    monkeypatch.setenv("ENABLE_APL", "false")
    response = util.pause("Pausing", _builder(), supports_apl=True)
    assert _directive_types(response) == ["AudioPlayer.Stop"]
    assert response.should_end_session is True
    assert "Pausing" in response.output_speech.ssml


def test_pause_with_apl_sends_media_command(monkeypatch):
    monkeypatch.setenv("ENABLE_APL", "true")
    response = util.pause(None, _builder(), supports_apl=True, session_new=False)
    directive = _as_dict(response)["directives"][0]
    assert directive["object_type"] == "Alexa.Presentation.APL.ExecuteCommands"
    assert directive["commands"][0]["command"] == "pause"
    assert directive["token"] == "playbackToken"
    assert response.should_end_session is False


def test_pause_with_apl_session_new_renders_document(monkeypatch):
    import shared_store

    monkeypatch.setenv("ENABLE_APL", "true")
    shared_store.set_ma(
        {
            "streamUrl": "https://streams.example.com/music/song.mp3",
            "title": "Song",
        }
    )
    response = util.pause(None, _builder(), supports_apl=True, session_new=True)
    assert _directive_types(response) == ["Alexa.Presentation.APL.RenderDocument"]
    assert response.should_end_session is False


def test_pause_session_new_render_failure_keeps_session_open(monkeypatch):
    monkeypatch.setenv("ENABLE_APL", "true")

    def boom(response_builder, start_paused=False):
        raise RuntimeError("render down")

    monkeypatch.setattr(util, "add_apl", boom)
    response = util.pause(None, _builder(), supports_apl=True, session_new=True)
    assert response.should_end_session is False
    assert not response.directives


def test_pause_apl_command_failure_falls_back_to_stop(monkeypatch):
    monkeypatch.setenv("ENABLE_APL", "true")

    def boom(*a, **kw):
        raise RuntimeError("directive down")

    # ControlMediaCommand construction blows up -> StopDirective fallback.
    monkeypatch.setattr(util, "ControlMediaCommand", boom)
    response = util.pause(None, _builder(), supports_apl=True, session_new=False)
    assert _directive_types(response) == ["AudioPlayer.Stop"]
    assert response.should_end_session is True


def test_clear_sends_clear_queue_directive():
    response = util.clear(_builder())
    directive = _as_dict(response)["directives"][0]
    assert directive["object_type"] == "AudioPlayer.ClearQueue"
    assert directive["clear_behavior"] == "CLEAR_ENQUEUED"


def test_update_apl_metadata_noop_when_disabled(monkeypatch):
    monkeypatch.setenv("ENABLE_APL", "false")
    builder = _builder()
    util.update_apl_metadata(builder)
    assert not builder.response.directives


def test_update_apl_metadata_builds_setvalue_commands(monkeypatch):
    monkeypatch.setenv("ENABLE_APL", "true")
    data._request_info.set(
        {
            "primaryText": "Song",
            "secondaryText": "Artist",
            "coverImageSource": "https://x/cover.jpg",
            "backgroundImageSource": "https://x/bg.jpg",
        }
    )
    builder = _builder()
    util.update_apl_metadata(builder)
    commands = builder.response.directives[0].commands
    component_ids = {command["componentId"] for command in commands}
    assert component_ids == {
        "Audio_PrimaryText",
        "Audio_SecondaryText",
        "AudioPlayerRoot",
        "Audio_CoverArt",
        "AlexaBackground",
    }
    assert all(command["type"] == "SetValue" for command in commands)


def test_update_apl_metadata_without_fields_adds_no_directive(monkeypatch):
    monkeypatch.setenv("ENABLE_APL", "true")
    data._request_info.set({})
    builder = _builder()
    util.update_apl_metadata(builder)
    assert not builder.response.directives


def test_update_apl_metadata_swallows_errors(monkeypatch):
    monkeypatch.setenv("ENABLE_APL", "true")

    def boom():
        raise RuntimeError("store gone")

    monkeypatch.setattr(data, "get_info", boom)
    builder = _builder()
    util.update_apl_metadata(builder)  # must not raise
    assert not builder.response.directives


def test_schedule_apl_refresh_noop_when_disabled(monkeypatch):
    monkeypatch.setenv("ENABLE_APL", "false")
    builder = _builder()
    util.schedule_apl_refresh(builder)
    assert not builder.response.directives


def test_schedule_apl_refresh_schedules_idle_then_send_event(monkeypatch):
    monkeypatch.setenv("ENABLE_APL", "true")
    builder = _builder()
    util.schedule_apl_refresh(builder, delay_ms=2500)
    commands = builder.response.directives[0].commands
    assert commands[0] == {"type": "Idle", "delay": 2500}
    assert commands[1]["type"] == "SendEvent"
    assert commands[1]["arguments"][0] == "MetadataRefresh"


def test_schedule_apl_refresh_swallows_errors(monkeypatch):
    monkeypatch.setenv("ENABLE_APL", "true")

    def boom(*a, **kw):
        raise RuntimeError("directive down")

    monkeypatch.setattr(util, "ExecuteCommandsDirective", boom)
    builder = _builder()
    util.schedule_apl_refresh(builder)  # must not raise
