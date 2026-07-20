"""Run a PayProbe host/responder simulator from a JSON config.

    python -m worker.responder examples/simulators/switch_responder.json

Stands in for a switch / acquirer / issuer / HSM so scenarios can run against a
simulated host. See ``worker/adapters/tcp/responder.py`` for the config schema.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys

from worker.adapters.tcp.responder import TcpResponder


def _make_responder(config: dict) -> TcpResponder:
    """payShield HSM simulator for ``protocol``/``kind`` == ``payshield``,
    otherwise the generic rules-driven host responder."""
    kind = str(config.get("protocol") or config.get("kind") or "").lower()
    if kind == "payshield":
        from worker.adapters.hsm.payshield import PayShieldSimulator

        return PayShieldSimulator(config)
    if kind == "visa":
        from worker.adapters.scheme.visa import VisaSimulator

        return VisaSimulator(config)
    if kind == "cybersource":
        from worker.adapters.scheme.cybersource import CyberSourceSimulator

        return CyberSourceSimulator(config)
    return TcpResponder(config)


async def _serve(config: dict) -> None:
    responder = _make_responder(config)
    port = await responder.start()
    logging.info(
        "Responder (%s) listening on %s:%s",
        config.get("protocol", "iso8583"),
        config.get("host", "127.0.0.1"),
        port,
    )
    try:
        await responder.serve_forever()
    except asyncio.CancelledError:
        pass
    finally:
        await responder.stop()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print(__doc__)
        return 2
    config = json.loads(open(argv[0]).read())
    try:
        asyncio.run(_serve(config))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
