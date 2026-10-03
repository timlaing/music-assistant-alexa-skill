"""Public HTTPS URL normalization shared by stream and artwork handling."""

from urllib.parse import quote, urlsplit, urlunsplit


def public_base(value):
    value = (value or "").strip().strip("\"'").rstrip("/")
    if not value:
        return ""
    if "://" not in value:
        value = "https://" + value
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Expected a public HTTPS base URL")
    _ = parsed.port  # Validate the optional port.
    return urlunsplit(("https", parsed.netloc, parsed.path.rstrip("/"), "", ""))


def rewrite_url(url, base):
    if not url:
        return url
    if not isinstance(url, str):
        raise TypeError("URL must be a string")
    source = urlsplit(url)
    if source.scheme not in ("http", "https") or not source.hostname:
        raise ValueError("Expected an HTTP or HTTPS source URL")
    target = urlsplit(public_base(base)) if base else source
    prefix = target.path.rstrip("/") if base else ""
    path = source.path
    if prefix and not (
        source.netloc == target.netloc
        and (path == prefix or path.startswith(prefix + "/"))
    ):
        path = prefix + "/" + path.lstrip("/")
    return urlunsplit(
        (
            target.scheme,
            target.netloc,
            quote(path, safe="/:%@!$&'()*+,;=-._~"),
            source.query,
            source.fragment,
        )
    )
