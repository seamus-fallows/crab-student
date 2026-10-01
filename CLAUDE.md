# crab-student - notes for Claude Code

crab-student rents Vast.ai machines and puts Claude Code agents on them. The user's Claude Code starts
a background launcher (a fork of the session that follows `crab-student guide launch`), which estimates
the task and runs `crab-student launch NAME` in a background Bash call (one per agent, in parallel):
the CLI rents two machines at once and keeps the first that starts, puts the agent's branch on GitHub,
then uploads the machine's files and sets it up (dependencies included) in one ssh call. Modes:
`autonomous` and `colleague` run the agent's Claude Code in tmux (the first never asks, the second asks
about important choices); `interactive` gives the user a Claude Code session in VS Code on the machine,
which `/handoff` later moves into tmux. On the machine a watchdog keeps a tmux agent alive and, after
`idle_minutes` without CPU, GPU or conversation activity, saves the work and stops the machine. A launch
opens a window by itself (`crab-student open NAME`) that shows its progress (also in `agents/NAME.log`)
and then becomes the agent's session.

`README.md` is for the user. `SPEC.md` is the protocol reference: records, machine files, the ssh
commands, the modes and the hand-off, the onstart and dead-man contract, the stop rules and the safety
invariants in full.

## Design principle

A single-user tool, used only by its owner, who wants it simple and fast.

- Prefer the simplest thing that works.
- Handle a rare failure with a clear error message that says what to do, not with new state or a
  recovery path that spans commands.
- No fallbacks for situations that cannot occur, and no backward-compatibility code (after a change to
  the records, the user deletes old agents).
- Trust the files the tool wrote itself (records, config, the machine's own files); validate external
  input (Vast, GitHub, the user's files and flags).

## Layout

Local side (`src/crab_student/`, Python 3.11+, Windows first, also macOS and Linux). No background
service: each command runs and exits.

| Module | Owns |
|---|---|
| `cli.py` | The parser, every command's handler and all terminal output (tables, status, `list`'s warnings about orphan machines) |
| `core.py` | `CrabError`, paths, settings (mode, model, effort, fast mode, ...) and the user's own Claude settings they default to, agent records and their OS locks, secrets (keyring, file fallback), VS Code's user settings file, `run`, formatting |
| `vast.py` | The Vast REST client (urllib). The only code that calls Vast from the user's computer |
| `offers.py` | Requirements, the search, the picking rule (`rank`, `fallbacks`; an interactive agent prefers hosts near `home`), the plan under the approval price (`plan`), the host lease check, the countries where Claude is unavailable (`BLOCKED`) |
| `github.py` | `gh` and git: the user's repo, the snapshot commit, the deploy key a repo's agents share, branches, files read from a branch (the report, the stop note), `pull` |
| `machine.py` | ssh: the generated ssh config, one-shot `cs.py` commands, the upload and setup, `open` / `open --all` / VS Code (the Claude panel pre-filled for an interactive agent 15 s after VS Code opens; polling for its hand-off), the window that follows a launch, the local VS Code's commit for the machine |
| `launch.py` | `launch`, the approval rule, `bring_up` (the race of two machines, repo, upload, setup; a launch cut off is resumed or planned afresh), the progress lines and the launch log, `approve`, `decline`, `start` |
| `agents.py` | Views for `list` and `status` (display states, mode, frontend, the mode sync after a hand-off, orphan machines), `stop`, `delete` (the safety check: the machine's own `check`, or the stop note on a stopped one's branch), `send`, `report`, `open` (following a launch; an interactive agent: VS Code, then its terminal after the hand-off) |
| `setup.py` | `init`, the skill and permission rules in `~/.claude` (`ALLOW`, `ASK`, `DENY`), `check_ready`, `guide` |
| `data/SKILL.md`, `data/launch.md` | The skill `init` installs, and the launcher's procedure (`guide launch`, which fills in the approval price) |

Machine side (`src/crab_student/remote/`, uploaded flat to `/root/crab-student/`; Python 3.8+ standard
library, runs as root): `cs.py` (setup, including the dependency install and VS Code's server for an
interactive agent, the watchdog, Claude Code in tmux, the hand-off `agent-handoff`, and the commands the
local side runs), `fake_claude.py` (a stand-in for `claude`, uploaded only for `--fake-claude`),
`rules.md` with `rules-autonomous.md` and `rules-colleague.md` (the agent's rules and its mode's
section, which become `~/.claude/CLAUDE.md`; an interactive agent has none) and `handoff.md` (the
`/handoff` command an interactive agent gets; setup moves it into `~/.claude/commands/`, as it moves the
brief to `/root/BRIEF.md`).

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
  `ready` (what `init` leaves). The autouse `windows` records the launch windows instead of opening
  them, and conftest hides the `code` command, so tests never open windows or VS Code.
- Test behaviour (records, calls on the fakes, files, exit codes, JSON), never wording or call order.
- The machine side is tested by importing `cs.py` with `CS_ROOT` (and `CS_BIN`, `CS_PROFILE_D`,
  `CS_VENV`) pointing at a temporary folder; parts that need Linux (fcntl, tmux, pgrep, bash) are
  skipped elsewhere.
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
  Several launches may run at once as separate processes; each holds its own agent's lock.
- Machine side: standard library only, Python 3.8 syntax (no `X | Y` at runtime, no dict `|`, no
  `removeprefix`, no `zip(strict=)`), LF line endings. The watchdog never dies of an exception.
- "Agent", not "worker", in text, identifiers and file names.

## Safety rules (SPEC section 1 has the detail)

- **Money**: a launch rents without asking only at or under `approve_above_usd_per_hour`, else it waits
  (`pending`, exit 3); only `approve`, an ask-rule command, goes over. Never add a flag, default or
  retry that rents more, or above the price without `approve`.
- **Never rent twice**: a rent request is never retried; its label is saved in `rents` before the
  request, and an unknown outcome is resolved only by finding that label. At most two rents race.
- **Own machines only**: act only on instance ids in the records, or our exact rent labels.
- **Never destroy work**: code destroys a machine by itself only before its upload or after a failed
  setup. `delete` checks for unsaved work first, else needs `--force`.
- **Stops are bounded**: the watchdog's idle stop (every mode, done or not) and the dead-man switch in
  the onstart command; every stop saves first.
- **Secrets** never go on command lines, into logs, the brief, records or git; they reach the machine
  only inside the upload stream, as 0600 files.
- **Branch-only pushes** and the secret and big-file hooks hold in every mode.
- **The launcher** runs only `setup.ALLOW` commands; whatever approves, destroys or changes settings is
  in `setup.ASK`.
- **The remote agent never hears "crab-student"**: what it sees (rules, prompts, the watchdog's
  `Automated message from the machine:` messages, hook and command output) describes things plainly.
- **A hand-off** never leaves two Claude sessions on one conversation: tmux runs a fork under a new
  session id.

## Easy to forget

- Change `data/launch.md` (the launcher's guide) and `data/SKILL.md` with the commands they describe:
  the launch's flags, result line and modes. `crab-student guide launch` fills
  `{{approve_above_usd_per_hour}}` in the guide with the current setting; the skill and README name the
  setting without a number.
- The model, effort and fast mode reach the machine as `claude_settings` in its `~/.claude/settings.json`
  (never as `claude` flags); unset keys come from the user's own `~/.claude/settings.json`, including
  the per-model efforts Claude Code keeps (`modelSettings.<model>.effortLevel`) unless an effort is
  chosen.
- Setup installs the repo's dependencies (step 3 of 6) and waits for it, up to 40 minutes; a failed
  install is a warning in the launch's result and a line in the agent's rules, never a failed setup. So
  the launcher runs `launch`, `start` and `approve` as background Bash calls (a Bash call stops at 10
  minutes). VS Code's server (interactive agents) is a setup job whose failure is only a warning.
- The machine folder stays `/root/crab-student` (the onstart command and the dead-man switch name it);
  the agent reads its brief at `/root/BRIEF.md` and never needs the folder.
- Agents don't use Remote Control (it refuses their long-lived token); the user steers agents from their
  phone through their own session's Remote Control, which runs crab-student commands.
- `crab-student open` (without `--vscode`) is interactive: the user runs it in their own terminal, Claude
  never does (a launch opens a window running it by itself, unless `open_window` is off). `open --vscode`
  only starts VS Code (for an interactive agent it waits a fixed 15 seconds before filling in the Claude
  panel), so Claude may run it.
- `init` needs re-running after the skill or the permission rules change.
- Changes to `remote/` reach only agents launched afterwards (the files are uploaded at launch).
