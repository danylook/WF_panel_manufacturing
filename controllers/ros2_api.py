"""
ROS2 REST API — WF Panel Manufacturing
=======================================
Exposes piece position data from panel sections so ROS2 nodes can
request where to place each piece during assembly.

Base URL: /api/wf/ros2

Endpoints
---------
GET /api/wf/ros2/section/<int:section_id>/pieces
    Returns all piece positions for a panel section.

GET /api/wf/ros2/production/<int:production_id>/pieces
    Returns all piece positions for the panel section linked to an MO.

GET /api/wf/ros2/workorder/<int:workorder_id>/pieces
    Returns all piece positions for the panel section linked to a work order.

POST /api/wf/ros2/workorder/<int:workorder_id>/acknowledge
    Mark a work order as started (state → progress) from ROS2.

Authentication
--------------
Pass an Odoo API key via the ``Authorization: Bearer <api_key>`` header,
or use basic session cookie auth (same as the Odoo web client).
"""

import json
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
}


def _json_response(data, status=200):
    body = json.dumps(data, ensure_ascii=False)
    return request.make_response(
        body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            **_CORS,
        },
        status=status,
    )


def _error(msg, status=400):
    return _json_response({"error": msg}, status=status)


def _authenticate_api_key():
    """
    Check Bearer token against ``res.users.apikeys``.
    Returns the user env if valid, None otherwise.
    """
    auth_header = request.httprequest.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    api_key = auth_header[7:]
    user_id = request.env["res.users.apikeys"]._check_credentials(
        scope="rpc", key=api_key
    )
    if not user_id:
        return None
    return request.env(user=user_id)


def _get_env():
    """Return authenticated env or fall back to current session env."""
    env = _authenticate_api_key()
    if env is not None:
        return env
    # Fallback: use the current session (web client cookie auth)
    return request.env


def _section_pieces(section):
    """Serialize wf.panel.component records for a section."""
    pieces = []
    for comp in section.component_ids.sorted(lambda c: (c.sequence, c.id)):
        pieces.append({
            "id": comp.id,
            "data_id": comp.data_id or "",
            "sequence": comp.sequence,
            "x": comp.x,
            "y": comp.y,
            "length": comp.data_length,
            "width": comp.data_width,
            "depth": comp.data_depth,
            "orientation": comp.data_orientation or "horizontal",
        })
    return pieces


class WFRos2Controller(http.Controller):

    # ------------------------------------------------------------------
    # GET /api/wf/ros2/section/<id>/pieces
    # ------------------------------------------------------------------
    @http.route(
        "/api/wf/ros2/section/<int:section_id>/pieces",
        type="http",
        auth="none",
        methods=["GET", "OPTIONS"],
        csrf=False,
    )
    def get_section_pieces(self, section_id, **_kw):
        if request.httprequest.method == "OPTIONS":
            return _json_response({})

        env = _get_env()
        section = env["wf.panel.section"].sudo().browse(section_id)
        if not section.exists():
            return _error(f"Section {section_id} not found", status=404)

        return _json_response({
            "section_id": section.id,
            "section_name": section.name,
            "panel": section.project_id.name if section.project_id else "",
            "pieces": _section_pieces(section),
        })

    # ------------------------------------------------------------------
    # GET /api/wf/ros2/production/<id>/pieces
    # ------------------------------------------------------------------
    @http.route(
        "/api/wf/ros2/production/<int:production_id>/pieces",
        type="http",
        auth="none",
        methods=["GET", "OPTIONS"],
        csrf=False,
    )
    def get_production_pieces(self, production_id, **_kw):
        if request.httprequest.method == "OPTIONS":
            return _json_response({})

        env = _get_env()
        production = env["mrp.production"].sudo().browse(production_id)
        if not production.exists():
            return _error(f"Production {production_id} not found", status=404)

        section = production.panel_section_id
        if not section:
            return _error(
                f"Production {production_id} has no linked panel section", status=422
            )

        return _json_response({
            "production_id": production.id,
            "production_name": production.name,
            "section_id": section.id,
            "section_name": section.name,
            "panel": section.project_id.name if section.project_id else "",
            "is_assembly_mo": production.wf_is_assembly_mo,
            "pieces": _section_pieces(section),
        })

    # ------------------------------------------------------------------
    # GET /api/wf/ros2/workorder/<id>/pieces
    # ------------------------------------------------------------------
    @http.route(
        "/api/wf/ros2/workorder/<int:workorder_id>/pieces",
        type="http",
        auth="none",
        methods=["GET", "OPTIONS"],
        csrf=False,
    )
    def get_workorder_pieces(self, workorder_id, **_kw):
        if request.httprequest.method == "OPTIONS":
            return _json_response({})

        env = _get_env()
        workorder = env["mrp.workorder"].sudo().browse(workorder_id)
        if not workorder.exists():
            return _error(f"Workorder {workorder_id} not found", status=404)

        production = workorder.production_id
        section = production.panel_section_id if production else None
        if not section:
            return _error(
                f"Workorder {workorder_id} has no linked panel section", status=422
            )

        return _json_response({
            "workorder_id": workorder.id,
            "workorder_name": workorder.name,
            "workorder_state": workorder.state,
            "production_id": production.id,
            "production_name": production.name,
            "section_id": section.id,
            "section_name": section.name,
            "panel": section.project_id.name if section.project_id else "",
            "pieces": _section_pieces(section),
        })

    # ------------------------------------------------------------------
    # POST /api/wf/ros2/workorder/<id>/acknowledge
    # ------------------------------------------------------------------
    @http.route(
        "/api/wf/ros2/workorder/<int:workorder_id>/acknowledge",
        type="http",
        auth="none",
        methods=["POST", "OPTIONS"],
        csrf=False,
    )
    def acknowledge_workorder(self, workorder_id, **_kw):
        """
        ROS2 signals it has received the task for this work order.
        Transitions the work order state from ``pending``/``ready`` → ``progress``.
        Body (JSON, optional): { "robot_id": "robot_1", "note": "..." }
        """
        if request.httprequest.method == "OPTIONS":
            return _json_response({})

        env = _get_env()
        if env.uid is None:
            return _error("Authentication required", status=401)

        workorder = env["mrp.workorder"].sudo().browse(workorder_id)
        if not workorder.exists():
            return _error(f"Workorder {workorder_id} not found", status=404)

        # Parse optional body
        body = {}
        raw = request.httprequest.get_data(as_text=True)
        if raw:
            try:
                body = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                pass

        robot_id = body.get("robot_id", "ROS2")
        note = body.get("note", "")

        if workorder.state not in ("pending", "ready", "waiting"):
            return _json_response({
                "workorder_id": workorder.id,
                "state": workorder.state,
                "message": "Already in progress or done, no change made.",
            })

        try:
            workorder.button_start()
        except Exception as exc:  # noqa: BLE001
            _logger.warning("ROS2 acknowledge: button_start failed: %s", exc)

        # Post chatter note on the MO
        note_parts = [f"ROS2 nodo '{robot_id}' ha reconocido el trabajo: {workorder.name}"]
        if note:
            note_parts.append(note)
        workorder.production_id.message_post(
            body=" — ".join(note_parts),
            subtype_xmlid="mail.mt_note",
        )

        return _json_response({
            "workorder_id": workorder.id,
            "workorder_name": workorder.name,
            "state": workorder.state,
            "message": "Acknowledged",
        })
