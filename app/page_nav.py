"""Shared application header for the server-rendered UI pages."""

from flask import render_template
from markupsafe import Markup

NAV_ITEMS = (
    ("status", "Status"),
    ("setup", "Setup"),
    ("devices", "Devices"),
    ("invocations", "Invocations"),
    ("simulator", "Simulator"),
)


def nav_html(current):
    """Return the shared header/navigation with the current page marked."""
    return Markup(render_template("_nav.html", current=current, nav_items=NAV_ITEMS))
