from __future__ import annotations
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from app.services.user_behavior_service import get_user_summary

try:
    from app.core.logger import logger
except Exception:
    import logging
    logger = logging.getLogger("app_logger")


router = APIRouter()


@router.get("/user_summary")
async def user_summary_endpoint(
    user_id: str = Query(..., min_length=1, description="User ID from user_behavior table"),
    project_slug: Optional[str] = Query(default=None, description="Optional project slug filter"),
):
    """
    Returns the stored user summary (summery/summary) from user_behavior table.
    """
    try:
        result = get_user_summary(user_id=user_id, project_slug=project_slug)
        if not result.get("found"):
            return {
                "success": False,
                "message": "user data still not available",
                "user_id": user_id,
                "project_slug": project_slug,
                "summery": None,
            }

        return {
            "success": True,
            "message": "ok",
            "user_id": result.get("user_id"),
            "project_slug": result.get("project_slug"),
            "summery": result.get("summery"),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(f"[USER_SUMMARY] failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch user summary")
