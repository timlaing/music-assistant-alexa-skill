"""Shared top navigation for the server-rendered UI pages."""

from pathlib import Path

from markupsafe import Markup

TEMPLATE = Path(__file__).parent / "templates" / "_nav.html"


def nav_html(current):
    """Return the shared navigation with the current page marked."""
    markup = TEMPLATE.read_text()
    marker = f'href="/{current}"'
    return Markup(markup.replace(marker, f'{marker} aria-current="page"', 1))
