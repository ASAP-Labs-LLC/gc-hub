"""v5.0 lane R: the Results, Settings and Help pages (Blueprint).

Pages only, all session-gated like every page (``web_auth.route_class``):

    GET /results    every sample's results: grouped D2887/D86 headers, key or
                    all points, filters in the query (read by the page), CSV,
                    distillation-curve overlay (templates/results.html)
    GET /settings   the settings, grouped by who changes them
                    (templates/settings.html)
    GET /help       a short plain-language help page (templates/help.html)

They read and change nothing on the server themselves: the pages call the
existing ``/api/`` routes (``/api/table``, ``/api/files``,
``/api/samples/<id>/distillation-curve``, ``/api/settings``,
``/api/standards``, ``/api/comparison-standard*``,
``/api/qbench-api-credentials``, ``/api/qbench-credentials``,
``/api/notifications``) with their existing rules.
"""
from __future__ import annotations

from flask import Blueprint, render_template

import version

bp = Blueprint("results_pages", __name__)


def _page(template: str, nav: str):
    return render_template(template, app_version=version.APP_VERSION, nav=nav)


@bp.route("/results", methods=["GET"])
def results_page():
    return _page("results.html", "results")


@bp.route("/settings", methods=["GET"])
def settings_page():
    return _page("settings.html", "settings")


@bp.route("/help", methods=["GET"])
def help_page():
    return _page("help.html", "help")
