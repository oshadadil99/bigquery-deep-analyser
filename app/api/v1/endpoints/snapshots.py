# app/api/v1/endpoints/snapshots.py
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field
from typing import Optional

from app.controllers.snapshot_controller import get_snapshots_controller

router = APIRouter()


class ProjectConfigIn(BaseModel):
    project_id: Optional[str] = Field(None, alias="projectId")
    project_slug: str = Field(..., alias="projectSlug")
    key: Optional[str] = None


class SnapshotsRequest(BaseModel):
    project: ProjectConfigIn
    fintra_api_key: str = Field(..., alias="fintraApiKey")
    page: int = 1
    limit: int = 20


@router.post("/snapshots")
async def get_snapshots(payload: SnapshotsRequest):
    return await get_snapshots_controller(
        project_id=payload.project.project_id,
        project_slug=payload.project.project_slug,
        key=payload.project.key,
        fintra_api_key=payload.fintra_api_key,
        page=payload.page,
        limit=payload.limit,
    )
