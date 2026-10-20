# app/core/settings.py
from __future__ import annotations
from typing import List, Dict, Optional
from functools import lru_cache
from pydantic_settings import BaseSettings
from pydantic import Field, field_validator, AnyUrl, StrictBool, conint, confloat

class VercelSettings(BaseSettings):
    api_key: Optional[str] = None
    base_url: str = "https://ai-gateway.vercel.sh/v1"
    enabled: bool = True

    model_config = {
        "env_prefix": "VERCEL_AI__",  # Explicitly set prefix
        "case_sensitive": False,
    }

    @field_validator("enabled", mode="before")
    def parse_enabled(cls, v):
        """Handle string/bool conversion"""
        if isinstance(v, str):
            return v.lower() in ("true", "1", "yes", "on")
        return bool(v) if v is not None else True

class MixBreadSettings(BaseSettings):
    api_key: Optional[str] = None
    base_url: Optional[str] = None

    model_config = {
        "env_prefix": "MIXED_BREAD__",  # Explicitly set prefix
        "case_sensitive": False,
    }


class Settings(BaseSettings):
    # server
    port: int = 3005
    node_env: str = "development"
    client_origin: str = "*"

    # subconfigs
    mixedbread: MixBreadSettings = MixBreadSettings()
    vercel_ai: VercelSettings = VercelSettings()


    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        "extra": "ignore",  # allows extra env variables
        "env_nested_delimiter": "__", 
    }

    @field_validator("port", mode="before")
    def _port_int(cls, v):
        return int(v) if v is not None else 3005

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Cached settings loader. Import and call get_settings() across your codebase
    to get a single shared Settings instance (fast and safe).
    """
    return Settings()
