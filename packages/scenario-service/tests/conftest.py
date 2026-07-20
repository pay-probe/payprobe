"""Test config for the scenario-service.

The auth gate (api/auth.py) fails closed: outside an explicit dev/test
environment it refuses requests that carry no credential. Most unit tests drive
the endpoints without tokens, so mark the environment as ``test`` to keep the
gate open. Auth behaviour itself (static bearer + JWT) is exercised where a test
sets ``API_TOKEN`` explicitly.
"""
import os

os.environ.setdefault("PAYPROBE_ENV", "test")
