# Contributing

Thanks for helping improve quotagauge. Keep changes small, explain the user-facing effect, and include tests for changed behavior.

## Set up a checkout

Clone this repository or your fork into a directory named `quotagauge`. Enter that directory before running project commands. The project needs Python 3.9 or newer and no third-party packages.

Run the full suite before making a change:

```console
python -m unittest discover -s tests -t .
```

Run the same command after the change. Tests must not use the network or depend on real Claude payloads, Codex sessions, or user state. Put fixtures in the tests and use temporary directories.

## Keep the runtime standard-library only

Runtime and test code must use the Python standard library. Do not add package dependencies.

The statusline and prompt hook run in a user's interactive coding loop. A dependency install, environment mismatch, or slow import can break that loop. Standard-library code keeps setup small and makes the hook work with the Python executable already selected by `install.py`.

## Preserve the data rules

- Treat stale pre-reset usage as a lower bound, never an exact current value.
- Treat a reading after its absolute reset as `post_reset` and `ok`.
- Parse source payloads defensively. Unknown shapes must not break the statusline.
- Keep state replacement atomic and history append-only.
- Keep public functions typed.

Add focused tests for parser variants, time boundaries, severity boundaries, persistence failures, and hook output.

## Keep prompt output quiet

The hook must print nothing when every severity is `ok`. Hook text enters the model context on every prompt. Unneeded text costs tokens each time and defeats the purpose of the project.

The Claude statusline has a different contract. It must always print one line and exit successfully, even when its input is empty or malformed.

## Write in the project style

Use sentence-case headings, direct prose, short sentences, and concrete names. Do not use em dashes. Comments should explain why a choice exists rather than repeat the code.

## Submit a change

Open a pull request with:

- the problem and the chosen behavior;
- tests that prove the behavior;
- any user-facing output changes;
- confirmation that the full test command passes.

Keep unrelated cleanup out of the same change.
