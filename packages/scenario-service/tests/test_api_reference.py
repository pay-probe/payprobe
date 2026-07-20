"""The interactive API reference (Scalar) is served and points at the spec."""
import os

# isolate the DB (and avoid seeding a shared on-disk db that pollutes other
# tests) — must be set before importing api.main, which binds DATABASE_URL.
os.environ["DATABASE_URL"] = ":memory:"

from fastapi.testclient import TestClient  # noqa: E402

import api.main as m  # noqa: E402


def test_reference_serves_scalar_html():
    with TestClient(m.app) as c:
        r = c.get("/reference")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    body = r.text
    assert "@scalar/api-reference" in body      # the interactive renderer
    assert 'data-url="/openapi.json"' in body   # fed by the live OpenAPI spec


def test_openapi_spec_is_available_for_the_reference():
    with TestClient(m.app) as c:
        spec = c.get("/openapi.json")
    assert spec.status_code == 200
    paths = spec.json()["paths"]
    # a few endpoints the reference will document
    assert "/scenarios" in paths
    assert "/connections/{name}" in paths
    assert "/iso8583/analyze" in paths
