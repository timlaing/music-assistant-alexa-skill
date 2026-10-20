"""Approved UI markup helpers shared by the status fragments.

Server-rendered fragments (rows, pills, timeline entries, banners) use the
same class names as the approved design so assets/ui.css styles them.
Descriptions passed to row() are treated as HTML and must already be
escaped by the caller; titles, pill labels and banner text are escaped here.
"""

from markupsafe import escape

_KIND_CLASS = {"success": "ma-ok", "error": "ma-error", "warning": "ma-warning"}

_BANNER_ICON = {
    "good": "check_circle",
    "warning": "alert_circle",
    "error": "alert_triangle",
    "neutral": "clock",
}

_SVG_ATTRS = (
    'width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"'
)

ICONS = {
    "check_circle": (
        '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/>'
        '<polyline points="22 4 12 14.01 9 11.01"/>'
    ),
    "alert_circle": (
        '<circle cx="12" cy="12" r="10"/>'
        '<line x1="12" y1="8" x2="12" y2="12"/>'
        '<line x1="12" y1="16" x2="12.01" y2="16"/>'
    ),
    "alert_triangle": (
        '<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 '
        '3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/>'
        '<line x1="12" y1="17" x2="12.01" y2="17"/>'
    ),
    "clock": '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
    "cloud_upload": (
        '<polyline points="16 16 12 12 8 16"/><line x1="12" y1="12" x2="12" y2="21"/>'
        '<path d="M20.39 18.39A5 5 0 0 0 18 9h-1.26A8 8 0 1 0 3 16.3"/>'
    ),
    "message_square": (
        '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>'
    ),
}


def icon(name):
    """Inline SVG for one of ICONS (feather-style, stroke = currentColor)."""
    return f"<svg {_SVG_ATTRS}>{ICONS[name]}</svg>"


def banner_icon(kind):
    return icon(_BANNER_ICON.get(kind, "clock"))


def pill(label, kind="neutral"):
    cls = _KIND_CLASS.get(kind, "")
    return (
        f'<span class="ma-state {cls}">'
        f'<span class="ma-dot" aria-hidden="true"></span>{escape(label)}</span>'
    )


def row(title, description_html, label, kind="neutral"):
    """One approved row: title, description (pre-escaped HTML) and a state pill."""
    description = f"<p>{description_html}</p>" if description_html else ""
    return (
        f'<div class="ma-row"><div><strong>{escape(title)}</strong>{description}</div>'
        f"{pill(label, kind)}</div>"
    )


def timeline(icon_name, title, time_label, description):
    """One approved activity-timeline entry with its icon tile."""
    return (
        f'<div class="ma-timeline"><span class="ma-timeicon">{icon(icon_name)}</span>'
        f'<div>{escape(title)}<div class="ma-small ma-muted">'
        f"{escape(time_label)} · {escape(description)}</div></div></div>"
    )


def banner(kind, title, description):
    """Readiness banner with the icon tile; data-summary lets the client detect change."""
    return (
        f'<div class="ma-banner" id="status-banner" data-kind="{escape(kind)}" '
        f'data-summary="{escape(kind + ":" + title + ":" + description)}" '
        'role="status" aria-live="polite">'
        f'<span class="ma-icon">{banner_icon(kind)}</span>'
        f"<div><strong>{escape(title)}</strong><p>{escape(description)}</p></div></div>"
    )
