# headroom-cli has been renamed to quotagauge

This package is no longer maintained. Development continues as
[**quotagauge**](https://pypi.org/project/quotagauge/).

```console
uv tool uninstall headroom-cli
uv tool install quotagauge
quotagauge init --all
```

## Why

The name collided with [Headroom](https://github.com/chopratejas/headroom), an
actively developed context compression layer for LLM agents that targets Claude
Code, Codex, Cursor and Aider. Same problem space, same clients, and far better
known. Publishing under a colliding name would have left two projects with the
same name aimed at the same users.

## What carries over

`quotagauge init --all` rewrites the Claude Code statusline and hook commands.
Remove any leftover `headroom` entries from `~/.claude/settings.json`, because
the old command no longer exists.

Accumulated state moves on first run. If `~/.quotagauge` is absent and
`~/.headroom` is present, the directory is renamed rather than copied, so
history carries over intact. If the rename cannot complete, the legacy
directory is used in place. Nothing is deleted on any path.

Every `HEADROOM_*` environment variable is now `QUOTAGAUGE_*`.

## Versions

0.1.4 is the final release under this name and changes nothing except this
notice. 0.1.0 through 0.1.3 remain installable but receive no further work.

Source, issues and releases: https://github.com/ajillepalli/quotagauge
