# Why the bounds are sound

[Back to the README](../README.md) | [How capture works](explanation-capture.md) | [CLI reference](reference-cli.md)

headroom does not guess current usage from an old sample. It turns each timestamped snapshot into a statement that remains true as time passes.

## A stale sample is a lower bound

Usage is monotonic inside one rate-limit window. After a reading says 65% used, later work can raise usage, but usage cannot fall before that window resets. If the snapshot becomes stale before its reset, the only sound statement is that current usage is at least 65%.

That is why a stale reading is rendered as `>=65% used`, not `65% used`. The stored percentage is a lower bound and `certain` is false. headroom does not estimate burn rate or tokens remaining.

## An absolute reset ends the old claim

`resets_at` names an absolute time. Once `now > resets_at`, the captured usage belongs to a finished window and says nothing adverse about the new window. headroom returns a `post_reset` reading with a 0% lower bound, sets `certain` to true, clears `limit_reached`, and assigns severity `ok`.

This is known good in the bounds model. It does not claim that no new usage has occurred. It says the previous window's high usage cannot be carried into the new window as a lower bound.

Most old readings therefore have a useful interpretation. Before reset, the sample is a conservative lower bound. After reset, the old high-water mark is retired. Only a stale reading whose reset is still ahead needs a caution adjustment.

## Reset times must be plausible

A bad reset timestamp could turn a high-usage snapshot into a false `post_reset` result, or display a meaningless countdown. headroom validates the timestamp against the capture time before using it.

When the bucket has a positive finite window duration, an accepted reset falls from five minutes before capture through the window duration plus five minutes after capture. When the duration is absent or invalid, the reset can be no more than 14 days ahead and no more than five minutes before capture.

If a reset is missing, invalid, or outside those limits, headroom keeps the valid usage percentage but stores no reset. Reports then say `reset time unknown`. Parsers add a diagnostic for invalid or implausible values, and `doctor` can surface it. A previously stored reset is checked again when a bound is calculated.

Reset timestamps may be Unix seconds, Unix milliseconds, numeric strings, or ISO 8601 strings. Numeric values at or above `100000000000` are treated as milliseconds. Current epoch seconds are around `10^9`, current epoch milliseconds are around `10^12`, and epoch seconds remain below that threshold until the year 5138.

## Confidence states

| Confidence | Meaning |
| --- | --- |
| `fresh` | Snapshot age is within the source freshness interval. The percentage is exact and `certain` is true. |
| `stale_bounded` | Snapshot age exceeds the interval and reset is still ahead or unknown. The percentage is shown with `>=` and `certain` is false. |
| `post_reset` | The accepted reset is in the past. The old window has a 0% lower bound, `certain` is true, and severity is `ok`. |
| `unknown` | No valid snapshot exists for that source and window. The percentage and reset are absent. |

The default freshness intervals are five minutes for Claude and 30 minutes for Codex. They can be changed with the variables in the [CLI reference](reference-cli.md).

## Severity from remaining headroom

Headroom is `100 - used percentage`.

| Severity | Base condition |
| --- | --- |
| `ok` | More than 40% headroom. |
| `notice` | From 20% through 40% headroom. |
| `warn` | From 10% up to, but not including, 20% headroom. |
| `critical` | Less than 10% headroom, or the source reports that the limit was reached. |

For a `stale_bounded` reading with less than 50% headroom, headroom raises the base severity by one level and caps it at `critical`. The code names this boundary `ESCALATE_BELOW_HEADROOM = 50.0`. Exactly 50% headroom does not escalate. A `post_reset` reading is always `ok`.

The prompt hook stays silent when all four readings are `ok`. For capture timing and source field names, see [How capture works](explanation-capture.md).
