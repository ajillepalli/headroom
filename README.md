<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="images/headroom-logo-dark.svg">
    <img src="images/headroom-logo.svg" alt="headroom" width="420">
  </picture><br>
  <strong>headroom</strong><br>
  <a href="#why-headroom">Why headroom</a> &middot;
  <a href="#install">Install</a> &middot;
  <a href="#commands">Commands</a> &middot;
  <a href="#the-bounds-model">Bounds</a> &middot;
  <a href="#limits">Limits</a><br><br>
  <img alt="MIT license" src="https://img.shields.io/badge/license-MIT-blue.svg">
  <img alt="Zero dependencies" src="https://img.shields.io/badge/dependencies-zero-success.svg">
  <img alt="Python 3.9 or newer" src="https://img.shields.io/badge/python-3.9%2B-blue.svg">
</p>

## Why headroom

Claude Code and Codex each enforce rolling usage limits. Each tool shows its numbers to a human, but the model never sees them. The model can keep fanning out subagents right up to the wall.

headroom closes that gap. It captures both tools' local rate-limit readings, stores them together, and gives the model a short warning when usage calls for a change in behavior. The prompt hook stays silent when usage is fine.

headroom uses only the Python standard library. It makes no direct network calls.

## How it works

| Source | Capture path |
| --- | --- |
| Claude Code | A configured statusline command receives JSON on standard input. Claude Code 2.1.80 and newer include a `rate_limits` value in that input. headroom searches the payload recursively because the nesting is not fixed. |
| Codex | On demand, headroom starts `codex app-server` and reads current rate limits over its local stdio RPC. If the RPC is disabled or yields no usable snapshot, headroom reads rollout files below `~/.codex/sessions/YYYY/MM/DD/`. |

Claude capture runs when Claude Code renders its statusline. Codex RPC capture runs when `status`, `json`, `hook`, or `doctor` is requested. The RPC call costs no Codex quota and creates no session rollout files. `statusline` never starts the app-server. It stays fast by rendering stored Codex state only.

The rollout fallback orders files newest first, stops at the first file with a usable snapshot, and takes the last usable `rate_limits` value in that file. Both capture sources accept the field variants present in current payloads, including snake case and camel case names.

Snapshots go to `state.json` under the state directory. Writes replace that file atomically. Each captured snapshot is also appended to `history.jsonl`.

## Install

Use Python 3.9 or newer. From the repository root, install the command with `uv` and configure Claude Code:

```console
uv tool install .
headroom init
```

Or use an editable `pip` install:

```console
pip install -e .
headroom init
```

`headroom init` merges the statusline and prompt hook into `~/.claude/settings.json`. It preserves unrelated settings and hook events, appends to an existing `UserPromptSubmit` array, and creates a timestamped backup before changing an existing file. Use `--settings PATH` to target another settings file, `--dry-run` to inspect the diff without writing, or `--print` to emit the JSON fragment only.

When `headroom` is on `PATH`, the generated commands are exactly `headroom statusline` and `headroom hook`. For a plain clone that has not been installed, the compatibility shim still works:

```console
python install.py
```

In that fallback mode, the generated settings use the current Python executable, an absolute checkout path in `PYTHONPATH`, and `python -m headroom.cli` internally so Claude Code can invoke headroom from any directory.

## Commands

| Subcommand | What it prints |
| --- | --- |
| `statusline` | Reads a Claude statusline JSON document from standard input, stores any snapshots it can parse, and prints one compact line. It prints a fallback line and exits successfully even for malformed input. |
| `status` | Refreshes Codex state through the app-server RPC with rollout fallback, then prints a multi-line report for Claude and Codex across the short and weekly windows. Missing readings appear as `unavailable`. |
| `json` | Refreshes Codex state through the app-server RPC with rollout fallback, then prints one compact JSON document with persisted state, diagnostics, and four bounded readings. |
| `hook` | Refreshes Codex state through the app-server RPC with rollout fallback, then prints guidance for the highest actionable severity. It prints nothing when every reading is `ok`. |
| `doctor` | Performs the same on-demand Codex read without updating state. It prints which source won, the state paths, parsed windows, rollout details when fallback was needed, and diagnostic notes. |
| `init` | Merges the Claude Code statusline and prompt hook into its settings, with backup, dry-run, and print-only modes. |

Run a subcommand with this form:

```console
headroom status
```

### Observed output

On August 4, 2026, the requested commands used a writable isolated state directory and the real local Codex sessions. They returned the following point-in-time output.

```console
headroom status
```

Output:

```text
Claude
  5h: unavailable
  7d: unavailable
Codex
  5h: unavailable
  7d: 4% used [ok], resets in 6d 19h
```

```console
headroom doctor
```

Output:

```text
State directory: C:\Users\ANANTH~1.JIL\AppData\Local\Temp\headroom-readme-state
State file: found
Claude readings: missing
Codex source: app-server
Codex sessions: not checked
Codex rollout: not checked
Codex readings: weekly
```

## The bounds model

The useful result is not a guess at current usage. It is a sound bound derived from a timestamped snapshot.

Usage is monotonic inside one limit window. It can rise, but it cannot fall until the reset. A stale reading therefore gives a lower bound on current usage. If the last reading was 65%, headroom reports `>=65% used`. It never presents that stale value as exact, and it never overestimates usage.

The reset time is absolute. Once `now` passes `resets_at`, the old window is over. headroom treats that snapshot as `post_reset`, sets its lower bound to 0%, and marks it certain and `ok`. It does not carry uncertainty from the previous window into the new one.

This makes staleness mostly harmless. Before the reset, the last value remains a valid lower bound and severity leans toward caution. After the reset, the old reading becomes known-good instead of uncertain. Only a stale reading that has not reached its reset needs a hedge.

The four confidence values are:

| Confidence | Meaning |
| --- | --- |
| `fresh` | The snapshot age is within the source freshness interval. Its percentage is shown as exact. |
| `stale_bounded` | The snapshot is older than that interval and its reset is still ahead. Its percentage is shown with `>=`. |
| `post_reset` | The recorded reset has passed. The previous window is treated as reset and `ok`. |
| `unknown` | No valid snapshot exists for that source and window. |

## Severity

Headroom means `100 - used percentage`.

| Severity | Base condition |
| --- | --- |
| `ok` | Headroom is greater than 40%. |
| `notice` | Headroom is from 20% through 40%. |
| `warn` | Headroom is from 10% up to, but not including, 20%. |
| `critical` | Headroom is below 10%, or the source says the limit was reached. |

For a `stale_bounded` reading with less than 50% headroom, headroom raises the base severity by one level and caps it at `critical`. The code names this boundary `ESCALATE_BELOW_HEADROOM = 50.0`. A reading at exactly 50% headroom does not escalate. A `post_reset` reading is always `ok`.

## Configuration

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `HEADROOM_STATE_DIR` | `~/.headroom` | Directory for `state.json` and `history.jsonl`. |
| `HEADROOM_CODEX_HOME` | `~/.codex` | Codex home directory. Sessions are read from its `sessions` child directory. |
| `HEADROOM_CODEX_RPC` | enabled | Set to `0` to skip the app-server RPC and use rollout files. |
| `HEADROOM_CODEX_RPC_TIMEOUT` | `6` | Hard timeout in seconds for app-server startup, initialization, and the rate-limit read. |
| `HEADROOM_CODEX_RPC_CMD` | `codex app-server` | Command used to start the RPC server. This is primarily useful for testing or nonstandard Codex installations. |
| `HEADROOM_FRESH_CLAUDE_SECONDS` | `300` | Number of seconds a Claude snapshot remains fresh. |
| `HEADROOM_FRESH_CODEX_SECONDS` | `1800` | Number of seconds a Codex snapshot remains fresh. |

Freshness values must be finite, non-negative numbers. The RPC timeout must be a finite number greater than zero.

## Limits

- Neither vendor publishes a token allowance through these sources. headroom reports percent used and reset time. It never reports tokens remaining.
- Claude readings are only as fresh as the last statusline render.
- On-demand Codex refresh needs a working `codex app-server` command. Without it, freshness depends on the newest usable rollout file.
- The Codex short window is often `null`, so it may remain unavailable while the weekly window is present.

## Tests

Run the standard-library test suite from the repository root:

```console
python -m unittest discover -s tests -t .
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for change guidelines, [SECURITY.md](SECURITY.md) for private vulnerability reports, and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community expectations.

## License

headroom is available under the [MIT License](LICENSE.txt).
