# app/services/user_behavior_service.py
from __future__ import annotations

import json
from datetime import datetime, date
from functools import lru_cache
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from google.cloud import bigquery

from app.core.settings import get_settings

try:
    from app.core.logger import logger
except Exception:
    import logging
    logger = logging.getLogger("app_logger")


# ----------------------------
# Helpers
# ----------------------------

def _iso(v: Any) -> Any:
    """Convert datetime/date to ISO string, keep others as-is."""
    if isinstance(v, datetime):
        return v.replace(microsecond=0).isoformat() + "Z"
    if isinstance(v, date):
        return v.isoformat()
    return v


def _safe_json_dump(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


def _maybe_json_load(v: Any) -> Any:
    if isinstance(v, str):
        s = v.strip()
        if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
            try:
                return json.loads(s)
            except Exception:
                return v
    return v


@lru_cache(maxsize=1)
def get_bq_client() -> bigquery.Client:
    settings = get_settings()
    return bigquery.Client(project=settings.bq_project_id)


@lru_cache(maxsize=16)
def get_table_schema(project_id: str, dataset_id: str, table_id: str) -> Dict[str, Dict[str, str]]:
    """
    Returns:
      {
        "col_name": {"type": "STRING|JSON|RECORD|...", "mode":"NULLABLE|REPEATED|REQUIRED"}
      }
    """
    client = get_bq_client()
    table_ref = f"{project_id}.{dataset_id}.{table_id}"
    table = client.get_table(table_ref)

    schema_map: Dict[str, Dict[str, str]] = {}
    for f in table.schema:
        schema_map[f.name] = {"type": f.field_type.upper(), "mode": f.mode.upper()}
    return schema_map


def _detect_column_names(schema: Dict[str, Dict[str, str]]) -> Dict[str, Optional[str]]:
    def pick(*cands: str) -> Optional[str]:
        for c in cands:
            if c in schema:
                return c
        return None

    return {
        "language_mix": pick("language_mix", "language_mix_json"),
        "top_intents": pick("top_intents", "top_intents_json"),
        "mentioned_assets": pick("mentioned_assets", "mentioned_assets_json"),
        "mentioned_amounts": pick("mentioned_amounts", "mentioned_amounts_json"),
        "mentioned_locations": pick("mentioned_locations", "mentioned_locations_json"),
        "tags": pick("tags", "tags_json"),
        "summery": pick("summery", "summary"),  # ✅ NEW (prefer summery)
        "last_updated_time": pick("last_updated_time", "updated_at", "last_updated_at"),
    }


def _coerce_value_for_bq(col_type: str, col_mode: str, value: Any) -> Any:
    """
    Convert Python values to what BigQuery streaming insert expects.
    IMPORTANT FIX:
      - For BigQuery JSON columns, insert_rows_json expects a JSON-formatted STRING.
        So dict/list -> json.dumps(...)
    """
    col_type = (col_type or "STRING").upper()
    col_mode = (col_mode or "NULLABLE").upper()

    value = _iso(value)

    if value is None:
        return None

    # REPEATED fields must always be list
    if col_mode == "REPEATED":
        if isinstance(value, list):
            return [_iso(v) for v in value]
        return [value]

    # ✅ JSON columns: streaming insert expects JSON string
    if col_type == "JSON":
        if isinstance(value, str):
            s = value.strip()
            if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
                return s
        return _safe_json_dump(value)

    # RECORD expects dict (non repeated)
    if col_type == "RECORD":
        if isinstance(value, dict):
            return {k: _iso(v) for k, v in value.items()}
        if isinstance(value, (list, dict)):
            return _safe_json_dump(value)
        return value

    # Non-JSON scalar columns cannot accept list/dict
    if isinstance(value, (list, dict)):
        return _safe_json_dump(value)

    return value


def _normalize_row_for_table(row: Dict[str, Any], schema: Dict[str, Dict[str, str]]) -> Dict[str, Any]:
    colmap = _detect_column_names(schema)
    out: Dict[str, Any] = {}

    # Update timestamp column if present
    upd_col = colmap.get("last_updated_time")
    if upd_col and upd_col in schema:
        out[upd_col] = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

    # start with everything, then filter to schema at end
    for k, v in row.items():
        out[k] = v

    def move(logical: str):
        src_val = row.get(logical)
        phys = colmap.get(logical)
        if not phys or phys not in schema:
            return
        t = schema[phys]["type"]
        m = schema[phys]["mode"]
        out[phys] = _coerce_value_for_bq(t, m, src_val)
        if phys != logical and logical in out:
            out.pop(logical, None)

    for logical_field in [
        "language_mix",
        "top_intents",
        "mentioned_assets",
        "mentioned_amounts",
        "mentioned_locations",
        "tags",
        "summery",  # ✅ NEW
    ]:
        move(logical_field)

    # Now coerce everything else and keep only schema fields
    cleaned: Dict[str, Any] = {}
    for k, v in out.items():
        if k not in schema:
            continue
        t = schema[k]["type"]
        m = schema[k]["mode"]
        cleaned[k] = _coerce_value_for_bq(t, m, v)

    return cleaned


# ----------------------------
# BigQuery Fetch (messages)
# ----------------------------

def fetch_todays_user_messages(*, project_slug: str, target_date: Optional[str] = None) -> List[Dict[str, Any]]:
    settings = get_settings()
    if not settings.bq_enabled:
        return []

    client = get_bq_client()

    if not target_date:
        tz = ZoneInfo(settings.app_tz or "Asia/Colombo")
        target_date = datetime.now(tz).date().isoformat()

    table_ref = f"`{settings.bq_project_id}.{settings.bq_dataset_id}.{settings.bq_messages_table_id}`"

    query = f"""
    SELECT
      user_id,
      message,
      created_at
    FROM {table_ref}
    WHERE project_slug = @project_slug
      AND DATE(created_at, "Asia/Colombo") = @target_date
      AND user_id IS NOT NULL
    ORDER BY created_at ASC
    """

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("project_slug", "STRING", project_slug),
            bigquery.ScalarQueryParameter("target_date", "DATE", target_date),
        ]
    )

    rows = list(client.query(query, job_config=job_config).result())
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "user_id": r.get("user_id"),
                "message": r.get("message"),
                "created_at": r.get("created_at"),
            }
        )
    return out


# ----------------------------
# Fetch existing profiles
# ----------------------------

def fetch_existing_profiles(*, project_slug: str, user_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    settings = get_settings()
    client = get_bq_client()

    table_id = settings.bq_user_behavior_table_id
    schema = get_table_schema(settings.bq_project_id, settings.bq_dataset_id, table_id)
    table_ref = f"`{settings.bq_project_id}.{settings.bq_dataset_id}.{table_id}`"

    wanted = [
        "user_id",
        "project_slug",
        "total_messages",
        "avg_messages_per_day_30d",
        "primary_language",
        "overall_sentiment",
        "urgency_level",
        "complaint_flag",
        "customer_stage",
        "last_resolved_status",
        "mentioned_accounts",
        "confidence",
    ]

    for base in ["language_mix", "top_intents", "mentioned_assets", "mentioned_amounts", "mentioned_locations", "tags"]:
        if base in schema:
            wanted.append(base)
        if f"{base}_json" in schema:
            wanted.append(f"{base}_json")

    # ✅ NEW: bring summary column back (prefer summery)
    if "summery" in schema:
        wanted.append("summery")
    if "summary" in schema:
        wanted.append("summary")

    wanted = [c for c in wanted if c in schema]

    query = f"""
    SELECT {", ".join(wanted)}
    FROM {table_ref}
    WHERE project_slug = @project_slug
      AND user_id IN UNNEST(@user_ids)
    """

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("project_slug", "STRING", project_slug),
            bigquery.ArrayQueryParameter("user_ids", "STRING", user_ids),
        ]
    )

    rows = list(client.query(query, job_config=job_config).result())
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        d = dict(r.items())
        for k, v in list(d.items()):
            d[k] = _maybe_json_load(v)
        uid = str(d.get("user_id"))
        out[uid] = d
    return out


# ----------------------------
# Upsert Option 1 (streaming insert)
# ----------------------------

def upsert_user_behavior_rows_option1(*, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    settings = get_settings()
    client = get_bq_client()

    table_id = settings.bq_user_behavior_table_id
    schema = get_table_schema(settings.bq_project_id, settings.bq_dataset_id, table_id)

    normalized: List[Dict[str, Any]] = [_normalize_row_for_table(r, schema) for r in rows]

    key_fields = ["language_mix", "top_intents", "mentioned_assets", "mentioned_amounts", "mentioned_locations", "tags", "summery"]
    found = {k: schema.get(k) for k in key_fields if k in schema}
    if found:
        logger.info(f"[BQ_SCHEMA_KEYS] {found}")

    table_ref = f"{settings.bq_project_id}.{settings.bq_dataset_id}.{table_id}"
    errors = client.insert_rows_json(table_ref, normalized)

    processed = len(rows)
    if errors:
        return {"processed": processed, "ok": 0, "failed": processed, "errors": [str(errors)]}

    return {"processed": processed, "ok": processed, "failed": 0, "errors": []}


def verify_recent_updates(*, minutes: int = 15) -> int:
    settings = get_settings()
    client = get_bq_client()

    table_id = settings.bq_user_behavior_table_id
    schema = get_table_schema(settings.bq_project_id, settings.bq_dataset_id, table_id)

    colmap = _detect_column_names(schema)
    ts_col = colmap.get("last_updated_time")
    if not ts_col or ts_col not in schema:
        return 0

    table_ref = f"`{settings.bq_project_id}.{settings.bq_dataset_id}.{table_id}`"

    query = f"""
    SELECT COUNT(1) AS c
    FROM {table_ref}
    WHERE {ts_col} >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @mins MINUTE)
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("mins", "INT64", int(minutes))]
    )
    row = next(iter(client.query(query, job_config=job_config).result()), None)
    return int(row["c"] if row else 0)


def upsert_user_behavior_rows_merge(*, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    UPSERT using DELETE + INSERT pattern to avoid streaming buffer issues.
    Deletes existing rows, then inserts new ones.
    """
    settings = get_settings()
    client = get_bq_client()

    if not rows:
        return {"processed": 0, "ok": 0, "failed": 0, "errors": []}

    table_id = settings.bq_user_behavior_table_id
    schema = get_table_schema(settings.bq_project_id, settings.bq_dataset_id, table_id)

    normalized: List[Dict[str, Any]] = [_normalize_row_for_table(r, schema) for r in rows]

    key_fields = ["language_mix", "top_intents", "mentioned_assets", "mentioned_amounts", "mentioned_locations", "tags", "summery"]
    found = {k: schema.get(k) for k in key_fields if k in schema}
    if found:
        logger.info(f"[BQ_SCHEMA_KEYS] {found}")

    target_table_ref = f"`{settings.bq_project_id}.{settings.bq_dataset_id}.{table_id}`"

    try:
        user_project_pairs = [(r["user_id"], r["project_slug"]) for r in normalized]

        conditions = " OR ".join([
            f"(user_id = '{uid}' AND project_slug = '{ps}')"
            for uid, ps in user_project_pairs
        ])

        delete_query = f"""
        DELETE FROM {target_table_ref}
        WHERE {conditions}
        """

        logger.info(f"[UPSERT] Deleting existing rows for {len(user_project_pairs)} users")
        deleted_count = 0
        try:
            delete_job = client.query(delete_query)
            delete_job.result()
            deleted_count = delete_job.num_dml_affected_rows or 0
            logger.info(f"[UPSERT] Deleted {deleted_count} existing rows")
        except Exception as delete_error:
            logger.warning(f"[UPSERT] Delete failed (likely streaming buffer): {delete_error}")
            logger.warning(f"[UPSERT] Continuing with INSERT - may create temporary duplicates")

        logger.info(f"[UPSERT] Inserting {len(normalized)} rows")

        job_config = bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            schema=client.get_table(target_table_ref.strip('`')).schema,
        )

        load_job = client.load_table_from_json(
            normalized,
            target_table_ref.strip('`'),
            job_config=job_config,
        )

        load_job.result()
        inserted_count = load_job.output_rows or 0
        logger.info(f"[UPSERT] Inserted {inserted_count} rows")

        return {
            "processed": len(rows),
            "ok": len(rows),
            "failed": 0,
            "errors": [],
            "rows_affected": inserted_count,
            "deleted": deleted_count,
        }

    except Exception as e:
        logger.exception(f"[UPSERT] Operation failed: {e}")
        return {
            "processed": len(rows),
            "ok": 0,
            "failed": len(rows),
            "errors": [str(e)]
        }


def get_user_summary(*, user_id: str, project_slug: Optional[str] = None) -> Dict[str, Any]:
    """
    Fetch one user's summary from user_behavior table.
    Returns a normalized payload for API response handling.
    """
    settings = get_settings()
    if not settings.bq_enabled:
        return {
            "found": False,
            "message": "user data still not available",
            "user_id": user_id,
            "project_slug": project_slug,
            "summery": None,
        }

    uid = (user_id or "").strip()
    if not uid:
        raise ValueError("user_id is required")

    client = get_bq_client()
    table_id = settings.bq_user_behavior_table_id
    schema = get_table_schema(settings.bq_project_id, settings.bq_dataset_id, table_id)
    colmap = _detect_column_names(schema)

    summary_col = colmap.get("summery")
    if not summary_col or summary_col not in schema:
        return {
            "found": False,
            "message": "user data still not available",
            "user_id": uid,
            "project_slug": project_slug,
            "summery": None,
        }

    select_cols = ["user_id", summary_col]
    if "project_slug" in schema:
        select_cols.append("project_slug")

    table_ref = f"`{settings.bq_project_id}.{settings.bq_dataset_id}.{table_id}`"
    where_parts = ["user_id = @user_id"]
    query_params: List[Any] = [
        bigquery.ScalarQueryParameter("user_id", "STRING", uid),
    ]

    pslug = (project_slug or "").strip()
    if pslug and "project_slug" in schema:
        where_parts.append("project_slug = @project_slug")
        query_params.append(bigquery.ScalarQueryParameter("project_slug", "STRING", pslug))

    order_clause = ""
    ts_col = colmap.get("last_updated_time")
    if ts_col and ts_col in schema:
        order_clause = f"ORDER BY {ts_col} DESC"

    query = f"""
    SELECT {", ".join(select_cols)}
    FROM {table_ref}
    WHERE {' AND '.join(where_parts)}
    {order_clause}
    LIMIT 1
    """.strip()

    job_config = bigquery.QueryJobConfig(query_parameters=query_params)
    row = next(iter(client.query(query, job_config=job_config).result()), None)
    if not row:
        return {
            "found": False,
            "message": "user data still not available",
            "user_id": uid,
            "project_slug": pslug or None,
            "summery": None,
        }

    summary_text = row.get(summary_col)
    if summary_text is None:
        return {
            "found": False,
            "message": "user data still not available",
            "user_id": uid,
            "project_slug": row.get("project_slug") if "project_slug" in select_cols else (pslug or None),
            "summery": None,
        }

    summary_text = str(summary_text).strip()
    if not summary_text:
        return {
            "found": False,
            "message": "user data still not available",
            "user_id": uid,
            "project_slug": row.get("project_slug") if "project_slug" in select_cols else (pslug or None),
            "summery": None,
        }

    return {
        "found": True,
        "message": "ok",
        "user_id": uid,
        "project_slug": row.get("project_slug") if "project_slug" in select_cols else (pslug or None),
        "summery": summary_text,
    }
