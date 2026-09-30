"""Advertise only a listening workstation, and withdraw it on shutdown."""
from __future__ import annotations

import asyncio
import logging


def run_discoverable(app, *, host: str, port: int, cert_file, key_file,
                     hostname: str, name: str) -> None:
    import uvicorn
    from .discovery import Advertiser, Candidate, advertised_addresses

    candidate = Candidate(server_id=app.state.remote_receipts.server_id,
                          name=name, hostname=hostname, port=port,
                          addresses=advertised_addresses(host))
    advertiser = Advertiser(candidate)

    class NearbyServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            if self.started:
                try:
                    await asyncio.to_thread(advertiser.start)
                except Exception:
                    logging.getLogger("donedatahoarder.remote").warning(
                        "Nearby discovery is unavailable. The HTTPS listener remains available for manual connections.")

        async def shutdown(self, sockets=None):
            try:
                await asyncio.to_thread(advertiser.close)
            finally:
                await super().shutdown(sockets=sockets)

    config = uvicorn.Config(app, host=host, port=port, workers=1,
                            log_level="warning", access_log=False, proxy_headers=False,
                            ssl_certfile=str(cert_file), ssl_keyfile=str(key_file))
    try:
        NearbyServer(config).run()
    finally:
        advertiser.close()
