# Why context is fresh-or-nothing

[Back to the README](../README.md) | [Why the bounds are sound](explanation-bounds.md) | [CLI reference](reference-cli.md)

headroom reports Claude's context-window usage as its own signal, separate from the rate-limit windows described in [Why the bounds are sound](explanation-bounds.md). That page's whole argument -- a stale reading is still a sound lower bound -- does not carry over here, and this page explains why, and what headroom does instead.

## The property rate limits have that context does not

The bounds model works because a rate-limit window has two properties. Usage is monotonic within the window, so a stale reading can only be too low, never too high. And the window has an absolute `resets_at`, so once that time passes, the old high-water mark is known to no longer apply.

Context usage has the first property only in part. It is monotonic non-decreasing except for compaction, an event that drops usage by an amount and at a moment the statusline payload never reports. It has no `resets_at` at all: compaction is triggered by Claude Code's own internal state, not scheduled, so nothing in the payload says when it will happen or whether it already did.

A stale context reading is therefore neither a sound lower bound (compaction may have already happened) nor a sound upper bound (usage may have kept rising since the last capture). There is also no future time at which it becomes known-good again, the way a rate-limit reading does at `resets_at`. It is simply unknown.

## The consequence: report it fresh, or not at all

Because a stale context reading cannot be bounded in either direction, headroom reports it only while it is genuinely fresh, and says nothing otherwise. There is no `>=` marker, no escalation, no hedge. `ContextReading.fresh` is the only confidence state that exists: `age_seconds` at or under the configured window is fresh, anything older is treated exactly like no reading at all.

This is a real, deliberate divergence from every other reading headroom produces. It is also the honest one: a bounded guess is not available here at any confidence, so headroom does not manufacture one.

## Why the freshness window is 300 seconds, not tighter

`headroom init` sets the Claude statusline's `refreshInterval` to 300 seconds, matching `HEADROOM_FRESH_CLAUDE_SECONDS`'s own default. Context rides the exact same statusline payload as the rate-limit windows, so its freshness window (`HEADROOM_FRESH_CONTEXT_SECONDS`) defaults to the same 300 seconds rather than something tighter.

A tighter window would not make context "more accurate" -- it would only make the signal go silent more often, on a clean install with nothing actually wrong, because it would be tighter than the tool's own configured sample rate. In practice the sample rate is usually much faster than 300 seconds anyway: Claude Code re-runs the statusline command on several event-driven triggers (a new assistant message, `/compact` finishing, a permission-mode change, a vim-mode toggle), and `refreshInterval` only adds a periodic re-render on top of those events during an idle session. It does not throttle invocation down to once per 300 seconds. See the [CLI reference](reference-cli.md#environment-variables) for the exact variable and its default.

## Context is per-session; rate limits are account-wide

Every other signal headroom reports is account-wide: a rate-limit window is the same number no matter which terminal tab asks. Context usage is not -- it belongs to one specific conversation. Two terminal tabs are two different context usages at any given moment, and nothing about the statusline payload changes that.

`state.json` keys context captures by `session_id`, taken from the statusline payload on write and from the `UserPromptSubmit` hook payload on read. When either side has no `session_id`, headroom reports nothing rather than guessing which session a stored reading belongs to. Without this, a flat, non-session-keyed slot would let one terminal tab's fresh 8%-used reading answer for a different, simultaneously-critical session's prompt -- the exact inverse of the truth, delivered with full confidence.

`json` and `status` have no session of their own (no stdin, no hook payload), so both report every currently-fresh session rather than picking one -- picking one would only relocate the same cross-session risk to a different command. `doctor` also has no session of its own, but answers a different question ("is context capture working at all"), so it reports the single most recently captured entry across every session instead, and states plainly when none exists.

## Severity reuses the existing ladder

Context severity is computed by the same headroom-based cascade as every rate-limit reading (`ok` above 40% headroom, `notice` from 20-40%, `warn` from 10-20%, `critical` below 10%), not a separate, differentiated set of thresholds. A differentiated ladder was proposed once and dropped: it turned out to match the existing one at two of its three points and be lower, not higher, at the third, contradicting its own stated rationale for existing.

## The advice is different, deliberately

The rate-limit hook tells the model to avoid parallel subagent fan-out, because fan-out spends the account's shared quota. That advice is backwards for context: a subagent explores using its own, separate context window and returns only a condensed result to the caller, so delegating a large read is the single best move when context is high, and it is the closest thing to compaction the model can actually invoke itself. The context advice text says so explicitly, and never instructs the model to "compact" -- compaction is not a tool the model has, only something that may happen to it. Text that says compaction "may happen without warning" is describing why to act, not asking the model to trigger it.

## Arbitration with the rate-limit ladder

A single hook invocation can have both a live rate-limit warning and a live context warning to report, and the ~60-word budget cannot always show both in full. headroom picks a winner:

1. Compute the worst rate-limit severity and this session's context severity separately.
2. If both are `ok` or absent, the hook says nothing.
3. If only one is above `ok`, that one is shown exactly as if the other did not exist.
4. If both are above `ok`, `critical` outranks `warn` outranks `notice`. A tie goes to the rate-limit side, because exhausting a rate-limit window blocks work for hours or days, while a context reading resolves itself (via compaction) in seconds.
5. The loser, if it is `warn` or `critical`, gets one short trailing clause with no second action line. A loser that is only `notice` is dropped entirely to protect the word budget.
6. A `critical` rate-limit line is never crowded out by context: since `critical` is the top of the ladder, context can never outrank it, so rule 4 already guarantees this without special-casing it.

Burn-rate projections (see [Why the bounds are sound](explanation-bounds.md#a-burn-rate-projection-is-not-a-threshold)) stay a purely rate-side addition and are not affected by context arbitration: they never appear when context wins, and a `critical` rate line still never gets a burn-rate addendum, only at most the one trailing context clause from rule 5.

## What this does not cover

Codex has no equivalent field in its app-server responses, so this is Claude-only. headroom does not predict when compaction will fire, only reports the last fresh percentage it observed. And this signal never changes the existing rate-limit `Reading` type or its own thresholds -- the two live side by side, deliberately not sharing a model.

For the exact field names, the environment variable, and example renders, see the [CLI reference](reference-cli.md). For what to do when context guidance never appears, see [Troubleshooting](howto-troubleshoot.md).
