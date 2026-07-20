"""Chaos / fault injection for the host/responder simulator.

Real switches, acquirers and HSMs misbehave: they go slow, drop messages,
return garbage, or hang up mid-frame. Happy-path loopback testing never
exercises the adapter's timeout, retry and parse-error paths — so this module
lets the :class:`~.responder.TcpResponder` deliberately *inject* those faults.

A **chaos** block can sit at the top level of a responder config (applies to
every reply) and/or inside an individual rule's ``respond`` (overrides the
global block for matching messages). Every fault is probabilistic, and an
optional ``seed`` makes a run fully reproducible.

Schema (all keys optional)::

    "chaos": {
      "seed": 1234,                       # deterministic RNG (omit = random)
      "drop_pct": 10,                     # 10% of replies vanish (client timeout)
      "latency_ms": {"min": 50, "max": 400, "pct": 30},  # or a flat int
      "malformed_pct": 5,
      "malformed_mode": "garbage",        # garbage | bad_mti | bad_length | flip_bits
      "partial_pct": 5,
      "partial_bytes": "half"             # int, or "half" of the frame
    }

Precedence within a single reply: **drop** wins outright (nothing is sent);
otherwise latency is applied, then the frame may be malformed, then it may be
delivered only partially (connection closed mid-frame).

Legacy ``drop: true`` and ``delay_ms`` on a rule's ``respond`` still work and
are honoured by the responder independently of this block.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

# Recognised malformed-frame strategies.
MALFORMED_MODES = ("garbage", "bad_mti", "bad_length", "flip_bits")


@dataclass
class ChaosOutcome:
    """What chaos decided to do to a single reply."""

    drop: bool = False
    latency_ms: float = 0.0
    malformed: str | None = None  # one of MALFORMED_MODES, or None
    partial: bool = False
    partial_bytes: int | str | None = None  # int byte count or "half"
    reasons: list[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        """True if any fault (other than zero latency) was injected."""
        return self.drop or bool(self.malformed) or self.partial or self.latency_ms > 0

    def summary(self) -> str:
        """Short human-readable tag for logs / the portal."""
        return "+".join(self.reasons) if self.reasons else ""


class ChaosEngine:
    """Rolls dice for fault injection, sharing one RNG stream per responder.

    A single engine is created per :class:`TcpResponder` so that a configured
    ``seed`` yields a deterministic, reproducible sequence of faults across the
    whole session. ``plan()`` is called with the *merged* (global + per-rule)
    chaos config for each reply, so different rules can carry different odds
    while still drawing from the same stream.
    """

    def __init__(self, seed: int | None = None) -> None:
        self.rng = random.Random(seed)

    # -- decision ------------------------------------------------------------

    def plan(self, cfg: dict | None) -> ChaosOutcome:
        out = ChaosOutcome()
        if not cfg:
            return out

        # Drop short-circuits everything else: no reply at all.
        if self._hit(cfg.get("drop_pct"), cfg.get("drop")):
            out.drop = True
            out.reasons.append("drop")
            return out

        out.latency_ms = self._latency(cfg.get("latency_ms"))
        if out.latency_ms > 0:
            out.reasons.append(f"latency:{int(out.latency_ms)}ms")

        if self._hit(cfg.get("malformed_pct"), cfg.get("malformed")):
            mode = cfg.get("malformed_mode", "garbage")
            if mode not in MALFORMED_MODES:
                mode = "garbage"
            out.malformed = mode
            out.reasons.append(f"malformed:{mode}")

        if self._hit(cfg.get("partial_pct"), cfg.get("partial")):
            out.partial = True
            out.partial_bytes = cfg.get("partial_bytes", "half")
            out.reasons.append("partial")

        return out

    # -- frame corruption ----------------------------------------------------

    def corrupt(
        self,
        frame: bytes,
        mode: str,
        *,
        prefix_bytes: int = 2,
        byte_order: str = "big",
    ) -> bytes:
        """Return a deliberately broken version of a fully framed reply.

        ``frame`` is ``length-prefix || (tpdu) || body``. Modes:

        - ``garbage``    — replace the body with random bytes of equal length
          (framing stays valid, so the client receives a whole frame it cannot
          parse → decode/parse error).
        - ``flip_bits``  — flip a handful of bits in the body (subtle corruption).
        - ``bad_mti``    — overwrite the first body bytes with ``"9999"`` so the
          MTI is nonsensical (valid frame, wrong message type).
        - ``bad_length`` — inflate the length prefix so the client waits for more
          bytes than will ever arrive (read stall / timeout or stream desync).
        """
        if not frame:
            return frame
        prefix, rest = frame[:prefix_bytes], frame[prefix_bytes:]

        if mode == "bad_length":
            declared = int.from_bytes(prefix, byte_order)
            bogus = (declared + max(8, len(rest))) & ((1 << (prefix_bytes * 8)) - 1)
            return bogus.to_bytes(prefix_bytes, byte_order) + rest

        if mode == "bad_mti":
            if len(rest) >= 4:
                return prefix + b"9999" + rest[4:]
            return prefix + b"9" * len(rest)

        if mode == "flip_bits":
            buf = bytearray(rest)
            if buf:
                n = max(1, len(buf) // 8)
                for _ in range(n):
                    i = self.rng.randrange(len(buf))
                    buf[i] ^= 1 << self.rng.randrange(8)
            return prefix + bytes(buf)

        # default: garbage body, framing untouched
        garbage = bytes(self.rng.randrange(256) for _ in range(len(rest)))
        return prefix + garbage

    def partial_len(self, frame: bytes, spec: int | str | None) -> int:
        """How many bytes of ``frame`` to send before hanging up mid-message."""
        if isinstance(spec, int) and spec >= 0:
            return min(spec, len(frame))
        # "half" (default): send roughly half, but at least one byte so the
        # client definitely sees a truncated/partial read.
        return max(1, len(frame) // 2)

    # -- helpers -------------------------------------------------------------

    def _hit(self, pct: float | int | None, flag: bool | None) -> bool:
        if flag:
            return True
        if pct is None:
            return False
        try:
            p = float(pct)
        except (TypeError, ValueError):
            return False
        if p <= 0:
            return False
        if p >= 100:
            return True
        return self.rng.random() * 100.0 < p

    def _latency(self, spec: int | float | dict | None) -> float:
        if spec is None:
            return 0.0
        if isinstance(spec, dict):
            pct = spec.get("pct")
            if pct is not None and not self._hit(pct, None):
                return 0.0
            lo = float(spec.get("min", 0))
            hi = float(spec.get("max", lo))
            if hi < lo:
                lo, hi = hi, lo
            return self.rng.uniform(lo, hi) if hi > lo else lo
        try:
            return max(0.0, float(spec))
        except (TypeError, ValueError):
            return 0.0
