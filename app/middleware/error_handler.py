# app/middleware/error_handler.py
from fastapi import Request
from fastapi.responses import JSONResponse
from app.core.logger import logger

async def error_handler(request: Request, exc: Exception):
    logger.exception(f"Unhandled error: {exc}")
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error"},
    )
