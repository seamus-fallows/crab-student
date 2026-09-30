# crab-student - notes for Claude Code

crab-student runs Claude Code workers on rented Vast.ai machines. The user's Claude Code starts a
background launcher (it follows `crab-student guide launch`), which estimates the task and runs
`crab-student launch`: the CLI picks a machine, rents it, puts the worker's branch on GitHub, uploads
the machine's files and sets it up in one ssh call. On the machine a watchdog keeps the worker's Claude
Code alive in tmux, enforces the idle and time limits, saves work and stops the machine. The user
watches with `crab-student open` (tmux attach over ssh).

- `README.md` is for users. `SPEC.md` is the protocol reference: records, machine files, the ssh
  commands, the onstart and dead-man contract, the stop rules and the safety invariants. Update it
  when a protocol or an invariant changes. `docs/REQUIREMENTS.md` says what the tool is for.

## Layout

Local side (`src/crab_student/`, Python 3.11+, Windows first, also macOS and Linux). No background
service: each command runs and exits.

| Module | Owns |
|---|---|
| `cli.py` | The parser, every command's handler and all terminal output (tables, status, `list --watch`) |
| `core.py` | `CrabError`, paths, settings, worker records and their OS locks, secrets (keyring, file fallback), `run`, formatting |
| `vast.py` | The Vast REST client (urllib). The only code that calls Vast from the user's computer |
| `offers.py` | Requirements, the search, the picking rule (`rank`, `fallbacks`), the budget (`plan`) |
| `github.py` | `gh` and git: the user's repo, the snapshot commit, deploy keys, branches, reports, `pull` |
| `machine.py` | ssh: the generated ssh config, one-shot `cs.py` commands, the upload and setup, `open` / `open --all` / VS Code |
| `launch.py` | `launch`, the approval gate, `bring_up` (rent, boot, repo, upload, setup), `approve`, `decline`, `start`, `extend` |
| `workers.py` | Views for `list` and `status` (display states), `stop`, `delete` (the safety check), `send`, `report` |
| `setup.py` | `init`, the skill and permission rules in `~/.claude`, `check_ready`, `guide` |
| `data/SKILL.md`, `data/launch.md` | The skill `init` installs, and the launcher's procedure (`guide launch`) |

Machine side (`src/crab_student/remote/`, uploaded flat to `/root/crab-student/`; Python 3.8+ standard
library, runs as root): `cs.py` (setup, the watchdog, Claude Code in tmux, and the commands the local
side runs), `fake_claude.py` (a stand-in for `claude`, for `--fake-claude`), `worker.md` (the worker's
rules, which become `~/.claude/CLAUDE.md`).

## Tests

```
uv run pytest -q                      # everything (the e2e test needs Docker and runs on Linux CI)
uv run pytest -q tests/test_launch.py # one file while you work
uv run ruff check src tests && uv run ruff format --check src tests
```

- Tests never touch the network, Vast, GitHub, the real home, `~/.ssh`, `~/.claude` or the keyring.
  `tests/conftest.py`: a temporary home, an in-memory keyring and no network for every test (autouse),
  plus `fake_vast` (a real HTTP server, `tests/fake_vast.py`), `fake_github` (bare repos and an
  in-process `gh`, `tests/fake_gh.py`), `user_repo`, `machines` (the local side's view of machines) and
  `ready` (what `init` leaves).
- Test behaviour (records, calls on the fakes, files, exit codes, JSON), never wording or call order.
- The machine side is tested by importing `cs.py` with `CS_ROOT` pointing at a temporary folder; parts
  that need Linux (fcntl, tmux, pgrep) are skipped elsewhere.
- First real run of a change that touches renting or the machine side: the cheapest offer,
  `--fake-claude`, a private test repo or `--no-repo`, and delete the worker afterwards.

## Conventions

- Exit codes: 0 ok, 1 error, 2 usage, 3 needs approval / refused by a limit / would lose work, 4 no
  machine fits, 5 setup failed. Raise `core.CrabError(message, code)`; the message says what happened
  and what to do, with the exact command.
- Terminal output is ASCII. `--json` prints exactly one JSON value on stdout; progress goes to stderr;
  errors go to stderr as `error: ...`.
- Style: short functions, type hints, docstrings only where the name does not say it all, comments
  only for why, no dead code, no speculative options. ruff (line length 110), `ruff format`.
- Paths are computed at call time from `Path.home()`. Records change under `core.locked(name)`: take a
  worker's lock once per command (a second lock on the same name in one process would wait on itself).
- Machine side: standard library only, Python 3.8 syntax (no `X | Y` at runtime, no dict `|`, no
  `removeprefix`, no `zip(strict=)`), LF line endings. The watchdog never dies of an exception.

## Safety invariants (keep them)

- **Money.** Every rent and every start of a stopped worker passes the approval gate
  (`auto_approve_usd`, `max_running_machines`); `extend` passes the budget part. Only `approve` skips it,
  and it is an ask-rule command. Never add a flag, default or retry that rents more.
- **Never rent twice.** A rent request is never retried; the label is saved in the record before the
  request; an unknown outcome is resolved only by finding that exact label.
- **Own machines only.** Act only on instance ids in the records (or an exact label of ours).
- **Never destroy work.** Code destroys a machine by itself only before its upload, or after its setup
  failed. `delete` checks for unsaved work first; a machine that cannot be checked needs `--force`.
- **Stops are bounded.** The watchdog stops the machine at the time limit and when idle, always saving
  first; the dead-man switch in the onstart command stops it if the watchdog never runs or dies.
- **Secrets** never go on command lines, into logs, the brief, records or git. They reach the machine
  only inside the upload stream, as 0600 files.
- **The launcher** can only run `setup.ALLOW` commands; anything that spends beyond the gate, destroys
  or changes settings is in `setup.ASK`.
