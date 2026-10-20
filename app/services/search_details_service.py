
# app/services/search_details_service.py
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
import traceback
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from collections import Counter

from google.cloud import bigquery
from google.oauth2 import service_account
from google.api_core import exceptions as gcp_exceptions

from app.core.settings import get_settings

try:
    from app.core.logger import logger
except Exception:
    import logging
    logger = logging.getLogger("app_logger")
    logging.basicConfig(level=logging.INFO)

# ---- Configuration Constants ----
MAX_ROWS_FETCH = 10000
GEMINI_TIMEOUT_SECONDS = 25.0
MAX_KEYWORD_VARIANTS = 12
MAX_TYPO_FIXES = 10
BQ_QUERY_TIMEOUT = 30.0
BQ_ON_DEMAND_USD_PER_TIB = 6.25
BYTES_PER_TIB = float(1024 ** 4)


# ----------------------------
# Optional: Vertex AI imports
# ----------------------------
_VERTEX_OK = True
try:
    import vertexai
    from vertexai.generative_models import GenerativeModel
except Exception as e:
    _VERTEX_OK = False
    logger.warning(f"[SEARCH_DETAILS] Vertex AI not available: {type(e).__name__}: {e}")


# ----------------------------
# Custom Errors
# ----------------------------
class SearchDetailsError(RuntimeError):
    pass


class VertexInitError(SearchDetailsError):
    pass


class GeminiPlanError(SearchDetailsError):
    pass


class BigQueryAuthError(SearchDetailsError):
    pass


class BigQueryFetchError(SearchDetailsError):
    pass


# ----------------------------
# Logging helpers
# ----------------------------
def _safe_preview(value: Any, limit: int = 1200) -> str:
    try:
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False, default=str)
        else:
            text = str(value)
    except Exception:
        text = "<unprintable>"

    text = text.replace("\n", "\\n")
    if len(text) > limit:
        return text[:limit] + f"...[truncated {len(text)-limit} chars]"
    return text


def _redact_env_value(v: Optional[str]) -> str:
    if v is None:
        return "<missing>"
    if not v.strip():
        return "<empty>"
    return "<set>"


def _log_checkpoint(trace_id: str, stage: str, payload: Dict[str, Any], level: str = "info") -> None:
    msg = f"[SEARCH_DETAILS][{trace_id}][{stage}] {_safe_preview(payload, 2000)}"
    if level == "warning":
        logger.warning(msg)
    elif level == "error":
        logger.error(msg)
    else:
        logger.info(msg)


def _log_exception(trace_id: str, stage: str, e: Exception, extra: Optional[Dict[str, Any]] = None) -> None:
    payload = {
        "error_type": type(e).__name__,
        "error": str(e),
        "extra": extra or {},
        "traceback": traceback.format_exc(limit=20),
    }
    _log_checkpoint(trace_id, stage, payload, level="error")


def _bq_cost_usd_from_bytes(bytes_billed: Optional[int]) -> float:
    try:
        b = int(bytes_billed or 0)
        if b <= 0:
            return 0.0
        return float((b / BYTES_PER_TIB) * BQ_ON_DEMAND_USD_PER_TIB)
    except Exception:
        return 0.0


def _gemini_rates_usd_per_million(model: str) -> Tuple[float, float]:
    """
    Returns (input_rate, output_rate) in USD per 1M tokens.
    Defaults to flash pricing if model family is unknown.
    """
    m = (model or "").strip().lower()
    if "flash-lite" in m:
        return 0.10, 0.40
    if "flash" in m:
        return 0.30, 2.50
    return 0.30, 2.50


def _extract_gemini_token_usage(resp: Any) -> Dict[str, int]:
    usage = getattr(resp, "usage_metadata", None)
    if usage is None:
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    def _read(obj: Any, key: str) -> int:
        try:
            if isinstance(obj, dict):
                return int(obj.get(key) or 0)
            return int(getattr(obj, key, 0) or 0)
        except Exception:
            return 0

    input_tokens = _read(usage, "prompt_token_count") or _read(usage, "input_token_count")
    output_tokens = _read(usage, "candidates_token_count") or _read(usage, "output_token_count")
    total_tokens = _read(usage, "total_token_count")
    if total_tokens <= 0:
        total_tokens = max(0, input_tokens + output_tokens)
    return {
        "input_tokens": int(max(0, input_tokens)),
        "output_tokens": int(max(0, output_tokens)),
        "total_tokens": int(max(0, total_tokens)),
    }


def _gemini_cost_usd(model: str, *, input_tokens: int, output_tokens: int) -> float:
    in_rate, out_rate = _gemini_rates_usd_per_million(model)
    return float((max(0, input_tokens) * in_rate + max(0, output_tokens) * out_rate) / 1_000_000.0)


# ----------------------------
# Enhanced BigQuery Client
# ----------------------------
def get_bq_client(*, trace_id: Optional[str] = None) -> bigquery.Client:
    """
    Get BigQuery client with proper authentication for both local and Heroku.
    """
    tid = trace_id or "no-trace"

    try:
        creds_json = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
        creds_file = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

        _log_checkpoint(tid, "BQ_AUTH_ENV", {
            "GOOGLE_APPLICATION_CREDENTIALS_JSON": _redact_env_value(creds_json),
            "GOOGLE_APPLICATION_CREDENTIALS": _redact_env_value(creds_file),
            "running_on_heroku": bool(os.getenv("DYNO")),
        })

        # Priority 1: JSON credentials from environment
        if creds_json:
            logger.info("[SEARCH_DETAILS] Using GOOGLE_APPLICATION_CREDENTIALS_JSON from environment")
            try:
                creds_dict = json.loads(creds_json)
            except json.JSONDecodeError as e:
                raise BigQueryAuthError("Invalid JSON in GOOGLE_APPLICATION_CREDENTIALS_JSON") from e

            try:
                credentials = service_account.Credentials.from_service_account_info(
                    creds_dict,
                    scopes=["https://www.googleapis.com/auth/cloud-platform"],
                )
                project_id = creds_dict.get("project_id")
                if not project_id:
                    raise BigQueryAuthError("project_id missing in GOOGLE_APPLICATION_CREDENTIALS_JSON")

                return bigquery.Client(credentials=credentials, project=project_id)

            except Exception as e:
                if isinstance(e, BigQueryAuthError):
                    raise
                raise BigQueryAuthError("Failed to build BigQuery client from JSON credentials") from e

        # Priority 2: File credentials
        if creds_file and os.path.exists(creds_file):
            logger.info(f"[SEARCH_DETAILS] Using credentials from file: {creds_file}")
            try:
                credentials = service_account.Credentials.from_service_account_file(
                    creds_file,
                    scopes=["https://www.googleapis.com/auth/cloud-platform"],
                )
                with open(creds_file, 'r') as f:
                    project_id = json.load(f).get("project_id")
                
                return bigquery.Client(credentials=credentials, project=project_id)
            except Exception as e:
                raise BigQueryAuthError("Failed to build BigQuery client from file credentials") from e

        # Priority 3: Default credentials
        logger.info("[SEARCH_DETAILS] Using default credentials")
        try:
            return bigquery.Client()
        except Exception as e:
            raise BigQueryAuthError("Failed to build BigQuery client from default credentials") from e

    except BigQueryAuthError:
        raise
    except Exception as e:
        raise BigQueryAuthError(f"BigQuery client initialization failed: {str(e)}") from e


def get_table_schema(project_id: str, dataset_id: str, table_id: str, *, trace_id: str) -> Dict[str, Dict[str, str]]:
    try:
        client = get_bq_client(trace_id=trace_id)
        table_ref = f"{project_id}.{dataset_id}.{table_id}"
        table = client.get_table(table_ref)

        out: Dict[str, Dict[str, str]] = {}
        for f in table.schema:
            out[f.name] = {"type": f.field_type}

        _log_checkpoint(trace_id, "BQ_SCHEMA_OK", {"table": table_ref, "fields": len(out)})
        return out

    except gcp_exceptions.NotFound as e:
        raise BigQueryFetchError(f"BigQuery table not found: {project_id}.{dataset_id}.{table_id}") from e
    except gcp_exceptions.PermissionDenied as e:
        raise BigQueryFetchError(f"Permission denied for BigQuery table: {project_id}.{dataset_id}.{table_id}") from e
    except BigQueryAuthError:
        raise
    except Exception as e:
        raise BigQueryFetchError(f"Failed to get table schema: {str(e)}") from e


# ----------------------------
# Stopwords + normalizers
# ----------------------------
_STOPWORDS = {
    "how", "many", "users", "user", "about", "the", "a", "an", "is", "are", "was", "were",
    "do", "does", "did", "to", "of", "in", "on", "for", "with", "and", "or", "please",
    "tell", "me", "show", "give", "find", "get", "based", "from", "total",
    "asked", "ask", "mention", "mentioned", "regarding", "related",
}

_RE_WS = re.compile(r"\s+")
_RE_QUOTED = re.compile(r'"([^"]+)"|\'([^\']+)\'')


def _normalize_text(s: str) -> str:
    try:
        s = (s or "").strip().lower()
        s = re.sub(r"[^\w\s\-\/]", " ", s)
        s = _RE_WS.sub(" ", s).strip()
        return s
    except Exception:
        return ""


def _extract_quoted_phrase(q: str) -> Optional[str]:
    try:
        m = _RE_QUOTED.search(q or "")
        if not m:
            return None
        return (m.group(1) or m.group(2) or "").strip()
    except Exception:
        return None


def _remove_stopwords(text: str) -> str:
    qn = _normalize_text(text)
    toks = [t for t in qn.split() if t and t not in _STOPWORDS]
    return " ".join(toks).strip()


def _looks_like_sql(s: str) -> bool:
    s2 = (s or "").lower()
    bad = ["select ", " from ", " where ", " join ", "`", ";", "insert ", "delete ", "update "]
    return any(b in s2 for b in bad)


def _extract_keyword_from_question(question: str) -> Optional[str]:
    try:
        q_raw = (question or "").strip()
        if not q_raw:
            return None

        quoted = _extract_quoted_phrase(q_raw)
        if quoted:
            kw = _normalize_text(quoted)
            return kw if kw else None

        q = _normalize_text(q_raw).strip(" ?")

        for anchor in ["about ", "regarding ", "on "]:
            idx = q.find(anchor)
            if idx >= 0:
                tail = q[idx + len(anchor):].strip(" ?")
                tail2 = _remove_stopwords(tail)
                return tail2 or tail or None

        patterns = [
            r"^how many users (asked|ask|mentioned|mention)( about)?\s+",
            r"^how many users\s+",
            r"^count( the)? users( who)?\s+",
            r"^number of users( who)?\s+",
            r"^how many\s+",
            r"^count\s+",
        ]
        for p in patterns:
            q2 = re.sub(p, "", q).strip()
            if q2 != q:
                q = q2
                break

        kw = _remove_stopwords(q)
        return kw or (q.strip() or None)
    except Exception:
        return None


# Enhanced synonym mapping
_SYNONYMS = {
    "loan": ["loan", "loans", "borrow", "borrowing", "credit", "lending"],
    "vehicle loan": ["vehicle loan", "vehicle loans", "car loan", "car loans", "auto loan", "auto loans"],
    "mobile phone": ["mobile phone", "mobile", "phone", "phones", "smartphone", "cellphone", "handset", "iphone", "android"],
    "laptop": ["laptop", "laptops", "notebook", "computer", "pc", "macbook", "laptop computer"],
    "payment": ["payment", "pay", "paid", "transaction", "amount", "money", "cash"],
    "verification": ["verify", "verification", "verifying", "identity", "kyc", "document"],
    "housing": ["housing", "house", "home", "apartment", "property", "residence", "real estate"],
}


def _expand_keyword_rule(keyword: str) -> List[str]:
    try:
        kw = _normalize_text(keyword)
        if not kw:
            return []

        variants = [kw]

        # Check exact matches first
        if kw in _SYNONYMS:
            variants.extend(_SYNONYMS[kw])
        else:
            # Check partial matches
            for key, vals in _SYNONYMS.items():
                if kw in key or key in kw:
                    variants.extend(vals)

        # Add plural/singular variants
        if kw.endswith("s") and len(kw) > 2:
            variants.append(kw[:-1])
        else:
            variants.append(kw + "s")

        # Remove duplicates and limit
        seen = set()
        result: List[str] = []
        for v in variants:
            vn = _normalize_text(v)
            if vn and vn not in seen and not _looks_like_sql(vn):
                if all(tok in _STOPWORDS for tok in vn.split()):
                    continue
                seen.add(vn)
                result.append(vn)
                if len(result) >= MAX_KEYWORD_VARIANTS:
                    break

        return result
    except Exception:
        return [keyword] if keyword else []


# ----------------------------
# Plan model
# ----------------------------
@dataclass
class SearchPlan:
    intent: str
    target_fields: List[str]
    keyword: Optional[str] = None
    keyword_variants: List[str] = field(default_factory=list)
    typo_fixes: List[Dict[str, str]] = field(default_factory=list)
    top_k: int = 10


# ----------------------------
# Relevance classifier
# ----------------------------
_RELEVANT_HINTS = {
    "mentioned", "mention", "users", "user", "intent", "intents",
    "sentiment", "urgency", "complaint", "complaints",
    "location", "locations", "asset", "assets", "amount", "amounts",
    "tags", "tag", "accounts", "language", "summary", "summery",
    "customer_stage", "resolved", "status", "total", "sum", "loan", "loans",
    "housing", "vehicle", "mobile", "phone", "laptop", "payment"
}


def _is_relevant_user_behavior_question(question: str) -> bool:
    q = _normalize_text(question)
    if not q:
        return False
    toks = set(q.split())
    return bool(toks & _RELEVANT_HINTS)


# ----------------------------
# Rule-based plan builder
# ----------------------------
def _build_plan_rule_based(question: str) -> SearchPlan:
    try:
        q = _normalize_text(question)

        is_count = ("how many" in q) or ("count" in q) or ("number of users" in q)
        is_sum = ("total amount" in q) or ("sum" in q and "amount" in q) or ("total" in q and "amount" in q)
        is_top = ("top" in q) or ("most common" in q) or ("most frequent" in q)
        is_dist = ("distribution" in q) or ("breakdown" in q)

        # Specific field queries
        if "sentiment" in q and not is_count:
            return SearchPlan(intent="distribution", target_fields=["overall_sentiment"])
        if "urgency" in q and not is_count:
            return SearchPlan(intent="distribution", target_fields=["urgency_level"])
        if ("customer stage" in q or "stage" in q) and not is_count:
            return SearchPlan(intent="distribution", target_fields=["customer_stage"])
        if ("resolved" in q or "status" in q) and not is_count:
            return SearchPlan(intent="distribution", target_fields=["last_resolved_status"])
        if "language" in q and not is_count:
            return SearchPlan(intent="distribution", target_fields=["primary_language"])
        if "complaint" in q:
            return SearchPlan(intent="count_flag", target_fields=["complaint_flag"])
        if "accounts" in q or "account" in q:
            return SearchPlan(intent="count_flag", target_fields=["mentioned_accounts"])

        # Sum queries
        if is_sum:
            kw = _extract_keyword_from_question(question)
            variants = _expand_keyword_rule(kw) if kw else []
            return SearchPlan(
                intent="sum_amounts",
                target_fields=["mentioned_amounts", "tags", "mentioned_assets", "top_intents", "summery"],
                keyword=kw,
                keyword_variants=variants,
            )

        # Count queries
        if is_count:
            kw = _extract_keyword_from_question(question)
            variants = _expand_keyword_rule(kw) if kw else []
            return SearchPlan(intent="count_keyword", target_fields=[], keyword=kw, keyword_variants=variants)

        # Top/distribution queries
        if is_top:
            return SearchPlan(intent="top_values", target_fields=["tags"], top_k=10)

        if is_dist:
            return SearchPlan(intent="distribution_generic", target_fields=["tags"])

        # Default: keyword search
        kw = _extract_keyword_from_question(question)
        if kw:
            variants = _expand_keyword_rule(kw)
            return SearchPlan(intent="count_keyword", target_fields=[], keyword=kw, keyword_variants=variants)

        return SearchPlan(intent="unknown", target_fields=[])

    except Exception as e:
        logger.error(f"Error building plan: {str(e)}")
        return SearchPlan(intent="unknown", target_fields=[])


# ----------------------------
# JSON extraction + sanitize
# ----------------------------
def _extract_json(text: str) -> Dict[str, Any]:
    try:
        if not text:
            return {}
        t = text.strip()
        t = re.sub(r"^```json\s*", "", t, flags=re.IGNORECASE).strip()
        t = re.sub(r"^```\s*", "", t).strip()
        t = re.sub(r"\s*```$", "", t).strip()

        try:
            return json.loads(t)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", t, flags=re.DOTALL)
            if m:
                try:
                    return json.loads(m.group(0))
                except json.JSONDecodeError:
                    return {}
        return {}
    except Exception:
        return {}


def _sanitize_variants(variants: Any, limit: int = MAX_KEYWORD_VARIANTS) -> List[str]:
    out: List[str] = []
    if not isinstance(variants, list):
        return out
    seen = set()
    for v in variants:
        if not isinstance(v, str):
            continue
        vv = _normalize_text(v)
        if not vv or len(vv) > 60:
            continue
        if _looks_like_sql(vv):
            continue
        if vv in seen:
            continue
        if all(tok in _STOPWORDS for tok in vv.split()):
            continue
        seen.add(vv)
        out.append(vv)
        if len(out) >= limit:
            break
    return out


def _sanitize_keyword(keyword: Any) -> Optional[str]:
    if keyword is None:
        return None
    if not isinstance(keyword, str):
        keyword = str(keyword)
    kw = _normalize_text(keyword)
    if not kw or _looks_like_sql(kw):
        return None
    kw2 = _remove_stopwords(kw)
    return kw2 or kw


def _sanitize_typos(typos: Any, limit: int = MAX_TYPO_FIXES) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    if not isinstance(typos, list):
        return out
    for item in typos[:limit]:
        if not isinstance(item, dict):
            continue
        a = _normalize_text(str(item.get("from") or ""))
        b = _normalize_text(str(item.get("to") or ""))
        if not a or not b:
            continue
        if len(a) > 60 or len(b) > 60:
            continue
        if abs(len(a) - len(b)) > 2:
            continue
        if _looks_like_sql(a) or _looks_like_sql(b):
            continue
        out.append({"from": a, "to": b})
    return out


# ----------------------------
# Vertex AI init
# ----------------------------
def _init_vertex_ai(*, trace_id: str) -> bool:
    if not _VERTEX_OK:
        _log_checkpoint(trace_id, "VERTEX_INIT", {"ok": False, "reason": "vertex_import_failed"}, "warning")
        return False

    settings = get_settings()
    try:
        creds_json = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
        creds_file = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

        _log_checkpoint(trace_id, "VERTEX_ENV", {
            "vertex_project_id": getattr(settings, "vertex_project_id", None),
            "vertex_location": getattr(settings, "vertex_location", None),
            "GOOGLE_APPLICATION_CREDENTIALS_JSON": _redact_env_value(creds_json),
            "GOOGLE_APPLICATION_CREDENTIALS": _redact_env_value(creds_file),
        })

        if creds_json:
            creds_dict = json.loads(creds_json)
            credentials = service_account.Credentials.from_service_account_info(
                creds_dict,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            vertexai.init(
                project=settings.vertex_project_id,
                location=settings.vertex_location,
                credentials=credentials,
            )
            _log_checkpoint(trace_id, "VERTEX_INIT", {"ok": True, "mode": "json_creds"})
            return True

        if creds_file and os.path.exists(creds_file):
            credentials = service_account.Credentials.from_service_account_file(
                creds_file,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            vertexai.init(
                project=settings.vertex_project_id,
                location=settings.vertex_location,
                credentials=credentials,
            )
            _log_checkpoint(trace_id, "VERTEX_INIT", {"ok": True, "mode": "file_creds"})
            return True

        vertexai.init(project=settings.vertex_project_id, location=settings.vertex_location)
        _log_checkpoint(trace_id, "VERTEX_INIT", {"ok": True, "mode": "default_creds"})
        return True

    except Exception as e:
        _log_exception(trace_id, "VERTEX_INIT_ERROR", e)
        return False


# ----------------------------
# Gemini plan parser
# ----------------------------
async def _plan_with_gemini(
    *,
    trace_id: str,
    question: str,
    model: str,
    log_llm_prompts: bool,
    checkpoints: Dict[str, Any],
) -> Optional[SearchPlan]:
    if not _VERTEX_OK:
        checkpoints.setdefault("llm", {}).setdefault("plan", {})
        checkpoints["llm"]["plan"] = {"used": False, "reason": "vertex_not_available"}
        return None

    t0 = time.perf_counter()
    try:
        if not _init_vertex_ai(trace_id=trace_id):
            checkpoints.setdefault("llm", {}).setdefault("plan", {})
            checkpoints["llm"]["plan"] = {"used": False, "reason": "vertex_init_failed"}
            return None

        gen = GenerativeModel(model)

        prompt = f"""
You are a router for an analytics endpoint over a BigQuery user_behavior table.

Output ONLY valid JSON with this schema:
{{
  "intent": "count_keyword" | "top_values" | "distribution" | "count_flag" | "sum_amounts" | "not_relevant",
  "target_fields": [string],
  "keyword": string|null,
  "keyword_variants": [string],
  "typo_fixes": [{{"from": string, "to": string}}],
  "top_k": number
}}

Allowed target_fields:
- mentioned_assets
- mentioned_amounts
- mentioned_locations
- tags
- top_intents
- primary_language
- overall_sentiment
- urgency_level
- customer_stage
- last_resolved_status
- complaint_flag
- mentioned_accounts
- summery

Rules:
- If question is not about user behavior analytics, intent="not_relevant".
- If question asks "how many users mentioned X", intent="count_keyword" and keyword="X".
- If question asks "top/most common", intent="top_values".
- If question asks distribution/breakdown, intent="distribution".
- If question asks total/sum of amounts, intent="sum_amounts" and keyword should be the topic phrase.
- keyword_variants: small synonyms and close spellings ONLY (max {MAX_KEYWORD_VARIANTS}). No SQL. No numbers.
- Do NOT answer the question. Only output the plan JSON.

QUESTION:
{question}
""".strip()

        if log_llm_prompts:
            _log_checkpoint(trace_id, "LLM_PLAN_REQUEST", {"model": model, "prompt_preview": _safe_preview(prompt)})

        resp = await asyncio.wait_for(
            asyncio.to_thread(gen.generate_content, prompt),
            timeout=GEMINI_TIMEOUT_SECONDS,
        )

        text = getattr(resp, "text", "") or ""
        usage = _extract_gemini_token_usage(resp)
        gemini_cost = _gemini_cost_usd(
            model,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
        )
        dt = round((time.perf_counter() - t0) * 1000, 2)

        if log_llm_prompts:
            _log_checkpoint(trace_id, "LLM_PLAN_RESPONSE", {
                "model": model,
                "ms": dt,
                "token_usage": usage,
                "estimated_cost_usd": round(gemini_cost, 8),
                "output_preview": _safe_preview(text),
            })

        data = _extract_json(text)
        if not data:
            checkpoints.setdefault("llm", {}).setdefault("plan", {})
            checkpoints["llm"]["plan"] = {
                "used": True,
                "ok": False,
                "reason": "empty_json",
                "ms": dt,
                "model": model,
                "token_usage": usage,
                "estimated_cost_usd": round(gemini_cost, 8),
            }
            return None

        intent = str(data.get("intent") or "unknown")
        target_fields = data.get("target_fields") or []
        if not isinstance(target_fields, list):
            target_fields = []

        keyword = _sanitize_keyword(data.get("keyword"))
        keyword_variants = _sanitize_variants(data.get("keyword_variants"))
        typo_fixes = _sanitize_typos(data.get("typo_fixes"))
        top_k = int(data.get("top_k") or 10)

        plan = SearchPlan(
            intent=intent,
            target_fields=[str(x) for x in target_fields],
            keyword=keyword,
            keyword_variants=keyword_variants,
            typo_fixes=typo_fixes,
            top_k=top_k,
        )

        checkpoints.setdefault("llm", {}).setdefault("plan", {})
        checkpoints["llm"]["plan"] = {
            "used": True,
            "ok": True,
            "model": model,
            "ms": dt,
            "token_usage": usage,
            "estimated_cost_usd": round(gemini_cost, 8),
            "parsed_plan": plan.__dict__,
        }
        return plan

    except asyncio.TimeoutError as e:
        dt = round((time.perf_counter() - t0) * 1000, 2)
        checkpoints.setdefault("llm", {}).setdefault("plan", {})
        checkpoints["llm"]["plan"] = {"used": True, "ok": False, "error": "timeout", "ms": dt}
        _log_exception(trace_id, "LLM_PLAN_TIMEOUT", e, {"ms": dt})
        return None
    except Exception as e:
        dt = round((time.perf_counter() - t0) * 1000, 2)
        checkpoints.setdefault("llm", {}).setdefault("plan", {})
        checkpoints["llm"]["plan"] = {"used": True, "ok": False, "error": str(e), "ms": dt}
        _log_exception(trace_id, "LLM_PLAN_ERROR", e, {"ms": dt})
        return None


# ----------------------------
# BigQuery helpers
# ----------------------------
def _maybe_json_load(v: Any) -> Any:
    try:
        if isinstance(v, str):
            s = v.strip()
            if s in ("", "null", "None"):
                return None
            if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
                try:
                    return json.loads(s)
                except Exception:
                    return v
        return v
    except Exception:
        return v


def _detect_summary_column(schema: Dict[str, Dict[str, str]]) -> Optional[str]:
    if "summery" in schema:
        return "summery"
    if "summary" in schema:
        return "summary"
    return None


# ----------------------------
# Fetch rows
# ----------------------------
def _fetch_rows_for_plan(
    *,
    project_slug: str,
    plan: SearchPlan,
    trace_id: str,
    max_rows: int = MAX_ROWS_FETCH,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    settings = get_settings()
    t0 = time.perf_counter()
    try:
        _log_checkpoint(trace_id, "BQ_FETCH_START", {
            "project_slug": project_slug,
            "plan_intent": plan.intent,
            "bq_project_id": settings.bq_project_id,
            "bq_dataset_id": settings.bq_dataset_id,
            "bq_table_id": settings.bq_user_behavior_table_id,
            "max_rows": max_rows,
        })

        client = get_bq_client(trace_id=trace_id)

        schema = get_table_schema(
            settings.bq_project_id,
            settings.bq_dataset_id,
            settings.bq_user_behavior_table_id,
            trace_id=trace_id,
        )

        table_ref = f"`{settings.bq_project_id}.{settings.bq_dataset_id}.{settings.bq_user_behavior_table_id}`"

        # Determine columns to fetch
        if plan.intent == "count_keyword" and not plan.target_fields:
            wanted = list(schema.keys())
        else:
            needed = {"user_id", "project_slug", "confidence"}
            for f in plan.target_fields:
                needed.add(f)

            summary_col = _detect_summary_column(schema)
            if "summery" in needed and summary_col:
                needed.remove("summery")
                needed.add(summary_col)

            wanted = [c for c in needed if c in schema]

        if "user_id" not in wanted:
            raise BigQueryFetchError("user_behavior table must contain user_id")

        query = f"""
        SELECT {", ".join(wanted)}
        FROM {table_ref}
        WHERE project_slug = @project_slug
        LIMIT @max_rows
        """.strip()

        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("project_slug", "STRING", project_slug),
                bigquery.ScalarQueryParameter("max_rows", "INT64", max_rows),
            ]
        )

        query_job = client.query(query, job_config=job_config)

        rows_raw = []
        try:
            for row in query_job.result(timeout=BQ_QUERY_TIMEOUT):
                rows_raw.append(row)
                if len(rows_raw) >= max_rows:
                    break
        except gcp_exceptions.GoogleAPIError as e:
            raise BigQueryFetchError(f"BigQuery query failed: {str(e)}") from e

        out: List[Dict[str, Any]] = []
        for r in rows_raw:
            d = dict(r.items())
            for k, v in list(d.items()):
                d[k] = _maybe_json_load(v)
            out.append(d)

        ms = round((time.perf_counter() - t0) * 1000, 2)
        bytes_processed = int(getattr(query_job, "total_bytes_processed", 0) or 0)
        bytes_billed = int(getattr(query_job, "total_bytes_billed", 0) or 0)
        bq_cost_usd = _bq_cost_usd_from_bytes(bytes_billed)
        meta = {
            "rows_total": len(out),
            "columns_used": wanted,
            "summary_column": _detect_summary_column(schema),
            "bq_query": query,
            "bq_params": {"project_slug": project_slug, "max_rows": max_rows},
            "bq_ms": ms,
            "bq_bytes_processed": bytes_processed,
            "bq_bytes_billed": bytes_billed,
            "bq_cost_usd": round(bq_cost_usd, 8),
        }
        _log_checkpoint(trace_id, "BQ_FETCH_OK", {
            "rows": len(out),
            "ms": ms,
            "columns_used": wanted,
            "bytes_processed": bytes_processed,
            "bytes_billed": bytes_billed,
            "estimated_cost_usd": round(bq_cost_usd, 8),
        })
        return out, meta

    except (BigQueryAuthError, BigQueryFetchError):
        raise
    except Exception as e:
        _log_exception(trace_id, "BQ_FETCH_UNEXPECTED_ERROR", e)
        raise BigQueryFetchError(f"Failed to fetch BigQuery data: {str(e)}") from e


# ----------------------------
# Matching + compute
# ----------------------------
def _match_keyword_in_text(text: str, keyword_variants: List[str]) -> bool:
    try:
        if not text or not keyword_variants:
            return False

        text_normalized = _normalize_text(text)

        for kw in keyword_variants:
            kw_normalized = _normalize_text(kw)
            if not kw_normalized:
                continue

            if kw_normalized in text_normalized:
                return True

            if " " not in kw_normalized:
                pattern = rf"\b{re.escape(kw_normalized)}\b"
                if re.search(pattern, text_normalized):
                    return True

        return False
    except Exception:
        return False


_NUM_RE = re.compile(r"(-?\d{1,3}(?:,\d{3})*(?:\.\d+)?|-?\d+(?:\.\d+)?)")


def _as_list(v: Any) -> List[str]:
    try:
        if v is None:
            return []
        v = _maybe_json_load(v)
        if v is None:
            return []
        if isinstance(v, list):
            return [str(x) for x in v if x is not None and str(x).strip() != ""]
        if isinstance(v, str):
            s = v.strip()
            if not s or s in ("[]", "null"):
                return []
            if "," in s and not (s.startswith("[") and s.endswith("]")):
                return [x.strip() for x in s.split(",") if x.strip()]
            return [s]
        return [str(v)]
    except Exception:
        return []


def _extract_amount_numbers(items: List[str]) -> List[float]:
    nums: List[float] = []
    try:
        for it in items:
            s = str(it)
            for m in _NUM_RE.findall(s):
                try:
                    nums.append(float(m.replace(",", "")))
                except Exception:
                    continue
        return nums
    except Exception:
        return []


def _compute_confidence_strict(*, total_rows: int, usable_rows: int, evidence_rows: int, avg_row_conf: float) -> float:
    try:
        if total_rows <= 0 or evidence_rows <= 0:
            return 0.0
        coverage = usable_rows / max(1, total_rows)
        support = min(1.0, evidence_rows / 25.0)
        avg_row_conf = max(0.0, min(1.0, float(avg_row_conf)))
        score = (0.45 * support) + (0.35 * coverage) + (0.20 * avg_row_conf)
        return max(0.0, min(1.0, float(score)))
    except Exception:
        return 0.0


def _compute_answer_from_rows(
    *,
    question: str,
    plan: SearchPlan,
    rows: List[Dict[str, Any]],
    schema_meta: Dict[str, Any],
    trace_id: str,
    log_checkpoints: bool,
) -> Tuple[str, float, Dict[str, Any]]:
    try:
        total_rows = len(rows)
        if total_rows == 0:
            return ("Not enough data to answer.", 0.0, {"reason": "no_rows"})

        if plan.intent == "count_keyword":
            keyword = (plan.keyword or "").strip()
            if not keyword:
                return ("Not enough data to answer.", 0.0, {"reason": "no_keyword"})

            kw_variants = plan.keyword_variants[:] if plan.keyword_variants else _expand_keyword_rule(keyword)

            usable = 0
            matched = 0
            confs: List[float] = []

            for r in rows:
                usable += 1
                confs.append(float(r.get("confidence") or 0.0))
                row_json = json.dumps(r, ensure_ascii=False, default=str).lower()
                if _match_keyword_in_text(row_json, kw_variants):
                    matched += 1

            if matched == 0:
                return ("Not enough data to answer.", 0.0, {
                    "intent": "count_keyword",
                    "keyword": keyword,
                    "keyword_variants": kw_variants,
                    "matched_users": 0,
                    "total_users": total_rows,
                })

            avg_conf = sum(confs) / max(1, len(confs))
            conf = _compute_confidence_strict(
                total_rows=total_rows,
                usable_rows=usable,
                evidence_rows=matched,
                avg_row_conf=avg_conf,
            )
            facts = {
                "intent": "count_keyword",
                "keyword": keyword,
                "keyword_variants": kw_variants,
                "matched_users": matched,
                "total_users": total_rows,
                "usable_rows": usable,
                "avg_row_conf": avg_conf,
            }
            return f"{matched} users match keyword '{keyword}'.", conf, facts

        if plan.intent == "sum_amounts":
            keyword = (plan.keyword or "").strip()
            kw_variants = plan.keyword_variants[:] if plan.keyword_variants else (_expand_keyword_rule(keyword) if keyword else [])

            usable = 0
            evidence_users = 0
            nums_all: List[float] = []
            confs: List[float] = []

            for r in rows:
                row_json = json.dumps(r, ensure_ascii=False, default=str).lower()
                if kw_variants and not _match_keyword_in_text(row_json, kw_variants):
                    continue

                amts = _as_list(r.get("mentioned_amounts"))
                if not amts:
                    continue

                usable += 1
                confs.append(float(r.get("confidence") or 0.0))

                nums = _extract_amount_numbers(amts)
                if nums:
                    evidence_users += 1
                    nums_all.extend(nums)

            if evidence_users == 0 or not nums_all:
                return ("Not enough data to answer.", 0.0, {
                    "intent": "sum_amounts",
                    "keyword": keyword,
                    "matched_users_with_amounts": 0,
                    "total_users": total_rows,
                })

            total_sum = float(sum(nums_all))
            avg_conf = sum(confs) / max(1, len(confs)) if confs else 0.0
            conf = _compute_confidence_strict(
                total_rows=total_rows,
                usable_rows=usable,
                evidence_rows=evidence_users,
                avg_row_conf=avg_conf,
            )
            facts = {
                "intent": "sum_amounts",
                "keyword": keyword,
                "keyword_variants": kw_variants,
                "sum": total_sum,
                "contributors": evidence_users,
                "usable_rows": usable,
                "total_users": total_rows,
                "avg_row_conf": avg_conf,
                "fields": plan.target_fields,
            }
            return f"Total related to '{keyword}' is {total_sum:.2f}.", conf, facts

        return ("This question is not supported by the current user behavior search.", 0.0, {"reason": "unsupported_intent"})

    except Exception as e:
        _log_exception(trace_id, "ANSWER_COMPUTE_ERROR", e)
        return ("Error processing query.", 0.0, {"reason": "computation_error", "error": str(e)})


# ----------------------------
# Main search runner
# ----------------------------
async def run_search_details(
    *,
    project_slug: str,
    question: str,
    model: str,
    user_id: Optional[str],
    messages: List[Dict[str, Any]],
    use_gemini: bool,
    min_confidence_to_answer: float,
    log_checkpoints: bool,
) -> Dict[str, Any]:
    trace_id = f"sd-{uuid.uuid4().hex[:10]}"
    started_ms = int(time.time() * 1000)

    checkpoints: Dict[str, Any] = {
        "trace_id": trace_id,
        "started_ms": started_ms,
        "relevance": {"status": "pending"},
        "plan": {"status": "pending"},
        "bq_fetch": {"status": "pending"},
        "answer": {"status": "pending"},
        "llm": {},
    }

    log_llm_prompts = bool(log_checkpoints)

    try:
        if log_checkpoints:
            _log_checkpoint(trace_id, "START", {
                "project_slug": project_slug,
                "user_id": user_id,
                "question": question,
                "use_gemini": use_gemini,
                "model": model,
                "min_confidence_to_answer": min_confidence_to_answer,
                "messages_count": len(messages or []),
            })

        # 1) Relevance check
        relevant = _is_relevant_user_behavior_question(question)
        checkpoints["relevance"] = {"status": "ok", "relevant": relevant}
        if not relevant:
            _log_checkpoint(trace_id, "SEARCH_COST", {
                "bq_cost_usd": 0.0,
                "gemini_cost_usd": 0.0,
                "total_cost_usd": 0.0,
                "reason": "not_relevant",
            })
            return {
                "project_slug": project_slug,
                "question": question,
                "raw_answer": "This question is not related to user behavior analytics.",
                "answer": "This question is not related to user behavior analytics.",
                "confidence": 0.0,
                "bq_query": None,
                "bq_params": None,
                "meta": {"reason": "not_relevant", "trace_id": trace_id},
                "checkpoints": checkpoints,
            }

        # 2) Plan
        plan = _build_plan_rule_based(question)
        checkpoints["plan"] = {"status": "ok", "plan": plan.__dict__, "source": "rule"}

        # (optional) Gemini planner if unknown
        if plan.intent == "unknown" and use_gemini:
            gplan = await _plan_with_gemini(
                trace_id=trace_id,
                question=question,
                model=model,
                log_llm_prompts=log_llm_prompts,
                checkpoints=checkpoints,
            )
            if gplan:
                plan = gplan
                checkpoints["plan"] = {"status": "ok", "plan": plan.__dict__, "source": "gemini"}

        # 3) Fetch BigQuery rows
        t0 = time.perf_counter()
        rows, schema_meta = _fetch_rows_for_plan(
            project_slug=project_slug,
            plan=plan,
            trace_id=trace_id,
            max_rows=MAX_ROWS_FETCH,
        )
        bq_ms = round((time.perf_counter() - t0) * 1000, 2)
        checkpoints["bq_fetch"] = {"status": "ok", "rows": len(rows), "ms": bq_ms, **schema_meta}

        # 4) Compute answer
        t1 = time.perf_counter()
        answer_draft, confidence, facts = _compute_answer_from_rows(
            question=question,
            plan=plan,
            rows=rows,
            schema_meta=schema_meta,
            trace_id=trace_id,
            log_checkpoints=log_checkpoints,
        )
        compute_ms = round((time.perf_counter() - t1) * 1000, 2)
        checkpoints["answer"] = {
            "status": "ok",
            "intent": plan.intent,
            "confidence": confidence,
            "ms": compute_ms,
            "draft_preview": _safe_preview(answer_draft, 500),
            "facts_preview": _safe_preview(facts, 1000),
        }

        llm_plan = ((checkpoints.get("llm") or {}).get("plan") or {})
        gemini_cost_usd = float(llm_plan.get("estimated_cost_usd") or 0.0)
        bq_cost_usd = float(schema_meta.get("bq_cost_usd") or 0.0)
        total_cost_usd = round(gemini_cost_usd + bq_cost_usd, 8)
        cost_breakdown = {
            "bq_cost_usd": round(bq_cost_usd, 8),
            "gemini_cost_usd": round(gemini_cost_usd, 8),
            "total_cost_usd": total_cost_usd,
        }
        _log_checkpoint(trace_id, "SEARCH_COST", {
            **cost_breakdown,
            "bq_bytes_billed": schema_meta.get("bq_bytes_billed", 0),
            "gemini_used": bool(llm_plan.get("used")),
            "gemini_model": llm_plan.get("model"),
            "gemini_token_usage": llm_plan.get("token_usage", {}),
        })

        # 5) Confidence gate
        if confidence <= 0.0 or confidence < float(min_confidence_to_answer):
            return {
                "project_slug": project_slug,
                "question": question,
                "raw_answer": "Not enough data to answer.",
                "answer": "Not enough data to answer.",
                "confidence": float(confidence),
                "bq_query": schema_meta.get("bq_query"),
                "bq_params": schema_meta.get("bq_params"),
                "meta": {
                    "trace_id": trace_id,
                    "intent": plan.intent,
                    "plan": plan.__dict__,
                    "evidence": facts,
                    "cost": cost_breakdown,
                    "min_confidence_to_answer": float(min_confidence_to_answer),
                    "rows_total": schema_meta.get("rows_total"),
                    "columns_used": schema_meta.get("columns_used"),
                },
                "checkpoints": checkpoints,
            }

        return {
            "project_slug": project_slug,
            "question": question,
            "raw_answer": answer_draft,
            "answer": answer_draft,
            "confidence": float(confidence),
            "bq_query": schema_meta.get("bq_query"),
            "bq_params": schema_meta.get("bq_params"),
            "meta": {
                "trace_id": trace_id,
                "intent": plan.intent,
                "plan": plan.__dict__,
                "evidence": facts,
                "cost": cost_breakdown,
                "rows_total": schema_meta.get("rows_total"),
                "columns_used": schema_meta.get("columns_used"),
            },
            "checkpoints": checkpoints,
        }

    except BigQueryAuthError as e:
        _log_exception(trace_id, "BQ_AUTH_ERROR", e)
        return {
            "project_slug": project_slug,
            "question": question,
            "raw_answer": f"BigQuery auth error: {str(e)}",
            "answer": "Unable to process query due to BigQuery authentication error.",
            "confidence": 0.0,
            "bq_query": None,
            "bq_params": None,
            "meta": {"reason": "bq_auth_error", "trace_id": trace_id, "error_details": str(e)},
            "checkpoints": checkpoints,
        }

    except BigQueryFetchError as e:
        _log_exception(trace_id, "BQ_FETCH_ERROR", e)
        return {
            "project_slug": project_slug,
            "question": question,
            "raw_answer": f"BigQuery fetch error: {str(e)}",
            "answer": "Unable to process query due to BigQuery fetch error.",
            "confidence": 0.0,
            "bq_query": None,
            "bq_params": None,
            "meta": {"reason": "bq_fetch_error", "trace_id": trace_id, "error_details": str(e)},
            "checkpoints": checkpoints,
        }

    except Exception as e:
        _log_exception(trace_id, "UNEXPECTED_ERROR", e)
        return {
            "project_slug": project_slug,
            "question": question,
            "raw_answer": f"Error processing query: {str(e)}",
            "answer": "An unexpected error occurred while processing your query.",
            "confidence": 0.0,
            "bq_query": None,
            "bq_params": None,
            "meta": {"reason": "unexpected_error", "trace_id": trace_id, "error_details": str(e)},
            "checkpoints": checkpoints,
        }


# ----------------------------
# Public function
# ----------------------------
async def search_details(
    *,
    project_slug: str,
    question: str,
    model: str,
    user_id: Optional[str] = None,
    messages: Optional[List[Dict[str, Any]]] = None,
    use_gemini: bool = True,
    min_confidence_to_answer: float = 0.1,
    log_checkpoints: bool = True,
) -> Dict[str, Any]:
    """
    Public API for search_details service.
    
    Args:
        project_slug: The project identifier
        question: The user's question
        model: The Gemini model to use (if use_gemini=True)
        user_id: Optional user identifier
        messages: Optional conversation history
        use_gemini: Whether to use Gemini for planning
        min_confidence_to_answer: Minimum confidence threshold
        log_checkpoints: Whether to log detailed checkpoints
        
    Returns:
        Dict containing answer, confidence, metadata, and checkpoints
    """
    return await run_search_details(
        project_slug=project_slug,
        question=question,
        model=model,
        user_id=user_id,
        messages=messages or [],
        use_gemini=use_gemini,
        min_confidence_to_answer=min_confidence_to_answer,
        log_checkpoints=log_checkpoints,
    )
