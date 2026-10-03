import json
import stat
import threading

import pytest
from app_settings import SettingsError, SettingsStore, get_setting, initialize, store
from env_secrets import get_env_secret
from skill_deployment import DeploymentManager, config_key, settings
from test_ingress import gateway


def test_migration_preserves_values_and_never_overrides_web_edits(
    tmp_path, monkeypatch
):
    legacy = tmp_path / "options.json"
    legacy.write_text(
        json.dumps(
            {
                "ma_hostname": "https://old.example.com/music",
                "api_password": "legacy-password",
                "enable_apl": True,
                "aws_default_region": "unused",
                "lwa_redirect_uri": "https://old.example.com/old-callback",
            }
        )
    )
    monkeypatch.setenv("ADDON_OPTIONS_PATH", str(legacy))
    monkeypatch.setenv("HA_INGRESS_ENABLED", "true")
    initialize()
    instance = store()
    assert get_env_secret("APP_PASSWORD") == "legacy-password"
    assert get_setting("ENABLE_APL") == "true"
    assert "aws_default_region" not in instance.values
    assert "lwa_redirect_uri" not in instance.values
    payload = {
        "revision": instance.public()["revision"],
        "values": {"ma_hostname": "https://new.example.com/music"},
    }
    instance.save(payload)
    assert (
        SettingsStore(instance.path).snapshot()["ma_hostname"]
        == "https://new.example.com/music"
    )
    initialize()
    assert get_setting("MA_HOSTNAME") == "https://new.example.com/music"
    assert stat.S_IMODE(instance.path.stat().st_mode) == 0o600


def test_first_start_generates_password_once(tmp_path, monkeypatch):
    monkeypatch.setenv("HA_INGRESS_ENABLED", "true")
    monkeypatch.setenv("ADDON_OPTIONS_PATH", str(tmp_path / "missing.json"))
    monkeypatch.delenv("APP_PASSWORD")
    initialize()
    password = get_env_secret("APP_PASSWORD")
    assert len(password) >= 32
    initialize()
    assert SettingsStore(store().path).snapshot()["api_password"] == password


def test_secret_files_bootstrap_but_web_secrets_are_literal(tmp_path, monkeypatch):
    secret = tmp_path / "token"
    secret.write_text("private-bootstrap-token")
    monkeypatch.setenv("MA_API_TOKEN", str(secret))
    assert store().snapshot()["ma_api_token"] == "private-bootstrap-token"
    instance = store()
    instance.save(
        {
            "revision": instance.public()["revision"],
            "values": {"ma_api_token": str(secret)},
        }
    )
    assert get_env_secret("MA_API_TOKEN") == str(secret)  # not an arbitrary file read
    public = instance.public()
    assert "ma_api_token" not in public["values"]
    instance.save({"revision": public["revision"], "values": {"ma_api_token": ""}})
    assert get_env_secret("MA_API_TOKEN") == str(secret)
    instance.save(
        {
            "revision": instance.public()["revision"],
            "values": {},
            "clear_secrets": ["ma_api_token"],
        }
    )
    assert get_env_secret("MA_API_TOKEN") is None


@pytest.mark.parametrize(
    "changes",
    [
        {"skill_hostname": "http://public.example.com"},
        {"ma_hostname": "https://user:pass@example.com"},
        {"ma_api_url": "file:///etc/passwd"},
        {"locale": "../en-US"},
        {"enable_apl": "false"},
        {"aws_default_region": "us-east-1"},
        {"lwa_redirect_uri": "https://elsewhere.example.com"},
        {"api_username": ""},
        {"skill_certificate_type": "SelfSigned"},
        {"lwa_client_secret": "secret\nvalue"},
    ],
)
def test_invalid_save_is_atomic(changes):
    instance = store()
    before = instance.public()
    with pytest.raises(SettingsError):
        instance.save({"revision": before["revision"], "values": changes})
    assert instance.public() == before
    assert not instance.path.exists()


def test_stale_browser_save_rejected():
    instance = store()
    revision = instance.public()["revision"]
    instance.save({"revision": revision, "values": {"locale": "en-GB"}})
    with pytest.raises(SettingsError, match="another window"):
        instance.save({"revision": revision, "values": {"locale": "en-US"}})
    assert get_setting("LOCALE") == "en-GB"


def test_ingress_save_changes_runtime_auth_deployment_and_callback(client, monkeypatch):
    from app import app

    monkeypatch.setenv("HA_INGRESS_ENABLED", "true")
    csrf = gateway(client, "/setup/status").json["csrf"]
    before = gateway(client, "/setup/settings").json
    payload = {
        "revision": before["revision"],
        "values": {
            "api_username": "new-user",
            "api_password": "new-password",
            "skill_hostname": "https://new.example.com/ma-alexa-skill/",
            "locale": "en-GB",
            "lwa_client_id": "client",
            "lwa_client_secret": "secret",
            "ma_hostname": "https://new.example.com/flow",
        },
    }
    assert gateway(client, "/setup/settings", "post", json=payload).status_code == 403
    assert client.post("/setup/settings", json=payload).status_code == 403
    response = gateway(
        client, "/setup/settings", "post", headers={"X-CSRF-Token": csrf}, json=payload
    )
    assert response.status_code == 200
    assert (
        response.json["callback_url"]
        == "https://new.example.com/ma-alexa-skill/setup/oauth/callback"
    )
    assert (
        "new-password" not in response.text
        and "lwa_client_secret" not in response.json["values"]
    )
    assert (
        client.get("/alexa/intents", auth=("test-user", "test-password")).status_code
        == 401
    )
    assert (
        client.get("/alexa/intents", auth=("new-user", "new-password")).status_code
        == 200
    )
    assert settings()["locale"] == "en-GB"
    key = config_key()
    instance = app.extensions["skill_deployment"]
    instance.update(review={"id": "old-review"})
    payload = {
        "revision": response.json["revision"],
        "values": {"lwa_client_secret": "changed"},
    }
    assert (
        gateway(
            client,
            "/setup/settings",
            "post",
            headers={"X-CSRF-Token": csrf},
            json=payload,
        ).status_code
        == 200
    )
    assert config_key() != key
    assert instance.state["review"] is None
    assert not instance.thread  # saving cannot deploy
    assert (
        gateway(client, "/setup/settings/api-password", "post", json={}).status_code
        == 403
    )
    assert (
        gateway(
            client,
            "/setup/settings/api-password",
            "post",
            headers={"X-CSRF-Token": csrf},
            json={},
        ).json["password"]
        == "new-password"
    )


def test_busy_deployment_blocks_setting_changes(client, monkeypatch, tmp_path):
    from app import app

    monkeypatch.setenv("HA_INGRESS_ENABLED", "true")
    instance = DeploymentManager(tmp_path / "deployment.json")
    app.extensions["skill_deployment"] = instance
    stop = threading.Event()
    instance.thread = threading.Thread(target=stop.wait)
    instance.thread.start()
    try:
        csrf = gateway(client, "/setup/status").json["csrf"]
        public = gateway(client, "/setup/settings").json
        response = gateway(
            client,
            "/setup/settings",
            "post",
            json={"revision": public["revision"], "values": {"locale": "en-GB"}},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 400
        assert get_setting("LOCALE") == "en-US"
    finally:
        stop.set()
        instance.thread.join()


def test_full_form_boolean_settings_and_request_control_credentials(monkeypatch):
    instance = store()
    values = instance.snapshot()
    values.update(
        enable_apl=True,
        skip_url_validation=False,
        ma_api_url="http://ma.local:8095",
        ma_api_token="control-token",
    )
    instance.save({"revision": instance.public()["revision"], "values": values})
    assert get_setting("ENABLE_APL") == "true"
    assert get_setting("SKIP_URL_VALIDATION") == "false"
    from skill import ma_control

    called = []

    async def command(url, token, player, action):
        called.append((url, token, player, action))

    monkeypatch.setattr(ma_control, "_send_command", command)
    assert ma_control.send_player_command("player", "next")
    assert called == [("http://ma.local:8095", "control-token", "player", "next")]
