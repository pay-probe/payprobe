"""Persisted settings for the AI assistant's LLM provider.

A single document holding the user's LLM config — provider (OpenAI or Anthropic
/ Claude), enable flag, endpoint, model, and API key. The key is
SecretBox-encrypted **at rest** (``enc:v1:`` when ``PAYPROBE_SECRET_KEY`` is
set — same box and format as every other secret store; invariant 8), kept
plaintext in memory for runtime use, and **never returned** by the API: reads
expose only ``key_set`` + a masked hint. A legacy plaintext file self-heals to
ciphertext on load. For production the ``ASSIST_LLM_*`` env override remains
available (and wins when set).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from pydantic import BaseModel

from .crypto import SecretBox, default_box, fingerprint

#: per-provider default endpoint + model
PROVIDER_DEFAULTS: dict[str, tuple[str, str]] = {
    "openai": ("https://api.openai.com/v1/chat/completions", "gpt-4o-mini"),
    "anthropic": ("https://api.anthropic.com/v1/messages", "claude-3-5-sonnet-latest"),
}
DEFAULT_PROVIDER = "openai"
DEFAULT_URL = PROVIDER_DEFAULTS[DEFAULT_PROVIDER][0]
DEFAULT_MODEL = PROVIDER_DEFAULTS[DEFAULT_PROVIDER][1]


def defaults_for(provider: str) -> tuple[str, str]:
    return PROVIDER_DEFAULTS.get(provider, PROVIDER_DEFAULTS[DEFAULT_PROVIDER])


class AssistConfigDraft(BaseModel):
    provider: str = DEFAULT_PROVIDER          # "openai" | "anthropic"
    enabled: bool = False
    base_url: str = ""                         # blank -> provider default
    model: str = ""                            # blank -> provider default
    #: None = leave the stored key unchanged; "" = clear it; else set it
    api_key: str | None = None


class AssistConfigStore:
    def __init__(self, path: str | None = None,
                 secret_box: SecretBox | None = None) -> None:
        self._lock = threading.Lock()
        self._path: Path | None = (
            Path(path) if path and path not in (":memory:", "") else None
        )
        self._box = secret_box or default_box
        self._doc: dict = {"provider": DEFAULT_PROVIDER, "enabled": False,
                           "base_url": "", "model": "", "api_key": ""}
        self._load()

    def _load(self) -> None:
        if self._path and self._path.is_file():
            try:
                on_disk = json.loads(self._path.read_text())
            except (json.JSONDecodeError, OSError):
                return
            # In-memory copy is plaintext (decrypt is key-agnostic — tagged
            # values only); the file stays/becomes ciphertext.
            self._doc.update(self._box.decrypt_doc(on_disk))
            stored_key = on_disk.get("api_key") or ""
            if (self._box.enabled and stored_key
                    and not SecretBox.is_encrypted(stored_key)):
                # Legacy plaintext on disk — self-heal to enc:v1: immediately.
                self._save()

    def _save(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._box.encrypt_doc(self._doc), indent=2))
        tmp.replace(self._path)

    # -- read ----------------------------------------------------------------

    def raw(self) -> dict:
        """Full config incl. the key, with provider defaults filled in. For
        server-side use only (never returned to clients)."""
        provider = self._doc.get("provider") or DEFAULT_PROVIDER
        d_url, d_model = defaults_for(provider)
        return {
            "provider": provider,
            "enabled": bool(self._doc.get("enabled")),
            "base_url": self._doc.get("base_url") or d_url,
            "model": self._doc.get("model") or d_model,
            "api_key": self._doc.get("api_key") or "",
        }

    def public(self) -> dict:
        raw = self.raw()
        key = raw["api_key"]
        return {
            "provider": raw["provider"],
            "enabled": raw["enabled"],
            "base_url": raw["base_url"],
            "model": raw["model"],
            "key_set": bool(key),
            "key_hint": ("…" + key[-4:]) if len(key) >= 4 else ("set" if key else ""),
        }

    def secret_refs(self) -> list[dict]:
        """Masked reference to the stored LLM key for the Secrets Vault
        inventory — ``{owner, field, fingerprint}``, never the value."""
        key = self._doc.get("api_key") or ""
        if not key:
            return []
        return [{"owner": "assistant (Settings → AI assistant)",
                 "field": "api_key", "fingerprint": fingerprint(key)}]

    # -- write ---------------------------------------------------------------

    def save(self, draft: AssistConfigDraft) -> dict:
        with self._lock:
            self._doc["provider"] = draft.provider or DEFAULT_PROVIDER
            self._doc["enabled"] = draft.enabled
            self._doc["base_url"] = draft.base_url or ""
            self._doc["model"] = draft.model or ""
            if draft.api_key is not None:   # None = keep existing
                self._doc["api_key"] = draft.api_key
            self._save()
        return self.public()
