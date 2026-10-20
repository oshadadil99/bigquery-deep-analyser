from __future__ import annotations

import asyncio
import json
import random
import re
import os
import traceback
from typing import Any, Dict, List, Tuple

import vertexai
from vertexai.generative_models import GenerativeModel

from app.core.settings import get_settings

try:
    from app.core.logger import logger
except Exception:
    import logging
    logger = logging.getLogger("oleeaitoolkit")
    logging.basicConfig(level=logging.INFO)


def _extract_json(text: str) -> Dict[str, Any]:
    if not text:
        return {}
    t = text.strip()
    t = re.sub(r"^```json\s*", "", t, flags=re.IGNORECASE).strip()
    t = re.sub(r"^```\s*", "", t).strip()
    t = re.sub(r"\s*```$", "", t).strip()

    try:
        return json.loads(t)
    except Exception:
        pass

    m = re.search(r"\{.*\}", t, flags=re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return {}
    return {}


def _looks_like_sql(s: str) -> bool:
    s2 = (s or "").lower()
    bad = ["select ", " from ", " where ", " join ", "`", ";", "insert ", "delete ", "update "]
    return any(b in s2 for b in bad)


def build_plan_prompt(
    *,
    question: str,
    allowed_intents: List[str],
    allowed_fields: List[str],
    max_variants: int = 12,
) -> str:
    return f"""
You are helping build a SAFE query plan for searching a user-behavior analytics table.

CRITICAL RULES:
- Output ONLY valid JSON. No markdown. No explanations.
- Do NOT output any counts, numbers, statistics, or answers.
- Do NOT output any SQL. Do NOT include SELECT/FROM/WHERE/backticks/semicolons.
- You may suggest small spelling corrections and close-meaning variants ONLY.
- The plan must be derived from the QUESTION only (no assumptions).

QUESTION:
{question}

ALLOWED INTENTS (pick ONE):
{allowed_intents}

ALLOWED FIELDS (pick 1-4):
{allowed_fields}

Return JSON exactly with this schema:

{{
  "intent": "one of allowed_intents",
  "target_fields": ["subset of allowed_fields"],
  "topic": "short topic string",
  "keyword_variants": ["strings, <= {max_variants} items, short"],
  "typo_fixes": [{{"from": "x", "to": "y"}}]
}}

Extra constraints:
- keyword_variants: max {max_variants} items, each <= 40 chars, no SQL-looking text.
- typo_fixes: ONLY small typos (like 1-2 letters), not big rewrites.
""".strip()


def _vertex_init_with_logs() -> Dict[str, Any]:
    settings = get_settings()
    meta = {
        "vertex_project_id": getattr(settings, "vertex_project_id", None),
        "vertex_location": getattr(settings, "vertex_location", None),
        "running_on_heroku": bool(os.getenv("DYNO")),
        "GOOGLE_APPLICATION_CREDENTIALS": "<set>" if os.getenv("GOOGLE_APPLICATION_CREDENTIALS") else "<missing>",
        "GOOGLE_APPLICATION_CREDENTIALS_JSON": "<set>" if os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON") else "<missing>",
    }
    vertexai.init(project=settings.vertex_project_id, location=settings.vertex_location)
    return meta


async def gemini_build_search_plan(
    *,
    question: str,
    model: str,
    allowed_intents: List[str],
    allowed_fields: List[str],
    max_variants: int = 12,
    max_retries: int = 4,
    retry_base_delay_s: float = 1.0,
    retry_max_delay_s: float = 12.0,
    timeout_s: float = 25.0,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Returns: (plan_json, meta_checkpoint)
    """
    prompt = build_plan_prompt(
        question=question,
        allowed_intents=allowed_intents,
        allowed_fields=allowed_fields,
        max_variants=max_variants,
    )

    attempt = 0
    while True:
        try:
            init_meta = _vertex_init_with_logs()
            gen_model = GenerativeModel(model)

            t0 = asyncio.get_event_loop().time()

            resp = await asyncio.wait_for(
                asyncio.to_thread(gen_model.generate_content, prompt),
                timeout=timeout_s,
            )

            text = getattr(resp, "text", "") or ""
            ms = int((asyncio.get_event_loop().time() - t0) * 1000)

            data = _extract_json(text)

            meta = {
                **init_meta,
                "model": model,
                "ms": ms,
                "attempt": attempt,
                "prompt_preview": prompt[:1200],
                "output_preview": (text[:1200] if text else ""),
                "parsed_ok": bool(data),
            }
            return data, meta

        except asyncio.TimeoutError as e:
            logger.error(f"[GEMINI_PLAN] timeout attempt={attempt} err={e}")
            meta = {
                "model": model,
                "attempt": attempt,
                "error_type": "TimeoutError",
                "error": str(e),
                "traceback": traceback.format_exc(limit=10),
            }
            if attempt >= max_retries:
                return {}, meta

        except Exception as e:
            logger.error(f"[GEMINI_PLAN] error attempt={attempt} type={type(e).__name__} err={e}")
            meta = {
                "model": model,
                "attempt": attempt,
                "error_type": type(e).__name__,
                "error": str(e),
                "traceback": traceback.format_exc(limit=12),
            }
            if attempt >= max_retries:
                return {}, meta

        delay = min(retry_max_delay_s, retry_base_delay_s * (2 ** attempt))
        delay = delay + random.uniform(0.0, 0.25 * delay)
        await asyncio.sleep(delay)
        attempt += 1


def validate_plan(
    plan: Dict[str, Any],
    *,
    allowed_intents: List[str],
    allowed_fields: List[str],
    max_variants: int = 12,
) -> Dict[str, Any]:
    if not isinstance(plan, dict):
        raise ValueError("plan must be a JSON object")

    intent = plan.get("intent")
    if intent not in allowed_intents:
        raise ValueError(f"invalid intent: {intent}")

    fields = plan.get("target_fields") or []
    if not isinstance(fields, list) or not fields:
        raise ValueError("target_fields must be a non-empty list")

    safe_fields = [f for f in fields if isinstance(f, str) and f in allowed_fields]
    if not safe_fields:
        raise ValueError("target_fields has no allowed fields")

    topic = plan.get("topic")
    if topic is not None and not isinstance(topic, str):
        topic = ""

    variants = plan.get("keyword_variants") or []
    if not isinstance(variants, list):
        variants = []

    clean_variants: List[str] = []
    for v in variants[:max_variants]:
        if not isinstance(v, str):
            continue
        vv = v.strip()
        if not vv or len(vv) > 40:
            continue
        if _looks_like_sql(vv):
            continue
        clean_variants.append(vv)

    typo_fixes = plan.get("typo_fixes") or []
    clean_typos: List[Dict[str, str]] = []
    if isinstance(typo_fixes, list):
        for tf in typo_fixes[:10]:
            if not isinstance(tf, dict):
                continue
            a = (tf.get("from") or "").strip()
            b = (tf.get("to") or "").strip()
            if not a or not b:
                continue
            if len(a) > 40 or len(b) > 40:
                continue
            if abs(len(a) - len(b)) > 2:
                continue
            if _looks_like_sql(a) or _looks_like_sql(b):
                continue
            clean_typos.append({"from": a, "to": b})

    return {
        "intent": intent,
        "target_fields": safe_fields,
        "topic": topic or "",
        "keyword_variants": clean_variants,
        "typo_fixes": clean_typos,
    }