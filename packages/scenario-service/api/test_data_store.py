"""Persistence for reusable *test data*: card pools, BIN ranges, terminal pools, keys.

A payment test needs data fed into it — PANs, terminal IDs, and crypto key
material. Today these live inline on a scenario/load payload (``cards`` /
``terminals`` lists) or are typed ad-hoc into ``${rand.pan(...)}`` and crypto
nodes. This store lets an author define each **once**, by name, and inspect it —
the same low-write, single-document, file-backed approach as the Connections,
Tables and Message Format registries::

    {"card_pools":     {name: {description, cards: [...]}},
     "bin_ranges":     {name: {description, bin, length, brand}},
     "terminal_pools": {name: {description, terminals: [...]}},
     "keys":           {name: {description, type, value}}}

Key material is **secret**: encrypted at rest via :class:`SecretBox` (same opt-in
Fernet key as Connections) and never returned in plaintext over the API — reads
mask it down to ``type``, ``length`` and a non-reversible ``fingerprint``. The
in-memory copy holds plaintext so an internal resolver (Phase 2) can hand it to
the worker; encryption happens only at the file boundary.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import threading
from pathlib import Path
from typing import Any, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .crypto import SecretBox, default_box

_ALLOWED_KEY_TYPES = {"bdk", "ipek", "pvk", "zmk", "tmk", "generic"}


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9_]+", "_", (text or "").lower()).strip("_")
    return s or "item"


# -- PAN generation (dependency-free; mirrors worker/engine/generators.py) ----

def luhn_check_digit(number: str) -> str:
    """The Luhn check digit that makes ``number + digit`` a valid card number."""
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - (total % 10)) % 10)


def generate_pan(bin_: str, length: int, rng: random.Random) -> str:
    bin_ = "".join(c for c in str(bin_) if c.isdigit())
    if not bin_:
        raise ValueError("a numeric BIN is required")
    if length < len(bin_) + 1:
        length = len(bin_) + 1
    body = bin_ + "".join(str(rng.randint(0, 9)) for _ in range(length - len(bin_) - 1))
    return body + luhn_check_digit(body)


def fingerprint(value: str) -> str:
    """A short, non-reversible identifier for a key value (sha256 prefix)."""
    return hashlib.sha256((value or "").encode()).hexdigest()[:8]


# -- drafts / models ---------------------------------------------------------

class CardPoolDraft(BaseModel):
    #: A pool entry is either a bare PAN string or a structured card record
    #: ``{pan, expiry, cvv, type, track2}``. The structured form lets a single
    #: transaction carry a self-consistent card (PAN + matching expiry/track2)
    #: via the worker's ``${card.*}`` generator namespace.
    description: str = ""
    cards: list[Union[str, dict[str, Any]]] = Field(default_factory=list)


class CardPool(CardPoolDraft):
    name: str


class BinRangeDraft(BaseModel):
    description: str = ""
    bin: str = ""
    length: int = 16
    brand: str = ""

    @field_validator("bin")
    @classmethod
    def _numeric_bin(cls, v: str) -> str:
        v = (v or "").strip()
        if v and not v.isdigit():
            raise ValueError("bin must be numeric")
        return v

    @field_validator("length")
    @classmethod
    def _length_range(cls, v: int) -> int:
        if not 12 <= v <= 19:
            raise ValueError("length must be between 12 and 19")
        return v


class BinRange(BinRangeDraft):
    name: str


class TerminalPoolDraft(BaseModel):
    description: str = ""
    terminals: list[str] = Field(default_factory=list)


class TerminalPool(TerminalPoolDraft):
    name: str


class KeyDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")

    description: str = ""
    type: str = "generic"
    #: hex key material. Empty on update => keep the existing stored value.
    value: str = ""

    @field_validator("type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        v = (v or "generic").lower()
        if v not in _ALLOWED_KEY_TYPES:
            raise ValueError(f"type must be one of {sorted(_ALLOWED_KEY_TYPES)}")
        return v

    @field_validator("value")
    @classmethod
    def _hex_value(cls, v: str) -> str:
        v = (v or "").strip().replace(" ", "")
        if v:
            if len(v) % 2 != 0:
                raise ValueError("key value must have an even number of hex digits")
            try:
                bytes.fromhex(v)
            except ValueError as exc:
                raise ValueError("key value must be valid hex") from exc
        return v


class KeyView(BaseModel):
    """Masked, API-safe view of a stored key — never includes the material."""

    name: str
    description: str = ""
    type: str = "generic"
    length: int = 0          # bytes of key material
    fingerprint: str = ""


class TestDataStore:
    __test__ = False  # not a pytest test class despite the name
    _COLLECTIONS = ("card_pools", "bin_ranges", "terminal_pools", "keys")

    def __init__(self, path: str | None = None, secret_box: SecretBox | None = None) -> None:
        self._lock = threading.Lock()
        self._path: Path | None = (
            Path(path) if path and path not in (":memory:", "") else None
        )
        self._box = secret_box or default_box
        self._data: dict[str, dict[str, dict]] = {c: {} for c in self._COLLECTIONS}
        self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if not (self._path and self._path.is_file()):
            return
        try:
            raw = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            return
        for c in self._COLLECTIONS:
            self._data[c] = dict(raw.get(c) or {})
        # decrypt key material into plaintext for in-memory/runtime use
        for name, doc in self._data["keys"].items():
            if "value" in doc:
                doc["value"] = self._box.decrypt(doc["value"])

    def _save(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        on_disk = {c: dict(self._data[c]) for c in self._COLLECTIONS}
        # encrypt key material just before it hits disk
        on_disk["keys"] = {
            name: {**doc, "value": self._box.encrypt(doc.get("value", ""))}
            for name, doc in self._data["keys"].items()
        }
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(on_disk, indent=2))
        tmp.replace(self._path)

    # -- generic helpers -----------------------------------------------------

    def _upsert(self, collection: str, name: str, doc: dict) -> str:
        key = slug(name)
        with self._lock:
            self._data[collection][key] = doc
            self._save()
        return key

    def _delete(self, collection: str, name: str) -> None:
        with self._lock:
            if self._data[collection].pop(name, None) is None:
                raise KeyError(name)
            self._save()

    # -- card pools ----------------------------------------------------------

    def list_card_pools(self) -> list[CardPool]:
        return [CardPool(name=n, **d) for n, d in sorted(self._data["card_pools"].items())]

    def get_card_pool(self, name: str) -> CardPool | None:
        d = self._data["card_pools"].get(name)
        return CardPool(name=name, **d) if d is not None else None

    def upsert_card_pool(self, name: str, draft: CardPoolDraft) -> CardPool:
        key = self._upsert("card_pools", name, draft.model_dump())
        return CardPool(name=key, **self._data["card_pools"][key])

    def delete_card_pool(self, name: str) -> None:
        self._delete("card_pools", name)

    # -- BIN ranges ----------------------------------------------------------

    def list_bin_ranges(self) -> list[BinRange]:
        return [BinRange(name=n, **d) for n, d in sorted(self._data["bin_ranges"].items())]

    def get_bin_range(self, name: str) -> BinRange | None:
        d = self._data["bin_ranges"].get(name)
        return BinRange(name=name, **d) if d is not None else None

    def upsert_bin_range(self, name: str, draft: BinRangeDraft) -> BinRange:
        key = self._upsert("bin_ranges", name, draft.model_dump())
        return BinRange(name=key, **self._data["bin_ranges"][key])

    def delete_bin_range(self, name: str) -> None:
        self._delete("bin_ranges", name)

    def generate_pans(
        self, name: str, count: int, *, seed: Any = None, as_pool: str | None = None
    ) -> list[str]:
        """Generate ``count`` Luhn-valid PANs from a BIN range; optionally save as a pool."""
        br = self.get_bin_range(name)
        if br is None:
            raise KeyError(name)
        rng = random.Random(seed)
        pans = [generate_pan(br.bin, br.length, rng) for _ in range(max(0, count))]
        if as_pool:
            self.upsert_card_pool(
                as_pool, CardPoolDraft(description=f"Generated from BIN range '{name}'", cards=pans)
            )
        return pans

    # -- terminal pools ------------------------------------------------------

    def list_terminal_pools(self) -> list[TerminalPool]:
        return [TerminalPool(name=n, **d) for n, d in sorted(self._data["terminal_pools"].items())]

    def get_terminal_pool(self, name: str) -> TerminalPool | None:
        d = self._data["terminal_pools"].get(name)
        return TerminalPool(name=name, **d) if d is not None else None

    def upsert_terminal_pool(self, name: str, draft: TerminalPoolDraft) -> TerminalPool:
        key = self._upsert("terminal_pools", name, draft.model_dump())
        return TerminalPool(name=key, **self._data["terminal_pools"][key])

    def delete_terminal_pool(self, name: str) -> None:
        self._delete("terminal_pools", name)

    # -- keys ----------------------------------------------------------------

    def _key_view(self, name: str, doc: dict) -> KeyView:
        value = doc.get("value", "")
        length = len(value) // 2 if value else 0
        return KeyView(
            name=name, description=doc.get("description", ""),
            type=doc.get("type", "generic"), length=length,
            fingerprint=fingerprint(value) if value else "",
        )

    def list_keys(self) -> list[KeyView]:
        return [self._key_view(n, d) for n, d in sorted(self._data["keys"].items())]

    def get_key_view(self, name: str) -> KeyView | None:
        d = self._data["keys"].get(name)
        return self._key_view(name, d) if d is not None else None

    def get_key_value(self, name: str) -> str | None:
        """Plaintext key material — internal use only (never exposed to the browser)."""
        d = self._data["keys"].get(name)
        return d.get("value") if d is not None else None

    def upsert_key(self, name: str, draft: KeyDraft) -> KeyView:
        key = slug(name)
        doc = draft.model_dump()
        # empty value on update => keep the existing material
        if not doc.get("value") and key in self._data["keys"]:
            doc["value"] = self._data["keys"][key].get("value", "")
        with self._lock:
            self._data["keys"][key] = doc
            self._save()
        return self._key_view(key, self._data["keys"][key])

    def delete_key(self, name: str) -> None:
        self._delete("keys", name)
