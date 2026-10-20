from __future__ import annotations

import os
from functools import lru_cache
from dotenv import load_dotenv

load_dotenv()


def _bool(v: str | None, default: bool = False) -> bool:
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


class Settings:
    def __init__(self) -> None:
        # =========================
        # Server
        # =========================
        self.port: int = int(os.getenv("PORT", "8000"))
        self.client_origin: str = os.getenv("CLIENT_ORIGIN", "*")

        # =========================
        # Upstream (Process Messages)
        # =========================
        # NEW: used by /v1/process_messages
        # If you don't set env, it will use a safe default and won't crash.
        self.upstream_base_url: str = os.getenv("UPSTREAM_BASE_URL", "http://127.0.0.1:3000")

        # Optional: if your upstream needs an API key/header
        self.upstream_api_key: str | None = os.getenv("UPSTREAM_API_KEY")

        # Optional: timeout seconds for upstream HTTP calls
        self.upstream_timeout_s: int = int(os.getenv("UPSTREAM_TIMEOUT_S", "30"))

        # =========================
        # BigQuery
        # =========================
        self.bq_enabled: bool = _bool(os.getenv("BQ_ENABLED", "true"), True)

        self.bq_project_id: str | None = os.getenv("BQ_PROJECT_ID")
        self.bq_dataset_id: str | None = os.getenv("BQ_DATASET_ID")

        # Old env var (backward compatible)
        self.bq_table_id: str = os.getenv("BQ_TABLE_ID", "messages")

        # Recommended explicit env vars
        self.bq_messages_table_id: str = os.getenv("BQ_MESSAGES_TABLE_ID", self.bq_table_id)
        self.bq_user_behavior_table_id: str = os.getenv("BQ_USER_BEHAVIOR_TABLE_ID", "user_behavior")

        # Timestamp column name in messages table (adjust if your table uses another)
        self.bq_messages_ts_col: str = os.getenv("BQ_MESSAGES_TS_COL", "created_at")

        # Limits (optional but useful)
        self.limit_messages_per_user: int = int(os.getenv("LIMIT_MESSAGES_PER_USER", "200"))

        # =========================
        # Vertex AI (Gemini)
        # =========================
        self.vertex_project_id: str | None = os.getenv("VERTEX_PROJECT_ID") or self.bq_project_id
        self.vertex_location: str = os.getenv("VERTEX_LOCATION", "us-central1")
        self.gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")

        # Gemini batch controls (optional defaults)
        self.gemini_log_raw: bool = _bool(os.getenv("GEMINI_LOG_RAW", "true"), True)
        self.gemini_log_raw_preview_chars: int = int(os.getenv("GEMINI_LOG_RAW_PREVIEW_CHARS", "1200"))
        self.gemini_max_concurrency: int = int(os.getenv("GEMINI_MAX_CONCURRENCY", "3"))

        # =========================
        # App timezone for “today”
        # =========================
        self.app_tz: str = os.getenv("APP_TZ", "Asia/Colombo")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
