"""Cached reachability checks from the add-on, without blocking playback/status."""

import json
import threading
import time
from urllib.parse import urlparse, urlunparse

import requests
from app_settings import get_setting
from flask import current_app
from markupsafe import escape
from public_urls import public_base, rewrite_url
from shared_store import get_ma


def targets():
    stream = get_setting("MA_HOSTNAME", "").strip()
    skill = get_setting("SKILL_HOSTNAME", "").strip()
    result = [("Skill endpoint", skill, "skill"), ("Stream host", stream, "host")]
    if skill:
        result.append(("Public audio delivery", skill, "testaudio"))
    latest = get_ma() or {}
    if latest.get("url"):
        try:
            audio = rewrite_url(latest["url"], stream)
        except (ValueError, TypeError):
            audio = "invalid:"
        result.append(("Current audio", audio, "audio"))
    return result


def _read_health_content(response):
    """Read at most 4096 bytes of a probe response body."""
    content = bytearray()
    for chunk in response.iter_content(1024):
        content.extend(chunk)
        if len(content) > 4096:
            break
    return content


def _classify_testaudio(code, response):
    """Classify the static test-audio probe (see _classify_response)."""
    if 200 <= code < 300:
        content_type = (response.headers.get("Content-Type") or "").lower()
        if content_type.startswith("audio/"):
            return "reachable", f"Public audio delivered over HTTPS (HTTP {code})"
        return (
            "warning",
            f"HTTPS reachable (HTTP {code}), but the response is not audio; check proxy routing",
        )
    return (
        "failed",
        f"Test audio returned HTTP {code}; check proxy routing to the add-on",
    )


def _classify_response(kind, code, response):
    """Map a completed probe response to a (status, message) pair.

    Runs inside probe()'s try block, so JSON decoding errors propagate to
    the same handlers as before.
    """
    if kind == "skill":
        if code != 200:
            return (
                "failed",
                f"HTTPS connected, but URL returned HTTP {code}; check proxy routing/access",
            )
        content = _read_health_content(response)
        payload = json.loads(content) if len(content) <= 4096 else None
        if isinstance(payload, dict) and payload.get("status") == "ok":
            return "reachable", "HTTPS reachable; add-on health confirmed"
        return (
            "warning",
            "HTTPS reachable, but response does not identify the add-on",
        )
    if kind == "testaudio":
        return _classify_testaudio(code, response)
    if 200 <= code < 300:
        return "reachable", f"HTTPS reachable (HTTP {code})"
    if kind == "host" and code in (404, 405):
        return (
            "warning",
            f"HTTPS reachable (HTTP {code}); test an actual audio URL to confirm playback access",
        )
    return (
        "failed",
        f"HTTPS connected, but URL returned HTTP {code}; check proxy routing/access",
    )


def probe(label, value, kind, session):
    result = {"label": label, "status": "unconfigured", "message": "Not configured"}
    if kind == "testaudio":
        result["kind"] = kind
    if not value:
        return result
    try:
        url = public_base(value) if kind != "audio" else value
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Public HTTPS required")
        result["url"] = urlunparse(parsed._replace(query="", fragment=""))
        if kind == "skill":
            url = url.rstrip("/") + "/health"
        if kind == "testaudio":
            url = result["url"].rstrip("/") + "/test-audio.mp3"
            result["url"] = url
        with session.request(
            "GET" if kind == "skill" else "HEAD",
            url,
            timeout=(3, 5),
            allow_redirects=False,
            stream=True,
        ) as response:
            code = response.status_code
            result["http_status"] = code
            status, message = _classify_response(kind, code, response)
            result.update(status=status, message=message)
    except requests.exceptions.SSLError:
        result.update(status="failed", message="TLS certificate validation failed")
    except requests.exceptions.Timeout:
        result.update(status="failed", message="Connection or response timed out")
    except requests.RequestException:
        result.update(status="failed", message="DNS lookup or connection failed")
    except (ValueError, TypeError):
        result.update(
            status="failed", message="Invalid HTTPS URL or unexpected response"
        )
    return result


class Reachability:
    def __init__(self):
        self.lock = threading.Lock()
        self.key = None
        self.checked = 0
        self.results = []
        self.thread = None

    def snapshot(self):
        configured = targets()
        with self.lock:
            if self.key != configured:
                self.key, self.checked, self.results = configured, 0, []
            if (
                not self.thread or not self.thread.is_alive()
            ) and time.time() - self.checked > 60:
                self.thread = threading.Thread(
                    target=self.check, args=(configured,), daemon=True
                )
                self.thread.start()
            return {
                "checked_at": self.checked or None,
                "checking": self.thread.is_alive(),
                "checks": list(self.results),
                "perspective": "Checked from this add-on; Amazon/Echo access still requires a device test.",
            }

    def check(self, configured):
        with requests.Session() as session:
            session.trust_env = False  # never send netrc credentials to public probes
            results = [probe(*item, session) for item in configured]
        with self.lock:
            if self.key == configured:
                self.results, self.checked = results, time.time()


def snapshot():
    # Called by Flask in a request context, but probes run outside it.
    checker = current_app.extensions.setdefault("reachability", Reachability())
    return checker.snapshot()


def html(result):
    parts = []
    colors = {
        "reachable": "green",
        "warning": "yellow",
        "failed": "red",
        "unconfigured": "yellow",
    }
    for check in result["checks"]:
        parts.append(
            f'<div><span class="led {colors[check["status"]]}"></span> {escape(check["label"])}: {escape(check["message"])} <span class="muted">{escape(check.get("url", ""))}</span></div>'
        )
        if (
            check.get("kind") == "testaudio"
            and check.get("status") == "reachable"
            and check.get("url")
        ):
            parts.append(
                f'<div><audio controls preload="none" src="{escape(check["url"])}"></audio> <span class="muted">Play the test tone through the same public route an Echo uses.</span></div>'
            )
    if result["checking"]:
        parts.append("<div>Checking public URLs…</div>")
    parts.append(f'<div class="muted">{escape(result["perspective"])}</div>')
    return "".join(parts)
