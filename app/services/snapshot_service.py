from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger("app_logger")


@dataclass
class UpstreamError(Exception):
    """Exception for upstream API errors (4xx, 5xx responses)"""
    message: str
    url: str
    status_code: int
    body_preview: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message": self.message,
            "upstream_url": self.url,
            "upstream_status": self.status_code,
            "upstream_body_preview": self.body_preview,
        }


def _build_auth_header(access_token: str) -> Dict[str, str]:
    """
    Normalize access token to proper Authorization header.
    
    Handles:
      - Raw token: eyJhbGci...
      - Bearer token: Bearer eyJhbGci...
    
    Returns:
      {"Authorization": "Bearer <token>"}
    """
    token = (access_token or "").strip()
    if not token:
        return {}
    
    if token.lower().startswith("bearer "):
        return {"Authorization": token}
    
    return {"Authorization": f"Bearer {token}"}


async def _request_json(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 30.0,
    max_retries: int = 2,
) -> Dict[str, Any]:
    """
    Make HTTP request with error handling and retry logic.
    
    Args:
        method: HTTP method (GET, POST, etc.)
        url: Full URL to request
        headers: Optional request headers
        timeout: Request timeout in seconds
        max_retries: Number of retries for transient errors
        
    Returns:
        Parsed JSON response
        
    Raises:
        ConnectionError: When unable to connect to service
        TimeoutError: When request times out
        UpstreamError: When upstream returns error status (4xx, 5xx)
    """
    last_error = None
    
    for attempt in range(max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                logger.debug(
                    f"[SNAPSHOT_SERVICE] {method} {url} "
                    f"(attempt {attempt + 1}/{max_retries + 1})"
                )
                
                resp = await client.request(method, url, headers=headers or {})
                
                text = resp.text or ""
                preview = text[:500]

                # Check for HTTP error status codes
                if resp.status_code >= 400:
                    logger.error(
                        f"[SNAPSHOT_SERVICE] HTTP {resp.status_code} from {url}: "
                        f"{preview}"
                    )
                    raise UpstreamError(
                        message=f"Upstream API returned {resp.status_code}",
                        url=url,
                        status_code=resp.status_code,
                        body_preview=preview,
                    )

                # Try to parse JSON
                try:
                    return resp.json()
                except Exception as json_err:
                    logger.error(
                        f"[SNAPSHOT_SERVICE] Invalid JSON from {url}: {preview}"
                    )
                    raise UpstreamError(
                        message="Upstream returned non-JSON response",
                        url=url,
                        status_code=resp.status_code,
                        body_preview=preview,
                    ) from json_err

        except httpx.ConnectError as e:
            last_error = e
            logger.error(
                f"[SNAPSHOT_SERVICE] Connection failed to {url} "
                f"(attempt {attempt + 1}/{max_retries + 1}): {e}"
            )
            
            # Don't retry connection errors - they usually won't resolve quickly
            raise ConnectionError(
                f"Cannot connect to upstream service at {url}. "
                f"Please verify the service is running and the URL is correct. "
                f"Error: {e}"
            ) from e

        except httpx.TimeoutException as e:
            last_error = e
            logger.error(
                f"[SNAPSHOT_SERVICE] Timeout for {url} "
                f"(attempt {attempt + 1}/{max_retries + 1}): {e}"
            )
            
            # Retry timeout errors
            if attempt < max_retries:
                logger.info(
                    f"[SNAPSHOT_SERVICE] Retrying after timeout "
                    f"(attempt {attempt + 2}/{max_retries + 1})..."
                )
                continue
            
            raise TimeoutError(
                f"Request to {url} timed out after {timeout} seconds "
                f"({max_retries + 1} attempts)"
            ) from e

        except httpx.HTTPError as e:
            last_error = e
            logger.error(
                f"[SNAPSHOT_SERVICE] HTTP error for {url} "
                f"(attempt {attempt + 1}/{max_retries + 1}): {e}"
            )
            
            # Retry on general HTTP errors
            if attempt < max_retries:
                logger.info(
                    f"[SNAPSHOT_SERVICE] Retrying after HTTP error "
                    f"(attempt {attempt + 2}/{max_retries + 1})..."
                )
                continue
            
            raise ConnectionError(
                f"HTTP error while connecting to {url}: {e}"
            ) from e

        except UpstreamError:
            # Don't retry on 4xx/5xx errors - they won't resolve with retries
            raise

        except Exception as e:
            last_error = e
            logger.exception(
                f"[SNAPSHOT_SERVICE] Unexpected error for {url}: {e}"
            )
            raise

    # Should not reach here, but just in case
    if last_error:
        raise ConnectionError(
            f"Failed to connect to {url} after {max_retries + 1} attempts"
        ) from last_error


async def list_snapshots(
    *,
    base_url: str,
    upstream_version: str,
    project_slug: str,
    access_token: str,
    page: int = 1,
    limit: int = 20,
) -> Dict[str, Any]:
    """
    List snapshots from upstream API.
    
    Endpoint: GET {base_url}/{upstream_version}/oleon/convos/{project_slug}/history/snapshots
    
    Args:
        base_url: Base URL of upstream service (e.g., "http://localhost:3000")
        upstream_version: API version (e.g., "v4")
        project_slug: Project identifier
        access_token: JWT access token for authentication
        page: Page number for pagination (default: 1)
        limit: Number of items per page (default: 20)
        
    Returns:
        JSON response containing list of snapshots
        
    Raises:
        ConnectionError: Cannot connect to upstream service
        TimeoutError: Request timed out
        UpstreamError: Upstream API returned error status
        ValueError: Invalid parameters
        
    Example:
        >>> snapshots = await list_snapshots(
        ...     base_url="http://localhost:3000",
        ...     upstream_version="v4",
        ...     project_slug="fintra",
        ...     access_token="eyJhbGci...",
        ...     page=1,
        ...     limit=20
        ... )
    """
    # Validate required parameters
    if not base_url:
        raise ValueError("base_url is required but not provided")
    if not upstream_version:
        raise ValueError("upstream_version is required but not provided")
    if not project_slug:
        raise ValueError("project_slug is required but not provided")
    if not access_token:
        raise ValueError("access_token is required but not provided")
    
    # Normalize URL components
    base_url = base_url.rstrip("/")
    upstream_version = upstream_version.strip("/")
    project_slug = project_slug.strip()

    # Build full URL
    url = (
        f"{base_url}/{upstream_version}/oleon/convos/{project_slug}"
        f"/history/snapshots?page={page}&limit={limit}"
    )

    # Build headers
    headers = {
        "Accept": "application/json",
        **_build_auth_header(access_token)
    }
    
    logger.info(
        f"[SNAPSHOT_SERVICE] Listing snapshots: "
        f"project={project_slug}, page={page}, limit={limit}"
    )
    logger.debug(f"[SNAPSHOT_SERVICE] URL: {url}")

    return await _request_json("GET", url, headers=headers)


async def get_snapshot_details(
    *,
    base_url: str,
    upstream_version: str,
    project_slug: str,
    snapshot_id: str,
    access_token: str,
) -> Dict[str, Any]:
    """
    Get detailed information for a specific snapshot.
    
    Endpoint: GET {base_url}/{upstream_version}/oleon/convos/{project_slug}/history/snapshots/{snapshot_id}
    
    Args:
        base_url: Base URL of upstream service
        upstream_version: API version
        project_slug: Project identifier
        snapshot_id: Unique snapshot identifier
        access_token: JWT access token for authentication
        
    Returns:
        JSON response containing snapshot details including:
          - snapshot_private_key: Decryption key
          - file_url: URL to encrypted snapshot file
          - other metadata
          
    Raises:
        ConnectionError: Cannot connect to upstream service
        TimeoutError: Request timed out
        UpstreamError: Upstream API returned error status
        ValueError: Invalid parameters
        
    Example:
        >>> details = await get_snapshot_details(
        ...     base_url="http://localhost:3000",
        ...     upstream_version="v4",
        ...     project_slug="fintra",
        ...     snapshot_id="snap_123abc",
        ...     access_token="eyJhbGci..."
        ... )
        >>> private_key = details["data"]["snapshot_private_key"]
        >>> file_url = details["data"]["file_url"]
    """
    # Validate required parameters
    if not base_url:
        raise ValueError("base_url is required but not provided")
    if not upstream_version:
        raise ValueError("upstream_version is required but not provided")
    if not project_slug:
        raise ValueError("project_slug is required but not provided")
    if not snapshot_id:
        raise ValueError("snapshot_id is required but not provided")
    if not access_token:
        raise ValueError("access_token is required but not provided")
    
    # Normalize URL components
    base_url = base_url.rstrip("/")
    upstream_version = upstream_version.strip("/")
    project_slug = project_slug.strip()
    snapshot_id = snapshot_id.strip()

    # Build full URL
    url = (
        f"{base_url}/{upstream_version}/oleon/convos/{project_slug}"
        f"/history/snapshots/{snapshot_id}"
    )

    # Build headers
    headers = {
        "Accept": "application/json",
        **_build_auth_header(access_token)
    }
    
    logger.info(
        f"[SNAPSHOT_SERVICE] Getting snapshot details: "
        f"project={project_slug}, snapshot_id={snapshot_id}"
    )
    logger.debug(f"[SNAPSHOT_SERVICE] URL: {url}")

    return await _request_json("GET", url, headers=headers)