# crab-student - notes for Claude Code

crab-student rents Vast.ai machines and puts Claude Code agents on them. The user's Claude Code starts
a background launcher (a fork of the session that follows `crab-student guide launch`), which estimates
the task and runs `crab-student launch` (`launch --batch` for several agents at once): the CLI picks a
machine, rents it, puts the agent's branch on GitHub, uploads the machine's files and sets it up in one
ssh call. There are three modes: `autonomous` and `colleague` run the agent's Claude Code in tmux on the
machine (the first never asks, the second asks about important choices); `interactive` gives the user a
plain Claude Code session in VS Code on the machine, which `/handoff` later moves into tmux. On the
machine a watchdog keeps a tmux agent alive and, after `idle_minutes` without CPU, GPU or conversation
activity (there is no time limit), saves the work and stops the machine. The user watches with
`crab-student open` (tmux attach over ssh) or `open --vscode`.

- `README.md` is for users. `SPEC.md` is the protocol reference: records, machine files, the ssh
  commands, the modes and the hand-off, the onstart and dead-man contract, the stop rules and the safety
  invariants. Update it when a protocol or an invariant changes. `docs/REQUIREMENTS.md` says what the
  tool is for.

## Layout

Local side (`src/crab_student/`, Python 3.11+, Windows first, also macOS and Linux). No background
service: each command runs and exits.

| Module | Owns |
|---|---|
| `cli.py` | The parser, every command's handler and all terminal output (tables, status), reading the `=== NAME [flags]` text of `launch --batch` |
| `core.py` | `CrabError`, paths, settings (mode, model, effort, fast mode, ...) and the user's own Claude settings they default to, agent records and their OS locks, secrets (keyring, file fallback), `run`, formatting |
| `vast.py` | The Vast REST client (urllib). The only code that calls Vast from the user's computer |
| `offers.py` | Requirements, the search, the picking rule (`rank`, `fallbacks`), the plan under the approval price (`plan`), the host lease check |
| `github.py` | `gh` and git: the user's repo, the snapshot commit, deploy keys, branches, files read from a branch (the report, the stop note), `pull` |
| `machine.py` | ssh: the generated ssh config, one-shot `cs.py` commands, the upload and setup, `open` / `open --all` / VS Code (with the Claude panel pre-filled for an interactive agent) |
| `launch.py` | `launch` and `launch_batch` (agents brought up side by side), the approval rule, `bring_up` (rent, boot, repo, upload, setup), `approve` (several names), `decline`, `start` |
| `agents.py` | Views for `list` and `status` (display states, mode, frontend, the mode sync after a hand-off), `stop`, `delete` (the safety check: the machine's own `check`, or the stop note of a stopped one), `send`, `report` |
| `setup.py` | `init` (including VS Code's `remote.SSH.defaultExtensions`), the skill and permission rules in `~/.claude`, the check of Claude Code's version (forks), `check_ready`, `guide` |
| `data/SKILL.md`, `data/launch.md` | The skill `init` installs, and the launcher's procedure (`guide launch`, which fills in the approval price) |

Machine side (`src/crab_student/remote/`, uploaded flat to `/root/crab-student/`; Python 3.8+ standard
library, runs as root): `cs.py` (setup, the watchdog, Claude Code in tmux, the hand-off `agent-handoff`,
and the commands the local side runs), `fake_claude.py` (a stand-in for `claude`, for `--fake-claude`),
`rules.md` with `rules-autonomous.md` and `rules-colleague.md` (the agent's rules and its mode's
section, which become `~/.claude/CLAUDE.md`; an interactive agent has none) and `handoff.md` (the
`/handoff` command an interactive agent gets).

## Tests

```
uv run pytest -q                      # everything (the integration test needs Linux and tmux: CI runs it)
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
  `--fake-claude`, a private test repo or `--no-repo`, and delete the agent afterwards. What VS Code and
  its Claude extension do on a machine (the token, the pre-filled prompt, the terminal attaching to
  tmux, `/handoff`) can only be checked on a real machine.

## Conventions

- Exit codes: 0 ok, 1 error, 2 usage, 3 needs approval / refused by a limit / would lose work, 4 no
  machine fits, 5 setup failed. Raise `core.CrabError(message, code)`; the message says what happened
  and what to do, with the exact command.
- Terminal output is ASCII. `--json` prints exactly one JSON value on stdout; progress goes to stderr;
  errors go to stderr as `error: ...`.
- Style: short functions, type hints, docstrings only where the name does not say it all, comments
  only for why, no dead code, no speculative options. ruff (line length 110), `ruff format`.
- Paths are computed at call time from `Path.home()`. Records change under `core.locked(name)`: take an
  agent's lock once per command (a second lock on the same name in one process would wait on itself).
- A batch launch brings each agent up in its own thread (progress lines prefixed `[name]`, one failure
  not stopping the others): whatever the threads share, like the ssh config and known_hosts, is written
  under a lock in `machine.py`.
- Machine side: standard library only, Python 3.8 syntax (no `X | Y` at runtime, no dict `|`, no
  `removeprefix`, no `zip(strict=)`), LF line endings. The watchdog never dies of an exception.
- Nothing the remote agent sees may name crab-student: its rules, first prompt, the watchdog's messages
  (which start with `Automated message from the machine:`), hook messages and command output describe
  the situation plainly. The machine's commands are `agent-status`, `agent-attach` and `agent-handoff`.
- "Agent", not "worker", in text, identifiers and file names. There is no backward-compatibility code
  for records made before a rename: users delete old ones.

## Safety invariants (keep them)

- **Money.** Every launch passes the price check: it rents without asking only a machine that costs
  `approve_above_usd_per_hour` or less, else it waits (`pending`, exit 3); a failed launch tried again
  with `start` is planned again under the same check. Only `approve` skips it, and it is an ask-rule
  command. Restarting a stopped agent has no gate, and there is no time limit and no cost estimate: the
  approval, the idle stop and the dead-man switch bound the bill. Never add a flag, default or retry
  that rents more, or rents above the price without `approve`.
- **Never rent twice.** A rent request is never retried; the label is saved in the record before the
  request; an unknown outcome is resolved only by finding that exact label.
- **Own machines only.** Act only on instance ids in the records (or an exact label of ours).
- **Never destroy work.** Code destroys a machine by itself only before its upload, or after its setup
  failed. `delete` checks for unsaved work first (a running machine itself; a stopped one from the stop
  note of its latest stop); a machine that cannot be checked needs `--force`, or starting first if it is
  stopped.
- **Stops are bounded.** In every mode the watchdog stops the machine after `idle_minutes` without
  activity (conversation activity counts wherever the conversation is, tmux or VS Code), done or not,
  always saving first (a final save also writes the stop note `delete` reads); the dead-man switch in
  the onstart command stops it if the watchdog never runs or dies.
- **Secrets** never go on command lines, into logs, the brief, records or git. They reach the machine
  only inside the upload stream, as 0600 files (setup also copies the token and task secrets into the
  machine's 0600 `~/.claude/settings.json`, for VS Code's Claude).
- **The launcher** (a background fork, or a general-purpose background agent when forks are missing)
  cannot ask the user anything, and a command outside `setup.ALLOW` would raise a permission prompt in
  the user's main session. Anything that approves a pricier machine, destroys or changes settings is in
  `setup.ASK`.
- **Modes.** The safety parts (branch-only pushes, the secret and big-file hooks, the autosave before
  every stop, the idle stop, the dead-man switch) hold in every mode, and all modes run Claude Code with
  `--dangerously-skip-permissions`. A hand-off must never leave two Claude sessions on one conversation:
  the tmux agent runs a fork of the VS Code session, under a new session id.

## Easy to forget

- `crab-student guide launch` fills `{{approve_above_usd_per_hour}}` in the guide with the current
  setting; the skill and README name the setting without a number. Change the code, the guide and the
  skill together when the launch's result line, flags or modes change.
- The model, effort and fast mode reach the machine as `claude_settings` in its `~/.claude/settings.json`
  (never as `claude` flags); unset keys come from the user's own `~/.claude/settings.json`. Claude Code
  keeps the effort picked with `/effort` per model (`modelSettings.<model>.effortLevel`; a top-level
  `effortLevel` there is only a default), so those per-model levels come along unless an effort is
  chosen.
- The machine folder stays `/root/crab-student` (the onstart command and the dead-man switch name it);
  the agent reads its brief at `/root/BRIEF.md` and never needs the folder.
- Agents don't use Remote Control: it refuses the long-lived token they sign in with, and a full login on
  a rented machine is not worth the risk. The user steers agents from their phone through their own
  Claude Code session's Remote Control, which runs crab-student commands (README).
- `crab-student open` (without `--vscode`) is interactive: the user runs it in their own terminal, Claude
  never does. `open --vscode` only starts VS Code, so Claude may run it for an interactive agent.
- `init` needs re-running after the skill or the permission rules change.
- Changes to `remote/` reach only agents launched afterwards (the files are uploaded at launch).
