# Install and configure headroom

[Back to the README](../README.md) | [CLI reference](reference-cli.md) | [Troubleshooting](howto-troubleshoot.md)

headroom requires Python 3.9 or newer. It has no third-party runtime dependencies.

## Install the published package

The `headroom-cli` package is not published yet. Once it is available, install the command with:

```console
uv tool install headroom-cli
```

Then configure Claude Code:

```console
headroom init
```

## Install from a clone

From the repository root, install an isolated command with uv:

```console
uv tool install .
```

For an editable installation, use pip:

```console
pip install -e .
```

Either installation exposes the `headroom` entry point. Run:

```console
headroom init
headroom status
```

## Preview the settings change

To inspect the JSON fragment without reading or writing a settings file, run:

```console
headroom init --print
```

To inspect a unified diff without changing the file or making a backup, run:

```console
headroom init --dry-run
```

To target a test or nondefault file, add:

```console
headroom init --settings PATH
```

The default target is `~/.claude/settings.json`.

## What init writes

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

## Configure a plain clone without a command on PATH

The compatibility shim runs the same installer:

```console
python install.py
```

When no `headroom` executable is on `PATH`, the generated statusline and hook commands use the current Python executable, an absolute checkout path in `PYTHONPATH`, and `python -m headroom.cli`. This lets Claude Code invoke the clone from any working directory. On Windows the environment prefix uses `set "PYTHONPATH=..." &&`; on POSIX systems it uses `PYTHONPATH=...` with shell quoting.

`python install.py` accepts `--settings`, `--dry-run`, and `--print` because it forwards them to `headroom init`.

## Verify the setup

Run:

```console
headroom status
headroom doctor
```

`status` shows all four source-window combinations. `doctor` identifies the state location and the winning Codex capture source. See [Troubleshooting](howto-troubleshoot.md) for each line.

Run the standard-library test suite from the repository root with:

```console
python -m unittest discover -s tests -t .
```

Project policies and contribution details are in [CONTRIBUTING.md](../CONTRIBUTING.md), [SECURITY.md](../SECURITY.md), and [CODE_OF_CONDUCT.md](../CODE_OF_CONDUCT.md). headroom is available under the [MIT License](../LICENSE.txt).
