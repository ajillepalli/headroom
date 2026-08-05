#!/usr/bin/env python3
"""Deterministic stdio peer for Codex app-server tests."""

import json
import os
from pathlib import Path
import sys
import time


def _write_marker(variable: str, value: str = "started") -> None:
    path = os.environ.get(variable)
    if path:
        Path(path).write_text(value, encoding="utf-8")


def _read() -> dict:
    line = sys.stdin.readline()
    if not line:
        raise SystemExit("unexpected end of input")
    log_path = os.environ.get("HEADROOM_TEST_RPC_LOG")
    if log_path:
        with Path(log_path).open("a", encoding="utf-8") as handle:
            handle.write(line)
    value = json.loads(line)
    if not isinstance(value, dict):
        raise SystemExit("request was not an object")
    return value


def _send(value: dict) -> None:
    print(json.dumps(value, separators=(",", ":")), flush=True)


def main() -> int:
    _write_marker("HEADROOM_TEST_RPC_STARTED")
    mode = os.environ.get("HEADROOM_TEST_RPC_MODE", "success")
    if mode == "timeout":
        time.sleep(1.5)
        _write_marker("HEADROOM_TEST_RPC_SURVIVED", "survived")
        return 0
    if mode == "garbage":
        print("this is not JSON", flush=True)
        return 0

    initialize = _read()
    client = initialize.get("params", {}).get("clientInfo", {})
    if (
        initialize.get("jsonrpc") != "2.0"
        or initialize.get("id") != 1
        or initialize.get("method") != "initialize"
        or client.get("name") != "headroom"
        or not isinstance(client.get("version"), str)
    ):
        raise SystemExit("invalid initialize request")

    _send(
        {
            "jsonrpc": "2.0",
            "method": "remoteControl/status/changed",
            "params": {"status": "connected"},
        }
    )
    _send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "userAgent": "stub",
                "codexHome": "stub",
                "platformOs": sys.platform,
            },
        }
    )

    initialized = _read()
    if initialized != {
        "jsonrpc": "2.0",
        "method": "initialized",
        "params": {},
    }:
        raise SystemExit("invalid initialized notification")

    request = _read()
    if request != {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "account/rateLimits/read",
        "params": None,
    }:
        raise SystemExit("invalid rate-limit request")

    _send(
        {
            "jsonrpc": "2.0",
            "method": "remoteControl/status/changed",
            "params": {"status": "ready"},
        }
    )
    _send(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "rateLimits": _bucket("codex", 9),
                "rateLimitsByLimitId": {
                    "other": _bucket("other", 77),
                    "codex_bengalfox": _bucket("codex", 4),
                },
            },
        }
    )
    return 0


def _bucket(limit_id: str, used_percent: int) -> dict:
    return {
        "limitId": limit_id,
        "limitName": None,
        "primary": {
            "usedPercent": used_percent,
            "windowDurationMins": 10_080,
            "resetsAt": 1_886_494_688,
        },
        "secondary": None,
        "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
        "individualLimit": None,
        "spendControlReached": False,
        "planType": "pro",
        "rateLimitReachedType": None,
    }


if __name__ == "__main__":
    raise SystemExit(main())
