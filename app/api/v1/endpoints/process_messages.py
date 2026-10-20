# app/api/v1/endpoints/process_messages.py
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.settings import get_settings
from app.core.logger import logger
from app.services.snapshot_service import list_snapshots, get_snapshot_details, UpstreamError
from app.services.bigquery_service import insert_messages_to_bigquery

router = APIRouter()


class ProcessMessagesRequest(BaseModel):
    version: str = Field(..., example="v4")
    projectSlug: str = Field(..., example="fintra")
    accessToken: str = Field(..., example="eyJhbGciOi...")
    limit: int = Field(10, ge=1)

    snapshotsPage: int = Field(1, ge=1)
    snapshotsLimit: int = Field(20, ge=1, le=200)


class ProcessMessagesResponse(BaseModel):
    success: bool
    upstream_version: str
    projectSlug: str
    snapshot_id: str
    count: int
    messages: List[Dict[str, Any]]
    bigquery: Dict[str, Any]


def _safe_row_preview(msg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Prevent leaking too much content in logs while still being useful for debugging.
    """
    if not isinstance(msg, dict):
        return {"_type": str(type(msg)), "value_preview": str(msg)[:300]}

    out: Dict[str, Any] = {}
    for k in ("id", "user_id", "created_at", "type", "message_type", "mime_type"):
        if k in msg:
            out[k] = msg.get(k)

    m = msg.get("message")
    if m is not None:
        out["message_preview"] = str(m)[:200]

    fu = msg.get("file_url")
    if fu:
        out["file_url_preview"] = str(fu)[:80]

    return out


def _extract_first_real_errors(errors: Any) -> List[Dict[str, Any]]:
    """
    BigQuery insert errors often contain many {'reason':'stopped'} after an earlier real validation error.
    This returns the earliest real errors (sorted by index).
    """
    if not errors:
        return []

    if isinstance(errors, str):
        return [{"error": errors}]

    if isinstance(errors, list):
        dicts = [e for e in errors if isinstance(e, dict)]
        if not dicts:
            return [{"error": str(errors)[:800]}]

        def _idx(e: Dict[str, Any]) -> int:
            try:
                return int(e.get("index", 10**9))
            except Exception:
                return 10**9

        dicts_sorted = sorted(dicts, key=_idx)

        non_stopped = []
        stopped = []
        for e in dicts_sorted:
            errs = e.get("errors") or []
            reasons = [x.get("reason") for x in errs if isinstance(x, dict)]
            if any(r and r != "stopped" for r in reasons):
                non_stopped.append(e)
            else:
                stopped.append(e)

        return (non_stopped[:5] if non_stopped else stopped[:5])

    return [{"error": str(errors)[:800]}]


def _log_bigquery_errors(errors: Any, messages: List[Dict[str, Any]]) -> None:
    first = _extract_first_real_errors(errors)
    logger.error(f"[BQ_INSERT_ERRORS_COUNT] total={len(errors) if isinstance(errors, list) else 'unknown'}")

    for e in first:
        idx = e.get("index")
        logger.error(f"[BQ_INSERT_ERROR] {e}")

        if idx is None:
            continue

        try:
            idx_i = int(idx)
        except Exception:
            continue

        if 0 <= idx_i < len(messages):
            logger.error(f"[BQ_FAILING_ROW_PREVIEW] index={idx_i} row={_safe_row_preview(messages[idx_i])}")


def _get_decrypt_snapshot_callable():
    """
    Lazy resolver so Gunicorn doesn't crash on import.
    Prefers toolkit if available; otherwise uses our safe service wrapper.
    """
    try:
        from oleeaitoolkit.cryptography import decrypt_snapshot as toolkit_decrypt  # type: ignore
        return toolkit_decrypt
    except Exception as e:
        logger.warning(f"[DECRYPT_IMPORT] toolkit decrypt_snapshot unavailable: {type(e).__name__}: {e}")

    from app.services.snapshot_decrypt_service import decrypt_snapshot as local_decrypt
    return local_decrypt


@router.post("/process_messages", response_model=ProcessMessagesResponse)
async def process_messages(payload: ProcessMessagesRequest):
    """
    Process messages from upstream snapshots and store in BigQuery.

    Steps:
    1. List snapshots from upstream API
    2. Get latest snapshot details (encryption key + file URL)
    3. Decrypt snapshot to extract messages
    4. Insert messages into BigQuery
    """
    settings = get_settings()

    logger.info(
        f"[PROCESS_MESSAGES] Starting processing for project={payload.projectSlug}, "
        f"version={payload.version}, limit={payload.limit}"
    )

    # ==========================================
    # Step 1: LIST snapshots
    # ==========================================
    try:
        logger.info(
            f"[PROCESS_MESSAGES] Fetching snapshots list (page={payload.snapshotsPage}, limit={payload.snapshotsLimit})"
        )

        snapshots_json = await list_snapshots(
            base_url=settings.upstream_base_url,
            upstream_version=payload.version,
            project_slug=payload.projectSlug,
            access_token=payload.accessToken,
            page=payload.snapshotsPage,
            limit=payload.snapshotsLimit,
        )

        logger.info("[PROCESS_MESSAGES] Successfully fetched snapshots")

    except UpstreamError as e:
        logger.error(f"[PROCESS_MESSAGES] Upstream API error while listing snapshots: {e}")
        raise HTTPException(
            status_code=502,
            detail={
                "error": "upstream_api_error",
                "message": "Failed to fetch snapshots from upstream API",
                "upstream_details": e.to_dict(),
            },
        )
    except ConnectionError as e:
        logger.error(f"[PROCESS_MESSAGES] Connection error to upstream API: {e}")
        raise HTTPException(
            status_code=503,
            detail={
                "error": "connection_error",
                "message": "Cannot connect to upstream snapshot service",
                "details": str(e),
                "hint": "Check if upstream service is running and accessible",
            },
        )
    except TimeoutError as e:
        logger.error(f"[PROCESS_MESSAGES] Timeout connecting to upstream API: {e}")
        raise HTTPException(
            status_code=504,
            detail={
                "error": "timeout_error",
                "message": "Upstream service took too long to respond",
                "details": str(e),
            },
        )
    except Exception as e:
        logger.exception(f"[PROCESS_MESSAGES] Unexpected error listing snapshots: {e}")
        raise HTTPException(
            status_code=500,
            detail={
                "error": "unexpected_error",
                "message": "An unexpected error occurred while fetching snapshots",
                "details": str(e),
            },
        )

    items = (((snapshots_json or {}).get("data") or {}).get("items")) or []
    if not items:
        logger.warning(f"[PROCESS_MESSAGES] No snapshots found for project={payload.projectSlug}")
        raise HTTPException(
            status_code=404,
            detail={
                "error": "no_snapshots",
                "message": f"No snapshots found for project '{payload.projectSlug}'",
                "project_slug": payload.projectSlug,
            },
        )

    latest_snapshot_id = items[0].get("id")
    if not latest_snapshot_id:
        logger.error("[PROCESS_MESSAGES] Snapshot response missing 'id' field")
        raise HTTPException(
            status_code=502,
            detail={
                "error": "invalid_upstream_response",
                "message": "Upstream snapshots response missing 'id' field",
                "snapshot_data": items[0] if items else None,
            },
        )

    logger.info(f"[PROCESS_MESSAGES] Latest snapshot ID: {latest_snapshot_id}")

    # ==========================================
    # Step 2: GET snapshot details
    # ==========================================
    try:
        logger.info(f"[PROCESS_MESSAGES] Fetching details for snapshot_id={latest_snapshot_id}")

        details_json = await get_snapshot_details(
            base_url=settings.upstream_base_url,
            upstream_version=payload.version,
            project_slug=payload.projectSlug,
            snapshot_id=latest_snapshot_id,
            access_token=payload.accessToken,
        )

        logger.info("[PROCESS_MESSAGES] Successfully fetched snapshot details")

    except UpstreamError as e:
        logger.error(f"[PROCESS_MESSAGES] Upstream API error while getting snapshot details: {e}")
        raise HTTPException(
            status_code=502,
            detail={
                "error": "upstream_api_error",
                "message": "Failed to fetch snapshot details from upstream API",
                "snapshot_id": latest_snapshot_id,
                "upstream_details": e.to_dict(),
            },
        )
    except ConnectionError as e:
        logger.error(f"[PROCESS_MESSAGES] Connection error to upstream API: {e}")
        raise HTTPException(
            status_code=503,
            detail={
                "error": "connection_error",
                "message": "Cannot connect to upstream snapshot service",
                "snapshot_id": latest_snapshot_id,
                "details": str(e),
            },
        )
    except Exception as e:
        logger.exception(f"[PROCESS_MESSAGES] Unexpected error getting snapshot details: {e}")
        raise HTTPException(
            status_code=500,
            detail={
                "error": "unexpected_error",
                "message": "An unexpected error occurred while fetching snapshot details",
                "snapshot_id": latest_snapshot_id,
                "details": str(e),
            },
        )

    details_data = (details_json or {}).get("data") or {}
    snapshot_private_key = details_data.get("snapshot_private_key")
    file_url = details_data.get("file_url")

    if not snapshot_private_key or not file_url:
        logger.error(
            "[PROCESS_MESSAGES] Snapshot details incomplete: "
            f"has_key={bool(snapshot_private_key)}, has_url={bool(file_url)}"
        )
        raise HTTPException(
            status_code=502,
            detail={
                "error": "incomplete_snapshot_details",
                "message": "Snapshot details missing required fields",
                "snapshot_id": latest_snapshot_id,
                "has_private_key": bool(snapshot_private_key),
                "has_file_url": bool(file_url),
            },
        )

    logger.info("[PROCESS_MESSAGES] Snapshot details validated, file_url present")

    # ==========================================
    # Step 3: Decrypt snapshot
    # ==========================================
    try:
        logger.info(f"[PROCESS_MESSAGES] Decrypting snapshot from {str(file_url)[:50]}...")

        decrypt_snapshot = _get_decrypt_snapshot_callable()
        decrypted = decrypt_snapshot(file_url, snapshot_private_key)

        if not decrypted or not isinstance(decrypted, dict):
            raise ValueError("Decryption returned invalid data structure")

        all_messages = decrypted.get("messages")
        if not isinstance(all_messages, list):
            raise ValueError(f"Expected 'messages' to be a list, got {type(all_messages)}")

        logger.info(f"[PROCESS_MESSAGES] Successfully decrypted {len(all_messages)} messages")

    except ValueError as e:
        logger.error(f"[PROCESS_MESSAGES] Invalid decrypted data structure: {e}")
        raise HTTPException(
            status_code=502,
            detail={
                "error": "invalid_decrypted_data",
                "message": "Decrypted snapshot has invalid data structure",
                "snapshot_id": latest_snapshot_id,
                "details": str(e),
            },
        )
    except Exception as e:
        # This will now include DecryptSnapshotUnavailable diagnostic detail
        logger.exception(f"[PROCESS_MESSAGES] Decryption failed: {type(e).__name__}: {e}")
        raise HTTPException(
            status_code=500,
            detail={
                "error": "decryption_failed",
                "message": "Failed to decrypt snapshot",
                "snapshot_id": latest_snapshot_id,
                "details": f"{type(e).__name__}: {str(e)}",
                "hint": "If toolkit decrypt is missing, deploy a toolkit version that includes oleeaitoolkit.cryptography",
            },
        )

    # Limit messages
    top_messages = all_messages[: payload.limit]
    logger.info(f"[PROCESS_MESSAGES] Selected top {len(top_messages)} messages (limit={payload.limit})")

    # ==========================================
    # Step 4: Insert into BigQuery
    # ==========================================
    bq_result: Dict[str, Any] = {"enabled": False, "inserted": 0, "errors": []}

    if settings.bq_enabled:
        try:
            logger.info(f"[PROCESS_MESSAGES] Inserting {len(top_messages)} messages into BigQuery")

            bq_result = insert_messages_to_bigquery(
                upstream_version=payload.version,
                project_slug=payload.projectSlug,
                snapshot_id=latest_snapshot_id,
                messages=top_messages,
            )

            inserted = int(bq_result.get("inserted", 0) or 0)
            errors = bq_result.get("errors") or []

            if inserted > 0:
                logger.info(f"[PROCESS_MESSAGES] Successfully inserted {inserted} messages into BigQuery")
            else:
                logger.warning(
                    "[PROCESS_MESSAGES] BigQuery insert completed but no rows inserted. "
                    "Logging first real errors (if any)."
                )
                _log_bigquery_errors(errors, top_messages)

        except ConnectionError as e:
            error_msg = f"Cannot connect to BigQuery service: {e}"
            logger.error(f"[PROCESS_MESSAGES] {error_msg}")
            bq_result = {
                "enabled": True,
                "inserted": 0,
                "errors": [error_msg],
                "hint": "Check BigQuery credentials and network connectivity",
            }
        except Exception as e:
            error_msg = f"BigQuery insert failed: {e}"
            logger.exception(f"[PROCESS_MESSAGES] {error_msg}")
            bq_result = {"enabled": True, "inserted": 0, "errors": [error_msg]}
    else:
        logger.info("[PROCESS_MESSAGES] BigQuery is disabled, skipping insert")
        bq_result = {"enabled": False, "inserted": 0, "errors": [], "message": "BigQuery is disabled in settings"}

    logger.info(
        "[PROCESS_MESSAGES] Processing complete: "
        f"snapshot_id={latest_snapshot_id}, "
        f"messages_count={len(top_messages)}, "
        f"bq_inserted={int(bq_result.get('inserted', 0) or 0)}"
    )

    return {
        "success": True,
        "upstream_version": payload.version,
        "projectSlug": payload.projectSlug,
        "snapshot_id": latest_snapshot_id,
        "count": len(top_messages),
        "messages": top_messages,
        "bigquery": bq_result,
    }