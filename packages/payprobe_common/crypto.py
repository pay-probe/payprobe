"""Encryption-at-rest for stored secrets (shared across services).

File-backed registries hold credentials — switch sign-on keys, HSM secrets, API
keys, TLS material — and sign-off snapshots may carry secret-named fields too.
Those were persisted as plaintext JSON. This module encrypts secret-named fields
**at rest** while keeping plaintext in memory for runtime use, so a leaked
file/backup/volume doesn't leak credentials.

Lives in the shared :mod:`payprobe_common` package so the scenario-service
(connections / variables / keys) and the orchestrator (sign-off snapshots) share
one ``SecretBox`` and one ciphertext format.

Design:

* **Opt-in, fail-open to plaintext.** Set ``PAYPROBE_SECRET_KEY`` (a Fernet key)
  to enable. With no key the box is a no-op — same behaviour as before — so dev
  and existing deployments keep working, then encrypt on the next write once a
  key is set. Generate one with ``SecretBox.generate_key()``.
* **Field-targeted.** Only values under secret-looking keys (``password``,
  ``api_key``, ``*_secret``, ``private_key``, …) are encrypted; non-secret config
  (host, port, framing) stays readable in the file.
* **Self-describing ciphertext.** Encrypted values are tagged ``enc:v1:<token>``
  so decryption is keyed off the value, not the field name — a renamed field
  still decrypts, and already-encrypted values are never double-wrapped.

If ``PAYPROBE_SECRET_KEY`` is set but the ``cryptography`` package is missing, we
raise rather than silently store plaintext — a configured intent to encrypt must
not fail open.
"""
from __future__ import annotations

import os
from typing import Any

_PREFIX = "enc:v1:"

# Keys whose values are treated as secret (case-insensitive, exact or suffix).
_SECRET_EXACT = {
    "password", "passwd", "secret", "secret_key", "secretkey", "api_key",
    "apikey", "token", "passphrase", "private_key", "credential", "credentials",
    "client_secret", "auth_token", "pin", "pin_key", "kek", "zmk", "tmk",
    # NATS auth (ADR-0006): the nkey seed and JWT creds are bearer secrets.
    "nkey_seed", "creds", "user_jwt",
}
_SECRET_SUFFIXES = ("_password", "_secret", "_token", "_api_key", "_apikey",
                    "_passphrase", "_private_key", "_credential")
# Explicitly NOT secret even though they contain "key".
_SECRET_DENY = {"public_key", "key", "keys", "key_id", "kid", "primary_key"}


def fingerprint(value: str) -> str:
    """A short, non-reversible identifier for a secret value (sha256 prefix).

    Lets the UI show that a secret exists / changed without ever revealing it.
    """
    import hashlib

    if not value:
        return ""
    return hashlib.sha256(value.encode()).hexdigest()[:8]


def is_secret_key(name: str) -> bool:
    n = name.lower()
    if n in _SECRET_DENY:
        return False
    if n in _SECRET_EXACT:
        return True
    return any(n.endswith(suf) for suf in _SECRET_SUFFIXES)


class SecretBox:
    """Encrypt/decrypt secret values. Disabled (passthrough) when no key."""

    def __init__(self, key: str | None = None) -> None:
        self._key = key if key is not None else os.environ.get("PAYPROBE_SECRET_KEY")
        self._fernet = None
        if self._key:
            try:
                from cryptography.fernet import Fernet
            except ImportError as exc:  # configured to encrypt but can't → fail loud
                raise RuntimeError(
                    "PAYPROBE_SECRET_KEY is set but the 'cryptography' package is "
                    "not installed; cannot encrypt secrets at rest"
                ) from exc
            self._fernet = Fernet(self._key.encode() if isinstance(self._key, str) else self._key)

    @property
    def enabled(self) -> bool:
        return self._fernet is not None

    @staticmethod
    def generate_key() -> str:
        from cryptography.fernet import Fernet

        return Fernet.generate_key().decode()

    @staticmethod
    def is_encrypted(value: Any) -> bool:
        return isinstance(value, str) and value.startswith(_PREFIX)

    def encrypt(self, plaintext: str) -> str:
        if not self.enabled or self.is_encrypted(plaintext):
            return plaintext
        token = self._fernet.encrypt(plaintext.encode()).decode()  # type: ignore[union-attr]
        return _PREFIX + token

    def decrypt(self, value: str) -> str:
        if not self.is_encrypted(value):
            return value
        if not self.enabled:
            # value is encrypted but we have no key — can't recover; leave as-is
            return value
        token = value[len(_PREFIX):]
        return self._fernet.decrypt(token.encode()).decode()  # type: ignore[union-attr]

    # -- document helpers ----------------------------------------------------
    def encrypt_doc(self, doc: Any) -> Any:
        """Deep-copy ``doc`` encrypting values under secret-named keys."""
        if not self.enabled:
            return doc
        return self._walk(doc, encrypt=True)

    def decrypt_doc(self, doc: Any) -> Any:
        """Deep-copy ``doc`` decrypting any ``enc:v1:`` values (key-agnostic)."""
        return self._walk(doc, encrypt=False)

    def _walk(self, node: Any, *, encrypt: bool, secret_ctx: bool = False) -> Any:
        if isinstance(node, dict):
            out: dict[str, Any] = {}
            for k, v in node.items():
                out[k] = self._walk(v, encrypt=encrypt, secret_ctx=is_secret_key(str(k)))
            return out
        if isinstance(node, list):
            return [self._walk(v, encrypt=encrypt, secret_ctx=secret_ctx) for v in node]
        if isinstance(node, str):
            if encrypt:
                return self.encrypt(node) if secret_ctx else node
            return self.decrypt(node)  # decrypt is key-agnostic (tagged values)
        return node


# Process-wide default box from the environment.
default_box = SecretBox()
