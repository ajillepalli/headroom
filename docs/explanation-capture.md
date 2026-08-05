# How capture works

[Back to the README](../README.md) | [Why the bounds are sound](explanation-bounds.md) | [Troubleshooting](howto-troubleshoot.md)

headroom combines two local capture paths. Claude arrives through statusline input. Codex is requested on demand through a local stdio RPC, with session rollouts as a fallback.

## Claude Code capture

Claude Code invokes a configured statusline command and sends a JSON document on standard input. Claude Code 2.1.80 and newer include `rate_limits` in that input. headroom reads the whole document whenever Claude Code renders the statusline.

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

The whole exchange shares a default six-second deadline. A timeout, launch failure, protocol error, unusable response, or disabled RPC becomes a diagnostic and leads to rollout fallback. The process is killed and reaped if it does not exit itself.

## Codex rollout fallback

The fallback searches `sessions/YYYY/MM/DD/rollout-*.jsonl` below the Codex home. By default that is `~/.codex`; `HEADROOM_CODEX_HOME` changes it.

Files are ordered newest first by modification time and then path. headroom scans until the first file with a usable snapshot. Inside each file it keeps the last usable `rate_limits` occurrence. A payload timestamp from `timestamp`, `created_at`, or `createdAt` becomes the capture time when valid; otherwise the file modification time is used.

Rollouts may contain `primary` and `secondary` buckets, and either can be `null`. Current samples often have a weekly primary bucket and a null short bucket, so the Codex 5h reading can remain unavailable. The parser accepts `used_percent`, `used_percentage`, `usedPercent`, and `usedPercentage`, plus all three supported window-duration spellings.

## Storage and limits of the sources

Successful snapshots from either source are merged into `state.json`, which is replaced atomically. Each snapshot is appended to `history.jsonl`. On-demand Codex refresh updates Codex diagnostics even when neither source yields a new snapshot. `doctor` performs capture discovery without changing state.

Neither source provides a published token allowance through these payloads. headroom reports percentage used and reset time, never tokens remaining. Claude can be only as fresh as its last statusline render. Codex can be only as fresh as the RPC response or newest usable rollout.

For output fields and source controls, see the [CLI reference](reference-cli.md). For missing readings and RPC fallback checks, see [Troubleshooting](howto-troubleshoot.md).
