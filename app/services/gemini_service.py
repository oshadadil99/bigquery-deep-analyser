# app/services/gemini_service.py
from __future__ import annotations

import asyncio
import json
import random
import re
from typing import Any, Dict, List, Tuple

import vertexai
from vertexai.generative_models import GenerativeModel

from app.core.settings import get_settings

try:
    from google.api_core.exceptions import ResourceExhausted, TooManyRequests
except Exception:
    ResourceExhausted = Exception  # type: ignore
    TooManyRequests = Exception  # type: ignore

try:
    from app.core.logger import logger
except Exception:
    import logging
    logger = logging.getLogger("app_logger")


def _is_rate_limit_error(e: Exception) -> bool:
    msg = str(e).lower()
    if "429" in msg:
        return True
    if "resource exhausted" in msg:
        return True
    if "too many requests" in msg:
        return True
    if "quota" in msg and ("exceeded" in msg or "exhaust" in msg):
        return True
    return False


def _extract_json(text: str) -> Dict[str, Any]:
    if not text:
        return {}

    text = text.strip()
    text = re.sub(r"^```json\s*", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"^```\s*", "", text).strip()
    text = re.sub(r"\s*```$", "", text).strip()

    try:
        return json.loads(text)
    except Exception:
        pass

    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return {}

    return {}


def _build_prompt(
    user_id: str,
    project_slug: str,
    messages: List[Dict[str, Any]],
    prev_profile: Dict[str, Any],
    static_categories: Dict[str, List[str]],
    dynamic_fields: List[str],
    *,
    max_msgs: int = 50,
    max_chars_per_msg: int = 500,
) -> str:
    msgs_in = messages[-max_msgs:] if max_msgs > 0 else messages

    msg_texts: List[str] = []
    for m in msgs_in:
        t = m.get("message") or m.get("text") or m.get("content") or ""
        if t:
            msg_texts.append(str(t)[:max_chars_per_msg])

    prev = {
        "primary_language": prev_profile.get("primary_language"),
        "language_mix": prev_profile.get("language_mix"),
        "top_intents": prev_profile.get("top_intents"),
        "overall_sentiment": prev_profile.get("overall_sentiment"),
        "urgency_level": prev_profile.get("urgency_level"),
        "customer_stage": prev_profile.get("customer_stage"),
        "last_resolved_status": prev_profile.get("last_resolved_status"),
        "complaint_flag": prev_profile.get("complaint_flag"),
        "mentioned_assets": prev_profile.get("mentioned_assets"),
        "mentioned_amounts": prev_profile.get("mentioned_amounts"),
        "mentioned_locations": prev_profile.get("mentioned_locations"),
        "mentioned_accounts": prev_profile.get("mentioned_accounts"),
        "tags": prev_profile.get("tags"),
        "confidence": prev_profile.get("confidence"),
        "summery": prev_profile.get("summery"),
    }

    return f"""
You are analyzing customer chat messages to update a user behavior profile.

PROJECT: {project_slug}
USER_ID: {user_id}

TODAY_MESSAGES (latest):
{json.dumps(msg_texts, ensure_ascii=False)}

PREVIOUS_PROFILE (may be null/empty):
{json.dumps(prev, ensure_ascii=False)}

STATIC CATEGORIES (you MUST choose from these):
- primary_language: {static_categories.get("primary_language", [])}
- overall_sentiment: {static_categories.get("overall_sentiment", [])}
- urgency_level: {static_categories.get("urgency_level", [])}
- customer_stage: {static_categories.get("customer_stage", [])}
- last_resolved_status: {static_categories.get("last_resolved_status", [])}
- top_intents: {static_categories.get("top_intents", [])}

DYNAMIC FIELDS (model decides values):
{dynamic_fields}

Return ONLY valid JSON (no markdown, no explanations) with this schema:

{{
  "primary_language": string | null,
  "language_mix": {{"si": number, "ta": number, "en": number, "mixed": number}},
  "top_intents": [string],
  "overall_sentiment": string | null,
  "urgency_level": string | null,
  "customer_stage": string | null,
  "last_resolved_status": string | null,
  "complaint_flag": boolean,
  "mentioned_assets": [string],
  "mentioned_amounts": [string],
  "mentioned_locations": [string],
  "mentioned_accounts": boolean,
  "tags": [string],
  "confidence": number,
  "summery": string
}}

Rules:
- If unknown, use null (for static string fields) or empty [] for lists.
- confidence must be between 0 and 1.
- top_intents must only contain values from the given top_intents category list.
- For primary_language / sentiment / urgency / stage / status: MUST be one of provided categories or null.
- summery MUST NOT be empty. If there is not enough info, return:
  "No meaningful signals found in today's messages for this user."
""".strip()


async def analyze_users_with_gemini_batch(
    *,
    project_slug: str,
    by_user: Dict[str, List[Dict[str, Any]]],
    existing_profiles: Dict[str, Dict[str, Any]],
    model: str,
    static_categories: Dict[str, List[str]],
    dynamic_fields: List[str],
    max_concurrency: int = 2,
    log_raw: bool = True,
    raw_max_chars: int = 1200,
    batch_size: int = 25,
    inter_batch_delay_s: float = 1.0,
    max_retries: int = 5,
    retry_base_delay_s: float = 1.0,
    retry_max_delay_s: float = 30.0,
    max_msgs_per_user: int = 50,
    max_chars_per_msg: int = 500,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    settings = get_settings()

    vertexai.init(project=settings.vertex_project_id, location=settings.vertex_location)
    gen_model = GenerativeModel(model)

    sem = asyncio.Semaphore(max(1, int(max_concurrency)))

    results: Dict[str, Dict[str, Any]] = {}
    per_user: List[Dict[str, Any]] = []

    async def _generate_with_retry(prompt: str) -> str:
        attempt = 0
        while True:
            try:
                resp = await asyncio.to_thread(gen_model.generate_content, prompt)
                return getattr(resp, "text", "") or ""
            except (ResourceExhausted, TooManyRequests) as e:
                if attempt >= max_retries:
                    raise
                delay = min(retry_max_delay_s, retry_base_delay_s * (2 ** attempt))
                delay = delay + random.uniform(0.0, 0.25 * delay)
                await asyncio.sleep(delay)
                attempt += 1
            except Exception as e:
                if _is_rate_limit_error(e) and attempt < max_retries:
                    delay = min(retry_max_delay_s, retry_base_delay_s * (2 ** attempt))
                    delay = delay + random.uniform(0.0, 0.25 * delay)
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue
                raise

    async def _one(user_id: str, msgs: List[Dict[str, Any]]):
        prev = existing_profiles.get(user_id, {}) or {}
        prompt = _build_prompt(
            user_id=user_id,
            project_slug=project_slug,
            messages=msgs,
            prev_profile=prev,
            static_categories=static_categories,
            dynamic_fields=dynamic_fields,
            max_msgs=max_msgs_per_user,
            max_chars_per_msg=max_chars_per_msg,
        )

        async with sem:
            try:
                text = await _generate_with_retry(prompt)

                if log_raw:
                    preview = (text[:raw_max_chars] if text else "")
                    logger.info(f"[GEMINI_RAW_PREVIEW] user={user_id} chars={len(text)} preview={preview}")

                data = _extract_json(text)

                data.setdefault("language_mix", {"si": 0, "ta": 0, "en": 0, "mixed": 0})
                data.setdefault("top_intents", [])
                data.setdefault("complaint_flag", False)
                data.setdefault("mentioned_assets", [])
                data.setdefault("mentioned_amounts", [])
                data.setdefault("mentioned_locations", [])
                data.setdefault("mentioned_accounts", False)
                data.setdefault("tags", [])
                if "confidence" not in data or data["confidence"] is None:
                    data["confidence"] = 0.5

                # ✅ NEW: ensure summery key exists (even if model forgot)
                if "summery" not in data or data["summery"] is None:
                    data["summery"] = ""

                results[user_id] = data
                per_user.append({"user_id": user_id, "status": "ok", "raw_preview": (text[:350] if text else "")})

            except Exception as e:
                per_user.append({"user_id": user_id, "status": "fail", "error": str(e)})

    items = list(by_user.items())
    if batch_size <= 0:
        batch_size = 25

    for start in range(0, len(items), batch_size):
        batch = items[start : start + batch_size]
        await asyncio.gather(*[_one(uid, msgs) for uid, msgs in batch])

        if inter_batch_delay_s and (start + batch_size) < len(items):
            await asyncio.sleep(float(inter_batch_delay_s))

    ok = sum(1 for x in per_user if x.get("status") == "ok")
    failed = sum(1 for x in per_user if x.get("status") == "fail")

    checkpoint = {
        "status": ("ok" if failed == 0 else ("partial" if ok > 0 else "fail")),
        "per_user": per_user,
        "ok": ok,
        "failed": failed,
        "total": len(per_user),
        "batching": {
            "batch_size": batch_size,
            "inter_batch_delay_s": inter_batch_delay_s,
            "max_concurrency": max(1, int(max_concurrency)),
            "max_retries": max_retries,
            "retry_base_delay_s": retry_base_delay_s,
            "retry_max_delay_s": retry_max_delay_s,
            "max_msgs_per_user": max_msgs_per_user,
            "max_chars_per_msg": max_chars_per_msg,
        },
    }
    return results, checkpoint
