# bigquery-deep-analyser

**Oleon Q Search** is a FastAPI backend. It pulls chat conversation snapshots from an upstream API and stores the messages in Google BigQuery. It then uses Gemini (Vertex AI) to build per-user behaviour profiles and answers questions about that data.

## What it does

- **Ingest:** fetches the latest encrypted conversation snapshot from the upstream API, decrypts it and writes the messages to a BigQuery `messages` table.
- **Analyse:** groups a day's messages by user, classifies each user with Gemini (language, sentiment, urgency, customer stage, resolution status, top intents) and upserts the result into a `user_behavior` table.
- **Search:** answers natural-language questions over `user_behavior` (for example, "How many users asked about phones?"). It uses rule-based query planning first and falls back to Gemini only when the intent is unclear.
- **Trends:** counts keyword mentions over time as daily, weekly, monthly or yearly series ready for a line chart. Gemini expands the keyword into related terms first.
- **Cost reporting:** search and intent-analysis responses include the BigQuery and Gemini cost of the call in USD.

## Tech stack

- Python 3.12, FastAPI, Uvicorn / Gunicorn
- Google Cloud BigQuery (`google-cloud-bigquery`)
- Vertex AI Gemini (`google-cloud-aiplatform`, `vertexai`). The default model is `gemini-2.5-flash-lite`.
- `cryptography` for decrypting snapshots

## Project structure

```
app/
├── index.py              # FastAPI app entry point (app.index:app)
├── api/
│   ├── routes.py         # Mounts the v1 routers under /v1
│   └── v1/endpoints/     # Endpoint handlers
├── controllers/          # Health / snapshot controllers
├── core/
│   ├── settings.py       # Environment-driven settings (main config)
│   ├── gcp_credentials.py# Writes service-account JSON from env on startup
│   └── logger.py
├── middleware/           # CORS, error handler, rate limiter
├── services/             # BigQuery, Gemini, snapshot, search, analysis logic
└── utils/
docs/API_DOCUMENTATION.md # Full request/response reference
tests/vertex_test.py      # Vertex AI connectivity check
Procfile                  # Heroku process definition
```

## API endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness check |
| `POST` | `/v1/process_messages` | Pull the latest snapshot from upstream, decrypt it and insert messages into BigQuery |
| `POST` | `/v1/analyze_user` | Start a background job that builds or updates user behaviour profiles for a date |
| `GET` | `/v1/analyze_user/status/{job_id}` | Check the status and result of an analysis job |
| `POST` | `/v1/search_details` | Answer a natural-language question about user behaviour |
| `GET` | `/v1/user_summary?user_id=…&project_slug=…` | Get the latest stored summary for a user |
| `POST` | `/v1/intent_analysis` | Get a keyword-mention trend series over a date range |

See [docs/API_DOCUMENTATION.md](docs/API_DOCUMENTATION.md) for request bodies, response shapes and processing details. When the server is running, interactive docs are at `/docs` (Swagger) and `/redoc`.

Analysis job results are kept in memory, so they are lost when the server restarts and aren't shared between workers.

## Getting started

### Prerequisites

- Python 3.12+
- A Google Cloud project with BigQuery and Vertex AI enabled
- A service account with BigQuery read/write and Vertex AI user permissions

### Install

```bash
python -m venv venv
# Windows
venv\Scripts\activate
# macOS / Linux
source venv/bin/activate

pip install -r requirements.txt
```

### Configure

Create a `.env` file in the project root. Settings are read in [app/core/settings.py](app/core/settings.py):

```env
# Server
PORT=8000
CLIENT_ORIGIN=*

# Upstream snapshot API
UPSTREAM_BASE_URL=https://your-upstream-api
UPSTREAM_API_KEY=            # optional
UPSTREAM_TIMEOUT_S=30

# BigQuery
BQ_ENABLED=true
BQ_PROJECT_ID=your-gcp-project
BQ_DATASET_ID=your_dataset
BQ_MESSAGES_TABLE_ID=messages          # falls back to BQ_TABLE_ID
BQ_USER_BEHAVIOR_TABLE_ID=user_behavior
BQ_MESSAGES_TS_COL=created_at
LIMIT_MESSAGES_PER_USER=200

# Vertex AI / Gemini
VERTEX_PROJECT_ID=your-gcp-project     # defaults to BQ_PROJECT_ID
VERTEX_LOCATION=us-central1
GEMINI_MODEL=gemini-2.5-flash-lite
GEMINI_MAX_CONCURRENCY=3
GEMINI_LOG_RAW=true
GEMINI_LOG_RAW_PREVIEW_CHARS=1200

# Timezone used to decide what "today" means
APP_TZ=Asia/Colombo

# Google credentials (choose one, see below)
GOOGLE_APPLICATION_CREDENTIALS=path/to/service-account.json
# GOOGLE_CREDENTIALS_JSON={"type":"service_account",...}
```

#### Google credentials

[app/core/gcp_credentials.py](app/core/gcp_credentials.py) runs on startup, before any BigQuery or Vertex client is created. Use one of these setups:

- **Local:** set `GOOGLE_APPLICATION_CREDENTIALS` to the path of a service-account JSON file.
- **Hosted (Heroku etc.):** put the whole service-account JSON in `GOOGLE_CREDENTIALS_JSON`. On startup the app writes it to `GOOGLE_APPLICATION_CREDENTIALS`, or to `/tmp/gcp-sa.json` if that isn't set.

> Never commit service-account keys or `.env`. Both are listed in `.gitignore`.

### Run locally

```bash
uvicorn app.index:app --reload --port 8000
```

Then open http://localhost:8000/docs.

## Deployment

The [Procfile](Procfile) runs the app on Heroku-style platforms:

```
gunicorn app.index:app -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:$PORT --workers 1 --threads 1 --timeout 120
```

On the host, set the environment variables above, with the service-account key in `GOOGLE_CREDENTIALS_JSON`. Keep it to a single worker, because analysis job state is stored in memory.

## Testing

`tests/vertex_test.py` checks that the configured credentials can reach Vertex AI / Gemini:

```bash
python tests/vertex_test.py
```

## Notes

- `app/api/v1/endpoints/get_messages.py`, `snapshots.py` and `health.py` exist but aren't mounted in `app/api/routes.py`.
- `vercel.json` is currently empty. The Procfile is the deployment config in use.
