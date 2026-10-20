from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

try:
    from app.core.logger import logger
except Exception:
    import logging
    logger = logging.getLogger("oleeaitoolkit")

# Import the service function correctly
from app.services.search_details_service import search_details as search_details_service

router = APIRouter()


class SearchDetailsRequest(BaseModel):
    # Accept both styles: project_slug OR nested project.slug
    project_slug: Optional[str] = None

    question: str
    model: str = "gemini-2.5-flash"

    user_id: Optional[str] = None
    messages: List[Dict[str, Any]] = Field(default_factory=list)

    use_gemini: bool = True
    min_confidence_to_answer: float = 0.1
    log_checkpoints: bool = True

    @model_validator(mode="before")
    @classmethod
    def _coerce_input(cls, data: Any) -> Any:
        """
        Lets you send payloads like:
        {
          "project": {"slug": "fintra"},
          "question": "...",
          ...
        }
        OR
        {
          "project_slug": "fintra",
          "question": "...",
          ...
        }
        """
        if isinstance(data, dict):
            # support nested project.slug
            if not data.get("project_slug") and isinstance(data.get("project"), dict):
                data["project_slug"] = data["project"].get("slug") or data["project"].get("project_slug")

            # optional alias: if someone sends query instead of question
            if not data.get("question") and data.get("query"):
                data["question"] = data["query"]

        return data


@router.post("/search_details")
async def search_details_endpoint(payload: SearchDetailsRequest):
    try:
        # must pass kwargs, not positional dict
        result = await search_details_service(**payload.model_dump())
        total_cost_usd = (((result.get("meta") or {}).get("cost") or {}).get("total_cost_usd"))
        logger.info(f"[SEARCH_DETAILS][FINAL_COST_USD] {total_cost_usd}")
        return result
    except Exception as e:
        logger.exception(f"[SEARCH_DETAILS][API] failed: {e}")
        raise HTTPException(status_code=500, detail="Search details failed")
