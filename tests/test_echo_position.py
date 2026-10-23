from unittest.mock import patch

import pytest
from skill import device_mapping, echo_position, playback_requests
from test_playback import AUTH, event, invoke, push

FLOW = "/flow/session/queue/item/Echo%20Dot%201"
# The pushed URL as the Echo receives it: on the public host, rewritten to MP3.
ECHO_TOKEN = "https://streams.example.com/music" + FLOW + ".mp3"


@pytest.fixture(autouse=True)
def no_reports(monkeypatch):
    monkeypatch.setattr(echo_position, "_reports", {})
    monkeypatch.setattr(echo_position, "_playing_devices", {})


@pytest.fixture
def playing(client):
    device_mapping.set_player_for_device("echo-1", "Echo Dot 1")
    push(client, streamUrl="http://ma.local:8097" + FLOW + ".flac")
    return client


def with_activity(payload, activity):
    payload["context"]["AudioPlayer"]["playerActivity"] = activity
    return payload


def test_pause_for_a_voice_request_is_reported_for_the_current_stream(playing):
    with patch.object(echo_position.time, "time", return_value=1000.0):
        invoke(event("AudioPlayer.PlaybackStopped", token=ECHO_TOKEN, offset=90_500))
    with patch.object(echo_position.time, "time", return_value=1003.0):
        report = echo_position.report("Echo Dot 1")
    assert report == {
        "playerId": "Echo Dot 1",
        "current": True,
        "offsetMs": 90_500,
        "activity": "STOPPED",
        "observedAt": 1000.0,
        "ageSeconds": 3.0,
    }


def test_resume_after_the_voice_request_is_reported(playing):
    invoke(event("AudioPlayer.PlaybackStopped", token=ECHO_TOKEN, offset=90_500))
    invoke(event("AudioPlayer.PlaybackStarted", token=ECHO_TOKEN, offset=90_500))
    assert echo_position.report("Echo Dot 1")["activity"] == "PLAYING"


def test_every_request_updates_the_position(playing):
    payload = event("IntentRequest", intent="WhatsPlayingIntent", token=None)
    payload["context"]["AudioPlayer"].update(
        token=ECHO_TOKEN, offsetInMilliseconds=200_000
    )
    invoke(with_activity(payload, "STOPPED"))
    report = echo_position.report("Echo Dot 1")
    assert (report["offsetMs"], report["activity"]) == (200_000, "STOPPED")


def test_requests_without_player_state_keep_the_last_report(playing):
    invoke(event("AudioPlayer.PlaybackStarted", token=ECHO_TOKEN, offset=5_000))
    invoke(event("IntentRequest", intent="AMAZON.HelpIntent"))
    assert echo_position.report("Echo Dot 1")["offsetMs"] == 5_000


def test_a_report_for_an_older_stream_is_not_current(playing):
    old = ECHO_TOKEN.replace("/session/", "/old-session/")
    invoke(event("AudioPlayer.PlaybackStarted", token=old, offset=5_000))
    assert echo_position.report("Echo Dot 1")["current"] is False


def test_unmapped_playback_events_need_a_music_assistant_stream(client):
    invoke(
        event("AudioPlayer.PlaybackStarted", token="https://x/other.mp3", offset=5_000)
    )
    assert echo_position.report("Echo Dot 1") == {
        "playerId": "Echo Dot 1",
        "current": False,
    }
    # Before Music Assistant pushed that player's stream, the report is not current.
    invoke(event("AudioPlayer.PlaybackStarted", token=ECHO_TOKEN, offset=5_000))
    assert echo_position.report("Echo Dot 1")["current"] is False


def test_endpoint_returns_the_report_to_music_assistant(playing):
    invoke(event("AudioPlayer.PlaybackStarted", token=ECHO_TOKEN, offset=5_000))
    response = playing.get(
        "/alexa/playback-state", query_string={"player_id": "Echo Dot 1"}, headers=AUTH
    )
    assert response.status_code == 200
    assert response.json["current"] is True
    assert response.json["offsetMs"] == 5_000
    assert response.json["activity"] == "PLAYING"


def test_endpoint_needs_credentials_and_a_player(client):
    query = {"player_id": "Echo Dot 1"}
    assert client.get("/alexa/playback-state", query_string=query).status_code == 401
    assert client.get("/alexa/playback-state", headers=AUTH).status_code == 400


def test_a_speaker_groups_playback_events_count_for_its_player(playing):
    # A group member pauses for a voice request; the group resumes under its own device ID.
    invoke(event("AudioPlayer.PlaybackStopped", token=ECHO_TOKEN, offset=90_500))
    invoke(
        event(
            "AudioPlayer.PlaybackStarted",
            device="alexa-group",
            token=ECHO_TOKEN,
            offset=90_500,
        )
    )
    report = echo_position.report("Echo Dot 1")
    assert (report["activity"], report["current"]) == ("PLAYING", True)


def test_unmapped_voice_requests_are_still_ignored(playing):
    payload = event("IntentRequest", device="other-echo", intent="AMAZON.HelpIntent")
    payload["context"]["AudioPlayer"].update(token=ECHO_TOKEN, offsetInMilliseconds=1)
    payload["context"]["AudioPlayer"]["playerActivity"] = "STOPPED"
    invoke(payload)
    assert echo_position.report("Echo Dot 1") == {
        "playerId": "Echo Dot 1",
        "current": False,
    }


def test_a_group_members_idle_player_does_not_pause_the_group(playing):
    # The group plays and reports under its own ID; the member you talk to reports its own
    # idle player ("STOPPED" at another offset), and the group sends no events around it.
    invoke(event("AudioPlayer.PlaybackStarted", device="alexa-group", token=ECHO_TOKEN))
    payload = event("IntentRequest", intent="WhatsPlayingIntent")
    payload["context"]["AudioPlayer"].update(
        token=ECHO_TOKEN, offsetInMilliseconds=8_604
    )
    payload["context"]["AudioPlayer"]["playerActivity"] = "STOPPED"
    with patch.object(echo_position.time, "time", return_value=2000.0):
        invoke(payload)
    report = echo_position.report("Echo Dot 1")
    assert (report["activity"], report["offsetMs"]) == ("PLAYING", 0)


def test_a_single_echos_own_voice_request_still_counts(playing):
    invoke(event("AudioPlayer.PlaybackStarted", token=ECHO_TOKEN, offset=1_000))
    payload = event("IntentRequest", intent="WhatsPlayingIntent")
    payload["context"]["AudioPlayer"].update(
        token=ECHO_TOKEN, offsetInMilliseconds=20_000
    )
    payload["context"]["AudioPlayer"]["playerActivity"] = "STOPPED"
    invoke(payload)
    report = echo_position.report("Echo Dot 1")
    assert (report["activity"], report["offsetMs"]) == ("STOPPED", 20_000)


def test_report_that_arrives_late_does_not_replace_a_newer_one(playing):
    newer = event("AudioPlayer.PlaybackStarted", token=ECHO_TOKEN, offset=90_500)
    older = event("AudioPlayer.PlaybackStopped", token=ECHO_TOKEN, offset=80_000)
    older["request"]["timestamp"] = "2026-10-02T19:59:58Z"
    invoke(newer)
    invoke(older)
    report = echo_position.report("Echo Dot 1")
    assert (report["offsetMs"], report["activity"]) == (90_500, "PLAYING")


def test_button_pause_cancels_a_start_pending_after_an_answer(playing):
    ticket = playback_requests.begin("Echo Dot 1")
    invoke(event("PlaybackController.PauseCommandIssued"))
    assert not playback_requests.current(ticket)
