# Security policy

## Supported code

Security fixes target the latest code on the default branch. The project does not maintain a support matrix for older snapshots yet.

## What headroom handles

headroom reads local usage telemetry from Claude Code statusline input and Codex rollout files. It writes snapshots and history to the configured state directory. It makes no network calls and sends nothing anywhere.

The current prompt hook emits only the source, window, bounded percentage, reading age, reset timing, and a short action. It does not emit raw source payloads.

## Risks to report

Please report issues such as:

- `state.json` or `history.jsonl` receiving permissions that let other local users read usage telemetry;
- hook output exposing fields that should stay local, including an account `plan_type` found in an upstream rate-limit object;
- path traversal or unsafe path redirection through `HEADROOM_STATE_DIR` or `HEADROOM_CODEX_HOME`;
- crafted rollout or statusline input causing file disclosure, unintended writes, command execution, or prompt submission failure;
- a race that exposes a partial or corrupted state file.

Anyone who can set the headroom environment variables can choose where state is written and where Codex sessions are read. Do not run headroom with untrusted environment values. Protect the state directory with operating-system permissions appropriate for local account data.

## Report a vulnerability

Use GitHub private vulnerability reporting:

1. Open this repository's **Security** tab.
2. Open **Advisories**.
3. Select **Report a vulnerability**.

Do not open a public issue for an unpatched vulnerability.

Include the affected code path, impact, reproduction steps, and any suggested fix. Remove real session contents, account details, and local paths that are not needed to reproduce the issue.

Maintainers will acknowledge the report within three business days. They will validate the issue, agree on a disclosure plan, prepare a fix, and credit the reporter unless the reporter asks to remain anonymous.
