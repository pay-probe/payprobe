"""Per-transaction data generators for load + scenario variation.

Lets payload fields vary every time a scenario runs by referencing a generator
namespace inside the usual ``${...}`` syntax. Without this the load driver
replays one scenario verbatim, so every transaction carries the *same*
PAN/STAN/RRN — which most switches reject as duplicates, making the test
meaningless. The supported tokens:

    ${uuid}                  -> a random 32-hex id
    ${seq.stan}              -> 1, 2, 3, ...            (a named monotonic counter)
    ${seq.stan(6)}           -> "000001", "000002", ... (zero-padded, wraps mod 10**6)
    ${seq.ksn}               -> "303950591234567800000000", ... (per-txn DUKPT KSN:
                                a counter in the low 8 hex of a 24-hex KSN; mirrors TestPay)
    ${seq.ksn(<base16hex>)}  -> ... with a custom 16-hex initial-key-id base
    ${rand.int(1,100)}       -> a random integer in [1, 100]
    ${rand.amount(100,5000)} -> a random minor-unit amount in [100, 5000]
    ${rand.digits(4)}        -> e.g. "0497"             (n random decimal digits)
    ${rand.hex(8)}           -> e.g. "9af3c012"         (n random hex chars)
    ${rand.pan(411111)}      -> a Luhn-valid PAN with that BIN, 16 digits
    ${rand.pan(411111,19)}   -> ... with a custom length
    ${now.rrn}               -> a 12-digit retrieval reference number
    ${now.epoch}             -> unix seconds
    ${now.iso}               -> ISO-8601 UTC timestamp
    ${pool.terminal}         -> next terminal id from the run's terminal pool
    ${pool.card}             -> next entry from the run's card pool

A *card pool* entry may be a bare PAN string or a structured record::

    {"pan": "...", "expiry": "2812", "cvv": "123", "type": "visa", "track2": "..."}

The ``card`` namespace exposes one card *bound for the whole transaction*, so the
PAN, expiry, track2 and brand a single message carries all describe the same card
(mirroring TestPay's ``TestDataGenerator``, which derived every variation from one
card record). A fresh card is drawn for each transaction:

    ${card.pan}              -> the bound card's PAN
    ${card.expiry}           -> yyMM expiry (default 2812 if unspecified)
    ${card.cvv}              -> CVV/CVV2 (default 000)
    ${card.type} / .brand    -> visa | mastercard | amex | ... (derived from the PAN if unset)
    ${card.track2}           -> Track 2 (given, or built as "<pan>D<expiry>101")
    ${card.seq}              -> 1-based index of the card bound to this transaction

The whole point is *one* :class:`GeneratorContext` per worker shard, shared
across every transaction, so counters advance and the RNG is reproducible
(seeded from run id + worker index). The functional engine creates a default
context per run, so generators also work in ordinary (non-load) scenarios.
:meth:`GeneratorContext.new_transaction` is called by the runner at the start of
each top-level transaction to rotate the transaction-bound card.
"""

from __future__ import annotations

import random
import time
from datetime import datetime, timezone
from typing import Any

#: Reserved leading tokens that route a ``${...}`` reference to a generator
#: rather than to a prior step's output. A scenario step must not be named after
#: one of these.
GENERATOR_NAMESPACES = frozenset({"rand", "seq", "uuid", "now", "pool", "card"})

#: Default 16-hex DUKPT initial-key-id base for ``${seq.ksn}`` (TestPay's value).
_DEFAULT_KSN_BASE = "3039505912345678"


class GeneratorError(KeyError):
    """A generator token was malformed or referenced an empty pool.

    Subclasses :class:`KeyError` so existing ``UnresolvedReferenceError``
    handling around variable resolution treats it the same way (surfaced as a
    clear ``last_error`` instead of crashing the worker)."""


def luhn_check_digit(number: str) -> str:
    """The Luhn check digit that makes ``number + digit`` a valid card number."""
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2 == 0:  # doubled positions (the check digit will sit at position 1)
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - (total % 10)) % 10)


def _brand_from_pan(pan: str) -> str:
    """Card brand inferred from the PAN's leading digit(s)."""
    if not pan:
        return "unknown"
    head = pan[0]
    if head == "4":
        return "visa"
    if head == "5" or pan[:2] in {"22", "23", "24", "25", "26", "27"}:
        return "mastercard"
    if pan[:2] in {"34", "37"}:
        return "amex"
    if head == "6":
        return "discover"
    return "unknown"


def _split_call(rest: str) -> tuple[str, list[str]]:
    """Split ``pan(411111,19)`` -> ("pan", ["411111", "19"]); ``stan`` -> ("stan", [])."""
    rest = rest.strip()
    if "(" in rest:
        name, _, tail = rest.partition("(")
        tail = tail.rstrip(") ")
        args = [a.strip() for a in tail.split(",") if a.strip() != ""]
        return name.strip(), args
    return rest, []


class GeneratorContext:
    """Stateful source of per-transaction data, shared across a worker shard.

    Seed it for reproducibility; leave ``seed=None`` for a fresh random stream.
    ``terminals`` / ``cards`` feed the ``${pool.*}`` round-robins.
    """

    def __init__(
        self,
        seed: Any = None,
        *,
        terminals: list[Any] | None = None,
        cards: list[Any] | None = None,
    ) -> None:
        self._rng = random.Random(seed)
        self._seq: dict[str, int] = {}
        self._terminals = [str(t) for t in (terminals or [])]
        self._cards = list(cards or [])
        self._ti = 0
        self._ci = 0
        #: index/record of the card bound to the *current* transaction by
        #: :meth:`new_transaction`. ``_card_i`` rotates independently of the
        #: ``${pool.card}`` cursor so the two namespaces don't fight.
        self._card_i = 0
        self._txn = 0
        self._current_card: Any = None
        self._current_card_seq = 0

    @staticmethod
    def is_generator(expr: str) -> bool:
        """True if ``expr`` is owned by a generator namespace (not a step ref)."""
        head = expr.split(".", 1)[0].split("(", 1)[0].strip()
        return head in GENERATOR_NAMESPACES

    def resolve(self, expr: str) -> Any:
        """Resolve a generator token to a fresh value, advancing any state."""
        ns, _, rest = expr.partition(".")
        ns = ns.strip()
        if ns == "uuid":
            return self._uuid()
        if ns not in GENERATOR_NAMESPACES:
            raise GeneratorError(expr)
        fname, args = _split_call(rest)

        if ns == "seq":
            return self._seq_next(fname or "default", args)
        if ns == "rand":
            return self._rand(fname, args, expr)
        if ns == "now":
            return self._now(fname, expr)
        if ns == "pool":
            return self._pool(fname, expr)
        if ns == "card":
            return self._card(fname, expr)
        raise GeneratorError(expr)  # pragma: no cover - unreachable

    def new_transaction(self) -> None:
        """Begin a new transaction: rotate the transaction-bound card.

        Called by the runner once per top-level transaction so every
        ``${card.*}`` reference inside that scenario describes the *same* card,
        while successive transactions draw successive cards (round-robin). A
        no-op when the run has no card pool."""
        self._txn += 1
        if self._cards:
            self._current_card = self._cards[self._card_i % len(self._cards)]
            self._current_card_seq = self._card_i + 1
            self._card_i += 1
        else:
            self._current_card = None
            self._current_card_seq = 0

    # -- namespace handlers --------------------------------------------------

    def _seq_next(self, name: str, args: list[str]) -> Any:
        if name == "ksn":
            return self._ksn(args)
        self._seq[name] = self._seq.get(name, 0) + 1
        n = self._seq[name]
        if args:
            width = int(args[0])
            return str(n % (10**width)).zfill(width)
        return n

    def _ksn(self, args: list[str]) -> str:
        """A 24-hex DUKPT KSN: 16-hex initial-key-id base + an 8-hex counter
        that advances per call (0-based, like TestPay's ``%08X`` of the index)."""
        base = (args[0] if args else _DEFAULT_KSN_BASE).strip().upper()
        key = f"ksn:{base}"
        n = self._seq.get(key, 0)
        self._seq[key] = n + 1
        return f"{base}{n:08X}"

    def _rand(self, fname: str, args: list[str], expr: str) -> Any:
        try:
            if fname in ("int", "amount"):
                return self._rng.randint(int(args[0]), int(args[1]))
            if fname == "digits":
                return "".join(str(self._rng.randint(0, 9)) for _ in range(int(args[0])))
            if fname == "hex":
                return "".join(self._rng.choice("0123456789abcdef") for _ in range(int(args[0])))
            if fname == "pan":
                length = int(args[1]) if len(args) > 1 else 16
                return self._pan(args[0], length)
        except (IndexError, ValueError) as exc:
            raise GeneratorError(f"{expr}: {exc}") from exc
        raise GeneratorError(expr)

    def _now(self, fname: str, expr: str) -> Any:
        if fname == "epoch":
            return int(time.time())
        if fname == "iso":
            return datetime.now(timezone.utc).isoformat()
        if fname == "rrn":
            return self._rrn()
        raise GeneratorError(expr)

    def _pool(self, fname: str, expr: str) -> Any:
        if fname == "terminal":
            if not self._terminals:
                raise GeneratorError("pool.terminal: terminal pool is empty")
            v = self._terminals[self._ti % len(self._terminals)]
            self._ti += 1
            return v
        if fname == "card":
            if not self._cards:
                raise GeneratorError("pool.card: card pool is empty")
            v = self._cards[self._ci % len(self._cards)]
            self._ci += 1
            return v
        raise GeneratorError(expr)

    def _card(self, fname: str, expr: str) -> Any:
        """Resolve a field of the transaction-bound card.

        Binds lazily to the first card if :meth:`new_transaction` was never
        called (e.g. a generator used standalone), so a one-shot resolution
        still returns a stable, consistent record."""
        if self._current_card is None and self._txn == 0:
            self.new_transaction()
        if self._current_card is None:
            raise GeneratorError("card.*: card pool is empty")
        rec = self._card_record(self._current_card)
        if fname in ("pan", "number", ""):
            return rec["pan"]
        if fname == "expiry":
            return rec["expiry"]
        if fname in ("cvv", "cvv2"):
            return rec["cvv"]
        if fname in ("type", "brand"):
            return rec["brand"]
        if fname == "track2":
            return rec["track2"]
        if fname == "seq":
            return self._current_card_seq
        raise GeneratorError(expr)

    @staticmethod
    def _card_record(card: Any) -> dict[str, str]:
        """Normalise a pool entry (PAN string *or* dict) into a full record."""
        if isinstance(card, dict):
            pan = str(card.get("pan") or card.get("number") or "").strip()
            expiry = str(card.get("expiry") or card.get("expiry_date") or "2812").strip()
            cvv = str(card.get("cvv") or card.get("cvv2") or "000").strip()
            brand = str(card.get("type") or card.get("brand") or "").strip().lower()
            track2 = str(card.get("track2") or "").strip()
        else:
            pan, expiry, cvv, brand, track2 = str(card).strip(), "2812", "000", "", ""
        if not brand:
            brand = _brand_from_pan(pan)
        if not track2 and pan:
            # minimal Track 2: PAN, field separator 'D', expiry, service code 101
            track2 = f"{pan}D{expiry}101"
        return {"pan": pan, "expiry": expiry, "cvv": cvv, "brand": brand, "track2": track2}

    # -- primitives ----------------------------------------------------------

    def _uuid(self) -> str:
        return "%032x" % self._rng.getrandbits(128)

    def _pan(self, bin_: str, length: int) -> str:
        bin_ = "".join(c for c in str(bin_) if c.isdigit())
        if not bin_:
            raise GeneratorError("rand.pan: a numeric BIN is required")
        if length < len(bin_) + 1:
            length = len(bin_) + 1
        body = bin_ + "".join(str(self._rng.randint(0, 9)) for _ in range(length - len(bin_) - 1))
        return body + luhn_check_digit(body)

    def _rrn(self) -> str:
        """A 12-digit RRN: yDDDHHMM time prefix + a 4-digit random trace."""
        now = datetime.now(timezone.utc)
        prefix = f"{now.year % 10}{now.timetuple().tm_yday:03d}{now.hour:02d}{now.minute:02d}"
        return prefix + "".join(str(self._rng.randint(0, 9)) for _ in range(4))
