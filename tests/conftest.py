import os
import sys
from pathlib import Path

import pytest

sys.path.insert(
    0,
    os.environ.get("SKILL_APP_PATH", str(Path(__file__).resolve().parents[1] / "app")),
)
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_SETTINGS_PATH", str(tmp_path / "app-settings.json"))
    import shared_store
    from skill import data

    monkeypatch.setattr(shared_store, "_ma", None)
    monkeypatch.setattr(shared_store, "_alexa", None)
    monkeypatch.setattr(shared_store, "_version", 0)
    monkeypatch.setenv("APP_USERNAME", "test-user")
    monkeypatch.setenv("APP_PASSWORD", "test-password")
    monkeypatch.setenv("MA_HOSTNAME", "https://streams.example.com/music")
    monkeypatch.setenv("SKIP_URL_VALIDATION", "true")
    monkeypatch.setenv("DEVICE_MAPPING_PATH", str(tmp_path / "device_players.json"))
    monkeypatch.setenv("ENABLE_APL", "false")
    monkeypatch.delenv("MA_API_URL", raising=False)
    monkeypatch.delenv("MA_API_TOKEN", raising=False)
    monkeypatch.setenv("SKILL_DEPLOYMENT_PATH", str(tmp_path / "skill-deployment.json"))
    from app import app
    app.extensions.pop("skill_deployment", None)
    data._request_info.set(None)
    yield
    data._request_info.set(None)


@pytest.fixture
def client():
    from app import app

    app.config["TESTING"] = True
    return app.test_client()
