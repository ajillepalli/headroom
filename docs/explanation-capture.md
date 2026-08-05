# How capture works

[Back to the README](../README.md) | [Why the bounds are sound](explanation-bounds.md) | [Troubleshooting](howto-troubleshoot.md)

headroom combines two local capture paths. Claude arrives through statusline input. Codex is requested on demand through a local stdio RPC, with session rollouts as a fallback.

## Claude Code capture

Claude Code invokes a configured statusline command and sends a JSON document on standard input. Claude Code 2.1.80 and newer include `rate_limits` in that input. headroom reads the whole document whenever Claude Code renders the statusline.

On a multi-machine setup, this capture sees only the statusline payloads observed on that machine, so its Claude reading is a lower bound on account-wide usage. See [Claude readings look too good on multiple machines](howto-troubleshoot.md#claude-readings-look-too-good-on-multiple-machines).

The nesting is not fixed, so the parser walks dictionaries and lists recursively. It accepts these bucket spellings:

| Meaning | Accepted fields |
| --- | --- |
| Usage | `used_percentage`, `usedPercentage`, `used_percent`, `usedPercent` |
| Reset | `resets_at`, `resetsAt` |
| Window duration | `window_minutes`, `windowMinutes` |
| Reached limit | `limit_reached`, `limitReached`, or usage at least 100% |

A duration of at least 1,440 minutes is weekly; a shorter duration is the short window. Without a usable duration, a path containing `week` or `7d` is weekly, while a path containing `5` or `hour` is short. Later usable buckets for the same window replace earlier ones during the recursive walk.

Parsed snapshots and unparsed notes are saved before the compact line is rendered. Unexpected JSON or internal failures cannot break the terminal contract: `statusline` prints a line and exits 0. It never starts `codex app-server`; stored Codex state is rendered as-is.

Claude freshness depends on the most recent statusline render. The default exact interval is 300 seconds.

## Codex app-server capture

`status`, `json`, `hook`, and `doctor` start the configured app-server command. headroom exchanges newline-delimited JSON-RPC messages over the child process's standard input and output:

1. Send `initialize` with `clientInfo.name` set to `headroom` and the package version.
2. Wait for response id 1, ignoring notifications and unrelated messages.
3. Send the `initialized` notification with an empty object.
4. Send `account/rateLimits/read` with request id 2 and `params` set to `null`.
5. Wait for response id 2, then stop and reap the child process.

The verified response has this shape, with other limit buckets allowed beside the Codex bucket:

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": {
    "rateLimits": { "limitId": "codex" },
    "rateLimitsByLimitId": {
      "codex_bengalfox": {
        "limitId": "codex",
        "limitName": null,
        "primary": {
          "usedPercent": 4,
          "windowDurationMins": 10080,
          "resetsAt": 1886494688
        },
        "secondary": null,
        "credits": {
          "hasCredits": false,
          "unlimited": false,
          "balance": "0"
        },
        "individualLimit": null,
        "spendControlReached": false,
        "planType": "pro",
        "rateLimitReachedType": null
      }
    }
  }
}
```

headroom first searches `rateLimitsByLimitId` for a bucket whose `limitId` is `codex`. If none is found, it uses the top-level `rateLimits` object when present. The RPC spells the duration `windowDurationMins`; rollout records spell it `window_minutes`. The shared parser accepts both, along with the other camel-case and snake-case variants listed in the source.

The `account/rateLimits/read` request costs no Codex quota and creates no session rollout. Those two side effects were checked against Codex 0.146.0.

Unlike Claude statusline capture, this RPC returns account-wide Codex figures regardless of which machine asks. See [Claude readings look too good on multiple machines](howto-troubleshoot.md#claude-readings-look-too-good-on-multiple-machines).

The whole exchange shares a default six-second deadline. A timeout, launch failure, protocol error, unusable response, or disabled RPC becomes a diagnostic and leads to rollout fallback. The process is killed and reaped if it does not exit itself.

## Codex rollout fallback

The fallback searches `sessions/YYYY/MM/DD/rollout-*.jsonl` below the Codex home. By default that is `~/.codex`; `HEADROOM_CODEX_HOME` changes it.

Files are ordered newest first by modification time and then path. headroom scans until the first file with a usable snapshot. Inside each file it keeps the last usable `rate_limits` occurrence. A payload timestamp from `timestamp`, `created_at`, or `createdAt` becomes the capture time when valid; otherwise the file modification time is used.

Rollouts may contain `primary` and `secondary` buckets, and either can be `null`. Current samples often have a weekly primary bucket and a null short bucket, so the Codex 5h reading can remain unavailable. The parser accepts `used_percent`, `used_percentage`, `usedPercent`, and `usedPercentage`, plus all three supported window-duration spellings.

## Which surfaces are covered

Claude Code runs in several places, and capture and injection do not reach the same set of them. Capture is how headroom reads the numbers. Injection is how the warning reaches the model.

| Surface | Claude capture | Injection |
| --- | --- | --- |
| Terminal CLI | yes | yes |
| Desktop app | no documented mechanism | yes |
| VS Code and JetBrains extensions | no documented mechanism | yes |
| Claude Code on the web | no documented mechanism | project or organization settings |

Injection is the documented case. The hooks documentation states that hooks run wherever Claude Code runs, covering terminal sessions, IDE extensions, the Desktop app, and the web. Firing is not the same as being configured, so a surface warns only where the headroom hook is actually installed for it.

Capture is the narrow one. Among the first-party clients, the statusline payload is the only documented source of Claude rate limits, and statusline is documented only for the terminal. Nothing states that the other surfaces refuse to run a statusline command; there is simply no documented mechanism for it, and those surfaces have no terminal status bar to render into. Treat that row as an absence of documentation rather than a tested failure.

Nothing else observes a first-party client's numbers passively. `/usage` draws its bars for a person to read rather than emitting anything a program can parse, no state or config file records the numbers, no other hook event carries them in its payload, and MCP servers are not given them.

The Agent SDK is the one real alternative, and it is an active route rather than a passive one. Its rate-limit events report utilization against the shared subscription limits, which can include what was spent interactively in Claude Code, so the numbers are relevant. Reading them means running an SDK session and consuming quota to ask, which is a different bargain from observing a payload Claude Code was already producing. headroom watches what is already there and spends nothing.

The web has a second constraint. Cloud sessions load hooks from project settings, meaning `.claude/settings.json` or `.claude/settings.local.json` in the repository, or from organization server-managed settings. They do not load `~/.claude/settings.json`. `headroom init` writes the user file, so a web session gets no hook today. That is tracked in [issue #36](https://github.com/ajillepalli/headroom/issues/36).

Codex is not affected the same way when the app-server RPC answers. That query asks the account for its own limits, so the result does not depend on which client spent the quota, and GUI and IDE use stays visible. The rollout fallback is different: it reads local session records, so it sees only work done by a client that wrote rollouts on this machine. `doctor` reports which source won, and the distinction matters whenever RPC fails or is disabled.

The practical result follows from the bound rather than from any special handling. Usage is monotonic within a window and the limit is account-wide, so a capture taken in the terminal stays a valid lower bound on work done anywhere else, until that window resets and the old percentage stops describing it. Working in the terminal some of the time and elsewhere the rest means headroom captures in the terminal and still warns on any surface where its hook is installed; the warning is sound and reads low. For Claude, working only outside the terminal means headroom captures nothing and stays quiet. It under-reports rather than misreporting, which is the same property described for [multiple machines](howto-troubleshoot.md#claude-readings-look-too-good-on-multiple-machines).

## Storage and limits of the sources

Successful snapshots from either source are merged into `state.json`, which is replaced atomically. Each snapshot is appended to `history.jsonl`. On-demand Codex refresh updates Codex diagnostics even when neither source yields a new snapshot. `doctor` performs capture discovery without changing state.

Neither source provides a published token allowance through these payloads. headroom reports percentage used and reset time, never tokens remaining. Claude can be only as fresh as its last statusline render. Codex can be only as fresh as the RPC response or newest usable rollout.

For output fields and source controls, see the [CLI reference](reference-cli.md). For missing readings and RPC fallback checks, see [Troubleshooting](howto-troubleshoot.md).
