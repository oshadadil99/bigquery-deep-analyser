from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query, Path
from typing import Any, Dict, List, Optional

from app.core.settings import get_settings
from app.core.logger import logger
from app.services.snapshot_service import (
    fetch_snapshots_list,
    pick_latest_snapshot_id,
    fetch_snapshot_detail,
    extract_private_key_and_file_url,
)
from app.services.snapshot_decrypt_service import decrypt_snapshot_async

router = APIRouter()


def extract_bearer_token(authorization: Optional[str]) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Authorization must be Bearer token")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Empty Bearer token")
    return token


@router.get("/{version}/oleon/convos/{projectSlug}/get_messages")
async def get_messages(
    version: str = Path(..., description="API version like v1/v4"),
    projectSlug: str = Path(..., description="Project slug"),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    limit: int = Query(10, ge=1, le=200, description="How many messages to return"),
    snapshots_page: int = Query(1, ge=1, description="Snapshots list page"),
    snapshots_limit: int = Query(20, ge=1, le=100, description="Snapshots list limit"),
) -> Dict[str, Any]:
    """
    One endpoint does everything:
    - uses Bearer token from request
    - fetch snapshots list
    - pick latest snapshot id
    - fetch snapshot detail
    - decrypt snapshot file
    - return top N messages
    """
    settings = get_settings()
    token = extract_bearer_token(authorization)

    try:
        # 1) list snapshots
        list_payload = await fetch_snapshots_list(
            upstream_base=settings.upstream_base,
            bearer_token=token,
            project_slug=projectSlug,
            page=snapshots_page,
            limit=snapshots_limit,
        )

        # 2) latest snapshot id
        latest_id = pick_latest_snapshot_id(list_payload)
        if not latest_id:
            raise HTTPException(status_code=404, detail="No snapshots found for this project")

        logger.info(f"[GET_MESSAGES] latest_snapshot_id={latest_id}")

        # 3) snapshot detail
        detail_payload = await fetch_snapshot_detail(
            upstream_base=settings.upstream_base,
            bearer_token=token,
            project_slug=projectSlug,
            snapshot_id=latest_id,
        )

        private_key, file_url = extract_private_key_and_file_url(detail_payload)
        if not private_key or not file_url:
            raise HTTPException(status_code=500, detail="snapshot_private_key or file_url missing from snapshot detail")

        # 4) decrypt
        decrypted = await decrypt_snapshot_async(file_url, private_key)

        messages = decrypted.get("messages", [])
        if not isinstance(messages, list):
            raise HTTPException(status_code=500, detail="Decrypted messages format invalid")

        top_messages: List[Dict[str, Any]] = messages[:limit]

        return {
            "success": True,
            "version": version,
            "projectSlug": projectSlug,
            "snapshot_id": latest_id,
            "count": len(top_messages),
            "messages": top_messages,
        }

    except HTTPException:
        raise
    except Exception as exc:
        # clean error output
        logger.error(f"[GET_MESSAGES] Failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))
