"""Read current Codex rate limits from the app-server RPC."""

from dataclasses import dataclass
import json
import math
import os
import queue
import shlex
import subprocess
import threading
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, TextIO, Tuple

from . import __version__
from .bounds import Snapshot
from .codexsrc import parse_rate_limits


RPC_ENABLED_ENV = "HEADROOM_CODEX_RPC"
RPC_TIMEOUT_ENV = "HEADROOM_CODEX_RPC_TIMEOUT"
RPC_COMMAND_ENV = "HEADROOM_CODEX_RPC_CMD"
DEFAULT_TIMEOUT_SECONDS = 6.0


@dataclass(frozen=True)
class CodexRpcResult:
    """Snapshots returned by app-server plus diagnostic details."""

    snapshots: Tuple[Snapshot, ...]
    attempted: bool
    notes: Tuple[str, ...]


class _RpcFailure(Exception):
    pass


class _RpcTimeout(_RpcFailure):
    pass


def read_rate_limits(
    environ: Optional[Mapping[str, str]] = None,
    deadline: Optional[float] = None,
) -> CodexRpcResult:
    """Query app-server without allowing failures or children to escape."""

    environment = os.environ if environ is None else environ
    if environment.get(RPC_ENABLED_ENV) == "0":
        return CodexRpcResult(
            (), False, ("app-server RPC disabled by HEADROOM_CODEX_RPC=0",)
        )

    notes: List[str] = []
    try:
        timeout = _timeout_seconds(environment, notes)
        command = _command(environment, notes)
    except Exception as error:
        return CodexRpcResult(
            (), False, ("invalid app-server RPC configuration: {}".format(error),)
        )
    if not command:
        return CodexRpcResult((), False, tuple(notes))

    process: Optional[subprocess.Popen] = None
    reader: Optional[threading.Thread] = None
    messages: "queue.Queue[Tuple[str, Optional[str]]]" = queue.Queue()
    snapshots: Tuple[Snapshot, ...] = ()
    rpc_deadline = time.monotonic() + timeout
    if deadline is not None:
        rpc_deadline = min(rpc_deadline, deadline)
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if process.stdout is None or process.stdin is None:
            raise _RpcFailure("app-server RPC pipes were unavailable")
        reader = threading.Thread(
            target=_read_lines,
            args=(process.stdout, messages),
            name="headroom-codex-rpc",
            daemon=True,
        )
        reader.start()

        _send(
            process.stdin,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "headroom", "version": __version__}
                },
            },
        )
        _wait_for_response(messages, 1, rpc_deadline, notes)
        _send(
            process.stdin,
            {"jsonrpc": "2.0", "method": "initialized", "params": {}},
        )
        _send(
            process.stdin,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "account/rateLimits/read",
                "params": None,
            },
        )
        response = _wait_for_response(messages, 2, rpc_deadline, notes)
        selected = _select_codex_limits(response.get("result"))
        if selected is None:
            notes.append("app-server RPC returned no Codex rate-limit bucket")
        else:
            snapshots = parse_rate_limits(selected, captured_at=time.time(), notes=notes)
            if not snapshots:
                notes.append("app-server RPC returned no usable rate limits")
    except _RpcTimeout:
        notes.append("app-server RPC timed out after {}s".format(_format_timeout(timeout)))
    except (OSError, ValueError, TypeError, _RpcFailure) as error:
        notes.append("app-server RPC failed: {}".format(error))
    except Exception as error:
        notes.append("app-server RPC failed unexpectedly: {}".format(error))
    finally:
        if process is not None:
            _stop_process(process, notes, deadline)
        if reader is not None:
            join_timeout = _remaining_timeout(deadline, 0.2)
            if join_timeout > 0.0:
                reader.join(timeout=join_timeout)

    return CodexRpcResult(snapshots, True, tuple(notes))


def _timeout_seconds(environment: Mapping[str, str], notes: List[str]) -> float:
    configured = environment.get(RPC_TIMEOUT_ENV)
    if configured is None:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        timeout = float(configured)
    except (TypeError, ValueError):
        timeout = -1.0
    if not math.isfinite(timeout) or timeout <= 0.0:
        notes.append(
            "invalid HEADROOM_CODEX_RPC_TIMEOUT; using {}s".format(
                _format_timeout(DEFAULT_TIMEOUT_SECONDS)
            )
        )
        return DEFAULT_TIMEOUT_SECONDS
    return timeout


def _command(
    environment: Mapping[str, str], notes: List[str]
) -> Optional[Sequence[str]]:
    configured = environment.get(RPC_COMMAND_ENV)
    if configured is None:
        return ("codex", "app-server")
    if not configured.strip():
        notes.append("HEADROOM_CODEX_RPC_CMD is empty")
        return None

    try:
        decoded = json.loads(configured)
    except ValueError:
        decoded = None
    if isinstance(decoded, list) and all(
        isinstance(part, str) and part for part in decoded
    ):
        return tuple(decoded)

    try:
        parts = shlex.split(configured, posix=os.name != "nt")
    except ValueError as error:
        notes.append("invalid HEADROOM_CODEX_RPC_CMD: {}".format(error))
        return None
    if os.name == "nt":
        parts = [_strip_windows_quotes(part) for part in parts]
    if not parts:
        notes.append("HEADROOM_CODEX_RPC_CMD is empty")
        return None
    return tuple(parts)


def _strip_windows_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _read_lines(
    stream: TextIO, messages: "queue.Queue[Tuple[str, Optional[str]]]"
) -> None:
    try:
        for line in stream:
            messages.put(("line", line))
    except (OSError, ValueError) as error:
        messages.put(("error", str(error)))
    finally:
        messages.put(("eof", None))


def _send(stream: TextIO, message: Dict[str, Any]) -> None:
    stream.write(json.dumps(message, separators=(",", ":")) + "\n")
    stream.flush()


def _wait_for_response(
    messages: "queue.Queue[Tuple[str, Optional[str]]]",
    request_id: int,
    deadline: float,
    notes: List[str],
) -> Dict[str, Any]:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            raise _RpcTimeout()
        try:
            kind, value = messages.get(timeout=remaining)
        except queue.Empty:
            raise _RpcTimeout()
        if kind == "eof":
            raise _RpcFailure(
                "app-server exited before response id {}".format(request_id)
            )
        if kind == "error":
            raise _RpcFailure("could not read app-server output: {}".format(value))
        try:
            message = json.loads(value or "")
        except ValueError:
            note = "ignored non-JSON app-server output"
            if note not in notes:
                notes.append(note)
            continue
        if not isinstance(message, dict) or message.get("id") != request_id:
            continue
        if message.get("error") is not None:
            raise _RpcFailure(
                "app-server response id {} contained an error: {}".format(
                    request_id, message["error"]
                )
            )
        return message


def _select_codex_limits(result: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(result, dict):
        return None
    by_limit_id = result.get("rateLimitsByLimitId")
    if isinstance(by_limit_id, dict):
        for bucket in by_limit_id.values():
            if isinstance(bucket, dict) and bucket.get("limitId") == "codex":
                return bucket
    top_level = result.get("rateLimits")
    return top_level if isinstance(top_level, dict) else None


def _stop_process(
    process: subprocess.Popen,
    notes: List[str],
    deadline: Optional[float] = None,
) -> None:
    try:
        if process.stdin is not None:
            process.stdin.close()
    except (OSError, ValueError):
        pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError as error:
            notes.append("could not stop app-server RPC process: {}".format(error))
    try:
        process.wait(timeout=_remaining_timeout(deadline, 1.0))
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            remaining = _remaining_timeout(deadline, 1.0)
            if remaining > 0.0:
                process.wait(timeout=remaining)
        except (OSError, subprocess.TimeoutExpired) as error:
            notes.append("could not reap app-server RPC process: {}".format(error))
    except OSError as error:
        notes.append("could not reap app-server RPC process: {}".format(error))
    try:
        if process.stdout is not None:
            process.stdout.close()
    except (OSError, ValueError):
        pass


def _format_timeout(value: float) -> str:
    return "{:g}".format(value)


def _remaining_timeout(deadline: Optional[float], maximum: float) -> float:
    if deadline is None:
        return maximum
    return max(0.0, min(maximum, deadline - time.monotonic()))
