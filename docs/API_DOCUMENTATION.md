# Olee Q Search - System and API Documentation

## 1. System Overview

This backend is a FastAPI service that:
- pulls conversation snapshots from an upstream API,
- decrypts snapshot payloads,
- stores and reads data in BigQuery,
- runs user behavior analysis using Gemini (Vertex AI),
- answers search questions over user behavior data,
- provides utility APIs for summary and intent trend analysis.

Active API router registration is in:
- `app/api/routes.py`

Mounted active endpoint modules:
- `app/api/v1/endpoints/process_messages.py`
- `app/api/v1/endpoints/analyze_user.py`
- `app/api/v1/endpoints/search_details.py`
- `app/api/v1/endpoints/user_summary.py`
- `app/api/v1/endpoints/intent_analysis.py`


## 2. Core Data Paths

### 2.1 Messages ingestion path
1. `POST /v1/process_messages`
2. Calls upstream snapshot APIs
3. Decrypts latest snapshot payload
4. Inserts selected messages into BigQuery messages table

Main files:
- `app/api/v1/endpoints/process_messages.py`
- `app/services/snapshot_service.py`
- `app/services/snapshot_decrypt_service.py`
- `app/services/bigquery_service.py`

### 2.2 User behavior analysis path
1. `POST /v1/analyze_user` starts background job
2. Fetches today's messages from BigQuery
3. Groups by user
4. Loads existing user_behavior profiles
5. Runs Gemini classification per user (if enabled)
6. Builds/upserts user_behavior rows
7. Verifies recent updates
8. Status read via `GET /v1/analyze_user/status/{job_id}`

Main files:
- `app/api/v1/endpoints/analyze_user.py`
- `app/services/user_behavior_service.py`
- `app/services/gemini_service.py`

### 2.3 Search path
1. `POST /v1/search_details`
2. Rule-based intent planning
3. Optional Gemini planner (only when rule intent is unknown)
4. BigQuery fetch from user_behavior
5. Python-side answer computation
6. Returns answer + confidence + query meta + cost meta

Main files:
- `app/api/v1/endpoints/search_details.py`
- `app/services/search_details_service.py`


## 3. API Endpoints (Active)

## 3.1 Process Messages
- Method: `POST`
- Path: `/v1/process_messages`
- File: `app/api/v1/endpoints/process_messages.py`

Request body:
```json
{
  "version": "v4",
  "projectSlug": "fintra",
  "accessToken": "Bearer-or-raw-token",
  "limit": 10,
  "snapshotsPage": 1,
  "snapshotsLimit": 20
}
```

Processing steps:
1. Fetch snapshot list using upstream API.
2. Pick latest snapshot id.
3. Fetch snapshot details (private key + file URL).
4. Decrypt snapshot.
5. Take top `limit` messages.
6. Insert into BigQuery via `insert_messages_to_bigquery`.

Response shape:
```json
{
  "success": true,
  "upstream_version": "v4",
  "projectSlug": "fintra",
  "snapshot_id": "....",
  "count": 10,
  "messages": [ ... ],
  "bigquery": {
    "enabled": true,
    "inserted": 10,
    "errors": []
  }
}
```


## 3.2 Analyze User (Background Job)
- Method: `POST`
- Path: `/v1/analyze_user`
- File: `app/api/v1/endpoints/analyze_user.py`

Request body:
```json
{
  "project_slug": "fintra",
  "target_date": "2026-02-20",
  "primary_language_categories": [],
  "overall_sentiment_categories": [],
  "urgency_level_categories": [],
  "customer_stage_categories": [],
  "last_resolved_status_categories": [],
  "top_intent_categories": [],
  "model": "gemini-2.5-flash-lite",
  "max_concurrency": 1,
  "use_gemini": true,
  "log_checkpoints": true,
  "log_samples": true
}
```

Immediate response:
```json
{
  "success": true,
  "status": "processing",
  "job_id": "uuid",
  "message": "...",
  "status_url": "/v1/analyze_user/status/{job_id}"
}
```

Status endpoint:
- Method: `GET`
- Path: `/v1/analyze_user/status/{job_id}`

Background processing details:
1. Fetch messages from BigQuery messages table.
2. Group rows by `user_id`.
3. Fetch existing user_behavior profiles.
4. Gemini classify users (if enabled).
5. Build profile rows (`summery`, tags, intents, confidence, etc.).
6. Upsert into user_behavior table.
7. Verify updates.
8. Store final result in in-memory `job_results`.


## 3.3 Search Details
- Method: `POST`
- Path: `/v1/search_details`
- File: `app/api/v1/endpoints/search_details.py`

Request body:
```json
{
  "project_slug": "fintra",
  "question": "How many users asked about phones?",
  "model": "gemini-2.5-flash",
  "user_id": null,
  "messages": [],
  "use_gemini": true,
  "min_confidence_to_answer": 0.1,
  "log_checkpoints": true
}
```

How it works:
1. Relevance check (user behavior domain check).
2. Rule-based plan build.
3. If rule intent is `unknown` and `use_gemini=true`, run Gemini planner.
4. Query user_behavior table in BigQuery.
5. Compute answer in Python logic from fetched rows.
6. Apply confidence gate.
7. Return answer and metadata.
8. Log final cost line: `[SEARCH_DETAILS][FINAL_COST_USD] ...`.

Response includes:
- `answer`, `confidence`
- `bq_query`, `bq_params`
- `meta.plan`, `meta.evidence`
- `meta.cost` with:
  - `bq_cost_usd`
  - `gemini_cost_usd`
  - `total_cost_usd`


## 3.4 User Summary
- Method: `GET`
- Path: `/v1/user_summary`
- File: `app/api/v1/endpoints/user_summary.py`

Query params:
- `user_id` (required)
- `project_slug` (optional)

How it works:
1. Reads user_behavior table.
2. Detects summary column (`summery` or `summary`).
3. Returns latest row summary (prefers latest timestamp column if present).

Response (found):
```json
{
  "success": true,
  "message": "ok",
  "user_id": "u123",
  "project_slug": "fintra",
  "summery": "..."
}
```

Response (not found):
```json
{
  "success": false,
  "message": "user data still not available",
  "user_id": "u123",
  "project_slug": "fintra",
  "summery": null
}
```


## 3.5 Intent Analysis
- Method: `POST`
- Path: `/v1/intent_analysis`
- File: `app/api/v1/endpoints/intent_analysis.py`

Primary 3-field request body:
```json
{
  "keyword": "phone",
  "duration": {
    "from_date": "02-11-2025",
    "to_date": "04-12-2025"
  },
  "frequency": "weekly"
}
```

Accepted frequency values:
- `daily`
- `weekly`
- `monthly`
- `yearly`

Backward-compatible body also supported:
```json
{
  "keyword": "phone",
  "from_date": "02-11-2025",
  "to_date": "04-12-2025",
  "frequency": "weekly"
}
```

How it works:
1. Validates keyword is single concept.
2. Parses date range.
3. Expands nearest keywords with Gemini.
4. Adds singular/plural variants.
5. Queries BigQuery messages table and counts mentions by bucket.
6. Returns line-chart-ready `series`.
7. Returns per-call cost and logs `[INTENT_ANALYSIS][FINAL_COST_USD] ...`.

Response includes:
- `expanded_keywords`
- `total_mentions`
- `series` as:
  - `period_start`
  - `mention_count`
- `cost`:
  - `bq_cost_usd`
  - `gemini_cost_usd`
  - `total_cost_usd`
  - bytes processed/billed and Gemini token usage


## 4. Configuration Inputs

Main settings are loaded from environment in:
- `app/core/settings.py`

Important vars:
- BigQuery:
  - `BQ_ENABLED`
  - `BQ_PROJECT_ID`
  - `BQ_DATASET_ID`
  - `BQ_MESSAGES_TABLE_ID` (or fallback `BQ_TABLE_ID`)
  - `BQ_USER_BEHAVIOR_TABLE_ID`
- Vertex/Gemini:
  - `VERTEX_PROJECT_ID`
  - `VERTEX_LOCATION`
  - `GEMINI_MODEL`
- Upstream API:
  - `UPSTREAM_BASE_URL`
  - `UPSTREAM_API_KEY` (optional)


## 5. Notes on Existing but Unmounted Endpoint Files

`app/api/v1/endpoints/get_messages.py` exists, but it is not included in `app/api/routes.py` in the current active router wiring.  
Only endpoints listed in Section 3 are mounted under `/v1`.

