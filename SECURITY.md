# Security policy

## Supported code

Security fixes target the latest code on the default branch. The project does not maintain a support matrix for older snapshots yet.

## What quotagauge handles

quotagauge reads local usage telemetry from Claude Code statusline input and Codex rollout files. It writes snapshots and history to the configured state directory. By default, it makes no outbound network calls and sends nothing anywhere. `hook` and `statusline` never perform an update check, even when update checking is enabled.

The state directory defaults to `~/.quotagauge`. On a machine that ran this project under its old name, the first command that resolves the default directory migrates a pre-existing `~/.headroom` there automatically (a directory rename, not a copy) and then never looks at the old path again. If the rename cannot complete, the old directory is left exactly as it was and used in place: nothing is ever deleted or overwritten by this migration, so existing history is never destroyed. It can, in narrow and documented cases (see [Troubleshooting](docs/howto-troubleshoot.md#migrating-from-the-earlier-headroom-name)), end up left behind under the old path rather than picked up automatically -- always recoverable by hand, never silently lost. Set `QUOTAGAUGE_STATE_DIR` to bypass this default (and its migration) entirely.

Update checking is off by default. Set `QUOTAGAUGE_UPDATE_CHECK=1` to opt in. When opted in, only `quotagauge status` and `quotagauge doctor` can make an HTTPS GET request to `https://pypi.org/pypi/quotagauge/json`. A request may be made when the cache is due, meaning there is no valid cache or the previous attempt is at least 24 hours old, and the installed version is one quotagauge can parse. When the installed version falls outside the supported PEP 440 subset (for example a `development` install), the check fails locally and no request is made, regardless of cache state. Successful checks and failures are both cached in `update-check.json`, separately from locally derived `state.json` and `history.jsonl` data.

The request goes to the Python Package Index at `pypi.org`. Redirects are not followed, so a redirect cannot move the check to another host or downgrade it to HTTP. Name resolution can send `pypi.org` to the configured DNS resolver. The TLS connection exposes the destination address, source address, timing, and `pypi.org` server name to the parties that normally handle the connection. Inside TLS, quotagauge sends `GET /pypi/quotagauge/json` with `Host: pypi.org`, `Accept: application/json`, `Connection: close`, `User-Agent: quotagauge-update-check`, and standard-library HTTP framing such as `Accept-Encoding: identity`. An explicitly configured HTTPS proxy can also observe connection metadata. quotagauge does not send usage readings, account data, local paths, the installed version, or cache contents.

Unset `QUOTAGAUGE_UPDATE_CHECK` or set it to any value other than the exact value `1` to disable network update checks. An unset variable causes `quotagauge status` to print one local-only discovery line. Set `QUOTAGAUGE_UPDATE_CHECK=0` to keep checking disabled and suppress that line.

The current prompt hook emits only the source, window, bounded percentage, reading age, reset timing, and a short action. It does not emit raw source payloads.

## Risks to report

Please report issues such as:

- `state.json` or `history.jsonl` receiving permissions that let other local users read usage telemetry;
- hook output exposing fields that should stay local, including an account `plan_type` found in an upstream rate-limit object;
- path traversal or unsafe path redirection through `QUOTAGAUGE_STATE_DIR` or `QUOTAGAUGE_CODEX_HOME`;
- crafted rollout or statusline input causing file disclosure, unintended writes, command execution, or prompt submission failure;
- a race that exposes a partial or corrupted state file.

Anyone who can set the quotagauge environment variables can choose where state is written and where Codex sessions are read. Do not run quotagauge with untrusted environment values. Protect the state directory with operating-system permissions appropriate for local account data.

## Report a vulnerability

Use GitHub private vulnerability reporting:

1. Open this repository's **Security** tab.
2. Open **Advisories**.
3. Select **Report a vulnerability**.

Do not open a public issue for an unpatched vulnerability.

Include the affected code path, impact, reproduction steps, and any suggested fix. Remove real session contents, account details, and local paths that are not needed to reproduce the issue.

Maintainers will acknowledge the report within three business days. They will validate the issue, agree on a disclosure plan, prepare a fix, and credit the reporter unless the reporter asks to remain anonymous.
