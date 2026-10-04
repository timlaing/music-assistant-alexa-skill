"""Persistent web settings, with legacy add-on and environment bootstrap support."""

import hashlib
import json
import os
import secrets
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from cryptography import x509

DEFAULTS = {
    "ma_hostname": "",
    "skill_hostname": "",
    "api_username": "ma-local-alexa-api",
    "api_password": "",
    "locale": "en-US",
    "skip_url_validation": False,
    "ma_api_url": "",
    "ma_api_token": "",
    "enable_apl": False,
    "lwa_client_id": "",
    "lwa_client_secret": "",
    "skill_certificate_type": "Trusted",
    "skill_certificate_pem": "",
}
ENV = {key: key.upper() for key in DEFAULTS}
ENV.update(api_username="APP_USERNAME", api_password="APP_PASSWORD")
SECRET_FIELDS = {"api_password", "ma_api_token", "lwa_client_secret"}
_LOCK = threading.RLock()
_STORES = {}


class SettingsError(ValueError):
    pass


def validate_certificate(value):
    if not isinstance(value, str) or len(value) > 16384:
        raise SettingsError("Choose a public PEM certificate file smaller than 16 KB.")
    value = value.strip()
    if (
        not value.startswith("-----BEGIN CERTIFICATE-----")
        or value.count("-----BEGIN CERTIFICATE-----") != 1
        or not value.endswith("-----END CERTIFICATE-----")
        or "PRIVATE KEY" in value
    ):
        raise SettingsError("Upload one public PEM certificate, without a private key.")
    try:
        x509.load_pem_x509_certificate(value.encode("ascii"))
    except (ValueError, UnicodeError) as exc:
        raise SettingsError("The file is not a valid public PEM certificate.") from exc
    return value


def effective_skill_endpoint(values):
    override = values["skill_hostname"].strip()
    audio = values["ma_hostname"].strip()
    if override or not audio:
        return override
    parsed = urlsplit(audio)
    if not parsed.netloc:
        return audio.rstrip("/") + "/ma-alexa-skill/"
    return urlunsplit((parsed.scheme, parsed.netloc, "/ma-alexa-skill/", "", ""))


def callback_url(endpoint):
    parsed = urlsplit(endpoint)
    return (
        urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                "/ma-alexa-skill/setup/oauth/callback",
                "",
                "",
            )
        )
        if parsed.netloc
        else ""
    )


def _environment(key):
    value = os.environ.get(ENV[key])
    if value is None:
        return DEFAULTS[key]
    if isinstance(DEFAULTS[key], bool):
        return value.lower() in ("true", "1", "yes", "on")
    if (key in SECRET_FIELDS or key == "api_username") and Path(value).is_file():
        return Path(value).read_text().strip()
    return value


class SettingsStore:
    def __init__(self, path):
        self.path = Path(path)
        self.values = None
        if self.path.exists():
            self.values = json.loads(self.path.read_text())
            if isinstance(self.values, dict) and set(self.values) == set(DEFAULTS) - {
                "skill_certificate_pem"
            }:
                self.values["skill_certificate_pem"] = ""
            if not isinstance(self.values, dict) or set(self.values) != set(DEFAULTS):
                raise SettingsError(
                    "Saved settings are invalid; restore your settings backup."
                )

    def snapshot(self):
        with _LOCK:
            return (
                dict(self.values)
                if self.values is not None
                else {key: _environment(key) for key in DEFAULTS}
            )

    def write(self, values):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=".app-settings-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(values, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            self.values = dict(values)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def migrate(self, options_path):
        with _LOCK:
            if self.values is not None:
                return
            values = self.snapshot()
            path = Path(options_path)
            if path.exists():
                options = json.loads(path.read_text())
                if not isinstance(options, dict):
                    raise SettingsError("Legacy add-on settings are invalid.")
                for key in DEFAULTS:
                    if key in options and options[key] is not None:
                        values[key] = options[key]
            # Never import the unused AWS region or the now-derived callback.
            if values["skill_certificate_type"] == "SelfSigned":
                validate_certificate(values["skill_certificate_pem"])
            if not values["api_username"]:
                values["api_username"] = DEFAULTS["api_username"]
            if not values["api_password"]:
                values["api_password"] = secrets.token_urlsafe(32)
            self.write(values)

    def public(self):
        values = self.snapshot()
        revision = hashlib.sha256(
            json.dumps(values, sort_keys=True).encode()
        ).hexdigest()
        return {
            "values": {
                key: value for key, value in values.items() if key not in SECRET_FIELDS
            },
            "secrets_set": {key: bool(values[key]) for key in SECRET_FIELDS},
            "skill_endpoint": effective_skill_endpoint(values),
            "callback_url": callback_url(effective_skill_endpoint(values)),
            "revision": revision,
            "locales": sorted(
                path.stem for path in (Path(__file__).parent / "models").glob("*.json")
            ),
        }

    def save(self, payload):
        with _LOCK:
            if not isinstance(payload, dict) or set(payload) - {
                "values",
                "revision",
                "clear_secrets",
            }:
                raise SettingsError("Invalid settings request.")
            if payload.get("revision") != self.public()["revision"]:
                raise SettingsError(
                    "Settings changed in another window. Reload before saving."
                )
            changes, clear = payload.get("values"), payload.get("clear_secrets", [])
            if not isinstance(changes, dict) or set(changes) - set(DEFAULTS):
                raise SettingsError("Unknown setting.")
            if not isinstance(clear, list) or any(
                key not in SECRET_FIELDS for key in clear
            ):
                raise SettingsError("Invalid secret removal request.")
            values = self.snapshot()
            for key, value in changes.items():
                if isinstance(DEFAULTS[key], bool):
                    if type(value) is not bool:
                        raise SettingsError("Checkbox settings must be true or false.")
                elif key == "skill_certificate_pem":
                    if value:
                        value = validate_certificate(value)
                elif (
                    not isinstance(value, str)
                    or len(value) > 8192
                    or any(ord(c) < 32 for c in value)
                ):
                    raise SettingsError(
                        "Settings must be plain text without control characters."
                    )
                if key not in SECRET_FIELDS or value:
                    values[key] = (
                        value
                        if key in SECRET_FIELDS or isinstance(value, bool)
                        else value.strip()
                    )
            for key in clear:
                if key == "api_password":
                    raise SettingsError(
                        "Replace the API password instead of removing it."
                    )
                values[key] = ""
            for key in ("ma_hostname", "skill_hostname", "ma_api_url"):
                value = values[key]
                if not value:
                    continue
                try:
                    parsed = urlsplit(value)
                    port = parsed.port
                    allowed = ("http", "https") if key == "ma_api_url" else ("https",)
                    valid = (
                        parsed.scheme in allowed
                        and parsed.hostname
                        and not (
                            parsed.username
                            or parsed.password
                            or parsed.query
                            or parsed.fragment
                        )
                    )
                    if key == "skill_hostname":
                        valid = valid and port in (None, 443)
                except ValueError:
                    valid = False
                if not valid:
                    raise SettingsError(
                        f"Invalid URL for {key}; use {'HTTP or HTTPS' if key == 'ma_api_url' else 'HTTPS'} without credentials, query or fragment."
                    )
            if values["locale"] not in self.public()["locales"]:
                raise SettingsError("Choose a locale with a bundled voice model.")
            if values["skill_certificate_type"] not in (
                "Trusted",
                "Wildcard",
                "SelfSigned",
            ):
                raise SettingsError(
                    "Choose Trusted, Trusted sub-domain or File certificate type."
                )
            if values["skill_certificate_type"] == "SelfSigned":
                validate_certificate(values["skill_certificate_pem"])
            if not values["api_username"] or not values["api_password"]:
                raise SettingsError("Set both API username and password.")
            self.write(values)
            return self.public()


def store():
    path = os.environ.get("APP_SETTINGS_PATH", "/data/app-settings.json")
    with _LOCK:
        if path not in _STORES:
            _STORES[path] = SettingsStore(path)
        return _STORES[path]


def initialize():
    if os.environ.get("HA_INGRESS_ENABLED", "").lower() == "true":
        store().migrate(os.environ.get("ADDON_OPTIONS_PATH", "/data/options.json"))


def get_setting(name, default=""):
    key = next((key for key, env in ENV.items() if env == name), None)
    if key is None:
        return os.environ.get(name, default)
    values = store().snapshot()
    value = effective_skill_endpoint(values) if key == "skill_hostname" else values[key]
    return str(value).lower() if isinstance(value, bool) else value


def saved_secret(name):
    key = next((key for key, env in ENV.items() if env == name), None)
    instance = store()
    if key is not None and instance.values is not None:
        return True, instance.snapshot()[key]
    return False, None
