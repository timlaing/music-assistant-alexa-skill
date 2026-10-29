"""Focused tests for skill/lambda_function.py handlers and interceptors."""

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
import shared_store
from skill import data, device_mapping, ma_control, util
from skill import lambda_function as handlers

APL_AND_AUDIO = {"AudioPlayer": {}, "Alexa.Presentation.APL": {}}


def event(
    request_type="LaunchRequest",
    device="echo-1",
    intent=None,
    interfaces=None,
    session_new=None,
    include_context=True,
    **extra,
):
    request = {
        "type": request_type,
        "requestId": "test-request",
        "timestamp": "2026-10-02T20:00:00Z",
        "locale": "en-US",
    }
    if intent:
        request["intent"] = {"name": intent, "slots": {}}
    request.update(extra)
    payload = {"version": "1.0", "request": request}
    if include_context:
        payload["context"] = {
            "System": {
                "application": {"applicationId": "test-skill"},
                "user": {"userId": "test-user"},
                "device": {
                    "deviceId": device,
                    "supportedInterfaces": interfaces
                    if interfaces is not None
                    else {"AudioPlayer": {}},
                },
            }
        }
    if session_new is not None:
        payload["session"] = {
            "new": session_new,
            "sessionId": "SessionId.test",
            "application": {"applicationId": "test-skill"},
            "user": {"userId": "test-user"},
        }
    return payload


def invoke(payload):
    return handlers.lambda_handler(payload, None)["response"]


def _speech(response):
    return response.get("outputSpeech", {}).get("ssml", "")


def _directive_types(response):
    return [d["type"] for d in response.get("directives", [])]


def push_stream(url="https://streams.example.com/music/flow/song.mp3", title="Song"):
    shared_store.set_ma({"streamUrl": url, "title": title})


# ###################### launch / play / help / fallback ######################


def test_launch_without_stream_apologizes():
    response = invoke(event("LaunchRequest"))
    assert "could not retrieve the latest music stream" in _speech(response)
    assert response["shouldEndSession"] is True


def test_play_audio_intent_without_stream_apologizes():
    response = invoke(event("IntentRequest", intent="PlayAudio"))
    assert "could not retrieve the latest music stream" in _speech(response)


def test_help_intent_keeps_session_open():
    push_stream()
    response = invoke(event("IntentRequest", intent="AMAZON.HelpIntent"))
    assert "Welcome to" in _speech(response)
    assert response["shouldEndSession"] is False


def test_fallback_intent_rejects_and_ends():
    response = invoke(event("IntentRequest", intent="AMAZON.FallbackIntent"))
    assert "could not understand" in _speech(response)
    assert response["shouldEndSession"] is True


def test_skill_enabled_event_returns_empty_response():
    response = invoke(event("AlexaSkillEvent.SkillEnabled"))
    assert response == {}


def test_session_ended_returns_empty_response():
    response = invoke(event("SessionEndedRequest"))
    assert response == {}


def test_apl_only_device_is_not_supported():
    response = invoke(
        event(
            "IntentRequest",
            intent="AMAZON.HelpIntent",
            interfaces={"Alexa.Presentation.APL": {}},
        )
    )
    assert "not supported on this device" in _speech(response)
    assert response["shouldEndSession"] is True


# ############################ next / previous / start over ############################


def test_next_without_mapping_asks_for_pairing():
    response = invoke(event("IntentRequest", intent="AMAZON.NextIntent"))
    assert "not paired with a Music Assistant player" in _speech(response)
    assert response["shouldEndSession"] is True


def test_next_with_failing_ma_command_reports_failure(monkeypatch):
    device_mapping.set_player_for_device("echo-1", "player")
    monkeypatch.setattr(ma_control, "send_player_command", lambda p, c: False)
    response = invoke(event("IntentRequest", intent="AMAZON.NextIntent"))
    assert "could not reach Music Assistant" in _speech(response)


def test_previous_with_failing_ma_command_reports_failure(monkeypatch):
    device_mapping.set_player_for_device("echo-1", "player")
    monkeypatch.setattr(ma_control, "send_player_command", lambda p, c: False)
    response = invoke(event("IntentRequest", intent="AMAZON.PreviousIntent"))
    assert "could not reach Music Assistant" in _speech(response)


def test_start_over_without_mapping_asks_for_pairing():
    response = invoke(event("IntentRequest", intent="AMAZON.StartOverIntent"))
    assert "not paired with a Music Assistant player" in _speech(response)


def test_start_over_with_failing_ma_command_reports_failure(monkeypatch):
    device_mapping.set_player_for_device("echo-1", "player")
    monkeypatch.setattr(ma_control, "send_player_command", lambda p, c: False)
    response = invoke(event("IntentRequest", intent="AMAZON.StartOverIntent"))
    assert "could not reach Music Assistant" in _speech(response)


# ############################ pause / stop / resume ############################


def test_stop_intent_sends_stop_directive(monkeypatch):
    seen = []
    monkeypatch.setattr(
        ma_control, "send_player_command", lambda p, c: seen.append(c) or True
    )
    response = invoke(event("IntentRequest", intent="AMAZON.StopIntent"))
    assert _directive_types(response) == ["AudioPlayer.Stop"]
    assert response["shouldEndSession"] is True
    # Unmapped device: no MA sync attempted.
    assert seen == []


def test_pause_sync_failure_still_stops_alexa(monkeypatch):
    device_mapping.set_player_for_device("echo-syncfail", "player")
    monkeypatch.setattr(ma_control, "send_player_command", lambda p, c: False)
    response = invoke(
        event("IntentRequest", intent="AMAZON.PauseIntent", device="echo-syncfail")
    )
    assert _directive_types(response) == ["AudioPlayer.Stop"]
    assert response["shouldEndSession"] is True


def test_pause_intent_new_apl_session_renders_document(monkeypatch):
    monkeypatch.setenv("ENABLE_APL", "true")
    push_stream()
    response = invoke(
        event(
            "IntentRequest",
            intent="AMAZON.PauseIntent",
            interfaces=APL_AND_AUDIO,
            session_new=True,
        )
    )
    assert _directive_types(response) == ["Alexa.Presentation.APL.RenderDocument"]
    assert response["shouldEndSession"] is False


def test_resume_without_stream_apologizes():
    response = invoke(event("IntentRequest", intent="AMAZON.ResumeIntent"))
    assert "couldn't reach the stream" in _speech(response)
    assert response["shouldEndSession"] is True


# ############################ loop / shuffle ############################


@pytest.mark.parametrize("intent", ["AMAZON.LoopOnIntent", "AMAZON.ShuffleOnIntent"])
def test_loop_and_shuffle_are_declined(intent):
    response = invoke(event("IntentRequest", intent=intent))
    assert "can not do that" in _speech(response)


# ############################ audioplayer / system events ############################


def test_playback_started_returns_empty_response():
    # No locale on AudioPlayer requests exercises the gettext fallback too.
    payload = event("AudioPlayer.PlaybackStarted", token="tok")
    del payload["request"]["locale"]
    assert invoke(payload) == {}


def test_system_exception_encountered_returns_empty_response():
    assert invoke(event("System.ExceptionEncountered")) == {}


def test_playback_stopped_swallows_recording_failure(monkeypatch):
    def boom(device_id, url, offset_ms):
        raise RuntimeError("store down")

    monkeypatch.setattr(util, "record_stopped_position", boom)
    assert (
        invoke(
            event("AudioPlayer.PlaybackStopped", token="tok", offsetInMilliseconds=5)
        )
        == {}
    )


# ############################ playback controller ############################


def test_play_command_with_stream_plays():
    push_stream()
    response = invoke(event("PlaybackController.PlayCommandIssued"))
    assert _directive_types(response) == ["AudioPlayer.Play"]


def test_play_command_without_stream_notifies_user():
    response = invoke(event("PlaybackController.PlayCommandIssued"))
    assert "couldn't reach the stream" in _speech(response)


def test_next_command_routed_to_mapped_player(monkeypatch):
    device_mapping.set_player_for_device("echo-1", "player")
    called = []
    monkeypatch.setattr(
        ma_control, "send_player_command", lambda p, c: called.append(c) or True
    )
    assert invoke(event("PlaybackController.NextCommandIssued")) == {}
    assert called == ["next"]


def test_previous_command_unmapped_is_ignored(monkeypatch):
    called = []
    monkeypatch.setattr(
        ma_control, "send_player_command", lambda p, c: called.append(c) or True
    )
    assert invoke(event("PlaybackController.PreviousCommandIssued")) == {}
    assert called == []


def test_pause_command_sends_stop():
    response = invoke(event("PlaybackController.PauseCommandIssued"))
    assert _directive_types(response) == ["AudioPlayer.Stop"]
    assert response["shouldEndSession"] is True


# ############################ APL user events ############################


def _user_event():
    return event(
        "Alexa.Presentation.APL.UserEvent",
        interfaces=APL_AND_AUDIO,
        arguments=["MetadataRefresh", "tick"],
    )


def test_apl_user_event_refresh_adds_update_and_schedules_next(monkeypatch):
    monkeypatch.setenv("ENABLE_APL", "true")
    push_stream()
    response = invoke(_user_event())
    assert response["shouldEndSession"] is False
    # SetValue metadata update + scheduled Idle/SendEvent refresh.
    assert _directive_types(response) == [
        "Alexa.Presentation.APL.ExecuteCommands",
        "Alexa.Presentation.APL.ExecuteCommands",
    ]
    first_commands = response["directives"][0]["commands"]
    assert any(c.get("componentId") == "Audio_PrimaryText" for c in first_commands)
    assert response["directives"][1]["commands"][0] == {
        "type": "Idle",
        "delay": 1000,
    }


def test_apl_user_event_without_metadata_keeps_session_open(monkeypatch):
    # Empty store: no audioSources -> warning branch, still keeps session open.
    response = invoke(_user_event())
    assert response["shouldEndSession"] is False
    assert not response.get("directives")  # APL disabled -> no refresh scheduled


def test_apl_user_event_metadata_fetch_failure_is_swallowed(monkeypatch):
    def boom():
        raise RuntimeError("store down")

    monkeypatch.setattr(data, "get_latest", boom)
    response = invoke(_user_event())
    assert response["shouldEndSession"] is False


def test_apl_user_event_with_unrelated_arguments_is_not_claimed():
    payload = _user_event()
    payload["request"]["arguments"] = ["SomethingElse"]
    # Falls through to the fallback handler.
    response = invoke(payload)
    assert "could not understand" in _speech(response)


# ############################ exception dispatch ############################


def test_unhandled_handler_error_hits_catch_all(monkeypatch):
    calls = {"n": 0}

    def flaky_audio_data():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return {}

    monkeypatch.setattr(util, "audio_data", flaky_audio_data)
    response = invoke(event("IntentRequest", intent="AMAZON.HelpIntent"))
    # CatchAllExceptionHandler speaks UNHANDLED_MSG and reprompts with HELP_MSG.
    assert "could not understand" in _speech(response)
    assert "reprompt" in response
    assert "Welcome to" in response["reprompt"]["outputSpeech"]["ssml"]


# ############################ helpers / interceptors ############################


@pytest.mark.parametrize(
    "audio,expected_url",
    [
        ({"url": "https://a/1.mp3"}, "https://a/1.mp3"),
        ({"audioSources": "https://a/2.mp3"}, "https://a/2.mp3"),
        ({"audio_sources": "https://a/3.mp3"}, "https://a/3.mp3"),
        ({"stream": "https://a/4.mp3"}, "https://a/4.mp3"),
        ({"url": ""}, None),
        ("https://a/5.mp3", "https://a/5.mp3"),
        (None, None),
    ],
)
def test_get_stream_url_shapes(monkeypatch, audio, expected_url):
    monkeypatch.setattr(util, "audio_data", lambda: audio)
    url, _ = handlers._get_stream_url(None)
    assert url == expected_url


def test_get_stream_url_survives_audio_data_failure(monkeypatch):
    def boom():
        raise RuntimeError("no data")

    monkeypatch.setattr(util, "audio_data", boom)
    assert handlers._get_stream_url(None) == (None, None)


def test_device_id_from_missing_context_returns_none():
    handler_input = SimpleNamespace(request_envelope=SimpleNamespace(context=None))
    assert handlers._device_id_from(handler_input) is None


def test_sanitize_log_value_strips_control_chars_and_limits():
    # Control characters (incl. CR/LF and ESC) are removed; text is kept.
    assert handlers._sanitize_log_value("a\nb\r\x1b[31mc") == "ab[31mc"
    assert len(handlers._sanitize_log_value("x" * 500)) == 128
    assert handlers._sanitize_log_value(None) == "None"


def _log_record(name="", pathname=""):
    return logging.LogRecord(name, logging.INFO, pathname, 1, "msg", None, None)


def test_component_filter_classifies_sources():
    cases = [
        ("music_assistant_api.ma_routes", "", "API"),
        ("ma_routes", "", "API"),
        ("lambda_function", "", "Skill"),
        ("alexa_api.alexa_routes", "", "Skill"),
        ("ask_sdk_core.dispatch", "", "Skill"),
        ("other", "/app/skill/util.py", "Skill"),
        ("other", "/app/music_assistant_api/ma_routes.py", "API"),
        ("other", "/app/alexa_api/alexa_routes.py", "API"),
        ("other", "/app/endpoints/status.py", "UI/Web"),
        ("other", "/srv/app/app.py", "UI/Web"),
        ("other", "/opt/sitecustomize.py", "UI/Web"),
        ("other", "", "UI/Web"),
    ]
    component_filter = handlers._ComponentFilter()
    for name, pathname, expected in cases:
        record = _log_record(name, pathname)
        assert component_filter.filter(record) is True
        assert record.component == expected


def test_windows_path_separator_normalized_in_filter(monkeypatch):
    import os

    # Simulate running on Windows, where os.sep is a backslash.
    monkeypatch.setattr(os, "sep", "\\")
    record = _log_record("other", "C:\\app\\skill\\apl.py")
    handlers._ComponentFilter().filter(record)
    assert record.component == "Skill"


def test_log_record_factory_assigns_missing_component():
    record = handlers._log_record_factory(
        "music_assistant_api.ma_routes", logging.INFO, "/x/mod.py", 1, "m", (), None
    )
    assert record.component == "API"
    record = handlers._log_record_factory(
        "ask_sdk_core", logging.INFO, "/x/mod.py", 1, "m", (), None
    )
    assert record.component == "Skill"
    record = handlers._log_record_factory(
        "werkzeug", logging.INFO, "/app/endpoints/status.py", 1, "m", (), None
    )
    assert record.component == "UI/Web"
    record = handlers._log_record_factory(
        "werkzeug", logging.INFO, "/opt/other.py", 1, "m", (), None
    )
    assert record.component == "UI/Web"
    record = handlers._log_record_factory(
        "werkzeug", logging.INFO, "", 1, "m", (), None
    )
    assert record.component == "UI/Web"


def test_apl_support_interceptor_without_context():
    handler = SimpleNamespace(
        request_envelope=SimpleNamespace(context=None),
        attributes_manager=SimpleNamespace(request_attributes={}),
    )
    handlers.APLSupportRequestInterceptor().process(handler)
    assert handler.attributes_manager.request_attributes["supports_apl"] is False


def test_request_logger_captures_intent_slots():
    handler = SimpleNamespace(
        request_envelope=SimpleNamespace(
            request=SimpleNamespace(
                object_type="IntentRequest",
                intent=SimpleNamespace(
                    name="PlayAudio",
                    slots={"track": SimpleNamespace(value="Song")},
                ),
            )
        )
    )
    handlers.RequestLogger().process(handler)  # must not raise


def test_request_logger_survives_broken_request():
    class BrokenRequest:
        object_type = property(lambda self: (_ for _ in ()).throw(ValueError("broken")))

    handler = SimpleNamespace(request_envelope=SimpleNamespace(request=BrokenRequest()))
    handlers.RequestLogger().process(handler)  # logs and continues


def test_request_logger_skips_apl_user_events():
    handler = SimpleNamespace(
        request_envelope=SimpleNamespace(
            request=SimpleNamespace(object_type="Alexa.Presentation.APL.UserEvent")
        )
    )
    handlers.RequestLogger().process(handler)  # early return


@pytest.mark.parametrize(
    "locale", ["en-US", "fr-CA", "fr-FR", "de-DE", "es-MX", "pt-BR", "it-IT"]
)
def test_localization_interceptor_loads_locale(locale):
    handler = SimpleNamespace(
        request_envelope=SimpleNamespace(request=SimpleNamespace(locale=locale)),
        attributes_manager=SimpleNamespace(request_attributes={}),
    )
    handlers.LocalizationInterceptor().process(handler)
    assert callable(handler.attributes_manager.request_attributes["_"])


@pytest.mark.parametrize("locale", ["fr-FR", "fr-CA", "de-DE", "es-ES", "it-IT"])
def test_localization_interceptor_keeps_empty_messages_empty(locale, monkeypatch):
    # gettext("") returns the catalog header ("Project-Id-Version: ...
    # Report-Msgid-Bugs-To: EMAIL@ADDRESS ..."); spoken as SSML, Alexa rejects it.
    # localedir is relative: run from app/ as the image does (/app/src).
    monkeypatch.chdir(Path(__file__).resolve().parent.parent / "app")
    handler = SimpleNamespace(
        request_envelope=SimpleNamespace(request=SimpleNamespace(locale=locale)),
        attributes_manager=SimpleNamespace(request_attributes={}),
    )
    handlers.LocalizationInterceptor().process(handler)
    _ = handler.attributes_manager.request_attributes["_"]
    # The committed catalog must really be loaded, or the test proves nothing:
    # NullTranslations (fallback) also returns "" for "".
    assert _(data.UNHANDLED_MSG) != data.UNHANDLED_MSG
    assert _(data.STOP_MSG) == ""
    assert _(data.WELCOME_MSG) == ""


def test_localization_interceptor_never_translates_empty_message(monkeypatch):
    # Independent of the .mo files: a catalog whose gettext("") is its header.
    class HeaderCatalog:
        def gettext(self, message):
            return "Report-Msgid-Bugs-To: EMAIL@ADDRESS" if message == "" else "ok"

    monkeypatch.setattr(
        handlers.gettext, "translation", lambda *args, **kwargs: HeaderCatalog()
    )
    handler = SimpleNamespace(
        request_envelope=SimpleNamespace(request=SimpleNamespace(locale="fr-FR")),
        attributes_manager=SimpleNamespace(request_attributes={}),
    )
    handlers.LocalizationInterceptor().process(handler)
    _ = handler.attributes_manager.request_attributes["_"]
    assert _(data.STOP_MSG) == ""
    assert _(data.UNHANDLED_MSG) == "ok"


def test_localization_interceptor_without_locale_uses_gettext():
    handler = SimpleNamespace(
        request_envelope=SimpleNamespace(request=SimpleNamespace(locale=None)),
        attributes_manager=SimpleNamespace(request_attributes={}),
    )
    handlers.LocalizationInterceptor().process(handler)
    import gettext

    assert handler.attributes_manager.request_attributes["_"] is gettext.gettext


def test_response_logger_is_noop():
    handlers.ResponseLogger().process(SimpleNamespace(), {"ok": True})
