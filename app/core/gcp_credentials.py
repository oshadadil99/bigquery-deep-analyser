from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

try:
    from app.core.logger import logger
except Exception:
    import logging
    logger = logging.getLogger("app_logger")


ENV_CREDENTIALS_JSON = "GOOGLE_CREDENTIALS_JSON"
ENV_GOOGLE_APPLICATION_CREDENTIALS = "GOOGLE_APPLICATION_CREDENTIALS"

DEFAULT_HEROKU_PATH = "/tmp/gcp-sa.json"


def ensure_gcp_credentials_file() -> Optional[str]:
    """
    Heroku pattern:
      - GOOGLE_CREDENTIALS_JSON contains the full service account JSON
      - GOOGLE_APPLICATION_CREDENTIALS points to /tmp/gcp-sa.json
      - We must write the JSON content to that file before BigQuery/Vertex init.

    Local pattern (Windows):
      - GOOGLE_APPLICATION_CREDENTIALS points to an existing .json file path
      - GOOGLE_CREDENTIALS_JSON is usually not set
      - We do nothing except log.
    """
    raw = os.getenv(ENV_CREDENTIALS_JSON, "").strip()
    target_path = (os.getenv(ENV_GOOGLE_APPLICATION_CREDENTIALS) or "").strip() or DEFAULT_HEROKU_PATH

    # If JSON is not provided, just rely on GOOGLE_APPLICATION_CREDENTIALS (local dev style)
    if not raw:
        if target_path and Path(target_path).exists():
            logger.info(f"[GCP_CREDENTIALS] Using existing GOOGLE_APPLICATION_CREDENTIALS file: {target_path}")
            return target_path

        logger.info("[GCP_CREDENTIALS] GOOGLE_CREDENTIALS_JSON not provided; skipping file write")
        return None

    # JSON is provided -> write it to target_path
    try:
        obj = json.loads(raw)  # validates JSON; supports multi-line input
    except Exception as e:
        logger.error(f"[GCP_CREDENTIALS] Invalid GOOGLE_CREDENTIALS_JSON. Error: {e}")
        raise

    # Ensure parent directory exists (on Heroku /tmp exists, but safe)
    p = Path(target_path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    # Write normalized JSON (single-line) so Google libs can read it reliably
    content = json.dumps(obj, ensure_ascii=False)

    try:
        p.write_text(content, encoding="utf-8")
        # Optional: best-effort file permission hardening
        try:
            os.chmod(str(p), 0o600)
        except Exception:
            pass
        # Force env var to be correct for Google ADC
        os.environ[ENV_GOOGLE_APPLICATION_CREDENTIALS] = str(p)

        logger.info(f"[GCP_CREDENTIALS] Wrote service account JSON to {p}")
        return str(p)

    except Exception as e:
        logger.error(f"[GCP_CREDENTIALS] Failed writing credentials file to {p}. Error: {e}")
        raise
