"""Network reachability status.

The hub is Ethernet-only. This route used to configure an access
point and a Wi-Fi client; those settings are gone. GET stays so an
older UI can see why the Wi-Fi card is not offered.
"""

from ..web import Request, Response
from ._ctx import ApiContext


def register_wifi(ctx: ApiContext) -> None:
    """Register GET /api/wifi — always Ethernet-only, nothing to set up."""
    server = ctx.server

    @server.route(
        "GET", "/api/wifi",
        summary="Report that Wi-Fi is disabled. The hub uses Ethernet only.")
    async def api_wifi_status(req: Request) -> Response:
        return Response.json({
            "available": False,
            "reason": "ethernet-only",
            "mode": "ethernet",
        })
