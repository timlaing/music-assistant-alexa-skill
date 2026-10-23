from unittest.mock import patch

import pytest
from test_playback import event, invoke, push

import shared_store
from skill import device_mapping


@pytest.mark.parametrize("kind", ["flow", "single"])
def test_stock_provider_streams_are_isolated_without_player_id(client, kind):
    device_mapping.set_player_for_device("echo-1", "Echo Dot 1")
    device_mapping.set_player_for_device("echo-2", "Echo Dot 1")
    device_mapping.set_player_for_device("echo-3", "Echo Dot 3")
    first = f"/{kind}/session/queue/item/Echo%20Dot%201.flac"
    other = f"/{kind}/session/other/item/Echo%20Dot%203.flac"
    push(client, streamUrl="http://ma.local:8097" + first)
    push(client, streamUrl="http://ma.local:8097" + other)
    for device, path in [("echo-1", first), ("echo-2", first), ("echo-3", other)]:
        response = invoke(event("IntentRequest", device=device, intent="PlayAudio"))
        assert response["directives"][0]["audioItem"]["stream"]["url"].endswith(
            path.replace(".flac", ".mp3")
        )


def test_mapped_player_without_stream_cannot_use_another_players_stream(client):
    device_mapping.set_player_for_device("echo-1", "Echo Dot 1")
    push(
        client,
        streamUrl="http://ma.local:8097/flow/session/queue/item/Echo%20Dot%203.mp3",
    )
    assert not invoke(event("IntentRequest", intent="PlayAudio")).get("directives")


def test_stream_snapshot_stays_consistent_if_another_push_arrives_during_request(
    client,
):
    from skill import lambda_function as handlers

    device_mapping.set_player_for_device("echo-1", "Echo Dot 1")
    first = "http://ma.local:8097/flow/session/queue/item/Echo%20Dot%201.mp3"
    push(client, streamUrl=first)
    original = handlers._get_stream_url

    def interleave(request):
        # A push for another player lands mid-request; the pinned snapshot must win.
        push(
            client,
            streamUrl="http://ma.local:8097/flow/session/other/item/Echo%20Dot%203.mp3",
        )
        return original(request)

    with patch("skill.lambda_function._get_stream_url", side_effect=interleave):
        response = invoke(event("IntentRequest", intent="PlayAudio"))
    assert response["directives"][0]["audioItem"]["stream"]["url"].endswith(
        "/Echo%20Dot%201.mp3"
    )
    assert device_mapping.get_player_for_device("echo-1") == "Echo Dot 1"


@pytest.mark.parametrize(
    "url", [None, "", "http://ma/arbitrary.mp3", "http://ma/flow/queue/song.mp3"]
)
def test_only_canonical_ma_stream_paths_identify_players(url):
    assert shared_store.player_from_stream(url) is None


def test_plugin_source_streams_identify_their_player():
    url = "http://ma.local:8097/source/session/linein/Echo%20Dot%201.mp3"
    assert shared_store.player_from_stream(url) == "Echo Dot 1"


def test_pushed_player_id_wins_over_the_stream_url(client):
    device_mapping.set_player_for_device("echo-1", "Kitchen")
    push(
        client,
        streamUrl="http://ma.local:8097/flow/session/queue/item/kitchen-uuid.mp3",
        playerId="Kitchen",
    )
    response = invoke(event("IntentRequest", device="echo-1", intent="PlayAudio"))
    assert response["directives"][0]["audioItem"]["stream"]["url"].endswith(
        "/kitchen-uuid.mp3"
    )
    assert shared_store.get_ma()["playerId"] == "Kitchen"


def test_push_for_an_unknown_player_replaces_older_player_streams(client):
    device_mapping.set_player_for_device("echo-1", "Echo Dot 1")
    push(client, streamUrl="http://ma.local:8097/flow/s/q/i/Echo%20Dot%201.mp3")
    push(client, streamUrl="http://ma.local:8097/stream/new-shape.mp3")
    response = invoke(event("IntentRequest", device="echo-1", intent="PlayAudio"))
    assert response["directives"][0]["audioItem"]["stream"]["url"].endswith(
        "/stream/new-shape.mp3"
    )
