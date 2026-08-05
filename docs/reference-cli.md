# CLI reference

[Back to the README](../README.md) | [How capture works](explanation-capture.md) | [Troubleshooting](howto-troubleshoot.md)

headroom requires Python 3.9 or newer and uses only the Python standard library. It makes no direct network calls. The installed entry point and `python -m headroom.cli` expose the same interface.

## Command form

```console
headroom COMMAND [OPTIONS]
```

`headroom -h` and `headroom --help` print top-level help. Every subcommand also accepts `-h` and `--help`. Help exits with status 0. A missing command, an unknown command, or an unrecognized flag is rejected by `argparse` and exits with status 2.

`headroom --version` prints the installed package version and exits with status 0 without requiring a command. When distribution metadata is unavailable, such as when running directly from a source checkout, it prints the package's source version.

## Commands

| Command | Input | Output and side effects |
| --- | --- | --- |
| `headroom statusline` | One Claude Code JSON document on standard input | Parses Claude readings, stores snapshots and diagnostics, and prints one compact line. It never starts the Codex app-server. Malformed input still produces a fallback line and exits 0. |
| `headroom status` | None | Refreshes Codex through the app-server with rollout fallback, updates state, and prints Claude and Codex short and weekly readings. Missing readings are `unavailable`. |
| `headroom json` | None | Performs the same Codex refresh as `status`, then prints one compact JSON document containing persisted state, diagnostics, and four bounded readings. |
| `headroom hook` | None | Performs the same Codex refresh, then prints guidance for the highest actionable severity. It prints nothing when all readings are `ok`. |
| `headroom doctor` | None | Reads current Codex sources without updating state, then reports paths, stored Claude windows, the winning Codex source, rollout discovery, parsed Codex windows, and diagnostic notes. |
| `headroom reset` | None | Removes `state.json` and `history.jsonl`. It reports whether anything was removed and leaves other files in the state directory untouched. |
| `headroom init` | None | Merges the Claude Code statusline and prompt hook into a settings file. See [Installation](howto-install.md). |

`statusline`, `status`, `json`, `hook`, `doctor`, and `reset` have no command-specific flags other than help.

## Init flags

| Flag | Default | Effect |
| --- | --- | --- |
| `--settings PATH` | `~/.claude/settings.json` | Selects the Claude Code settings file. |
| `--dry-run` | Off | Prints a unified diff and writes no settings or backup files. If the merged result is unchanged, it prints `headroom init: no changes`. |
| `--print` | Off | Prints the generated JSON fragment only. It does not read or write the selected settings path. |
| `-h`, `--help` | Off | Prints init help and exits 0. |

When both `--print` and `--dry-run` are present, `--print` takes effect first and emits only the fragment.

## Environment variables

| Variable | Default | Accepted value and behavior |
| --- | --- | --- |
| `HEADROOM_STATE_DIR` | `~/.headroom` | Directory containing `state.json` and `history.jsonl`. A nonempty value is expanded as a user path. |
| `HEADROOM_CODEX_HOME` | `~/.codex` | Codex home directory. Rollout fallback reads its `sessions` child directory. |
| `HEADROOM_CODEX_RPC` | Enabled | The exact value `0` skips the app-server RPC. Every other value, including an unset value, enables it. |
| `HEADROOM_CODEX_RPC_TIMEOUT` | `6` | Shared timeout in seconds for startup, initialization, and the rate-limit response. It must be finite and greater than zero. An invalid value falls back to 6 seconds and adds a diagnostic note. |
| `HEADROOM_CODEX_RPC_CMD` | `codex app-server` | App-server command. It may be a JSON array of nonempty strings or a shell-style command string. An empty or unparseable value skips the command and records a note. |
| `HEADROOM_FRESH_CLAUDE_SECONDS` | `300` | Seconds for which a Claude snapshot is exact. The value must be finite and non-negative. |
| `HEADROOM_FRESH_CODEX_SECONDS` | `1800` | Seconds for which a Codex snapshot is exact. The value must be finite and non-negative. |

An invalid freshness value causes `status`, `json`, or `hook` to exit 1. `statusline` keeps its terminal contract, prints `headroom: usage unavailable`, and exits 0.

## State and JSON fields

`state.json` is replaced atomically through a temporary file in the same directory. A failed or partial read produces an empty in-memory state. The document has `version` set to 1, a `sources` object keyed by source and window, and any stored `diagnostics`. Every captured snapshot is also appended as one compact object to `history.jsonl`, with the file flushed and synced after writing.

Each stored snapshot contains:

| Field | Meaning |
| --- | --- |
| `used_percentage` | Captured non-negative usage percentage. |
| `captured_at` | Unix timestamp for the capture. |
| `resets_at` | Validated absolute Unix reset timestamp, or `null`. |
| `window` | `short` or `weekly`. |
| `source` | `claude` or `codex`. |
| `limit_reached` | Whether the payload reported a reached limit, or usage was at least 100%. |
| `raw` | Original bucket fields used to create the snapshot. |

The `json` command adds four reading objects. Each has `certain`, `lower_bound_percent`, `resets_at`, `age_seconds`, `window`, `source`, `confidence`, and `limit_reached`. Confidence is one of `fresh`, `stale_bounded`, `post_reset`, or `unknown`. See [Why the bounds are sound](explanation-bounds.md).

Claude diagnostics contain an `unparsed` list of parser notes. Codex diagnostics contain `source`, `rpc_attempted`, `rpc_notes`, `file`, `files_checked`, and the combined `notes` list. A Codex source is `app-server`, `rollout`, or `none`.

## Exit codes

| Status | When it is used |
| --- | --- |
| `0` | A command completed, help or version information was printed, `hook` had nothing to say, or `statusline` recovered from malformed input or an internal failure. |
| `1` | `init` could not read, merge, back up, or write settings; `reset` could not remove state; or another on-demand command raised a handled I/O, value, or type error. |
| `2` | Command-line parsing failed because the command or arguments were missing or invalid. |

The compatibility script `python install.py` forwards its arguments to `headroom init` and uses the same exit codes.

For source-specific failure messages, continue with [Troubleshooting](howto-troubleshoot.md). For install behavior and project links, see [Installation](howto-install.md).
