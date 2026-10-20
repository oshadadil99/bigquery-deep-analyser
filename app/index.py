from __future__ import annotations

from fastapi import FastAPI

from app.core.logger import logger
from app.core.gcp_credentials import ensure_gcp_credentials_file


# Must happen BEFORE importing routes/services that may init BigQuery/Vertex
ensure_gcp_credentials_file()

from app.middleware.cors import add_cors_middleware
from app.middleware.error_handler import error_handler
from app.api.routes import api_router


app = FastAPI(title="Oleon Q Search")

add_cors_middleware(app)
app.add_exception_handler(Exception, error_handler)
app.include_router(api_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
