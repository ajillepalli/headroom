# Troubleshoot readings and capture

[Back to the README](../README.md) | [How capture works](explanation-capture.md) | [CLI reference](reference-cli.md)

Start with:

```console
quotagauge doctor
```

It checks the configured state and Codex sources without updating stored snapshots.

## A hook silently produces nothing after a source change

Editing a clone does not update a command previously installed with `uv tool install .`. The installed command is a separate copy and can continue to exit successfully while knowing nothing about the new hook behavior. The typical symptom is a hook that silently produces nothing even though the command exists, exits 0, and reports the expected package version.

Reinstall the command from the repository root after changing the source:

```console
uv tool install . --force
```

Then compare `quotagauge doctor` with `quotagauge --version`. The `Install` section shows the imported package path, whether it is an installed copy or source checkout, the package version, and the most recent modification time among the loaded quotagauge modules. A source checkout also includes its short commit hash in `--version`; a wheel install does not.

## A reading shows unavailable

`unavailable` means no valid snapshot exists for that source and window.

For Claude, confirm that `quotagauge init` configured the statusline and that Claude Code has rendered it at least once. Claude Code 2.1.80 or newer is needed for `rate_limits` in statusline input. Malformed or unrecognized buckets are stored as diagnostics instead of crashing the statusline.

For Codex, use the `Codex source` and `Notes` lines from `doctor`. The app-server may be missing, disabled, timed out, or may have returned no usable Codex bucket. Rollout fallback may report a missing sessions directory or no usable rate limits. A null short bucket is valid, so the Codex 5h line may be unavailable while 7d is present.

## Claude readings look too good on multiple machines

Claude usage can look lower than account-wide usage when you run Claude Code on more than one machine. Each machine has separate local state, and Claude capture sees only the statusline payloads observed on that machine. Its reading can therefore undercount usage from the other machines.

Treat each machine's Claude reading as a lower bound, not an account-complete percentage. Prefer the reading from the machine where you do most of your work. Codex app-server readings do not have this limitation: `account/rateLimits/read` returns account-wide figures regardless of which machine asks. Run `quotagauge doctor` and check that `Codex source` is `app-server` before relying on that distinction.

## A reading has a greater-than-or-equal marker

`>=65% used` is a stale lower bound. Usage can only rise before the window resets, so the last percentage remains safe as a minimum. It is not presented as a current estimate.

Claude is fresh for 300 seconds by default. Codex is fresh for 1,800 seconds. Change those intervals only with finite, non-negative values:

```console
$env:QUOTAGAUGE_FRESH_CLAUDE_SECONDS = "600"
$env:QUOTAGAUGE_FRESH_CODEX_SECONDS = "3600"
quotagauge status
```

On POSIX shells, set the same environment variable names with that shell's syntax. See [Why the bounds are sound](explanation-bounds.md) before increasing an interval.

## A reading says reset time unknown

The percentage parsed, but `resets_at` was absent, invalid, or implausible for its window. quotagauge keeps the usage bound and refuses to render a misleading countdown. Run `quotagauge doctor` and inspect `Notes` for `invalid resets_at` or `rejected implausible resets_at`.

An accepted reset can be at most the reported window duration plus five minutes after capture, with five minutes of grace before capture. Without a valid duration, it can be at most 14 days ahead.

## Context guidance never appears

Check `quotagauge doctor`'s dedicated `Claude context` line first; it names the cause directly. In likelihood order:

1. **Stale by cadence.** `Claude context: stale (last capture Xm Ys ago, exceeds 300s freshness)`. Claude's own statusline hasn't rendered recently enough -- the terminal may be idle, or minimized. Context is fresh-or-nothing (see [Why context is fresh-or-nothing](explanation-context.md)): unlike a rate-limit reading, a stale context reading is never shown as a bound, only as nothing. Interact with Claude Code again; the very next statusline render or `UserPromptSubmit` refreshes it.
2. **No `session_id` in the last statusline payload.** `Claude context: not available (no session_id in last statusline payload)`. Update Claude Code -- `session_id` alongside `context_window` requires a version that supplies both. `quotagauge doctor`'s generic `Notes` line will not show this cause on its own, because an absence is not a parse rejection; the dedicated context line exists specifically to surface it.
3. **Different session than the one asking.** The hook only ever reports the session named in its own `UserPromptSubmit` payload. If another terminal tab or a much older conversation has a fresh context reading, this session still reports nothing until its own statusline has rendered at least once. This is deliberate: a shared, non-session-keyed reading would let one conversation's context percentage answer for a different one's prompt.
4. **Genuinely `ok`.** `Claude context: ok (N% used, below notice threshold)`. Below 60% used, `hook` and `statusline` stay quiet by design; run `quotagauge status` or `quotagauge json` to see the number anyway.
5. **Claude Code version too old.** `context_window` and `session_id` are both recent additions to the statusline payload; an old client sends neither, so nothing is ever captured for context specifically, even though rate-limit readings work normally.

## Read doctor output

| Line | Interpretation |
| --- | --- |
| `Install / Path` | Resolved path of the imported `quotagauge` package. Use it to distinguish the command's files from the clone you edited. |
| `Install / Mode` | `installed` for a copied package or `source` when the imported package is the checkout's top-level `quotagauge` directory. |
| `Install / Version` | Package version reported by installed distribution metadata, with the source version as fallback. |
| `Install / Modified` | Latest modification time, in UTC, among quotagauge module files loaded by `doctor`. |
| `State directory` | Directory selected by `QUOTAGAUGE_STATE_DIR` or the `~/.quotagauge` default. |
| `State file` | Whether `state.json` exists. A corrupt or unreadable file still appears as found but is read as empty state. |
| `Claude readings` | Stored Claude windows, or `missing`. |
| `Claude context` | `not available`, `stale`, or the most recent session's usage and severity. See [Context guidance never appears](#context-guidance-never-appears). |
| `Codex source` | `app-server`, `rollout`, or `none` for this check. |
| `Codex sessions` | `not checked` when RPC won, otherwise whether any rollout files were checked. |
| `Codex rollout` | `not checked`, the selected rollout path, or `no usable snapshot`. |
| `Codex readings` | Windows parsed during this check, or `missing`. |
| `Notes` | Deduplicated stored diagnostics, rejected stored resets, RPC notes, and rollout notes. The line is omitted when there are no notes. |

## Migrating from the earlier `headroom` name

On a machine that ran this project under its earlier name, the first command that resolves the default state directory renames a pre-existing `~/.headroom` to `~/.quotagauge` automatically, once, and uses the new location from then on. If that rename cannot complete for any reason, the old directory is left exactly as it was and used in place, so `State directory` may still point at `~/.headroom` on such a machine -- that is expected, not a bug, and no history is lost either way.

This automatic migration has three limits worth knowing about, all of them safe (nothing is ever deleted or overwritten) but worth knowing so a missing reading does not look like data loss:

- **It only looks at the default `~/.headroom` path.** If the earlier install used a custom `HEADROOM_STATE_DIR`, that data is not found automatically -- set `QUOTAGAUGE_STATE_DIR` to the same path yourself. Environment variables are trivially re-set, unlike accumulated history, which is why only the directory itself is migrated automatically.
- **An already-present `~/.quotagauge` always wins, even over an untouched `~/.headroom` beside it.** This covers the common "already migrated" case, but also means a stray, unrelated `~/.quotagauge` (however it got there) would shadow real legacy history without erasing it. The same can happen, very narrowly, from running several `quotagauge` commands at nearly the same moment right after upgrading: if one loses a rare race and falls back to writing under `~/.headroom` while another succeeds and moves on to `~/.quotagauge`, that write is not lost, just left sitting under the old path once everything else has moved on. If `doctor` shows an empty or unexpectedly small state right after upgrading, check whether `~/.headroom` still has real data sitting next to a near-empty `~/.quotagauge`, and merge or remove the stray directory by hand.
- **`init` does not remove an old `headroom` hook registration.** If both `headroom` and `quotagauge` commands are installed and configured, both hook entries run on every prompt. Uninstall the old `headroom-cli` package (or manually remove its entry from `settings.json`/`hooks.json`) once you have switched to `quotagauge`.

## Disable the Codex RPC

Set the exact value `0` to skip `codex app-server` and read rollout files only:

```console
$env:QUOTAGAUGE_CODEX_RPC = "0"
quotagauge status
```

On a POSIX shell:

```console
QUOTAGAUGE_CODEX_RPC=0 quotagauge status
```

This is useful when the app-server command cannot start or when testing rollout capture. Freshness then depends on the newest usable rollout below the configured Codex home.

## Verify model context injection

Use `QUOTAGAUGE_FORCE_SEVERITY` to test the complete hook path without waiting for real usage to approach a limit. This is a temporary diagnostic, not a usage-control feature.

First, exit Claude Code. Set a forced severity in the shell that will launch it:

```console
$env:QUOTAGAUGE_FORCE_SEVERITY = "critical"
claude
```

On a POSIX shell:

```console
QUOTAGAUGE_FORCE_SEVERITY=critical claude
```

Submit a prompt asking what forced quotagauge test context accompanied the prompt. The model should report context beginning with `FORCED TEST (critical)` and state that it is not a real usage warning. This confirms that Claude Code received the hook's `additionalContext`, not just that the hook ran.

You can inspect the documented JSON envelope separately in PowerShell:

```console
'{"hook_event_name":"UserPromptSubmit"}' | quotagauge hook
```

For human-readable output instead, run `quotagauge hook --plain`. Then unset the diagnostic and restart Claude Code so the child process no longer inherits it:

```console
$env:QUOTAGAUGE_FORCE_SEVERITY = $null
```

On a POSIX shell:

```console
unset QUOTAGAUGE_FORCE_SEVERITY
```

Accepted diagnostic values are `notice`, `warn`, and `critical`. Any other value is ignored. `quotagauge status` is unaffected.

## Clear stored state

To remove captured snapshots, diagnostics, and history, run:

```console
quotagauge reset
```

This deletes only `state.json` and `history.jsonl` from the configured state directory. The next statusline render or on-demand Codex command starts repopulating state.

For all environment defaults and exit statuses, see the [CLI reference](reference-cli.md). For source selection and accepted fields, see [How capture works](explanation-capture.md).
