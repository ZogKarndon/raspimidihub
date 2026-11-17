"""OSC routes. Appliance settings: saved immediately, no dirty flag.

GET always answers so Settings can render an unavailable hint when
the transport was not constructed. POST updates the osc config block
and rebinds the socket.
"""

import logging

from ..web import Request, Response
from ._ctx import ApiContext

log = logging.getLogger(__name__)


def register_osc(ctx: ApiContext) -> None:
    server = ctx.server
    config = ctx.config
    osc = ctx.osc
    autosaver = ctx.autosaver

    @server.route(
        "GET", "/api/osc",
        summary="OSC status: listen port, target, and discovered SuperCollider servers.")
    async def api_osc(req: Request) -> Response:
        if osc is None:
            return Response.json({
                "available": False,
                "reason": "no-osc-manager",
                "enabled": False,
                "servers": [],
            })
        return Response.json(osc.status())

    if osc is None:
        return

    @server.route(
        "POST", "/api/osc/settings",
        summary="Update OSC settings (enable, ports, target, manual SuperCollider peers).")
    async def api_osc_settings(req: Request) -> Response:
        err = osc.apply_patch(req.json or {})
        if err:
            return Response.error(err)
        await config.asave()
        if autosaver is not None:
            await autosaver.autosave_now()
        await osc.reconfigure()
        return Response.json(osc.status())
