from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

from app.services.intent_analysis_service import intent_analysis

try:
    from app.core.logger import logger
except Exception:
    import logging
    logger = logging.getLogger("app_logger")


router = APIRouter()


class IntentAnalysisRequest(BaseModel):
    # 3-field style: keyword + duration + frequency
    keyword: str = Field(..., min_length=1, description="Single keyword concept, e.g. phone")
    frequency: str = Field(..., description="daily | weekly | monthly | yearly")
    duration: dict = Field(..., description='{"from_date":"YYYY-MM-DD|DD-MM-YYYY","to_date":"YYYY-MM-DD|DD-MM-YYYY"}')

    @model_validator(mode="before")
    @classmethod
    def _coerce_duration(cls, data):
        if not isinstance(data, dict):
            return data
        # Backward compatibility: accept direct from_date/to_date payloads too
        if "duration" not in data:
            fd = data.get("from_date")
            td = data.get("to_date")
            if fd and td:
                data["duration"] = {"from_date": fd, "to_date": td}
        return data


@router.post("/intent_analysis")
async def intent_analysis_endpoint(payload: IntentAnalysisRequest):
    try:
        result = intent_analysis(
            keyword=payload.keyword,
            from_date=str((payload.duration or {}).get("from_date") or ""),
            to_date=str((payload.duration or {}).get("to_date") or ""),
            frequency=payload.frequency,
            model="gemini-2.5-flash",
        )
        logger.info(
            f"[INTENT_ANALYSIS][FINAL_COST_USD] "
            f"{((result.get('cost') or {}).get('total_cost_usd'))}"
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(f"[INTENT_ANALYSIS][API] failed: {e}")
        raise HTTPException(status_code=500, detail="Intent analysis failed")
