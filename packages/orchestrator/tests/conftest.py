"""Test config for the orchestrator.

The auth gate (api/auth.py) fails closed: outside an explicit dev/test
environment it refuses requests that carry no credential. The unit tests drive
the endpoints without tokens, so mark the environment as ``test`` to keep the
gate open. Auth behaviour itself is covered separately in test_auth_gate.py.
"""
import os

os.environ.setdefault("PAYPROBE_ENV", "test")
# Code nodes are gated when auth is off and no sandbox is active. The suite runs
# trusted snippets without a network sandbox, so opt in explicitly (the gating
# itself is covered in test_code_node_gating.py, which clears this).
os.environ.setdefault("PAYPROBE_ALLOW_UNAUTH_CODE", "1")
