from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any, Dict, List, Tuple

from google.cloud import bigquery

from app.core.settings import get_settings

try:
    from app.core.logger import logger
except Exception:
    import logging
    logger = logging.getLogger("app_logger")

_VERTEX_OK = True
try:
    import vertexai
    from vertexai.generative_models import GenerativeModel
except Exception:
    _VERTEX_OK = False


BQ_ON_DEMAND_USD_PER_TIB = 6.25
BYTES_PER_TIB = float(1024 ** 4)


def _normalize_keyword(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _validate_single_keyword(value: str) -> str:
    kw = _normalize_keyword(value)
    if not kw:
        raise ValueError("keyword is required")
    if "," in kw or ";" in kw or " and " in kw:
        raise ValueError("keyword must be a single concept")
    return kw


def _parse_date_str(v: str) -> date:
    s = (v or "").strip()
    if not s:
        raise ValueError("date is required")
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"invalid date format: {v}")


def _frequency_expr(frequency: str) -> str:
    f = (frequency or "").strip().lower()
    if f == "daily":
        return "DATE(created_at)"
    if f == "weekly":
        return "DATE_TRUNC(DATE(created_at), WEEK(MONDAY))"
    if f == "monthly":
        return "DATE_TRUNC(DATE(created_at), MONTH)"
    if f == "yearly":
        return "DATE_TRUNC(DATE(created_at), YEAR)"
    raise ValueError("frequency must be one of: daily, weekly, monthly, yearly")


def _extract_json_obj(text: str) -> Dict[str, Any]:
    t = (text or "").strip()
    if not t:
        return {}
    t = re.sub(r"^```json\s*", "", t, flags=re.IGNORECASE).strip()
    t = re.sub(r"^```\s*", "", t).strip()
    t = re.sub(r"\s*```$", "", t).strip()
    try:
        return json.loads(t)
    except Exception:
        m = re.search(r"\{.*\}", t, flags=re.DOTALL)
        if not m:
            return {}
        try:
            return json.loads(m.group(0))
        except Exception:
            return {}


def _dedupe_keywords(values: List[str], seed: str, max_items: int = 12) -> List[str]:
    out: List[str] = []
    seen = set()
    for v in [seed] + list(values or []):
        vv = _normalize_keyword(v)
        if not vv:
            continue
        if vv in seen:
            continue
        if len(vv) > 60:
            continue
        seen.add(vv)
        out.append(vv)
        if len(out) >= max_items:
            break
    return out


def _with_simple_morph_variants(keywords: List[str], max_items: int = 24) -> List[str]:
    """
    Add light singular/plural variants so `phone` also matches `phones`.
    Keeps behavior conservative (single-token terms only).
    """
    out: List[str] = []
    seen = set()

    def _add(v: str) -> None:
        vv = _normalize_keyword(v)
        if not vv or vv in seen:
            return
        seen.add(vv)
        out.append(vv)

    for kw in keywords or []:
        _add(kw)
        if " " in kw:
            continue
        if kw.endswith("s") and len(kw) > 2:
            _add(kw[:-1])
        else:
            _add(f"{kw}s")
        if len(out) >= max_items:
            break

    return out[:max_items]


def _gemini_rates_usd_per_million(model: str) -> Tuple[float, float]:
    m = (model or "").strip().lower()
    if "flash-lite" in m:
        return 0.10, 0.40
    if "flash" in m:
        return 0.30, 2.50
    return 0.30, 2.50


def _extract_token_usage(resp: Any) -> Dict[str, int]:
    usage = getattr(resp, "usage_metadata", None)
    if usage is None:
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    def _get(obj: Any, key: str) -> int:
        try:
            if isinstance(obj, dict):
                return int(obj.get(key) or 0)
            return int(getattr(obj, key, 0) or 0)
        except Exception:
            return 0

    input_tokens = _get(usage, "prompt_token_count") or _get(usage, "input_token_count")
    output_tokens = _get(usage, "candidates_token_count") or _get(usage, "output_token_count")
    total_tokens = _get(usage, "total_token_count") or (input_tokens + output_tokens)
    return {
        "input_tokens": max(0, int(input_tokens)),
        "output_tokens": max(0, int(output_tokens)),
        "total_tokens": max(0, int(total_tokens)),
    }


def _gemini_cost_usd(model: str, usage: Dict[str, int]) -> float:
    in_rate, out_rate = _gemini_rates_usd_per_million(model)
    return float(
        (
            (usage.get("input_tokens", 0) * in_rate)
            + (usage.get("output_tokens", 0) * out_rate)
        )
        / 1_000_000.0
    )


def _bq_cost_usd(bytes_billed: int) -> float:
    if not bytes_billed or bytes_billed <= 0:
        return 0.0
    return float((bytes_billed / BYTES_PER_TIB) * BQ_ON_DEMAND_USD_PER_TIB)


def _expand_keywords_with_gemini(*, keyword: str, model: str) -> Tuple[List[str], Dict[str, Any]]:
    if not _VERTEX_OK:
        return _dedupe_keywords([], seed=keyword), {"used": False, "reason": "vertex_not_available"}

    settings = get_settings()
    try:
        vertexai.init(project=settings.vertex_project_id, location=settings.vertex_location)
        gen = GenerativeModel(model)
        prompt = f"""
You are generating close keyword variants for analytics matching.
Input keyword: "{keyword}"

Rules:
- Single concept only (same intent only).
- Include close synonyms and common spellings.
- Do not include unrelated concepts.
- Return ONLY JSON:
{{"keywords": ["..."]}}
""".strip()

        resp = gen.generate_content(prompt)
        text = getattr(resp, "text", "") or ""
        data = _extract_json_obj(text)
        kws = data.get("keywords") if isinstance(data, dict) else []
        if not isinstance(kws, list):
            kws = []
        variants = _dedupe_keywords([str(x) for x in kws], seed=keyword)
        usage = _extract_token_usage(resp)
        cost = _gemini_cost_usd(model, usage)
        return variants, {
            "used": True,
            "model": model,
            "token_usage": usage,
            "estimated_cost_usd": round(cost, 8),
        }
    except Exception as e:
        logger.warning(f"[INTENT_ANALYSIS] Gemini keyword expansion failed: {type(e).__name__}: {e}")
        return _dedupe_keywords([], seed=keyword), {
            "used": False,
            "reason": "gemini_error",
            "error": str(e),
        }


def intent_analysis(
    *,
    keyword: str,
    from_date: str,
    to_date: str,
    frequency: str,
    model: str = "gemini-2.5-flash",
) -> Dict[str, Any]:
    settings = get_settings()
    if not settings.bq_enabled:
        return {
            "success": False,
            "message": "BigQuery is disabled",
            "series": [],
            "cost": {"bq_cost_usd": 0.0, "gemini_cost_usd": 0.0, "total_cost_usd": 0.0},
        }

    kw = _validate_single_keyword(keyword)
    start_d = _parse_date_str(from_date)
    end_d = _parse_date_str(to_date)
    if start_d > end_d:
        raise ValueError("from_date must be <= to_date")
    period_expr = _frequency_expr(frequency)

    expanded_keywords, llm_meta = _expand_keywords_with_gemini(keyword=kw, model=model)
    expanded_keywords = _with_simple_morph_variants(expanded_keywords)
    escaped = [re.escape(v) for v in expanded_keywords]
    # Word-boundary regex for case-insensitive matching in LOWER(message)
    # For multi-word phrases, \b still works at edges.
    regex = r"\b(?:%s)\b" % "|".join(escaped)

    client = bigquery.Client(project=settings.bq_project_id)
    table_ref = f"`{settings.bq_project_id}.{settings.bq_dataset_id}.{settings.bq_messages_table_id}`"

    query = f"""
    SELECT
      {period_expr} AS period_start,
      SUM(ARRAY_LENGTH(REGEXP_EXTRACT_ALL(LOWER(CAST(message AS STRING)), @regex_pattern))) AS mentions
    FROM {table_ref}
    WHERE DATE(created_at) BETWEEN @from_date AND @to_date
      AND message IS NOT NULL
      AND REGEXP_CONTAINS(LOWER(CAST(message AS STRING)), @regex_pattern)
    GROUP BY period_start
    ORDER BY period_start ASC
    """.strip()

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("from_date", "DATE", start_d.isoformat()),
            bigquery.ScalarQueryParameter("to_date", "DATE", end_d.isoformat()),
            bigquery.ScalarQueryParameter("regex_pattern", "STRING", regex),
        ]
    )

    query_job = client.query(query, job_config=job_config)
    rows = list(query_job.result())

    series: List[Dict[str, Any]] = []
    total_mentions = 0
    for r in rows:
        p = r.get("period_start")
        c = int(r.get("mentions") or 0)
        total_mentions += c
        period_str = p.isoformat() if hasattr(p, "isoformat") else str(p)
        series.append({"period_start": period_str, "mention_count": c})

    bytes_processed = int(getattr(query_job, "total_bytes_processed", 0) or 0)
    bytes_billed = int(getattr(query_job, "total_bytes_billed", 0) or 0)
    bq_cost = _bq_cost_usd(bytes_billed)
    gemini_cost = float(llm_meta.get("estimated_cost_usd") or 0.0)
    total_cost = round(bq_cost + gemini_cost, 8)

    logger.info(
        f"[INTENT_ANALYSIS][COST] "
        f"bq_cost_usd={round(bq_cost, 8)} gemini_cost_usd={round(gemini_cost, 8)} "
        f"total_cost_usd={total_cost} bytes_billed={bytes_billed}"
    )

    return {
        "success": True,
        "message": "ok",
        "keyword": kw,
        "expanded_keywords": expanded_keywords,
        "from_date": start_d.isoformat(),
        "to_date": end_d.isoformat(),
        "frequency": frequency.lower(),
        "total_mentions": total_mentions,
        "series": series,  # line-chart ready
        "cost": {
            "bq_cost_usd": round(bq_cost, 8),
            "gemini_cost_usd": round(gemini_cost, 8),
            "total_cost_usd": total_cost,
            "bq_bytes_processed": bytes_processed,
            "bq_bytes_billed": bytes_billed,
            "gemini": llm_meta,
        },
        "query_meta": {
            "table": f"{settings.bq_project_id}.{settings.bq_dataset_id}.{settings.bq_messages_table_id}",
        },
    }
