# CLI reference

[Back to the README](../README.md) | [How capture works](explanation-capture.md) | [Troubleshooting](howto-troubleshoot.md)

headroom requires Python 3.9 or newer and uses only the Python standard library. By default, it makes no outbound network calls. The installed entry point and `python -m headroom.cli` expose the same interface.

## Command form

```console
headroom COMMAND [OPTIONS]
```

`headroom -h` and `headroom --help` print top-level help. Every subcommand also accepts `-h` and `--help`. Help exits with status 0. A missing command, an unknown command, or an unrecognized flag is rejected by `argparse` and exits with status 2.

`headroom --version` prints the package version and exits with status 0 without requiring a command. When running from the package directory in a Git checkout, it appends the checkout's short commit hash. It falls back to the plain version when Git metadata is absent or unreadable, or when running from an installed wheel.

## Commands

| Command | Input | Output and side effects |
| --- | --- | --- |
| `headroom statusline` | One Claude Code JSON document on standard input | Parses Claude readings, stores snapshots and diagnostics, and prints one compact line. It never starts the Codex app-server or checks for updates. Malformed input still produces a fallback line and exits 0. |
| `headroom status` | None | Refreshes Codex through the app-server with rollout fallback, updates state, and prints Claude and Codex short and weekly readings. Missing readings are `unavailable`. When update checking is unset, it adds one local-only discovery line. When opted in, it reports an available update but stays quiet on current or failed checks. When a burn-rate projection exists for a window and clears the burn-rate policy's trust bar (see [Why the bounds are sound](explanation-bounds.md)), it prints one additional `Burn rate` line for that window. A declined or untrustworthy projection prints nothing; it does not crowd out the readings above it. |
| `headroom json` | None | Performs the same Codex refresh as `status`, then prints one compact JSON document containing persisted state, diagnostics, four bounded readings, and one burn-rate projection object per source and window found in history. It does not check for updates. |
| `headroom hook` | Optional Claude Code hook JSON | Performs the same Codex refresh, then emits guidance for the highest actionable severity. A `UserPromptSubmit` payload selects the documented JSON envelope. With no hook payload, output is human-readable text. It emits nothing when all readings are `ok` and there is no trustworthy burn-rate warning, and never checks for updates. When a burn-rate projection exists, clears the trust bar, and projects exhaustion before the window's reset, it appends a short warning naming concrete actions. A CRITICAL rate-limit reading always prints alone; the burn-rate warning is never appended to it. |
| `headroom doctor` | None | Reads current Codex sources without updating usage state, then reports install provenance, hook registration, state and Codex diagnostics, burn-rate diagnostics, and update-check status. When update checking is enabled, it makes a check only if the daily cache is due. |
| `headroom reset` | None | Removes `state.json` and `history.jsonl`. It reports whether anything was removed and leaves other files in the state directory untouched. |
| `headroom init` | None | Merges the Claude Code statusline and prompt hook by default. `--codex` selects the Codex hook, and `--all` configures both. See [Installation](howto-install.md). |
| `headroom update` | None | Detects a source checkout, uv tool install, or pip install and prints the appropriate update command. It does not run the command or change anything. Unknown install modes are reported without guessing. |

`statusline`, `status`, `json`, `doctor`, `reset`, and `update` have no command-specific flags other than help.

`headroom update` prints `uv tool upgrade headroom-cli` for a detected uv tool install and `pip install -U headroom-cli` for a detected pip install. For a source checkout, it prints `git pull` followed by `pip install -e .` from the checkout. Every path ends with `Nothing was changed.` The command never performs an update.

## Hook output

When standard input is a JSON object whose `hook_event_name` is `UserPromptSubmit`, `headroom hook` writes one compact JSON document:

```json
{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"<human-readable guidance>"}}
```

This is the documented Claude Code path for adding hook text to the model context. When standard input is empty, malformed, or not a `UserPromptSubmit` payload, the command falls back to human-readable text. Malformed input never crashes the hook. In every output mode, the command writes nothing at all when every reading is `ok`.

`headroom hook --plain` always selects human-readable text, even when standard input contains a hook payload. This is useful for manual inspection and does not change `headroom status`.

## Init flags

| Flag | Default | Effect |
| --- | --- | --- |
| `--codex` | Off | Configures Codex only. It is mutually exclusive with `--all`. |
| `--all` | Off | Configures both Claude Code and Codex. Both files are preflighted before either is written. It is mutually exclusive with `--codex`. |
| `--settings PATH` | `~/.claude/settings.json` | Selects the Claude Code settings file. |
| `--codex-home PATH` | `CODEX_HOME`, then `~/.codex` | Selects the Codex home containing `hooks.json`. |
| `--dry-run` | Off | Prints a unified diff for every selected target and writes no configuration or backup files. If every merged result is unchanged, it prints `headroom init: no changes`. |
| `--print` | Off | Prints the selected generated JSON fragment without reading or writing a target. With `--all`, it prints an object containing `claude` and `codex` fragments. |
| `-h`, `--help` | Off | Prints init help and exits 0. |

When both `--print` and `--dry-run` are present, `--print` takes effect first and emits only the fragment.

With no target flag, init configures Claude Code only. Codex uses `hooks.json` inside the selected Codex home. Its document has only `description` and `hooks` when created fresh, and the `UserPromptSubmit` array contains the required inner `{"hooks": [...]}` wrapper. Existing unrelated keys, events, and prompt entries are preserved. Changed existing files receive a timestamped `.bak` copy first. Invalid JSON is refused with status 1 and left untouched.

## Environment variables

| Variable | Default | Accepted value and behavior |
| --- | --- | --- |
| `CODEX_HOME` | `~/.codex` | Codex home used by `init --codex` and by `doctor` when locating `hooks.json`. `--codex-home` overrides it for init. |
| `HEADROOM_STATE_DIR` | `~/.headroom` | Directory containing `state.json`, `history.jsonl`, and the separate `update-check.json` cache when those files are created. A nonempty value is expanded as a user path. |
| `HEADROOM_UPDATE_CHECK` | Off | The exact value `1` allows only `status` and `doctor` to query PyPI. A valid cache whose recorded version matches the installed version suppresses another query while its age is no more than five minutes in the future and less than 24 hours old. A cache exactly five minutes ahead still suppresses; one exactly 24 hours old does not. A missing, unreadable, invalid, stale, far-future, or different-version cache makes the check due, and a completed check that fails to persist can leave the next invocation due. A due check still fails locally without querying PyPI when the installed version cannot be parsed. Unset it to disable checking and show the local discovery hint in `status`. Set it to `0` to disable checking and suppress the hint. Every other value also disables checking. |
| `HEADROOM_CODEX_HOME` | `~/.codex` | Codex home used only for usage capture. Rollout fallback reads its `sessions` child directory. It does not select the hook installation path. |
| `HEADROOM_CODEX_RPC` | Enabled | The exact value `0` skips the app-server RPC. Every other value, including an unset value, enables it. |
| `HEADROOM_CODEX_RPC_TIMEOUT` | `6` | Shared timeout in seconds for startup, initialization, and the rate-limit response. It must be finite and greater than zero. An invalid value falls back to 6 seconds and adds a diagnostic note. |
| `HEADROOM_CODEX_RPC_CMD` | `codex app-server` | App-server command. It may be a JSON array of nonempty strings or a shell-style command string. An empty or unparseable value skips the command and records a note. |
| `HEADROOM_FRESH_CLAUDE_SECONDS` | `300` | Seconds for which a Claude snapshot is exact. The value must be finite and non-negative. |
| `HEADROOM_FRESH_CODEX_SECONDS` | `1800` | Seconds for which a Codex snapshot is exact. The value must be finite and non-negative. |
| `HEADROOM_FORCE_SEVERITY` | Unset | Hook diagnostic accepting `notice`, `warn`, or `critical`. It forces hook output at that severity and marks the text as a forced test. Any other value is ignored. Unset it after verifying context injection. |

An invalid freshness value causes `status`, `json`, or `hook` to exit 1. `statusline` keeps its terminal contract, prints `headroom: usage unavailable`, and exits 0.

## State and JSON fields

`state.json` is replaced atomically through a temporary file in the same directory. A failed or partial read produces an empty in-memory state. The document has `version` set to 1, a `sources` object keyed by source and window, and any stored `diagnostics`. Every captured snapshot is also appended as one compact object to `history.jsonl`, with the file flushed and synced after writing.

Update results are stored separately in `update-check.json`. The cache contains the check time, installed version, outcome, available version when applicable, and a local failure reason when applicable. Both successful and failed attempts become eligible for another check after 24 hours. A cache created for another installed version is inapplicable. A cache timestamp more than five minutes in the future is also inapplicable, so a clock that jumps far ahead cannot suppress checking indefinitely. The five-minute allowance covers ordinary clock movement such as an NTP correction or a resumed virtual machine. It is a tolerance for real clock behavior, not a guarantee about the timestamp. The response body is limited to 256 KB. An approximately two-second deadline is checked between response reads, but a pathological peer can still stretch a single blocking read. PyPI data is treated as untrusted, release keys are conservatively parsed, and eligibility is determined from a credible catalog of non-yanked files in the `releases` object rather than `info.version`.

The `doctor` update section reports `Enabled`, `Cache`, `Last outcome`, `Last checked`, and `Next eligible check`. A failure reason is part of `Last outcome`. When checking is disabled, these fields come only from the local cache. When there is no cache, the outcome is `not checked`, the last check is `never`, and the next eligible check is `now`.

With `HEADROOM_UPDATE_CHECK=1`, only `status` and `doctor` can send an HTTPS GET request to `https://pypi.org/pypi/headroom-cli/json`, and only when the cache is due. Redirects are treated as failures and are not followed. The request identifies the fixed package name and includes `Accept: application/json` and `User-Agent: headroom-update-check`. The destination can observe the source IP address and timing. No usage readings, account data, local paths, installed version, or cache data are sent. `hook` and `statusline` cannot invoke this request path.

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

## Burn-rate projections

`headroom json` adds a `burn_rate_projections` list at the top level (a sibling of `readings`, not nested inside it), with one object per source and window found in `history.jsonl`. Each object has every field of a projection: `source`, `window`, `rate_percent_per_second`, `projected_exhaustion_at`, `exhaustion_precedes_reset`, `samples_used`, `span_seconds`, `reason`, and eight measurement fields (`max_relative_deviation`, `max_usage_share`, `intervals_used`, `rate_drift`, `effective_intervals`, `zero_delta_fraction`, `max_raw_rate_ratio`, `longest_above_overall_rate_run`). `reason` is `null` when a projection exists and one of the documented decline reasons (for example `"too_few_samples"` or `"insufficient_span_for_horizon"`) when it does not; the measurement fields are `null` exactly when `reason` is set. `exhaustion_precedes_reset` is a tri-state field: `true`, `false`, or `null`. `null` means either the window's reset time is unknown (a projection still exists) or no projection could be made at all (`reason` is set); a caller distinguishes the two by checking `reason`, not by re-inspecting this field.

headroom fits this projection from the persisted usage history, not from a single reading, and takes no position on whether a given projection is trustworthy enough to act on -- it only reports what the data supports and, when it does not support a projection, why. `headroom doctor` translates a decline `reason` into a plain-language sentence (for example "not enough usage samples recorded yet") and, for an existing projection, prints the same eight measurements alongside the projected exhaustion time. This is the place to find out why no projection appeared.

`headroom status` and `headroom hook` additionally apply a burn-rate policy: a projection is shown to a human or a model only when its own measurements clear a conservative trust bar (low deviation between intervals, no single interval dominating the evidence, enough independent intervals, low drift between the window's early and late portions, and no raw interval running far faster than the segment's average pace). This policy exists precisely because a hard cutoff cannot live inside the projection itself; see [Why the bounds are sound](explanation-bounds.md) for why that separation matters and what the policy declines to claim. `status` prints one line per window whose projection clears the bar; a declined or untrustworthy projection is silent there, matching `hook`'s existing behavior of saying nothing when there is nothing worth saying.

`headroom hook` speaks about a burn-rate projection only when it clears the trust bar AND `exhaustion_precedes_reset` is `true` -- the case where the model can still change its behavior before the window resets. The warning names concrete actions (using cheaper models, checkpointing work) and never says "compact," which the model cannot invoke. A CRITICAL rate-limit warning always prints alone: the burn-rate warning is never appended to it, so the highest-urgency signal is never diluted or displaced. Otherwise, a burn-rate warning composes with whatever the existing severity ladder has to say: if nothing is otherwise actionable, the burn-rate lines are the only output; if a NOTICE or WARN reading is also present, both appear.

## Exit codes

| Status | When it is used |
| --- | --- |
| `0` | A command completed, help or version information was printed, `hook` had nothing to say, or `statusline` recovered from malformed input or an internal failure. |
| `1` | `init` could not read, merge, back up, or write selected configuration; `reset` could not remove state; or another on-demand command raised a handled I/O, value, or type error. |
| `2` | Command-line parsing failed because the command or arguments were missing or invalid. |

The compatibility script `python install.py` forwards its arguments to `headroom init` and uses the same exit codes.

For source-specific failure messages, continue with [Troubleshooting](howto-troubleshoot.md). For install behavior and project links, see [Installation](howto-install.md).
