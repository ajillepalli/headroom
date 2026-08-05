# Install and configure headroom

[Back to the README](../README.md) | [CLI reference](reference-cli.md) | [Troubleshooting](howto-troubleshoot.md)

headroom requires Python 3.9 or newer. It has no third-party runtime dependencies.

## Install the published package

The `headroom-cli` package is on PyPI. Install the command with:

```console
uv tool install headroom-cli
```

To upgrade later, run `uv tool upgrade headroom-cli`. `headroom update` prints the command for a detected uv tool, pip, or source-checkout install, and says nothing is suggested when it does not recognize the install.

Then configure Claude Code, Codex, or both:

```console
headroom init
headroom init --codex
headroom init --all
```

Plain `headroom init` keeps its original behavior and configures Claude Code only. Use `--codex` for Codex only or `--all` for both.

## Install from a clone

From the repository root, install an isolated command with uv:

```console
uv tool install .
```

This copies the package into uv's tool environment. Editing the clone afterward does not update that installed command. During development, reinstall after source changes:

```console
uv tool install . --force
```

Use `headroom doctor` to confirm the imported package path, install mode, version, and modification time. When running directly from a Git checkout, `headroom --version` also includes the checkout's short commit hash.

For an editable installation, use pip:

```console
pip install -e .
```

Either installation exposes the `headroom` entry point. To configure both clients and inspect the readings, run:

```console
headroom init --all
headroom status
```

## Preview the settings change

To inspect the JSON fragment without reading or writing a settings file, run:

```console
headroom init --print
```

Add `--codex` to preview the verified Codex document. With `--all`, the printed JSON has `claude` and `codex` keys containing the two fragments.

To inspect a unified diff without changing the file or making a backup, run:

```console
headroom init --dry-run
```

To target a test or nondefault file, add:

```console
headroom init --settings PATH
```

The default Claude Code target is `~/.claude/settings.json`. To select a test or nondefault Codex home, run:

```console
headroom init --codex --codex-home PATH
```

The Codex target is `hooks.json` inside `--codex-home`, then `CODEX_HOME` when that environment variable is set, and otherwise `~/.codex`.

## What init writes for Claude Code

The generated fragment sets a command statusline with a 300-second refresh interval and adds one `UserPromptSubmit` command hook:

```json
{
  "statusLine": {
    "type": "command",
    "command": "headroom statusline",
    "refreshInterval": 300
  },
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "headroom hook"
          }
        ]
      }
    ]
  }
}
```

`init` merges this fragment into the existing top-level object. It never overwrites the settings file wholesale. It preserves unrelated settings and hook events, appends the headroom prompt hook to an existing `UserPromptSubmit` array, and does not append a duplicate on later runs. It sets `statusLine` to the generated headroom statusline.

Before changing an existing file, `init` copies it to `settings.json.TIMESTAMP.bak` in the same directory. A new file needs no backup. An unchanged file is neither rewritten nor backed up. Invalid JSON, a non-object top level, a non-object `hooks` value, or a non-array `hooks.UserPromptSubmit` value is refused without a settings change.

## What init writes for Codex

`headroom init --codex` writes this verified shape to the selected `hooks.json`:

```json
{
  "description": "headroom",
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "headroom hook",
            "timeoutSec": 10
          }
        ]
      }
    ]
  }
}
```

The inner object containing `hooks` is required. headroom always emits the PascalCase file event `UserPromptSubmit`; Codex normalizes it internally when it registers the command.

The Codex path uses the same merge discipline as the Claude Code path. It preserves unrelated keys, unrelated hook events, and existing `UserPromptSubmit` entries, then appends the headroom wrapper only when it is absent. Before changing an existing file, it copies the original content to `hooks.json.TIMESTAMP.bak` and prints the backup path. Malformed JSON and incompatible `hooks` shapes are refused without changing the file.

`--dry-run` reads and merges every selected file but writes no file or backup. `--print` reads no selected file and writes nothing. `--all` preflights both files before it creates backups or writes either one. If a later write still fails after the first target was updated, init restores the already-written target and reports the failure. If restoration fails, it explicitly reports that configuration may be partially applied.

## Configure a plain clone without a command on PATH

The compatibility shim runs the same installer:

```console
python install.py
```

When no `headroom` executable is on `PATH`, the generated Claude Code statusline and hook commands use the current Python executable, an absolute checkout path in `PYTHONPATH`, and `python -m headroom.cli`. This lets Claude Code invoke the clone from any working directory. On Windows the environment prefix uses `set "PYTHONPATH=..." &&`; on POSIX systems it uses `PYTHONPATH=...` with shell quoting. The verified Codex document always uses the literal command `headroom hook`, so install the entry point before enabling the Codex hook.

`python install.py` accepts all init flags because it forwards them to `headroom init`.

## Verify the setup

Run:

```console
headroom status
headroom doctor
```

`status` shows all four source-window combinations. `doctor` reports the selected Codex hooks file, whether the headroom hook is registered, the state location, and the winning Codex capture source. See [Troubleshooting](howto-troubleshoot.md) for each line.

The only documented Claude capture path this configuration uses is the terminal statusline. A local Desktop or IDE session has no documented mechanism for supplying the numbers, but hooks still fire there and the command and state are on the same machine, so it receives warnings built from whatever the terminal last captured. Claude Code on the web receives nothing, because a cloud session runs on Anthropic-managed infrastructure that has neither the `headroom` command nor the local state file. The same applies to any session running somewhere else, including an SSH host or a dev container, unless headroom is installed there with its own state. Codex stays client-independent while the app-server RPC answers; its rollout fallback reads local session records instead. See [which surfaces are covered](explanation-capture.md#which-surfaces-are-covered) before assuming a setup reads everything.

Run the standard-library test suite from the repository root with:

```console
python -m unittest discover -s tests -t .
```

Project policies and contribution details are in [CONTRIBUTING.md](../CONTRIBUTING.md), [SECURITY.md](../SECURITY.md), and [CODE_OF_CONDUCT.md](../CODE_OF_CONDUCT.md). headroom is available under the [MIT License](../LICENSE.txt).
