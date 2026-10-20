from pathlib import Path
import os
from dotenv import load_dotenv

import vertexai
from vertexai.generative_models import GenerativeModel

# Load .env from project root (one folder above /tests)
ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

PROJECT_ID = os.getenv("VERTEX_PROJECT_ID") or os.getenv("BQ_PROJECT_ID")
LOCATION = os.getenv("VERTEX_LOCATION", "us-central1")
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")

print("PROJECT_ID =", PROJECT_ID)
print("LOCATION   =", LOCATION)
print("CRED_FILE  =", os.getenv("GOOGLE_APPLICATION_CREDENTIALS"))

# Initialize Vertex
vertexai.init(project=PROJECT_ID, location=LOCATION)

model = GenerativeModel(MODEL_NAME)
resp = model.generate_content("Say: Vertex is working.")
print(resp.text)
