from __future__ import annotations

from typing import Any, Dict, List, Optional
from datetime import datetime, timezone
import json
import time

from google.cloud import bigquery
from google.api_core.exceptions import ServiceUnavailable, TooManyRequests, BadRequest

from app.core.settings import get_settings
from app.core.logger import logger


# ----------------------------
# Helpers
# ----------------------------

def _now_ts() -> str:
    # RFC3339-ish, BigQuery accepts this string for TIMESTAMP/DATETIME fields
    return datetime.now(timezone.utc).isoformat()


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _is_json_string(s: str) -> bool:
    s = (s or "").strip()
    return (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]"))


def _to_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes", "on"):
            return True
        if v in ("false", "0", "no", "off"):
            return False
        if v in ("", "none", "null"):
            return None
    return None


def _chunk_list(items: List[Dict[str, Any]], size: int) -> List[List[Dict[str, Any]]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _coerce_for_bq(field_type: str, field_mode: str, value: Any) -> Any:
    """
    Coerce Python values to what BigQuery insert_rows_json expects for the given schema type.
    NOTE:
      - JSON fields in insert_rows_json: pass JSON-formatted STRING
      - REPEATED fields: must be list
      - Scalars cannot be dict/list -> stringify
    """
    t = (field_type or "STRING").upper()
    m = (field_mode or "NULLABLE").upper()

    if value is None:
        return None

    # REPEATED fields must be list
    if m == "REPEATED":
        if isinstance(value, list):
            return value
        return [value]

    # TIMESTAMP / DATETIME: accept datetime or string
    if t in ("TIMESTAMP", "DATETIME"):
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)

    # DATE: accept date-like string; keep as string
    if t == "DATE":
        return str(value)

    # BOOL
    if t in ("BOOL", "BOOLEAN"):
        return _to_bool(value)

    # INT / FLOAT
    if t in ("INT64", "INTEGER"):
        try:
            return int(value)
        except Exception:
            return None
    if t in ("FLOAT64", "FLOAT", "NUMERIC", "BIGNUMERIC"):
        try:
            return float(value)
        except Exception:
            return None

    # JSON: BigQuery streaming expects JSON STRING
    if t == "JSON":
        if isinstance(value, str) and _is_json_string(value):
            return value.strip()
        return _json_dump(value)

    # RECORD: expects dict (non-repeated)
    if t == "RECORD":
        if isinstance(value, dict):
            return value
        return _json_dump(value)

    # STRING and other scalars: no dict/list
    if isinstance(value, (dict, list)):
        return _json_dump(value)

    return value


def _build_row_from_message(
    *,
    m: Dict[str, Any],
    upstream_version: str,
    project_slug: str,
    snapshot_id: str,
    ingest_ts: str,
) -> Dict[str, Any]:
    """
    Create a normalized row using your known keys.
    Later we will filter to schema columns and coerce types.
    """
    return {
        "ingest_ts": ingest_ts,
        "upstream_version": upstream_version,
        "project_slug": project_slug,
        "snapshot_id": snapshot_id,

        # IMPORTANT: your table might use message_id OR id. We set both; schema filter will keep what exists.
        "message_id": m.get("id"),
        "id": m.get("id"),

        "user_id": m.get("user_id"),
        "message": m.get("message"),
        "file_url": m.get("file_url"),
        "type": m.get("type"),
        "message_type": m.get("message_type"),
        "mime_type": m.get("mime_type"),

        "created_at": m.get("created_at"),
        "updated_at": m.get("updated_at"),
        "parent_message_id": m.get("parent_message_id"),
        "message_state": m.get("message_state"),
        "message_sent_by": m.get("message_sent_by"),
        "message_sent_by_user": m.get("message_sent_by_user"),

        "is_reported": _to_bool(m.get("is_reported")),
        "is_new": _to_bool(m.get("is_new")),

        "signature": m.get("signature"),
        "project_id": m.get("project_id"),
        "change_type": m.get("change_type"),
        "integration_id": m.get("integration_id"),

        # complex fields (keep as raw; schema coercion will stringify or JSON-encode properly)
        "other_data": m.get("other_data"),
        "structured_message": m.get("structured_message"),
        "oleon_convos_users": m.get("oleon_convos_users"),
    }


def insert_messages_to_bigquery(
    *,
    upstream_version: str,
    project_slug: str,
    snapshot_id: str,
    messages: List[Dict[str, Any]],
    batch_size: int = 200,
    per_batch_timeout: int = 60,
    max_retries: int = 3,
) -> Dict[str, Any]:
    """
    Final fixed insert function:
      ✅ Uses correct table id (BQ_MESSAGES_TABLE_ID preferred)
      ✅ Filters fields to match BigQuery schema
      ✅ Coerces each field based on schema (fixes "stopped" / 0 inserted issues)
      ✅ Logs exact failing rows + errors
      ✅ Batches so one bad row doesn't kill everything
    """
    settings = get_settings()

    if not getattr(settings, "bq_enabled", True):
        return {"enabled": False, "inserted": 0, "errors": []}

    if not settings.bq_project_id or not settings.bq_dataset_id:
        return {
            "enabled": True,
            "inserted": 0,
            "errors": ["Missing BQ_PROJECT_ID or BQ_DATASET_ID in .env"],
        }

    # Prefer BQ_MESSAGES_TABLE_ID if present, fallback to BQ_TABLE_ID for backward compatibility
    table_name = getattr(settings, "bq_messages_table_id", None) or getattr(settings, "bq_table_id", None)
    if not table_name:
        return {
            "enabled": True,
            "inserted": 0,
            "errors": ["Missing BQ_MESSAGES_TABLE_ID (or BQ_TABLE_ID) in .env"],
        }

    table_id = f"{settings.bq_project_id}.{settings.bq_dataset_id}.{table_name}"
    client = bigquery.Client(project=settings.bq_project_id)

    # Fetch schema once (critical fix)
    try:
        table = client.get_table(table_id)
    except Exception as e:
        return {
            "enabled": True,
            "inserted": 0,
            "errors": [f"Cannot access BigQuery table {table_id}: {e}"],
            "table": table_id,
        }

    schema_map: Dict[str, Dict[str, str]] = {
        f.name: {"type": f.field_type.upper(), "mode": f.mode.upper()}
        for f in table.schema
    }

    ingest_ts = _now_ts()

    # Build raw rows
    raw_rows: List[Dict[str, Any]] = [
        _build_row_from_message(
            m=m,
            upstream_version=upstream_version,
            project_slug=project_slug,
            snapshot_id=snapshot_id,
            ingest_ts=ingest_ts,
        )
        for m in (messages or [])
    ]

    # Filter to schema + coerce types
    rows: List[Dict[str, Any]] = []
    for r in raw_rows:
        cleaned: Dict[str, Any] = {}
        for k, v in r.items():
            if k not in schema_map:
                continue
            cleaned[k] = _coerce_for_bq(schema_map[k]["type"], schema_map[k]["mode"], v)
        rows.append(cleaned)

    total_inserted = 0
    all_errors: List[Any] = []

    chunks = _chunk_list(rows, max(1, int(batch_size)))
    logger.info(
        f"[BIGQUERY] Inserting {len(rows)} rows into {table_id} in {len(chunks)} batch(es), batch_size={batch_size}"
    )

    for batch_index, batch_rows in enumerate(chunks, start=1):
        attempt = 0
        while True:
            attempt += 1
            try:
                errors = client.insert_rows_json(
                    table_id,
                    batch_rows,
                    timeout=per_batch_timeout,
                )

                if errors:
                    # Row-level errors
                    logger.error(f"[BIGQUERY] Batch {batch_index}/{len(chunks)} row errors count={len(errors)}")

                    # Log first few errors with their bad rows
                    for e in errors[:5]:
                        idx = e.get("index")
                        err_list = e.get("errors")
                        bad_row = batch_rows[idx] if isinstance(idx, int) and 0 <= idx < len(batch_rows) else None
                        logger.error(f"[BIGQUERY] error index={idx} errors={err_list} bad_row={bad_row}")

                    all_errors.extend(errors)
                else:
                    total_inserted += len(batch_rows)
                    logger.info(f"[BIGQUERY] Batch {batch_index}/{len(chunks)} inserted {len(batch_rows)}")

                break

            except (ServiceUnavailable, TooManyRequests) as exc:
                if attempt <= max_retries:
                    sleep_s = min(2 ** attempt, 10)
                    logger.warning(
                        f"[BIGQUERY] Batch {batch_index} transient error: {exc}. "
                        f"Retrying in {sleep_s}s (attempt {attempt}/{max_retries})"
                    )
                    time.sleep(sleep_s)
                    continue
                logger.exception(f"[BIGQUERY] Batch {batch_index} failed after retries: {exc}")
                all_errors.append({"batch": batch_index, "error": str(exc)})
                break

            except BadRequest as exc:
                # This usually indicates schema mismatch / invalid value type
                logger.exception(f"[BIGQUERY] Batch {batch_index} bad request: {exc}")
                all_errors.append({"batch": batch_index, "error": str(exc)})
                break

            except Exception as exc:
                logger.exception(f"[BIGQUERY] Batch {batch_index} failed: {exc}")
                all_errors.append({"batch": batch_index, "error": str(exc)})
                break

    return {
        "enabled": True,
        "inserted": total_inserted,
        "errors": all_errors,
        "table": table_id,
        "batches": len(chunks),
        "batch_size": batch_size,
        "schema_columns": list(schema_map.keys()),
    }
