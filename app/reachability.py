"""Cached reachability checks from the add-on, without blocking playback/status."""

import json
import os
import threading
import time
from urllib.parse import urlparse, urlunparse

import requests
from flask import current_app
from markupsafe import escape
from public_urls import public_base, rewrite_url
from shared_store import get_ma


def targets():
    stream = os.environ.get("MA_HOSTNAME", "").strip()
    skill = os.environ.get("SKILL_HOSTNAME", "").strip()
    result = [("Skill endpoint", skill, "skill"), ("Stream host", stream, "host")]
    latest = get_ma() or {}
    if latest.get("url"):
        result.append(("Current audio", rewrite_url(latest["url"], stream), "audio"))
    return result


def probe(label, value, kind, session):
    result = {"label": label, "status": "unconfigured", "message": "Not configured"}
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
        with session.request(
            "GET" if kind == "skill" else "HEAD",
            url,
            timeout=(3, 5),
            allow_redirects=False,
            stream=True,
        ) as response:
            code = response.status_code
            result["http_status"] = code
            if kind == "skill" and code == 200:
                content = bytearray()
                for chunk in response.iter_content(1024):
                    content.extend(chunk)
                    if len(content) > 4096:
                        break
                if (
                    len(content) <= 4096
                    and isinstance(json.loads(content), dict)
                    and json.loads(content).get("status") == "ok"
                ):
                    result.update(
                        status="reachable",
                        message="HTTPS reachable; add-on health confirmed",
                    )
                else:
                    result.update(
                        status="warning",
                        message="HTTPS reachable, but response does not identify the add-on",
                    )
            elif kind != "skill" and 200 <= code < 300:
                result.update(
                    status="reachable", message=f"HTTPS reachable (HTTP {code})"
                )
            elif kind == "host" and code in (404, 405):
                result.update(
                    status="warning",
                    message=f"HTTPS reachable (HTTP {code}); test an actual audio URL to confirm playback access",
                )
            else:
                result.update(
                    status="failed",
                    message=f"HTTPS connected, but URL returned HTTP {code}; check proxy routing/access",
                )
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
    if result["checking"]:
        parts.append("<div>Checking public URLs…</div>")
    parts.append(f'<div class="muted">{escape(result["perspective"])}</div>')
    return "".join(parts)
