"""Cached reachability checks from the add-on, without blocking playback/status."""

import ipaddress
import json
import socket
import threading
import time
from urllib.parse import urlparse, urlunparse

import requests
from app_settings import get_setting
from flask import current_app
from markupsafe import escape
from public_urls import public_base, rewrite_url
from shared_store import get_ma
from ui_markup import row as markup_row

_AUDIO_CHECK = "Public audio delivery"

# Public resolvers consulted only when local DNS points inward, so split-DNS
# setups (private local view, public internet view) are judged the way Alexa
# resolves them. Cloudflare first, Google as the fallback.
_DOH_ENDPOINTS = (
    "https://cloudflare-dns.com/dns-query",
    "https://dns.google/resolve",
)

# Split-horizon note (issue #49): a local DNS rewrite (e.g. AdGuard Home)
# makes every probe from this add-on succeed via the LAN while the Echo's
# real path — public DNS, then router hairpin — stays untested.
_INTERNAL_NOTE = (
    "Resolves internally (LAN) — this check does not test the public/Echo path"
)


def targets():
    stream = get_setting("MA_HOSTNAME", "").strip()
    skill = get_setting("SKILL_HOSTNAME", "").strip()
    result = [("Skill endpoint", skill, "skill"), ("Stream host", stream, "host")]
    if skill:
        result.append((_AUDIO_CHECK, skill, "testaudio"))
    latest = get_ma() or {}
    # The store key is streamUrl (ma_routes/devices use it; "url" survives as
    # a fallback for older pushes) — reading only "url" left this row dead.
    url = latest.get("streamUrl") or latest.get("url")
    if url:
        try:
            audio = rewrite_url(url, stream)
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
    """Classify the streamed test-audio GET (see _classify_response)."""
    if code == 200:
        content_type = (response.headers.get("Content-Type") or "").lower()
        if content_type.startswith("audio/"):
            if _read_health_content(response):
                return "reachable", "Public audio delivered over HTTPS (HTTP 200)"
            return (
                "failed",
                f"Test audio returned HTTP {code} with no audio data; check proxy routing",
            )
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
    if kind == "host" and code == 405:
        return "reachable", "HTTPS reachable (HTTP 405; HEAD not supported)"
    if kind == "host" and code == 404:
        return (
            "warning",
            f"HTTPS reachable (HTTP {code}); test an actual audio URL to confirm playback access",
        )
    return (
        "failed",
        f"HTTPS connected, but URL returned HTTP {code}; check proxy routing/access",
    )


def _resolved_addresses(hostname):
    """Local-view addresses for hostname, or None when it does not resolve."""
    try:
        return {
            ipaddress.ip_address(info[4][0].partition("%")[0])
            for info in socket.getaddrinfo(hostname, None)
        }
    except (OSError, ValueError):
        return None


def _public_addresses(hostname):
    """Public-internet addresses via DNS-over-HTTPS; None when every resolver failed.

    Split-DNS setups resolve privately here but publicly for Alexa, so the
    public view — not this add-on's local view — decides whether a pushed
    stream URL points inward. Only consulted when local resolution is
    private, so ordinary setups never send a DoH query.
    """
    for endpoint in _DOH_ENDPOINTS:
        found, failed = _doh_lookup(endpoint, hostname)
        if not failed:
            return found
    return None


def _doh_lookup(endpoint, hostname):
    """(addresses, failed) for one resolver; failed means try the next one."""
    found = set()
    with requests.Session() as doh:
        doh.trust_env = False  # never leak netrc/proxy env to a resolver
        for record_type in ("A", "AAAA"):
            try:
                payload = doh.get(
                    endpoint,
                    params={"name": hostname, "type": record_type},
                    headers={"Accept": "application/dns-json"},
                    timeout=(2, 4),
                ).json()
            except (requests.RequestException, ValueError):
                return found, True
            if payload.get("Status") not in (0, 3):  # NOERROR or NXDOMAIN
                return found, True
            for answer in payload.get("Answer") or []:
                try:
                    found.add(ipaddress.ip_address(answer["data"]))
                except (KeyError, ValueError):
                    pass  # CNAME hops carry a hostname, not an address
    return found, False


def _resolution_views(hostname):
    """(local, public) resolution views for hostname.

    The public (DoH) view is fetched only when local resolution points
    inward, so ordinary setups never send a DoH query. Either entry may be
    None: local when the hostname does not resolve, public when every DoH
    resolver failed.
    """
    local = _resolved_addresses(hostname)
    if not local or all(address.is_global for address in local):
        return local, None  # public (or unresolvable) locally — no DoH query
    return local, _public_addresses(hostname)


def _must_not_probe(local, public):
    """True when the pushed host is inward-only on the public internet too."""
    if not local or all(address.is_global for address in local):
        return False  # public (or unresolvable) locally — the probe reports it
    if public is None:
        return False  # DoH unavailable: probe rather than block
    return not any(address.is_global for address in public)


def _resolves_internally(local, public):
    """True when only the local view points inward (split-horizon DNS).

    A local rewrite (e.g. AdGuard Home) makes this add-on's probes succeed
    via the LAN while the Echo's public path is never exercised, so the
    check must not report a plain pass.
    """
    return (
        bool(local)
        and not all(address.is_global for address in local)
        and public is not None
        and any(address.is_global for address in public)
    )


def _probe_request(value, kind):
    """(url, parsed) for the probe request; ValueError when not public HTTPS."""
    url = public_base(value) if kind != "audio" else value
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Public HTTPS required")
    return url, parsed


def _kind_probe_url(url, clean_url, kind):
    """Purpose-specific path appended for skill health and the test tone."""
    if kind == "skill":
        return url.rstrip("/") + "/health"
    if kind == "testaudio":
        return clean_url.rstrip("/") + "/test-audio.mp3"
    return url


def _downgrade_split_horizon_pass(status, result):
    """A split-horizon pass is not a plain pass: keep it yellow."""
    if status == "reachable" and "note" in result:
        return "warning"
    return status


def probe(label, value, kind, session):
    """Check a public target and classify its HTTPS response for its purpose."""
    result = {"label": label, "status": "unconfigured", "message": "Not configured"}
    if kind == "testaudio":
        result["kind"] = kind
    if not value:
        return result
    try:
        url, parsed = _probe_request(value, kind)
        result["url"] = urlunparse(parsed._replace(query="", fragment=""))
        # One resolution pass feeds both the block guard and the
        # split-horizon note, so a probe never sends a second DoH query.
        local, public = _resolution_views(parsed.hostname)
        if kind == "audio" and _must_not_probe(local, public):
            # Caller-influenced URL: never turn a reachability check into a
            # request toward hosts Alexa itself could not resolve.
            result.update(
                status="warning",
                message=(
                    "Stream URL resolves only to private or internal addresses; "
                    "Alexa cannot fetch it from the internet"
                ),
            )
            return result
        if _resolves_internally(local, public):
            # Still probe (the LAN view is what this add-on can reach), but
            # record why the outcome cannot vouch for the Echo's path.
            result["note"] = _INTERNAL_NOTE
        url = _kind_probe_url(url, result["url"], kind)
        if kind == "testaudio":
            result["url"] = url
        with session.request(
            "GET" if kind in ("skill", "testaudio") else "HEAD",
            url,
            timeout=(3, 5),
            allow_redirects=False,
            stream=True,
        ) as response:
            code = response.status_code
            result["http_status"] = code
            status, message = _classify_response(kind, code, response)
            result.update(
                status=_downgrade_split_horizon_pass(status, result), message=message
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


# Approved row titles (the Connectivity panel renames one probe label).
_ROW_TITLE = {_AUDIO_CHECK: "Audio delivery"}

# Probe status -> (pill label, pill kind).
_PILL = {
    "reachable": ("Reachable", "success"),
    "warning": ("Warning", "warning"),
    "failed": ("Failed", "error"),
    "unconfigured": ("Not configured", "warning"),
}


def html(result):
    """Approved row markup for the Connectivity panel (plus the test-tone player)."""
    parts = []
    for check in result["checks"]:
        label, kind = _PILL.get(check["status"], (check["status"], "neutral"))
        description = escape(check["message"])
        note = check.get("note")
        if note:
            description = f'{description} <span class="muted">{escape(note)}</span>'
        url = check.get("url")
        if url and check["status"] != "reachable":
            description = f'{description} <span class="muted">{escape(url)}</span>'
        parts.append(
            markup_row(
                _ROW_TITLE.get(check["label"], check["label"]),
                description,
                label,
                kind,
            )
        )
        if (
            check.get("kind") == "testaudio"
            and check.get("status") == "reachable"
            and check.get("url")
        ):
            parts.append(
                f'<div><audio controls preload="none" src="{escape(check["url"])}"></audio> '
                f'<span class="muted">Play the test tone through the same public route an Echo uses.</span></div>'
            )
    # No transient "checking" line here: it would grow/shrink the panel on
    # every probe cycle. The in-progress/complete state lives in diagnostics.
    return "".join(parts)


def diagnostics(result):
    """Plain-text block for the approved "Connection diagnostics" disclosure."""
    if not result["checks"] and not result["checking"]:
        return "Reachability checks have not run yet."
    lines = []
    for check in result["checks"]:
        outcome = (
            f"HTTP {check['http_status']}"
            if "http_status" in check
            else check["status"]
        )
        line = f"{check['label']}: {outcome} — {check['message']}"
        if check.get("note"):
            line += f" [{check['note']}]"
        if check.get("url"):
            line += f" ({check['url']})"
        lines.append(line)
    # Persistent status line (in progress / complete) so the checking state is
    # always visible here without resizing the Connectivity panel above.
    lines.append(
        "Status: checks in progress…"
        if result["checking"]
        else "Status: checks complete"
    )
    lines.append(result["perspective"])
    return "\n".join(lines)


def summary(result):
    """Readiness banner state derived from the reachability checks."""
    checks = {check["label"]: check["status"] for check in result["checks"]}
    skill = checks.get("Skill endpoint")
    audio = checks.get(_AUDIO_CHECK)
    if result["checking"] and not result["checked_at"]:
        return {
            "kind": "neutral",
            "title": "Checking public reachability…",
            "description": "Running the add-on reachability checks.",
        }
    if skill == "reachable" and audio == "reachable":
        return {
            "kind": "good",
            "title": "Service is ready for an Echo test",
            "description": (
                "The add-on and public audio route are reachable. "
                "Play a track on your Echo to verify end-to-end playback."
            ),
        }
    if skill in (None, "unconfigured") or audio in (None, "unconfigured"):
        return {
            "kind": "warning",
            "title": "Public URLs need attention",
            "description": (
                "Set your public skill URL in Setup so reachability checks "
                "and the Echo test can run."
            ),
        }
    attention = [check for check in result["checks"] if check["status"] != "reachable"]
    return {
        "kind": "error",
        "title": "Not ready for an Echo test",
        "description": (
            f"{len(attention)} of {len(result['checks'])} reachability checks "
            "need attention. Review Connectivity below."
        ),
    }
