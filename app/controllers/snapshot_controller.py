from __future__ import annotations

from typing import Any, Dict, Optional
from app.core.settings import get_settings
from app.services.snapshot_service import fetch_snapshots


def build_project_config(project_id: Optional[str], project_slug: str, key: Optional[str]) -> Dict[str, Any]:
    return {
        "projectId": project_id,
        "projectSlug": project_slug,
        "key": key,
    }


async def get_snapshots_controller(
    project_id: Optional[str],
    project_slug: str,
    key: Optional[str],
    fintra_api_key: str,
    page: int = 1,
    limit: int = 20,
) -> Dict[str, Any]:
    settings = get_settings()
    project = build_project_config(project_id, project_slug, key)

    payload, ids = await fetch_snapshots(
        upstream_base=settings.upstream_base,
        fintra_api_key=fintra_api_key,
        project_slug=project_slug,
        page=page,
        limit=limit,
    )

    # NOTE: printing on API call is optional; your main requirement is startup printing.
    return {
        "success": True,
        "project": project,
        "snapshot_ids": ids,
        "data": payload.get("data", {}),
    }
