"""Opt-in, cached checks for newer headroom releases on PyPI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import unicodedata
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib import error, request

from .config import resolve_state_dir


UPDATE_CHECK_ENV = "HEADROOM_UPDATE_CHECK"
PYPI_URL = "https://pypi.org/pypi/headroom-cli/json"
CACHE_FILENAME = "update-check.json"
CACHE_SECONDS = 24 * 60 * 60
DEADLINE_SECONDS = 2.0
MAX_RESPONSE_BYTES = 256 * 1024
MAX_CACHE_BYTES = 16 * 1024
READ_SIZE = 16 * 1024
_VERSION = re.compile(
    r"^([0-9]+(?:\.[0-9]+)*)(?:(a|b|rc)([0-9]+))?(?:\.post([0-9]+))?$",
    re.IGNORECASE,
)
_PRECEDENCE = {"a": 0, "b": 1, "rc": 2}


@dataclass(frozen=True)
class ParsedVersion:
    """A deliberately small, conservative subset of PEP 440."""

    release: Tuple[int, ...]
    prerelease: Optional[Tuple[int, int]]
    post: Optional[int]

    @property
    def is_prerelease(self) -> bool:
        return self.prerelease is not None


@dataclass(frozen=True)
class UpdateResult:
    """A validated update-check result, whether fresh or cached."""

    outcome: str
    checked_at: float
    installed_version: str
    latest_version: Optional[str] = None
    reason: Optional[str] = None
    cached: bool = False

    @property
    def update_available(self) -> bool:
        return self.outcome == "update" and self.latest_version is not None

    @property
    def next_check_at(self) -> float:
        return self.checked_at + CACHE_SECONDS


def update_check_enabled(environ: Optional[Mapping[str, str]] = None) -> bool:
    """Return whether the exact opt-in value is present."""

    environment = os.environ if environ is None else environ
    return environment.get(UPDATE_CHECK_ENV) == "1"


def discovery_line(environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Return the default local-only discovery hint, unless explicitly configured."""

    environment = os.environ if environ is None else environ
    if UPDATE_CHECK_ENV in environment:
        return None
    return "Updates: checking is off; set HEADROOM_UPDATE_CHECK=1 to enable."


def parse_version(value: str) -> Optional[ParsedVersion]:
    """Parse the supported release, pre-release, and post-release forms."""

    if not isinstance(value, str) or len(value) > 128:
        return None
    match = _VERSION.fullmatch(value)
    if match is None:
        return None
    prerelease_label = match.group(2)
    post_text = match.group(4)
    if prerelease_label is not None and post_text is not None:
        return None
    try:
        release = tuple(int(part) for part in match.group(1).split("."))
        prerelease = (
            (_PRECEDENCE[prerelease_label.lower()], int(match.group(3)))
            if prerelease_label is not None
            else None
        )
        post = int(post_text) if post_text is not None else None
    except (KeyError, TypeError, ValueError):
        return None
    return ParsedVersion(release, prerelease, post)


def version_is_newer(candidate: ParsedVersion, installed: ParsedVersion) -> bool:
    """Compare parsed versions numerically."""

    width = max(len(candidate.release), len(installed.release))
    candidate_release = candidate.release + (0,) * (width - len(candidate.release))
    installed_release = installed.release + (0,) * (width - len(installed.release))
    if candidate_release != installed_release:
        return candidate_release > installed_release
    return _suffix_key(candidate) > _suffix_key(installed)


def select_latest_release(document: Any, installed: ParsedVersion) -> Optional[str]:
    """Select the newest eligible, non-yanked release from a PyPI document."""

    if not isinstance(document, dict):
        raise ValueError("PyPI response is not an object")
    releases = document.get("releases")
    if not isinstance(releases, dict):
        raise ValueError("PyPI response has no releases object")
    selected_name: Optional[str] = None
    selected: Optional[ParsedVersion] = None
    credible_release = False
    for name, files in releases.items():
        if not isinstance(name, str) or not _has_non_yanked_file(files):
            continue
        parsed = parse_version(name)
        if parsed is None:
            continue
        credible_release = True
        if parsed.is_prerelease and not installed.is_prerelease:
            continue
        if not version_is_newer(parsed, installed):
            continue
        if selected is None or version_is_newer(parsed, selected):
            selected_name = name
            selected = parsed
    if not credible_release:
        raise ValueError("PyPI response has no credible releases")
    return selected_name


def check_for_update(
    installed_version: str,
    state_dir: Optional[Path] = None,
    now: Optional[float] = None,
) -> UpdateResult:
    """Return a daily cached result, performing the only network call in headroom."""

    checked_at = time.time() if now is None else now
    cached = read_cached_result(state_dir, installed_version)
    if cached is not None:
        age = checked_at - cached.checked_at
        if 0 <= age < CACHE_SECONDS:
            return _with_cached(cached)

    installed = parse_version(installed_version)
    if installed is None:
        result = UpdateResult(
            "failure",
            checked_at,
            installed_version,
            reason="installed version {!r} is outside the supported PEP 440 subset".format(
                _safe_text(installed_version, 80)
            ),
        )
        _try_write_cache(result, state_dir)
        return result

    try:
        document = _fetch_pypi_json()
        latest = select_latest_release(document, installed)
        result = UpdateResult(
            "update" if latest is not None else "current",
            checked_at,
            installed_version,
            latest_version=latest,
        )
    except _ResponseTooLarge:
        result = UpdateResult(
            "failure",
            checked_at,
            installed_version,
            reason="PyPI response exceeded {} bytes".format(MAX_RESPONSE_BYTES),
        )
    except json.JSONDecodeError:
        result = UpdateResult(
            "failure", checked_at, installed_version, reason="PyPI returned invalid JSON"
        )
    except ValueError as exc:
        result = UpdateResult(
            "failure", checked_at, installed_version, reason=_safe_text(str(exc), 160)
        )
    except (TimeoutError, error.URLError, OSError) as exc:
        result = UpdateResult(
            "failure",
            checked_at,
            installed_version,
            reason="network check failed: {}".format(_safe_text(str(exc), 120)),
        )
    except Exception as exc:
        result = UpdateResult(
            "failure",
            checked_at,
            installed_version,
            reason="update check failed: {}".format(type(exc).__name__),
        )
    _try_write_cache(result, state_dir)
    return result


def read_cached_result(
    state_dir: Optional[Path] = None,
    installed_version: Optional[str] = None,
) -> Optional[UpdateResult]:
    """Read and validate the separate, attacker-influenced update cache."""

    path = resolve_state_dir(state_dir) / CACHE_FILENAME
    # Cache authentication is intentionally out of scope. Anyone who can write
    # the user's state directory can also replace the headroom executable, so
    # integrity material would add complexity without protecting this threat model.
    try:
        with path.open("rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return None
            payload = handle.read(MAX_CACHE_BYTES + 1)
        if len(payload) > MAX_CACHE_BYTES:
            return None
        value = json.loads(payload)
        result = _result_from_cache(value)
        if (
            result is not None
            and installed_version is not None
            and result.installed_version != installed_version
        ):
            return None
        return result
    except (
        OSError,
        UnicodeError,
        ValueError,
        TypeError,
        RecursionError,
        OverflowError,
    ):
        return None


def format_timestamp(timestamp: float) -> str:
    """Format a cache timestamp for doctor output."""

    try:
        value = datetime.fromtimestamp(timestamp, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return "unavailable"
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _fetch_pypi_json() -> Any:
    started = time.monotonic()
    deadline = started + DEADLINE_SECONDS
    request_value = request.Request(
        PYPI_URL,
        headers={
            "Accept": "application/json",
            "Connection": "close",
            "User-Agent": "headroom-update-check",
        },
        method="GET",
    )
    try:
        response = _open_pypi(request_value, timeout=DEADLINE_SECONDS)
    except error.HTTPError as exc:
        try:
            raise ValueError("PyPI returned HTTP status {}".format(exc.code)) from None
        finally:
            exc.close()
    try:
        if getattr(response, "status", 200) != 200:
            raise ValueError("PyPI returned a non-success response")
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                if int(content_length) > MAX_RESPONSE_BYTES:
                    raise _ResponseTooLarge
            except ValueError:
                pass
        chunks = []
        total = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("deadline exceeded")
            _set_response_timeout(response, remaining)
            chunk = response.read(min(READ_SIZE, MAX_RESPONSE_BYTES + 1 - total))
            if time.monotonic() >= deadline:
                raise TimeoutError("deadline exceeded")
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise _ResponseTooLarge
            chunks.append(chunk)
        return json.loads(b"".join(chunks).decode("utf-8"))
    finally:
        response.close()


def _open_pypi(request_value: request.Request, timeout: float) -> Any:
    """Open the fixed PyPI URL without following redirects."""

    opener = request.build_opener(_NoRedirectHandler())
    return opener.open(request_value, timeout=timeout)


def _set_response_timeout(response: Any, timeout: float) -> None:
    """Limit read inactivity; the caller checks its deadline between reads."""

    try:
        response.fp.raw._sock.settimeout(timeout)
    except (AttributeError, OSError):
        pass


def _suffix_key(version: ParsedVersion) -> Tuple[int, int, int]:
    if version.prerelease is not None:
        return (0, version.prerelease[0], version.prerelease[1])
    if version.post is None:
        return (1, 0, 0)
    return (2, 0, version.post)


def _has_non_yanked_file(files: Any) -> bool:
    return isinstance(files, list) and any(
        isinstance(item, dict) and item.get("yanked") is False for item in files
    )


def _result_from_cache(value: Any) -> Optional[UpdateResult]:
    if not isinstance(value, dict) or value.get("version") != 1:
        return None
    outcome = value.get("outcome")
    checked_at = value.get("checked_at")
    installed_version = value.get("installed_version")
    latest_version = value.get("latest_version")
    reason = value.get("reason")
    if outcome not in ("current", "update", "failure"):
        return None
    if (
        isinstance(checked_at, bool)
        or not isinstance(checked_at, (int, float))
        or not math.isfinite(float(checked_at))
        or checked_at < 0
    ):
        return None
    if not _safe_cache_string(installed_version):
        return None
    if latest_version is not None and not _safe_cache_string(latest_version):
        return None
    if reason is not None and not _safe_cache_string(reason, 200):
        return None
    if outcome == "update":
        installed = parse_version(installed_version)
        latest = parse_version(latest_version) if isinstance(latest_version, str) else None
        if (
            installed is None
            or latest is None
            or (latest.is_prerelease and not installed.is_prerelease)
            or not version_is_newer(latest, installed)
            or reason is not None
        ):
            return None
    elif outcome == "current":
        if parse_version(installed_version) is None or latest_version is not None or reason is not None:
            return None
    elif latest_version is not None:
        return None
    if outcome == "failure" and not reason:
        return None
    return UpdateResult(
        outcome,
        float(checked_at),
        installed_version,
        latest_version,
        reason,
        cached=True,
    )


def _safe_cache_string(value: Any, limit: int = 128) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= limit
        and all(
            character.isprintable() and unicodedata.category(character) != "Cf"
            for character in value
        )
    )


def _safe_text(value: Any, limit: int) -> str:
    text = "".join(
        character
        if character.isprintable() and unicodedata.category(character) != "Cf"
        else " "
        for character in str(value)
    )
    text = " ".join(text.split())
    return (text[: limit - 3] + "...") if len(text) > limit else text


def _with_cached(result: UpdateResult) -> UpdateResult:
    return UpdateResult(
        result.outcome,
        result.checked_at,
        result.installed_version,
        result.latest_version,
        result.reason,
        cached=True,
    )


def _try_write_cache(result: UpdateResult, state_dir: Optional[Path]) -> None:
    try:
        _write_cache(result, state_dir)
    except OSError:
        pass


def _write_cache(result: UpdateResult, state_dir: Optional[Path]) -> None:
    directory = resolve_state_dir(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / CACHE_FILENAME
    document: Dict[str, Any] = {
        "version": 1,
        "checked_at": result.checked_at,
        "installed_version": result.installed_version,
        "outcome": result.outcome,
        "latest_version": result.latest_version,
        "reason": result.reason,
    }
    temporary_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(directory),
            prefix=".update-check-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(document, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, target)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except OSError:
                pass


class _ResponseTooLarge(Exception):
    pass


class _NoRedirectHandler(request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None
