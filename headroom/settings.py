"""Install headroom hooks without discarding user configuration."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime
import difflib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


def default_settings_path() -> Path:
    """Return the default Claude Code settings path."""
    return Path.home() / ".claude" / "settings.json"


def codex_home_path(override: Optional[Path] = None) -> Path:
    """Return the selected Codex home without touching it."""
    if override is not None:
        return override.expanduser()
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def codex_hooks_path(codex_home: Optional[Path] = None) -> Path:
    """Return the Codex hooks file selected by an override or CODEX_HOME."""
    return codex_home_path(codex_home) / "hooks.json"


def shell_command(command_parts: Sequence[str]) -> str:
    """Return a command line quoted for the current platform's shell."""
    return subprocess.list2cmdline(command_parts) if os.name == "nt" else shlex.join(command_parts)


def headroom_command(subcommand: str) -> str:
    """Return an installed command, with a checkout-safe Python fallback."""
    if shutil.which("headroom") is not None:
        return "headroom {}".format(subcommand)

    repository = str(Path(__file__).resolve().parent.parent)
    command = shell_command([sys.executable, "-m", "headroom.cli", subcommand])
    if os.name == "nt":
        return 'set "PYTHONPATH={}" && {}'.format(repository, command)
    return "PYTHONPATH={} {}".format(shlex.quote(repository), command)


def settings_snippet() -> Dict[str, Any]:
    """Return the settings fragment for the current installation mode."""
    return {
        "statusLine": {
            "type": "command",
            "command": headroom_command("statusline"),
            "refreshInterval": 300,
        },
        "hooks": {
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": headroom_command("hook"),
                        }
                    ]
                }
            ]
        },
    }


def codex_hooks_snippet() -> Dict[str, Any]:
    """Return the hook document accepted by Codex."""
    return {
        "description": "headroom",
        "hooks": {
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": headroom_command("hook"),
                            "timeoutSec": 10,
                        }
                    ]
                }
            ]
        },
    }


def merge_settings(existing: Dict[str, Any], snippet: Dict[str, Any]) -> Dict[str, Any]:
    """Merge headroom settings while preserving unrelated configuration."""
    merged = copy.deepcopy(existing)
    hooks = merged.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("the existing 'hooks' setting is not a JSON object")

    prompt_hooks = hooks.setdefault("UserPromptSubmit", [])
    if not isinstance(prompt_hooks, list):
        raise ValueError("the existing 'hooks.UserPromptSubmit' setting is not a JSON array")

    headroom_hook = snippet["hooks"]["UserPromptSubmit"][0]
    if not _contains_headroom_hook(prompt_hooks):
        prompt_hooks.append(copy.deepcopy(headroom_hook))
    merged["statusLine"] = copy.deepcopy(snippet["statusLine"])
    return merged


def merge_codex_hooks(existing: Dict[str, Any], snippet: Dict[str, Any]) -> Dict[str, Any]:
    """Merge the Codex hook while preserving every other entry and event."""
    merged = copy.deepcopy(existing)
    merged.setdefault("description", snippet["description"])
    hooks = merged.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("the existing 'hooks' setting is not a JSON object")

    prompt_hooks = hooks.setdefault("UserPromptSubmit", [])
    if not isinstance(prompt_hooks, list):
        raise ValueError("the existing 'hooks.UserPromptSubmit' setting is not a JSON array")

    headroom_hook = snippet["hooks"]["UserPromptSubmit"][0]
    if not _contains_headroom_hook(prompt_hooks):
        prompt_hooks.append(copy.deepcopy(headroom_hook))
    return merged


@dataclass(frozen=True)
class _InstallTarget:
    path: Path
    snippet: Dict[str, Any]
    merger: Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]]


@dataclass(frozen=True)
class _PreparedInstall:
    target: _InstallTarget
    existed: bool
    original_bytes: bytes
    original_text: str
    rendered: str
    changed: bool
    customized_hook: bool


def run_init(
    settings_path: Optional[Path] = None,
    *,
    codex: bool = False,
    all_targets: bool = False,
    codex_home: Optional[Path] = None,
    dry_run: bool = False,
    print_only: bool = False,
) -> int:
    """Print or install the selected Claude Code and Codex fragments."""
    targets: List[_InstallTarget] = []
    if not codex or all_targets:
        targets.append(
            _InstallTarget(
                settings_path if settings_path is not None else default_settings_path(),
                settings_snippet(),
                merge_settings,
            )
        )
    if codex or all_targets:
        targets.append(
            _InstallTarget(
                codex_hooks_path(codex_home),
                codex_hooks_snippet(),
                merge_codex_hooks,
            )
        )

    if len(targets) > 1 and _same_file(targets[0].path, targets[1].path):
        print(
            "headroom init: refusing to configure the same file as both Claude and Codex: {}".format(
                targets[0].path
            ),
            file=sys.stderr,
        )
        return 1

    if print_only:
        document = (
            targets[0].snippet
            if len(targets) == 1
            else {"claude": targets[0].snippet, "codex": targets[1].snippet}
        )
        print(json.dumps(document, indent=2))
        return 0

    prepared: List[_PreparedInstall] = []
    for target in targets:
        try:
            prepared.append(_prepare_install(target))
        except ValueError as error:
            print("headroom init: refusing to change {}: {}".format(target.path, error), file=sys.stderr)
            return 1

    if dry_run:
        for item in prepared:
            if item.changed:
                diff = difflib.unified_diff(
                    item.original_text.splitlines(keepends=True),
                    item.rendered.splitlines(keepends=True),
                    fromfile=str(item.target.path),
                    tofile=str(item.target.path),
                )
                sys.stdout.writelines(diff)
        if not any(item.changed for item in prepared):
            print("headroom init: no changes")
        return 0

    changed = [item for item in prepared if item.changed]
    if not changed:
        for item in prepared:
            if item.customized_hook:
                print(
                    "headroom init: {} already has a customized headroom hook; leaving it unchanged".format(
                        item.target.path
                    )
                )
            else:
                print("headroom init: {} is already configured".format(item.target.path))
        return 0

    applied: List[_PreparedInstall] = []
    try:
        for item in changed:
            item.target.path.parent.mkdir(parents=True, exist_ok=True)
            _write_prepared(item)
            applied.append(item)
    except OSError as error:
        rollback_errors = _rollback_installs(applied)
        if rollback_errors:
            print(
                "headroom init: update failed after another target was applied; "
                "rollback was incomplete: {}; original error: {}".format(
                    "; ".join(rollback_errors), error
                ),
                file=sys.stderr,
            )
        elif applied:
            print(
                "headroom init: update failed; restored the previously updated target(s): {}".format(error),
                file=sys.stderr,
            )
        else:
            print("headroom init: update failed: {}".format(error), file=sys.stderr)
        return 1

    for item in changed:
        print("Updated: {}".format(item.target.path))
    for item in prepared:
        if item.customized_hook:
            print(
                "headroom init: {} already has a customized headroom hook; leaving it unchanged".format(
                    item.target.path
                )
            )
        elif not item.changed:
            print("headroom init: {} is already configured".format(item.target.path))
    return 0


def _prepare_install(target: _InstallTarget) -> _PreparedInstall:
    path = target.path
    existed = path.exists()
    original_bytes = b""
    original_text = ""
    existing: Dict[str, Any] = {}
    if existed:
        try:
            original_bytes = path.read_bytes()
            original_text = original_bytes.decode("utf-8")
            parsed = json.loads(original_text)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("invalid JSON ({})".format(error))
        if not isinstance(parsed, dict):
            raise ValueError("the top level must be a JSON object")
        existing = parsed

    merged = target.merger(existing, target.snippet)
    rendered = json.dumps(merged, indent=2) + "\n"
    changed = merged != existing
    prompt_hooks = _prompt_hooks(existing)
    expected = target.snippet["hooks"]["UserPromptSubmit"][0]
    customized_hook = _contains_headroom_hook(prompt_hooks) and expected not in prompt_hooks
    return _PreparedInstall(
        target,
        existed,
        original_bytes,
        original_text,
        rendered,
        changed,
        customized_hook,
    )


def _write_prepared(item: _PreparedInstall) -> None:
    """Commit a preflighted update only if its target is still unchanged."""

    path = item.target.path
    if not item.existed:
        try:
            _exclusive_write(path, item.rendered)
        except FileExistsError as error:
            raise OSError(
                "{} appeared after preflight; refusing to overwrite it".format(path)
            ) from error
        return

    _assert_preflight_content(path, item.original_bytes)
    backup = _backup_path(path)
    _exclusive_write(backup, item.original_text)
    print("Backup: {}".format(backup))
    _assert_preflight_content(path, item.original_bytes)
    _atomic_write(path, item.rendered)


def _assert_preflight_content(path: Path, expected: bytes) -> None:
    try:
        current = path.read_bytes()
    except FileNotFoundError as error:
        raise OSError("{} changed after preflight (file was removed)".format(path)) from error
    if current != expected:
        raise OSError("{} changed after preflight; refusing to overwrite it".format(path))


def _rollback_installs(applied: Sequence[_PreparedInstall]) -> List[str]:
    errors: List[str] = []
    for item in reversed(applied):
        try:
            if item.existed:
                _atomic_write(item.target.path, item.original_text)
            else:
                item.target.path.unlink()
        except OSError as error:
            errors.append("{}: {}".format(item.target.path, error))
    return errors


def codex_hook_registration(codex_home: Optional[Path] = None) -> Tuple[Path, str]:
    """Return the selected hooks path and its headroom registration status."""
    path = codex_hooks_path(codex_home)
    if not path.exists():
        return path, "not registered (file missing)"
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return path, "not registered (invalid JSON)"
    except (OSError, UnicodeError) as error:
        return path, "not registered (unreadable: {})".format(error)
    if not isinstance(parsed, dict):
        return path, "not registered (invalid top level)"
    hooks = parsed.get("hooks")
    prompt_hooks = hooks.get("UserPromptSubmit") if isinstance(hooks, dict) else None
    return (
        (path, "registered")
        if isinstance(prompt_hooks, list) and _contains_headroom_hook(prompt_hooks)
        else (path, "not registered")
    )


def codex_hook_command_availability(codex_home: Optional[Path] = None) -> str:
    """Return whether the registered Codex hook command can be launched."""

    path = codex_hooks_path(codex_home)
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "not checked (hook not registered)"
    prompt_hooks = _prompt_hooks(parsed if isinstance(parsed, dict) else {})
    commands = list(_headroom_hook_commands(prompt_hooks))
    if not commands:
        return "not checked (hook not registered)"
    return "available" if any(_command_is_available(command) for command in commands) else "unavailable"


def _backup_path(path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S.%f")
    return path.with_name("{}.{}.bak".format(path.name, timestamp))


def _same_file(first: Path, second: Path) -> bool:
    """Compare paths after resolving aliases, including existing same-file links."""

    first_resolved = first.expanduser().resolve(strict=False)
    second_resolved = second.expanduser().resolve(strict=False)
    if os.path.normcase(str(first_resolved)) == os.path.normcase(str(second_resolved)):
        return True
    try:
        return os.path.samefile(str(first), str(second))
    except (FileNotFoundError, OSError):
        return False


def _prompt_hooks(document: Dict[str, Any]) -> List[Any]:
    hooks = document.get("hooks")
    prompt_hooks = hooks.get("UserPromptSubmit") if isinstance(hooks, dict) else None
    return prompt_hooks if isinstance(prompt_hooks, list) else []


def _contains_headroom_hook(prompt_hooks: Sequence[Any]) -> bool:
    return any(_headroom_hook_commands(prompt_hooks))


def _headroom_hook_commands(prompt_hooks: Sequence[Any]):
    for wrapper in prompt_hooks:
        if not isinstance(wrapper, dict):
            continue
        registrations = wrapper.get("hooks")
        if not isinstance(registrations, list):
            registrations = [wrapper]
        for registration in registrations:
            if not isinstance(registration, dict) or registration.get("type") != "command":
                continue
            command = registration.get("command")
            if isinstance(command, str) and _is_headroom_hook_command(command):
                yield command


def _command_segments(command: str) -> List[List[str]]:
    segments: List[List[str]] = []
    for raw_segment in re.split(r"\s*(?:&&|\|\||;)\s*", command.strip()):
        if not raw_segment:
            continue
        try:
            tokens = shlex.split(raw_segment, posix=os.name != "nt")
        except ValueError:
            tokens = raw_segment.split()
        cleaned = [token.strip("\"'") for token in tokens]
        while cleaned and "=" in cleaned[0] and not cleaned[0].startswith(("-", "/")):
            cleaned.pop(0)
        if cleaned and cleaned[0].casefold() == "env":
            cleaned.pop(0)
            while cleaned and "=" in cleaned[0]:
                cleaned.pop(0)
        if cleaned:
            segments.append(cleaned)
    return segments


def _is_headroom_hook_command(command: str) -> bool:
    for tokens in _command_segments(command):
        executable = tokens[0].replace("\\", "/").rsplit("/", 1)[-1].casefold()
        if executable in ("headroom", "headroom.exe") and len(tokens) > 1:
            if tokens[1].casefold() == "hook":
                return True
        lowered = [token.casefold() for token in tokens]
        for index in range(len(lowered) - 2):
            if lowered[index : index + 3] == ["-m", "headroom.cli", "hook"]:
                return True
    return False


def _command_is_available(command: str) -> bool:
    for tokens in _command_segments(command):
        lowered = [token.casefold() for token in tokens]
        is_hook = False
        if len(tokens) > 1:
            name = tokens[0].replace("\\", "/").rsplit("/", 1)[-1].casefold()
            is_hook = name in ("headroom", "headroom.exe") and lowered[1] == "hook"
        is_hook = is_hook or any(
            lowered[index : index + 3] == ["-m", "headroom.cli", "hook"]
            for index in range(len(lowered) - 2)
        )
        if not is_hook:
            continue
        executable = tokens[0]
        if Path(executable).is_absolute():
            return Path(executable).is_file()
        return shutil.which(executable) is not None
    return False


def _exclusive_write(path: Path, text: str) -> None:
    """Create a complete new file without replacing one that already exists."""

    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def _atomic_write(path: Path, text: str) -> None:
    temporary_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=str(path.parent),
            prefix="{}.".format(path.name),
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(text)
            temporary_name = temporary.name
        os.replace(temporary_name, str(path))
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
