# app/services/snapshot_decrypt_service.py
from __future__ import annotations

import asyncio
import importlib
import os
import pkgutil
import traceback
from typing import Any, Dict, Callable


class DecryptSnapshotUnavailable(RuntimeError):
    """
    Raised when decrypt_snapshot is not available in this deployment.
    """


def _debug_toolkit_state() -> Dict[str, Any]:
    """
    Collects diagnostic info to understand why oleeaitoolkit.cryptography is missing.
    Safe to log (does NOT include secrets).
    """
    info: Dict[str, Any] = {
        "python_version": None,
        "running_on_heroku": bool(os.getenv("DYNO")),
        "module_check": {
            "oleeaitoolkit_installed": False,
            "oleeaitoolkit_version": None,
            "cryptography_submodule_present": False,
            "available_submodules": [],
        },
    }

    try:
        import sys
        info["python_version"] = sys.version
    except Exception:
        pass

    # Try import oleeaitoolkit
    try:
        import oleeaitoolkit  # type: ignore

        info["module_check"]["oleeaitoolkit_installed"] = True
        info["module_check"]["oleeaitoolkit_version"] = getattr(oleeaitoolkit, "__version__", None)

        # List submodules under oleeaitoolkit (best-effort)
        try:
            pkg_path = getattr(oleeaitoolkit, "__path__", None)
            if pkg_path:
                subs = [m.name for m in pkgutil.iter_modules(pkg_path)]
                info["module_check"]["available_submodules"] = sorted(subs)[:200]
                info["module_check"]["cryptography_submodule_present"] = "cryptography" in subs
        except Exception:
            pass

    except Exception:
        # toolkit not importable at all
        return info

    return info


def _load_decrypt_snapshot() -> Callable[[str, str], Dict[str, Any]]:
    """
    Lazy-load decrypt_snapshot from oleeaitoolkit.cryptography.
    This prevents app boot failure when module doesn't exist.
    """
    try:
        mod = importlib.import_module("oleeaitoolkit.cryptography")
    except Exception as e:
        diag = _debug_toolkit_state()
        raise DecryptSnapshotUnavailable(
            "oleeaitoolkit.cryptography could not be imported. "
            "This deployment cannot decrypt snapshots until the toolkit provides that module."
            f" | diagnostics={diag}"
        ) from e

    fn = getattr(mod, "decrypt_snapshot", None)
    if not callable(fn):
        diag = _debug_toolkit_state()
        raise DecryptSnapshotUnavailable(
            "oleeaitoolkit.cryptography imported but decrypt_snapshot() not found/callable."
            f" | diagnostics={diag}"
        )

    return fn  # type: ignore[return-value]


def decrypt_snapshot(file_url: str, private_key: str) -> Dict[str, Any]:
    """
    Synchronous decrypt wrapper (keeps old functionality).
    """
    fn = _load_decrypt_snapshot()
    return fn(file_url, private_key)


async def decrypt_snapshot_async(file_url: str, private_key: str) -> Dict[str, Any]:
    """
    Async wrapper used by endpoints/services.
    """
    try:
        return await asyncio.to_thread(decrypt_snapshot, file_url, private_key)
    except Exception:
        # keep traceback for callers/loggers
        raise