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


# A stalled system resolver must not hold the check thread — and the status
# page — indefinitely (issue #49 review). A timed-out lookup is distinct from
# one that simply does not resolve: the former must not be probed, because the
# request's own DNS lookup could hang or resolve inward.
_DNS_TIMEOUT_SECONDS = 5.0
_DNS_TIMED_OUT = object()  # sentinel: the resolver stalled past its bound

# One in-flight worker per hostname so a stalled resolver is reused, not
# re-spawned on every check run (which would slowly leak blocked daemon
# threads). Only a still-alive worker is reused; a completed one drops out so
# the next run re-resolves fresh.
_dns_lock = threading.Lock()
_dns_inflight = {}


def _resolved_addresses(hostname):
    """Local-view addresses, None when unresolved, or ``_DNS_TIMED_OUT`` if stalled.

    ``socket.getaddrinfo`` runs in a worker thread so a stalled system resolver
    degrades to a bounded timeout instead of hanging the check.
    """
    with _dns_lock:
        entry = _dns_inflight.get(hostname)
        if entry is not None and entry[0].is_alive():
            worker, found = entry  # reuse the in-flight (possibly stalled) lookup
        else:
            found = {}

            def _lookup():
                try:
                    found["addrs"] = {
                        ipaddress.ip_address(info[4][0].partition("%")[0])
                        for info in socket.getaddrinfo(hostname, None)
                    }
                except (OSError, ValueError):
                    found["addrs"] = None

            worker = threading.Thread(target=_lookup, daemon=True)
            _dns_inflight[hostname] = (worker, found)
            worker.start()
    worker.join(_DNS_TIMEOUT_SECONDS)
    if worker.is_alive():
        return _DNS_TIMED_OUT  # keep the entry so later runs reuse this worker
    with _dns_lock:
        if _dns_inflight.get(hostname, (None,))[0] is worker:
            del _dns_inflight[hostname]  # done: the next run re-resolves fresh
    return found.get("addrs")


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


def _parse_doh_payload(payload):
    """(answers, failed) for one DoH response; failed means try the next resolver.

    NOERROR or NXDOMAIN is a valid answer. A non-object JSON body, a bad
    status, or a malformed Answer collection all count as a resolver failure
    so the fallback resolver still runs (issue #49 review) — never an
    AttributeError that aborts the whole check run before it can commit.
    """
    if not isinstance(payload, dict) or payload.get("Status") not in (0, 3):
        return None, True
    answers = payload.get("Answer")
    if answers is None:
        return [], False
    if not isinstance(answers, list):
        return None, True
    return answers, False


def _collect_doh_addresses(found, answers):
    """Add every parseable address in answers to found, skipping CNAME hops."""
    for answer in answers:
        if not isinstance(answer, dict):
            continue  # stray non-object entry
        try:
            found.add(ipaddress.ip_address(answer["data"]))
        except (KeyError, ValueError, TypeError):
            pass  # CNAME hops carry a hostname, not an address


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
            answers, failed = _parse_doh_payload(payload)
            if failed:
                return found, True
            _collect_doh_addresses(found, answers)
    return found, False


# Suffixes (plus single-label names and IP literals, handled in
# _could_have_public_view) that the public internet cannot resolve to a
# routable address. A DoH query about such a name can never reveal a
# split-horizon public view — it would only leak an internal hostname (or
# address) to a public resolver (issue #49 review).
_LAN_ONLY_SUFFIXES = (
    ".local",  # mDNS, e.g. homeassistant.local
    ".localdomain",
    ".lan",
    ".home",
    ".home.arpa",
    ".internal",
    ".intranet",
    ".private",
    ".corp",
    ".localhost",
    ".homenetwork",
    ".test",  # IANA-reserved (RFC 6761): never publicly routable
    ".invalid",
    ".onion",  # Tor
    # ".example" is RFC-reserved too, but the test suite uses it as the mock
    # public namespace (e.g. streams.example), so it is deliberately not
    # blocked here.
)


def _could_have_public_view(hostname):
    """False for names only the LAN can resolve, so no DoH query leaks them."""
    host = hostname.partition("%")[0]
    try:
        ipaddress.ip_address(host)
        return False  # IP literal: nothing to resolve, and DoH would leak it
    except ValueError:
        pass
    try:
        socket.inet_aton(host)
        return False  # legacy IPv4 spelling (e.g. "127.1") resolvers accept
    except OSError:
        pass
    name = host.lower().rstrip(".")
    if "." not in name:
        return False  # single-label (e.g. "music-assistant") is LAN-only
    return not name.endswith(_LAN_ONLY_SUFFIXES)


def _resolution_views(hostname, cache=None):
    """(local, public) resolution views for hostname.

    The public (DoH) view is fetched only when local resolution points
    inward, so ordinary setups never send a DoH query. Either entry may be
    None: local when the hostname does not resolve, public when every DoH
    resolver failed. When ``cache`` is supplied, each hostname's views are
    computed once and reused for the rest of the check run, so checks that
    share a hostname do not repeat the local and DoH lookups.
    """
    if cache is not None and hostname in cache:
        return cache[hostname]
    local = _resolved_addresses(hostname)
    if local is _DNS_TIMED_OUT:
        # A stalled resolver is not "unresolved": probing would send a second
        # DNS lookup that can hang or resolve inward (issue #49 review).
        views = _DNS_TIMED_OUT, None
    elif not local or all(address.is_global for address in local):
        views = local, None  # public (or unresolvable) locally — no DoH query
    elif not _could_have_public_view(hostname):
        # LAN-only name: it cannot have a public view, so record an empty one
        # — not None, which means "DoH unavailable" and would let an audio
        # target past the must-not-probe guard (issue #49 review).
        views = local, set()
    else:
        views = local, _public_addresses(hostname)
    if cache is not None:
        cache[hostname] = views
    return views


def _must_not_probe(local, public):
    """True when the pushed host is inward-only on the public internet too."""
    if not local or all(address.is_global for address in local):
        return False  # public (or unresolvable) locally — the probe reports it
    if public is None:
        return False  # DoH unavailable: probe rather than block
    return not any(address.is_global for address in public)


def _resolves_internally(local, public):
    """True when only the local view points inward (split-horizon or LAN-only).

    A local rewrite (e.g. AdGuard Home) makes this add-on's probes succeed
    via the LAN while the Echo's public path is never exercised, so the
    check must not report a plain pass. An empty public view — a LAN-only
    name that cannot resolve publicly at all — is even more clearly
    internal-only, so it is flagged too (issue #49 review).
    """
    return (
        bool(local)
        and not all(address.is_global for address in local)
        and public is not None
        and (any(address.is_global for address in public) or not public)
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


def _request_and_classify(session, kind, url, result):
    """Send the probe request for kind and record its classified outcome.

    A split-horizon pass is downgraded to a warning but keeps its
    ``downgraded`` flag, so the test-tone player survives only for a real
    pass (issue #48/#49 review).
    """
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
        was_reachable = status == "reachable"
        result.update(
            status=_downgrade_split_horizon_pass(status, result), message=message
        )
        if was_reachable and result["status"] == "warning":
            # Bytes/route were confirmed; only the public path is unverified,
            # so the test-tone player may stay (issue #48 review). A non-audio
            # warning never set this flag.
            result["downgraded"] = True


def probe(label, value, kind, session, views_cache=None):
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
        local, public = _resolution_views(parsed.hostname, views_cache)
        if local is _DNS_TIMED_OUT:
            # A stalled resolver is not "unresolved": do not send a second DNS
            # lookup that can hang or resolve inward (issue #49 review).
            result.update(
                status="failed",
                message="DNS lookup timed out; could not confirm the host resolves publicly",
            )
            return result
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
        _request_and_classify(session, kind, url, result)
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
        # One resolution view per hostname for the whole run: the skill and
        # test-tone checks share a host, as do the stream-host and current-
        # audio checks, so caching avoids repeating slow DoH lookups.
        views_cache = {}
        with requests.Session() as session:
            session.trust_env = False  # never send netrc credentials to public probes
            results = [
                probe(*item, session, views_cache=views_cache) for item in configured
            ]
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
        # Keep the test-tone player only for a real pass: a reachable check,
        # or a split-horizon pass downgraded to a warning (the bytes were
        # confirmed; only the public path is unverified). A non-audio warning
        # never verified audio, so it gets no player (issue #48 review).
        testaudio = check.get("kind") == "testaudio" and check.get("url")
        if testaudio and (
            check.get("status") == "reachable" or check.get("downgraded")
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


def _counts_ready(check):
    """True when a check passed: plainly, or as a downgraded split-horizon pass.

    A split-horizon pass is downgraded to a warning but did confirm the
    service, so it must not trip the not-ready error (issue #49 review). A
    genuine warning (e.g. non-audio response, HTTP error) has no downgraded
    flag and does not count.
    """
    if not check:
        return False
    return check["status"] == "reachable" or check.get("downgraded") is True


def summary(result):
    """Readiness banner state derived from the reachability checks."""
    by_label = {check["label"]: check for check in result["checks"]}
    skill = by_label.get("Skill endpoint")
    audio = by_label.get(_AUDIO_CHECK)
    if result["checking"] and not result["checked_at"]:
        return {
            "kind": "neutral",
            "title": "Checking public reachability…",
            "description": "Running the add-on reachability checks.",
        }
    if _counts_ready(skill) and _counts_ready(audio):
        failed = [check for check in result["checks"] if check["status"] == "failed"]
        if failed:
            # A failed check outweighs the internal-resolution note: report it
            # rather than the softer note-only warning that would mask it
            # (issue #49 review).
            return {
                "kind": "warning",
                "title": "Some checks need attention",
                "description": (
                    f"{len(failed)} of {len(result['checks'])} reachability "
                    "checks failed even though the skill endpoint and audio "
                    "route are reachable. Review Connectivity below."
                ),
            }
        if any(check.get("note") for check in result["checks"]):
            # Skill + audio are green, but another check resolved only via the
            # LAN, so part of the Echo path is unverified (issue #49 review).
            return {
                "kind": "warning",
                "title": "Some checks resolve internally",
                "description": (
                    "The skill endpoint and audio route are reachable, but at "
                    "least one check resolved to a LAN address, so its "
                    "public/Echo path was not tested. Play a track on your "
                    "Echo to confirm end-to-end playback."
                ),
            }
        return {
            "kind": "good",
            "title": "Service is ready for an Echo test",
            "description": (
                "The add-on and public audio route are reachable. "
                "Play a track on your Echo to verify end-to-end playback."
            ),
        }
    skill_status = skill["status"] if skill else None
    audio_status = audio["status"] if audio else None
    if skill_status in (None, "unconfigured") or audio_status in (None, "unconfigured"):
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
