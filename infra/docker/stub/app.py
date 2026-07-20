"""Generic placeholder service.

Stands in for PayProbe services that are scaffolded but not yet implemented
(auth-service, report-service, the helper probes). It starts cleanly and passes
health checks, so the full infrastructure topology can be brought up, but every
real endpoint returns HTTP 501 to make the "not built yet" status unambiguous.

Configure via SERVICE_NAME.
"""
import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse

SERVICE = os.environ.get("SERVICE_NAME", "service")

app = FastAPI(title=f"PayProbe {SERVICE} (placeholder)")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": SERVICE, "implemented": False}


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
)
def not_implemented(path: str) -> JSONResponse:
    return JSONResponse(
        status_code=501,
        content={
            "service": SERVICE,
            "detail": f"{SERVICE} is not implemented yet (placeholder container).",
            "path": f"/{path}",
        },
    )
