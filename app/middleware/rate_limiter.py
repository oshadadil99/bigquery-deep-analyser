# rate_limiter.py
from slowapi import Limiter
from slowapi.util import get_remote_address
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.status import HTTP_429_TOO_MANY_REQUESTS

# Create a Limiter instance
limiter = Limiter(key_func=get_remote_address)

# Custom 429 response
def rate_limit_exceeded(request: Request, exc):
    return JSONResponse(
        status_code=HTTP_429_TOO_MANY_REQUESTS,
        content={"message": "Too many requests, please try again after 15 minutes."}
    )
