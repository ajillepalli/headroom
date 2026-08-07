<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="images/quotagauge-logo-dark.svg">
    <img src="images/quotagauge-logo.svg" alt="quotagauge" width="420">
  </picture><br>
  <strong>quotagauge</strong><br>
  <a href="docs/explanation-capture.md">Capture</a> &middot;
  <a href="docs/howto-install.md">Install</a> &middot;
  <a href="docs/reference-cli.md">CLI reference</a> &middot;
  <a href="docs/explanation-bounds.md">Bounds</a> &middot;
  <a href="docs/howto-troubleshoot.md">Troubleshooting</a><br><br>
  <img alt="MIT license" src="https://img.shields.io/badge/license-MIT-blue.svg">
  <img alt="Zero dependencies" src="https://img.shields.io/badge/dependencies-zero-success.svg">
  <img alt="Python 3.9 or newer" src="https://img.shields.io/badge/python-3.9%2B-blue.svg">
</p>

Claude Code and Codex enforce rolling usage limits but show the numbers only to a human, so the model never sees them and can keep working right up to the wall. quotagauge captures those readings and warns the model when it should conserve usage.

## Install and first run

The shortest path:

```console
uv tool install quotagauge
quotagauge init --all
quotagauge status
```

To work from a clone instead, run one of these:

```console
uv tool install .
```

```console
pip install -e .
```

Then run `quotagauge init --all` and `quotagauge status`. Plain `quotagauge init` configures Claude Code only, while `--codex` configures Codex only. See the [installation guide](docs/howto-install.md) for safe previews and the plain-clone fallback.

## Example output

`quotagauge status` reports every source and window:

```text
Claude
  5h: 12% used [ok], resets in 1h 59m
  7d: unavailable
Codex
  5h: unavailable
  7d: >=94% used [critical], resets in 1h 59m
```

Near a limit, the prompt hook gives the model a short action:

```text
Usage headroom: Codex weekly >=94% used (reading 1h 0m old), resets in 1h 59m.
Stop parallel subagent fan-out, use cheaper models, and checkpoint work now.
```

## Documentation

| Document | What it covers |
| --- | --- |
| [CLI reference](docs/reference-cli.md) | Every command, flag, environment variable, state file, exit code, and the opt-in update check. |
| [Why the bounds are sound](docs/explanation-bounds.md) | Fresh, stale, and post-reset readings, reset validation, and severity. |
| [How capture works](docs/explanation-capture.md) | Claude statusline input, Codex app-server RPC, and rollout fallback. |
| [Installation guide](docs/howto-install.md) | Claude Code settings, the `CODEX_HOME/hooks.json` path, safe init previews, backups, and clone installs. |
| [Troubleshooting](docs/howto-troubleshoot.md) | Unavailable or stale readings, unknown resets, doctor output, and state reset. |
