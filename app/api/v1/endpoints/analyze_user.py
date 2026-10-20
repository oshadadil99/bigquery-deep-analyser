# app/api/v1/endpoints/analyze_user.py
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional
from datetime import datetime

from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field

from app.core.settings import get_settings

from app.services.user_behavior_service import (
    fetch_todays_user_messages,
    fetch_existing_profiles,
    upsert_user_behavior_rows_merge,
    verify_recent_updates,
)

from app.services.gemini_service import analyze_users_with_gemini_batch

try:
    from app.core.logger import logger
except Exception:
    import logging
    logger = logging.getLogger("app_logger")

router = APIRouter()

# In-memory job storage (use Redis in production for multiple dynos)
job_results: Dict[str, Dict[str, Any]] = {}


class AnalyzeUserRequest(BaseModel):
    project_slug: str = Field(..., min_length=1)
    target_date: Optional[str] = None

    primary_language_categories: List[str] = Field(default_factory=list)
    overall_sentiment_categories: List[str] = Field(default_factory=list)
    urgency_level_categories: List[str] = Field(default_factory=list)
    customer_stage_categories: List[str] = Field(default_factory=list)
    last_resolved_status_categories: List[str] = Field(default_factory=list)
    top_intent_categories: List[str] = Field(default_factory=list)

    model: str = Field(default="gemini-2.5-flash-lite")
    max_concurrency: int = Field(default=1, ge=1, le=10)
    use_gemini: bool = Field(default=True)

    log_checkpoints: bool = Field(default=True)
    log_samples: bool = Field(default=True)


def _log_cp(enabled: bool, name: str, data: Dict[str, Any]) -> None:
    if not enabled:
        return
    logger.info(f"[CHECKPOINT] {name} => {data}")


def _build_non_hypothesis_summary(
    *,
    msgs: List[Dict[str, Any]],
    row: Dict[str, Any],
    max_msgs: int = 3,
    max_chars: int = 160,
) -> str:
    """
    Builds a summary using ONLY:
      - actual message text
      - extracted fields already in row (intents/assets/amounts/locations/tags)
      - static categories already in row (sentiment/urgency/stage/status/language)
    No guesses, no assumptions.
    """
    previews: List[str] = []
    for m in (msgs or [])[-max_msgs:]:
        t = (m.get("message") or "").replace("\n", " ").strip()
        if t:
            previews.append(t[:max_chars])

    def _fmt_list(key: str, limit: int = 8) -> Optional[str]:
        v = row.get(key) or []
        if isinstance(v, list) and v:
            return ", ".join([str(x) for x in v[:limit]])
        return None

    signals: List[str] = []
    for k, label in [
        ("primary_language", "Language"),
        ("overall_sentiment", "Sentiment"),
        ("urgency_level", "Urgency"),
        ("customer_stage", "Stage"),
        ("last_resolved_status", "LastStatus"),
    ]:
        v = row.get(k)
        if v:
            signals.append(f"{label}={v}")

    intents = _fmt_list("top_intents", limit=5)
    assets = _fmt_list("mentioned_assets")
    amounts = _fmt_list("mentioned_amounts")
    locations = _fmt_list("mentioned_locations")
    tags = _fmt_list("tags", limit=8)

    parts: List[str] = []

    if previews:
        parts.append("Recent messages: " + " | ".join(previews))

    if signals or intents:
        s = []
        if signals:
            s.append(", ".join(signals))
        if intents:
            s.append(f"TopIntents=[{intents}]")
        parts.append("Signals: " + " ; ".join(s))

    entities_bits: List[str] = []
    if assets:
        entities_bits.append(f"Assets=[{assets}]")
    if amounts:
        entities_bits.append(f"Amounts=[{amounts}]")
    if locations:
        entities_bits.append(f"Locations=[{locations}]")
    if entities_bits:
        parts.append("Entities: " + " ; ".join(entities_bits))

    if tags:
        parts.append(f"Tags=[{tags}]")

    if not parts:
        return "No summary available: no messages or extracted signals found for this user."

    out = ". ".join(parts).strip()
    return out[:1200]


async def _process_analysis_background(job_id: str, payload: AnalyzeUserRequest):
    """Background task that does the heavy processing"""
    settings = get_settings()
    
    job_results[job_id]["status"] = "processing"
    job_results[job_id]["started_at"] = datetime.utcnow().isoformat()

    checkpoints: Dict[str, Any] = {
        "bq_fetch": {"status": "pending"},
        "grouping": {"status": "pending"},
        "existing_profiles": {"status": "pending"},
        "gemini": {"status": "pending", "per_user": []},
        "upsert": {"status": "pending"},
        "verify": {"status": "pending"},
    }

    try:
        # Step 1: Fetch messages
        logger.info(f"[JOB {job_id}] Fetching messages for {payload.project_slug}")
        today_rows = fetch_todays_user_messages(
            project_slug=payload.project_slug,
            target_date=payload.target_date,
        )
        checkpoints["bq_fetch"] = {"status": "ok", "rows": len(today_rows)}
        job_results[job_id]["checkpoints"] = checkpoints

        if not today_rows:
            checkpoints["grouping"] = {"status": "ok", "users_found": 0}
            checkpoints["existing_profiles"] = {"status": "ok", "found": 0}
            checkpoints["gemini"] = {"status": "skipped", "per_user": []}
            checkpoints["upsert"] = {"status": "skipped"}
            checkpoints["verify"] = {"status": "skipped", "updated_last_15m": 0}
            
            job_results[job_id]["status"] = "completed"
            job_results[job_id]["completed_at"] = datetime.utcnow().isoformat()
            job_results[job_id]["result"] = {
                "success": True,
                "project_slug": payload.project_slug,
                "users_found": 0,
                "processed_profiles": 0,
                "upsert": {"processed": 0, "ok": 0, "failed": 0, "errors": []},
                "checkpoints": checkpoints,
            }
            return

        # Step 2: Group by user_id
        by_user: Dict[str, List[Dict[str, Any]]] = {}
        for r in today_rows:
            uid = r.get("user_id")
            if not uid:
                continue
            by_user.setdefault(str(uid), []).append(r)

        user_ids = sorted(by_user.keys())
        checkpoints["grouping"] = {"status": "ok", "users_found": len(user_ids)}
        job_results[job_id]["checkpoints"] = checkpoints
        logger.info(f"[JOB {job_id}] Found {len(user_ids)} users")

        # Step 3: Fetch existing profiles
        existing_profiles = fetch_existing_profiles(
            project_slug=payload.project_slug,
            user_ids=user_ids,
        )
        checkpoints["existing_profiles"] = {"status": "ok", "found": len(existing_profiles)}
        job_results[job_id]["checkpoints"] = checkpoints

        # Step 4: Gemini classify
        static_categories = {
            "primary_language": payload.primary_language_categories,
            "overall_sentiment": payload.overall_sentiment_categories,
            "urgency_level": payload.urgency_level_categories,
            "customer_stage": payload.customer_stage_categories,
            "last_resolved_status": payload.last_resolved_status_categories,
            "top_intents": payload.top_intent_categories,
        }

        dynamic_fields = [
            "mentioned_assets",
            "mentioned_amounts",
            "mentioned_locations",
            "mentioned_accounts",
            "tags",
            "confidence",
            "language_mix",
            "summery",
        ]

        gemini_results_by_user: Dict[str, Dict[str, Any]] = {}
        if payload.use_gemini:
            logger.info(f"[JOB {job_id}] Running Gemini analysis")
            gemini_results_by_user, gemini_checkpoint = await analyze_users_with_gemini_batch(
                project_slug=payload.project_slug,
                by_user=by_user,
                existing_profiles=existing_profiles,
                model=payload.model,
                static_categories=static_categories,
                dynamic_fields=dynamic_fields,
                max_concurrency=payload.max_concurrency,
            )
            checkpoints["gemini"] = gemini_checkpoint
            job_results[job_id]["checkpoints"] = checkpoints
        else:
            checkpoints["gemini"] = {"status": "skipped", "per_user": []}

        # Step 5: Build rows
        profiles_to_upsert: List[Dict[str, Any]] = []

        for uid, msgs in by_user.items():
            prev = existing_profiles.get(uid, {}) or {}
            total_prev = int(prev.get("total_messages") or 0)
            total_now = total_prev + len(msgs)

            row: Dict[str, Any] = {
                "user_id": uid,
                "project_slug": payload.project_slug,
                "total_messages": total_now,
                "avg_messages_per_day_30d": float(prev.get("avg_messages_per_day_30d") or 0.0),
            }

            g = gemini_results_by_user.get(uid)

            if g:
                row["primary_language"] = g.get("primary_language")
                row["overall_sentiment"] = g.get("overall_sentiment")
                row["urgency_level"] = g.get("urgency_level")
                row["customer_stage"] = g.get("customer_stage")
                row["last_resolved_status"] = g.get("last_resolved_status")
                row["complaint_flag"] = bool(g.get("complaint_flag") or False)
                row["mentioned_accounts"] = bool(g.get("mentioned_accounts") or False)
                row["confidence"] = float(g.get("confidence") or 0.5)
                row["language_mix"] = g.get("language_mix") or {"si": 0, "ta": 0, "en": 0, "mixed": 0}
                row["top_intents"] = g.get("top_intents") or []
                row["mentioned_assets"] = g.get("mentioned_assets") or []
                row["mentioned_amounts"] = g.get("mentioned_amounts") or []
                row["mentioned_locations"] = g.get("mentioned_locations") or []
                row["tags"] = g.get("tags") or []
                row["summery"] = (g.get("summery") or "").strip()
            else:
                row["primary_language"] = prev.get("primary_language")
                row["overall_sentiment"] = prev.get("overall_sentiment")
                row["urgency_level"] = prev.get("urgency_level")
                row["customer_stage"] = prev.get("customer_stage")
                row["last_resolved_status"] = prev.get("last_resolved_status")
                row["complaint_flag"] = bool(prev.get("complaint_flag") or False)
                row["mentioned_accounts"] = bool(prev.get("mentioned_accounts") or False)
                row["confidence"] = float(prev.get("confidence") or 0.3)
                row["language_mix"] = prev.get("language_mix") or {"si": 0, "ta": 0, "en": 0, "mixed": 0}
                row["top_intents"] = prev.get("top_intents") or []
                row["mentioned_assets"] = prev.get("mentioned_assets") or []
                row["mentioned_amounts"] = prev.get("mentioned_amounts") or []
                row["mentioned_locations"] = prev.get("mentioned_locations") or []
                row["tags"] = prev.get("tags") or []
                row["summery"] = (prev.get("summery") or "").strip()

            if not (row.get("summery") or "").strip():
                row["summery"] = _build_non_hypothesis_summary(msgs=msgs, row=row)

            profiles_to_upsert.append(row)

        # Step 6: Upsert
        logger.info(f"[JOB {job_id}] Upserting {len(profiles_to_upsert)} profiles")
        upsert_result = upsert_user_behavior_rows_merge(rows=profiles_to_upsert)
        checkpoints["upsert"] = {"status": "ok", **upsert_result}
        job_results[job_id]["checkpoints"] = checkpoints

        # Step 7: Verify
        updated_count = verify_recent_updates(minutes=15)
        checkpoints["verify"] = {"status": "ok", "updated_last_15m": updated_count}
        job_results[job_id]["checkpoints"] = checkpoints

        # Mark as completed
        job_results[job_id]["status"] = "completed"
        job_results[job_id]["completed_at"] = datetime.utcnow().isoformat()
        job_results[job_id]["result"] = {
            "success": True,
            "project_slug": payload.project_slug,
            "target_date": payload.target_date,
            "model": payload.model,
            "users_found": len(user_ids),
            "processed_profiles": len(profiles_to_upsert),
            "upsert": upsert_result,
            "checkpoints": checkpoints,
        }
        
        logger.info(f"[JOB {job_id}] Completed successfully")

    except Exception as e:
        logger.exception(f"[JOB {job_id}] Failed: {e}")
        job_results[job_id]["status"] = "failed"
        job_results[job_id]["completed_at"] = datetime.utcnow().isoformat()
        job_results[job_id]["error"] = str(e)
        job_results[job_id]["checkpoints"] = checkpoints


@router.post("/analyze_user")
async def analyze_user(payload: AnalyzeUserRequest, background_tasks: BackgroundTasks):
    """
    Starts user analysis in background. Returns immediately with job_id.
    """
    job_id = str(uuid.uuid4())
    
    # Initialize job
    job_results[job_id] = {
        "job_id": job_id,
        "status": "processing",
        "project_slug": payload.project_slug,
        "target_date": payload.target_date,
        "model": payload.model,
        "created_at": datetime.utcnow().isoformat(),
        "checkpoints": {},
    }
    
    # Add to background tasks
    background_tasks.add_task(_process_analysis_background, job_id, payload)
    
    logger.info(f"[ANALYZE_USER] Started job {job_id} for project {payload.project_slug}")
    
    # Return immediately
    return {
        "success": True,
        "status": "processing",
        "job_id": job_id,
        "message": f"Analysis started in background. Poll GET /v1/analyze_user/status/{job_id} to check progress.",
        "status_url": f"/v1/analyze_user/status/{job_id}",
    }


@router.get("/analyze_user/status/{job_id}")
async def get_job_status(job_id: str):
    """
    Check the status of an analysis job.
    Returns: processing, completed, or failed
    """
    if job_id not in job_results:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    
    return job_results[job_id]