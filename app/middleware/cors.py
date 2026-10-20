from fastapi.middleware.cors import CORSMiddleware
from app.core.settings import get_settings

def add_cors_middleware(app):
    settings = get_settings()
    origins = ["*"] if settings.client_origin == "*" else [settings.client_origin]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
