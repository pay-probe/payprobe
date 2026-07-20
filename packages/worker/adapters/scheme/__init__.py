"""Payment-scheme simulators — scheme-flavored host responders.

Each module here subclasses :class:`~worker.adapters.tcp.responder.TcpResponder`
and ships the message flows, response-code logic and (optionally) the card
cryptography of a specific card scheme, so PayProbe can stand in for the scheme
network during loopback testing.

Currently:

* :mod:`.visa` — a VISA Base I-style authorization/clearing simulator.
"""

from __future__ import annotations

from .visa import VisaSimulator

__all__ = ["VisaSimulator"]
