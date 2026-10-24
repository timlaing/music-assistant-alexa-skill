"""Issue #33: several Echoes playing at once must each get their own stream.

Music Assistant pushes one URL per player within a fraction of a second, then
each Echo invokes the skill. With a single shared "latest stream" slot every
PlayAudio read the last push, so only one queue was actually heard.

Covers the three places that resolve "the current stream":

- the request pin (PlayAudio/Launch) -> ``begin_request``/``select_stream``
- the stale-URL refresh wait  -> ``_refresh_stream_url``
- the APL metadata refresh    -> ``data.get_latest``
"""

import json

import shared_store
from skill import data, device_mapping, ma_control, util
from skill import lambda_function as handlers
from test_playback import event, invoke, push

MARS = "http://ma.local:8097/flow/ThJr5Cf2/Mars/2db142e0/Mars.mp3"
VENUS = "http://ma.local:8097/flow/ThJr5Cf2/Venus/2db142e0/Venus.mp3"

STALE_MARS = "https://streams.example.com/music/flow/s/Mars/old/Mars.mp3"
FRESH_MARS = "https://streams.example.com/music/flow/s/Mars/new/Mars.mp3"
FRESH_VENUS = "https://streams.example.com/music/flow/s/Venus/new/Venus.mp3"


def play_url(device):
    response = invoke(event("IntentRequest", device=device, intent="PlayAudio"))
    directives = response.get("directives") or []
    streams = [
        d["audioItem"]["stream"]["url"]
        for d in directives
        if d.get("type") == "AudioPlayer.Play"
    ]
    return streams[0] if streams else None


def test_each_echo_plays_the_stream_pushed_for_its_own_player(client):
    device_mapping.set_player_for_device("echo-mars", "Mars")
    device_mapping.set_player_for_device("echo-venus", "Venus")
    push(client, streamUrl=MARS, title="Song on Mars")
    push(client, streamUrl=VENUS, title="Song on Venus")  # pushed last

    mars = play_url("echo-mars")
    venus = play_url("echo-venus")

    assert mars, "Mars Echo got no Play directive"
    assert "/Mars.mp3" in mars, f"Mars Echo got: {mars}"
    assert venus, "Venus Echo got no Play directive"
    assert "/Venus.mp3" in venus, f"Venus Echo got: {venus}"


def test_request_order_does_not_matter(client):
    device_mapping.set_player_for_device("echo-mars", "Mars")
    device_mapping.set_player_for_device("echo-venus", "Venus")
    push(client, streamUrl=VENUS, title="Song on Venus")
    push(client, streamUrl=MARS, title="Song on Mars")

    venus = play_url("echo-venus")
    mars = play_url("echo-mars")

    assert mars, "Mars Echo got no Play directive"
    assert "/Mars.mp3" in mars, f"Mars Echo got: {mars}"
    assert venus, "Venus Echo got no Play directive"
    assert "/Venus.mp3" in venus, f"Venus Echo got: {venus}"


def test_metadata_refresh_of_a_mapped_echo_without_its_own_push_stays_empty(
    client, monkeypatch
):
    """A mapped Echo's card stays empty until its own player pushes.

    Taking the latest push instead would put the other queue on this Echo's
    card. The PlayAudio half of the same isolation is
    ``test_mapped_player_without_stream_cannot_use_another_players_stream``.
    """
    monkeypatch.setenv("ENABLE_APL", "true")
    device_mapping.set_player_for_device("echo-venus", "Venus")
    push(client, streamUrl=MARS, title="Song on Mars")  # Venus never pushes

    payload = event("Alexa.Presentation.APL.UserEvent", device="echo-venus", apl=True)
    payload["request"]["arguments"] = ["MetadataRefresh", "tick"]

    directives = invoke(payload).get("directives") or []
    rendered = json.dumps(directives)
    # The handler re-arms the refresh loop with a SendEvent tick, so its
    # presence proves it ran end to end rather than returning early and
    # leaving the card without any further updates.
    refresh_ticks = [
        command
        for directive in directives
        for command in directive.get("commands", [])
        if command.get("type") == "SendEvent"
    ]

    assert "Song on Mars" not in rendered
    # The request did pin metadata - it is empty, not another player's track.
    assert data.get_info()["audioSources"] == ""
    assert refresh_ticks, f"no scheduled refresh in: {rendered}"
    assert "MetadataRefresh" in refresh_ticks[0]["arguments"]


def test_stale_url_refresh_ignores_another_players_push(monkeypatch):
    """The stale-URL wait must take THIS player's fresh push, not any push.

    While waiting for Mars's re-pushed stream, Venus's queue refreshes too
    (exactly the multi-Echo situation of the issue) - handing that URL to the
    Mars Echo would play the wrong queue.
    """
    device_mapping.set_player_for_device("echo-1", "Mars")
    monkeypatch.setenv("SKIP_URL_VALIDATION", "false")
    monkeypatch.setattr(handlers, "_REFRESH_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(handlers, "_REFRESH_POLL_SECONDS", 0.01)
    monkeypatch.setattr(util, "validate_stream_url", lambda url: False)
    shared_store.set_ma({"streamUrl": STALE_MARS, "title": "t", "playerId": "Mars"})
    sent = []

    def fake_send(player, command):
        sent.append((player, command))
        # The other Echo's queue pushes during the wait window; Mars never does.
        shared_store.set_ma(
            {"streamUrl": FRESH_VENUS, "title": "v", "playerId": "Venus"}
        )
        return True

    monkeypatch.setattr(ma_control, "send_player_command", fake_send)

    response = invoke(event())

    assert sent == [("Mars", "resume")]
    # No fresh URL for Mars: the stored one fails validation instead.
    assert "directives" not in response
    assert FRESH_VENUS not in json.dumps(response)
    assert "can't reach the audio file" in response["outputSpeech"]["ssml"]


def test_stale_url_refresh_picks_up_its_own_players_fresh_push(monkeypatch):
    device_mapping.set_player_for_device("echo-1", "Mars")
    monkeypatch.setenv("SKIP_URL_VALIDATION", "false")
    monkeypatch.setattr(handlers, "_REFRESH_WAIT_SECONDS", 1)
    monkeypatch.setattr(handlers, "_REFRESH_POLL_SECONDS", 0.01)
    monkeypatch.setattr(util, "validate_stream_url", lambda url: False)
    shared_store.set_ma({"streamUrl": STALE_MARS, "title": "t", "playerId": "Mars"})

    def fake_send(player, command):
        # Venus pushes first, then Mars's own fresh URL arrives.
        shared_store.set_ma(
            {"streamUrl": FRESH_VENUS, "title": "v", "playerId": "Venus"}
        )
        shared_store.set_ma({"streamUrl": FRESH_MARS, "title": "t", "playerId": "Mars"})
        return True

    monkeypatch.setattr(ma_control, "send_player_command", fake_send)

    response = invoke(event())

    assert response["directives"][0]["audioItem"]["stream"]["url"] == FRESH_MARS


def test_metadata_refresh_shows_this_echoes_own_stream(client, monkeypatch):
    """The APL card refresh must not render the other queue's track."""
    monkeypatch.setenv("ENABLE_APL", "true")
    device_mapping.set_player_for_device("echo-mars", "Mars")
    push(client, streamUrl=MARS, title="Song on Mars")
    push(client, streamUrl=VENUS, title="Song on Venus")  # pushed last

    payload = event("Alexa.Presentation.APL.UserEvent", device="echo-mars", apl=True)
    payload["request"]["arguments"] = ["MetadataRefresh", "tick"]

    rendered = json.dumps(invoke(payload)["directives"])
    assert "Song on Mars" in rendered
    assert "Song on Venus" not in rendered
