"""UI routes — server-rendered pages at root (/", "/peers", etc.)."""
import logging

from flask import Blueprint, render_template

from porpulsion import state

log = logging.getLogger("porpulsion.routes.ui")

bp = Blueprint("ui", __name__)


def _context():
    return {"agent_name": state.AGENT_NAME}


@bp.route("/")
def index():
    return render_template("ui/overview.html", **_context())


@bp.route("/peers")
def peers():
    return render_template("ui/peers.html", **_context())


@bp.route("/workloads")
def workloads():
    return render_template("ui/workloads.html", **_context())


@bp.route("/tunnels")
def tunnels():
    return render_template("ui/tunnels.html", **_context())


@bp.route("/logs")
def logs():
    return render_template("ui/logs.html", **_context())


@bp.route("/settings")
def settings():
    return render_template("ui/settings.html", **_context())


@bp.route("/docs")
def docs():
    return render_template("ui/docs.html", **_context())
