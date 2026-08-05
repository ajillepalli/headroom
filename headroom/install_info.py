"""Describe the package copy backing the current headroom process."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
import re
import sys
from typing import Optional, Tuple


_PACKAGE_PATH = Path(__file__).resolve().parent
_OBJECT_ID = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")


@dataclass(frozen=True)
class InstallInfo:
    """Installation details useful when diagnosing stale commands."""

    path: Path
    mode: str
    version: str
    modified_at: Optional[float]
    commit: Optional[str]
    update_mode: str = "unknown"


def inspect_install(version: str, package_path: Optional[Path] = None) -> InstallInfo:
    """Return details about the imported package without invoking Git."""

    try:
        path = (package_path or _PACKAGE_PATH).resolve()
    except (OSError, RuntimeError, ValueError):
        path = package_path or _PACKAGE_PATH
    checkout = _checkout_root(path)
    return InstallInfo(
        path=path,
        mode="source" if checkout is not None else "installed",
        version=version,
        modified_at=_loaded_module_mtime(path),
        commit=_short_commit(checkout) if checkout is not None else None,
        update_mode="source" if checkout is not None else _installed_update_mode(path, package_path is None),
    )


def source_commit(package_path: Optional[Path] = None) -> Optional[str]:
    """Return the checkout commit for a source import, if safely available."""

    try:
        path = (package_path or _PACKAGE_PATH).resolve()
        checkout = _checkout_root(path)
        return _short_commit(checkout) if checkout is not None else None
    except (OSError, RuntimeError, ValueError):
        return None


def format_modified_time(timestamp: Optional[float]) -> str:
    """Format a module modification timestamp for doctor output."""

    if timestamp is None:
        return "unavailable"
    try:
        value = datetime.fromtimestamp(timestamp, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return "unavailable"
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _checkout_root(package_path: Path) -> Optional[Path]:
    """Recognize this project's package when imported from its checkout."""

    root = package_path.parent
    if package_path.name != "headroom":
        return None
    if not (root / "pyproject.toml").is_file():
        return None
    return root


def _loaded_module_mtime(package_path: Path) -> Optional[float]:
    latest: Optional[float] = None
    for name, module in tuple(sys.modules.items()):
        if name != "headroom" and not name.startswith("headroom."):
            continue
        raw_path = getattr(module, "__file__", None)
        if not isinstance(raw_path, str):
            continue
        try:
            path = Path(raw_path).resolve()
            path.relative_to(package_path)
            modified = path.stat().st_mtime
        except (OSError, ValueError):
            continue
        latest = modified if latest is None else max(latest, modified)
    return latest


def _short_commit(checkout: Optional[Path]) -> Optional[str]:
    if checkout is None:
        return None
    git_dirs = _git_directories(checkout / ".git")
    if git_dirs is None:
        return None
    git_dir, common_dir = git_dirs
    head = _read_small_text(git_dir / "HEAD")
    if head is None:
        return None
    head = head.strip()
    if _OBJECT_ID.fullmatch(head):
        return head[:7].lower()
    if not head.startswith("ref: "):
        return None
    reference = head[5:].strip()
    if not reference or Path(reference).is_absolute() or ".." in Path(reference).parts:
        return None
    for directory in _unique_paths((git_dir, common_dir)):
        value = _read_small_text(directory / reference)
        if value is not None and _OBJECT_ID.fullmatch(value.strip()):
            return value.strip()[:7].lower()
    for directory in _unique_paths((common_dir, git_dir)):
        value = _packed_reference(directory / "packed-refs", reference)
        if value is not None:
            return value[:7].lower()
    return None


def _git_directories(marker: Path) -> Optional[Tuple[Path, Path]]:
    try:
        if marker.is_dir():
            git_dir = marker.resolve()
        elif marker.is_file():
            value = _read_small_text(marker)
            if value is None or not value.strip().lower().startswith("gitdir:"):
                return None
            target = value.strip().split(":", 1)[1].strip()
            if not target:
                return None
            candidate = Path(target)
            git_dir = (
                candidate if candidate.is_absolute() else marker.parent / candidate
            ).resolve()
            if not git_dir.is_dir():
                return None
        else:
            return None
        common_dir = git_dir
        common_value = _read_small_text(git_dir / "commondir")
        if common_value:
            candidate = Path(common_value.strip())
            common_dir = (
                candidate if candidate.is_absolute() else git_dir / candidate
            ).resolve()
            if not common_dir.is_dir():
                common_dir = git_dir
        return git_dir, common_dir
    except (OSError, ValueError):
        return None


def _read_small_text(path: Path) -> Optional[str]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(4096)
    except OSError:
        return None


def _packed_reference(path: Path, reference: str) -> Optional[str]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle.read(1024 * 1024).splitlines():
                if line.startswith(("#", "^")):
                    continue
                fields = line.split(" ", 1)
                if len(fields) != 2 or fields[1] != reference:
                    continue
                return fields[0] if _OBJECT_ID.fullmatch(fields[0]) else None
    except OSError:
        return None
    return None


def _unique_paths(paths: Tuple[Path, Path]) -> Tuple[Path, ...]:
    return tuple(dict.fromkeys(paths))


def _installed_update_mode(package_path: Path, inspect_metadata: bool) -> str:
    """Identify only installation modes with reliable on-disk evidence."""

    try:
        for parent in (package_path,) + tuple(package_path.parents):
            if (parent / "uv-receipt.toml").is_file():
                return "uv-tool"
            if (parent / "pipx_metadata.json").is_file():
                return "unknown"
    except OSError:
        pass
    if not inspect_metadata:
        return "unknown"
    try:
        installer = metadata.distribution("headroom-cli").read_text("INSTALLER")
    except Exception:
        return "unknown"
    return "pip" if installer is not None and installer.strip().lower() == "pip" else "unknown"
