# Troubleshoot readings and capture

[Back to the README](../README.md) | [How capture works](explanation-capture.md) | [CLI reference](reference-cli.md)

Start with:

```console
headroom doctor
```

It checks the configured state and Codex sources without updating stored snapshots.

## A reading shows unavailable

`unavailable` means no valid snapshot exists for that source and window.

For Claude, confirm that `headroom init` configured the statusline and that Claude Code has rendered it at least once. Claude Code 2.1.80 or newer is needed for `rate_limits` in statusline input. Malformed or unrecognized buckets are stored as diagnostics instead of crashing the statusline.

For Codex, use the `Codex source` and `Notes` lines from `doctor`. The app-server may be missing, disabled, timed out, or may have returned no usable Codex bucket. Rollout fallback may report a missing sessions directory or no usable rate limits. A null short bucket is valid, so the Codex 5h line may be unavailable while 7d is present.

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

## Clear stored state

To remove captured snapshots, diagnostics, and history, run:

```console
headroom reset
```

This deletes only `state.json` and `history.jsonl` from the configured state directory. The next statusline render or on-demand Codex command starts repopulating state.

For all environment defaults and exit statuses, see the [CLI reference](reference-cli.md). For source selection and accepted fields, see [How capture works](explanation-capture.md).
