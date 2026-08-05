# Troubleshoot readings and capture

[Back to the README](../README.md) | [How capture works](explanation-capture.md) | [CLI reference](reference-cli.md)

Start with:

```console
headroom doctor
```

It checks the configured state and Codex sources without updating stored snapshots.

## A hook silently produces nothing after a source change

Editing a clone does not update a command previously installed with `uv tool install .`. The installed command is a separate copy and can continue to exit successfully while knowing nothing about the new hook behavior. The typical symptom is a hook that silently produces nothing even though the command exists, exits 0, and reports the expected package version.

Reinstall the command from the repository root after changing the source:

```console
uv tool install . --force
```

Then compare `headroom doctor` with `headroom --version`. The `Install` section shows the imported package path, whether it is an installed copy or source checkout, the package version, and the most recent modification time among the loaded headroom modules. A source checkout also includes its short commit hash in `--version`; a wheel install does not.

## A reading shows unavailable

`unavailable` means no valid snapshot exists for that source and window.

For Claude, confirm that `headroom init` configured the statusline and that Claude Code has rendered it at least once. Claude Code 2.1.80 or newer is needed for `rate_limits` in statusline input. Malformed or unrecognized buckets are stored as diagnostics instead of crashing the statusline.

For Codex, use the `Codex source` and `Notes` lines from `doctor`. The app-server may be missing, disabled, timed out, or may have returned no usable Codex bucket. Rollout fallback may report a missing sessions directory or no usable rate limits. A null short bucket is valid, so the Codex 5h line may be unavailable while 7d is present.

## Claude readings look too good on multiple machines

Claude usage can look lower than account-wide usage when you run Claude Code on more than one machine. Each machine has separate local state, and Claude capture sees only the statusline payloads observed on that machine. Its reading can therefore undercount usage from the other machines.

Treat each machine's Claude reading as a lower bound, not an account-complete percentage. Prefer the reading from the machine where you do most of your work. Codex app-server readings do not have this limitation: `account/rateLimits/read` returns account-wide figures regardless of which machine asks. Run `headroom doctor` and check that `Codex source` is `app-server` before relying on that distinction.

## A reading has a greater-than-or-equal marker

`>=65% used` is a stale lower bound. Usage can only rise before the window resets, so the last percentage remains safe as a minimum. It is not presented as a current estimate.

Claude is fresh for 300 seconds by default. Codex is fresh for 1,800 seconds. Change those intervals only with finite, non-negative values:

```console
$env:HEADROOM_FRESH_CLAUDE_SECONDS = "600"
$env:HEADROOM_FRESH_CODEX_SECONDS = "3600"
headroom status
```

On POSIX shells, set the same environment variable names with that shell's syntax. See [Why the bounds are sound](explanation-bounds.md) before increasing an interval.

## A reading says reset time unknown

The percentage parsed, but `resets_at` was absent, invalid, or implausible for its window. headroom keeps the usage bound and refuses to render a misleading countdown. Run `headroom doctor` and inspect `Notes` for `invalid resets_at` or `rejected implausible resets_at`.

An accepted reset can be at most the reported window duration plus five minutes after capture, with five minutes of grace before capture. Without a valid duration, it can be at most 14 days ahead.

## Read doctor output

| Line | Interpretation |
| --- | --- |
| `Install / Path` | Resolved path of the imported `headroom` package. Use it to distinguish the command's files from the clone you edited. |
| `Install / Mode` | `installed` for a copied package or `source` when the imported package is the checkout's top-level `headroom` directory. |
| `Install / Version` | Package version reported by installed distribution metadata, with the source version as fallback. |
| `Install / Modified` | Latest modification time, in UTC, among headroom module files loaded by `doctor`. |
| `State directory` | Directory selected by `HEADROOM_STATE_DIR` or the `~/.headroom` default. |
| `State file` | Whether `state.json` exists. A corrupt or unreadable file still appears as found but is read as empty state. |
| `Claude readings` | Stored Claude windows, or `missing`. |
| `Codex source` | `app-server`, `rollout`, or `none` for this check. |
| `Codex sessions` | `not checked` when RPC won, otherwise whether any rollout files were checked. |
| `Codex rollout` | `not checked`, the selected rollout path, or `no usable snapshot`. |
| `Codex readings` | Windows parsed during this check, or `missing`. |
| `Notes` | Deduplicated stored diagnostics, rejected stored resets, RPC notes, and rollout notes. The line is omitted when there are no notes. |

## Disable the Codex RPC

Set the exact value `0` to skip `codex app-server` and read rollout files only:

```console
$env:HEADROOM_CODEX_RPC = "0"
headroom status
```

On a POSIX shell:

```console
HEADROOM_CODEX_RPC=0 headroom status
```

This is useful when the app-server command cannot start or when testing rollout capture. Freshness then depends on the newest usable rollout below the configured Codex home.

## Verify model context injection

Use `HEADROOM_FORCE_SEVERITY` to test the complete hook path without waiting for real usage to approach a limit. This is a temporary diagnostic, not a usage-control feature.

First, exit Claude Code. Set a forced severity in the shell that will launch it:

```console
$env:HEADROOM_FORCE_SEVERITY = "critical"
claude
```

On a POSIX shell:

```console
HEADROOM_FORCE_SEVERITY=critical claude
```

Submit a prompt asking what forced headroom test context accompanied the prompt. The model should report context beginning with `FORCED TEST (critical)` and state that it is not a real usage warning. This confirms that Claude Code received the hook's `additionalContext`, not just that the hook ran.

You can inspect the documented JSON envelope separately in PowerShell:

```console
'{"hook_event_name":"UserPromptSubmit"}' | headroom hook
```

For human-readable output instead, run `headroom hook --plain`. Then unset the diagnostic and restart Claude Code so the child process no longer inherits it:

```console
$env:HEADROOM_FORCE_SEVERITY = $null
```

On a POSIX shell:

```console
unset HEADROOM_FORCE_SEVERITY
```

Accepted diagnostic values are `notice`, `warn`, and `critical`. Any other value is ignored. `headroom status` is unaffected.

## Clear stored state

To remove captured snapshots, diagnostics, and history, run:

```console
headroom reset
```

This deletes only `state.json` and `history.jsonl` from the configured state directory. The next statusline render or on-demand Codex command starts repopulating state.

For all environment defaults and exit statuses, see the [CLI reference](reference-cli.md). For source selection and accepted fields, see [How capture works](explanation-capture.md).
