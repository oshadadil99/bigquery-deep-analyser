import os
from openai import OpenAI
from app.core.settings import get_settings
from oleeaitoolkit.logger import logger
from typing import Optional, Type, Union
from pydantic import BaseModel
from oleeaitoolkit.ai import VercelOpenAI

settings = get_settings()

# Reusable instance
VercelOpenAI = VercelOpenAI(
    api_key=settings.vercel_ai.api_key,
    base_url=settings.vercel_ai.base_url,
)