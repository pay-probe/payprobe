"""Encryption-at-rest for stored secrets — re-exported from the shared package.

This module used to be a byte-for-byte mirror of ``payprobe_common.crypto``
(same ``enc:v1:`` format), kept only because this service's container built
from its own directory and couldn't vendor a sibling package. The build moved
to the ``packages/`` context (it ships ``payprobe_common`` for the assistant
toolkit), so the mirror is gone — one implementation, imported here so every
existing ``from .crypto import …`` keeps working.
"""
from __future__ import annotations

try:  # packages/ on the path (dev: PYTHONPATH=packages; container: /app)
    from payprobe_common.crypto import (  # noqa: F401
        SecretBox,
        default_box,
        fingerprint,
        is_secret_key,
    )
except ModuleNotFoundError:  # pragma: no cover — running from the package dir
    import sys
    from pathlib import Path

    _here = Path(__file__).resolve()
    for _cand in (_here.parents[1], _here.parents[2]):
        if (_cand / "payprobe_common").is_dir():
            sys.path.insert(0, str(_cand))
            break
    from payprobe_common.crypto import (  # noqa: F401
        SecretBox,
        default_box,
        fingerprint,
        is_secret_key,
    )
