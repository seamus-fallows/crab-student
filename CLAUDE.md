# crab-student

Rents Vast.ai machines and puts Claude Code agents on them. `README.md` is for its users; `SPEC.md` holds the
safety invariants, the contracts between the parts, and the flows that spend money, keep work and carry secrets.

## Commands

```bash
uv run pytest -q                      # all tests (the integration test needs Linux and tmux: CI runs it)
uv run pytest -q tests/test_launch.py # one file
uv run ruff check src tests && uv run ruff format --check src tests
npx --yes markdownlint-cli2 "**/*.md" "#node_modules" "#.venv"   # the Markdown (.markdownlint.json)
```

## Design principle

A tool for its owner and one or two collaborators, who want it simple and fast.

- Prefer the simplest thing that works. Handle a rare failure with a clear error that says what to do, not
  with new state or a recovery path across commands.
- No fallbacks for situations that cannot occur, no backward-compatibility code (the user deletes old agents
  after a record change).
- Trust the files the tool wrote itself; validate external input (Vast, GitHub, the user's files and flags).

## Layout

- `src/crab_student/`: the local side (Python 3.11+, Windows first). No background service: each command runs
  and exits. `cli.py` prints the commands' results; the interactive flows (`init`, `open`, following a launch)
  print as they go, and `launch.Progress` prints progress. `vast.py` is the only code that calls Vast.
- Which machine to rent is the launcher's choice (`data/launch.md`, served by `crab-student guide launch`), not
  code. `offers.py` only applies fixed rules and filters.
- `src/crab_student/remote/`: the machine side, uploaded flat to `/root/.machine/` (`machine.REMOTE_DIR`, which
  the onstart command uses; `D` in cs.py). Standard library only, Python 3.11+ like the rest, LF line endings.
  The watchdog never dies of an exception.

## Conventions

- Exit codes: 0 ok, 1 error, 2 usage, 3 needs approval or would lose work, 4 no machine fits or the offers
  given are gone or all failed, 5 setup failed. Raise `core.CrabError(message, code)`; the message says what
  to do, with the exact command.
- Terminal output is ASCII (`core.say`). `--json` prints exactly one JSON value on stdout; progress and
  `error: ...` go to stderr.
- Comments only for why; docstrings only where the name does not say it all; no speculative options.
- Paths are computed at call time from `Path.home()` (tests swap the home).
- Take an agent's lock (`core.locked(name)`) once per command: a second lock on the same name in one process
  waits on itself.
- Say "agent", never "worker".

## Tests

- `tests/conftest.py` isolates every test (temporary home, in-memory keyring, no network, no windows or VS
  Code) and provides fakes for Vast and GitHub. Tests never touch the real network, home, `~/.ssh`,
  `~/.claude` or keyring.
- Test behaviour (records, calls on the fakes, files, exit codes, JSON), never wording or call order.
- The machine side is tested by importing `cs.py` with `CS_ROOT` (and `CS_BIN`, `CS_PROFILE_D`, `CS_VENV`)
  pointing at a temporary folder.
- A change to renting or the machine side needs one real run: the cheapest offer, `--fake-claude`,
  `--no-repo`, then delete the agent. It costs money: ask the user first. VS Code on a machine (the token,
  the pre-filled prompt, `/handoff`) can only be checked this way.

## Safety rules

IMPORTANT: read SPEC section 1 before changing renting, stopping, `delete`, secrets, GitHub access or the
permission rules.

- Never add a flag, default or retry that rents more, or rents above `approve_above_usd_per_hour` without
  `approve`. The launcher is never told the price; the CLI enforces it.
- A rent request is never retried; its label is saved before the request, and an unknown outcome is resolved
  only by finding that label.
- Act only on instance ids in the records or our exact rent labels (the Vast account may be shared).
- Code destroys a machine by itself only before its upload or after a failed setup.
- Secrets never go on command lines, into logs, the brief, records or git.
- crab-student never writes to the user's repo; agents can write only to their agent repo.
- The remote agent never sees the word "crab-student" in its rules, prompts or command output.

## Easy to forget

- Update `data/launch.md` and `data/SKILL.md` with the commands they describe. The guide never shows the
  approval price, so the launcher picks for the task.
- `init` must be re-run after the skill or permission rules (`setup.ALLOW`, `ASK`, `DENY`) change.
- Changes to `remote/` reach only agents launched afterwards.
- Model, effort and fast mode reach the machine as `claude_settings` in its `~/.claude/settings.json`, never
  as `claude` flags.
- `launch`, `start` and `approve` run as background Bash calls: setup can outlast a 10-minute Bash call.
- `crab-student open` without `--vscode` is interactive: never run it. `open --vscode` is fine.
- Agents can't use Remote Control (it refuses their long-lived token); the user steers through their own
  session's.
