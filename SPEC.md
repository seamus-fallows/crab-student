# crab-student: protocols and invariants

What is stored where, what crosses ssh, how machines start and stop, and the rules that must hold. How
the code does it is in the modules named here.

## 1. Safety invariants

1. **Money.** A launch rents without asking only a machine that costs `approve_above_usd_per_hour` or
   less; otherwise it records `pending` and exits 3. Only `approve`, an ask-rule command, launches it,
   at up to the price it showed. A failed launch tried again with `start` is planned again up to the
   record's `max_dph` or the current setting, whichever is higher. Starting a stopped agent has no
   gate. No time limit, no cost estimate: the approval, the idle stop and the dead-man switch bound the
   bill.
2. **Never rent twice.** A rent request is never retried. Its label is saved in `rents` before the
   request; an unknown outcome (timeout, connection error, 5xx) is resolved only by finding that exact
   label. The race (4) has at most two rents in flight, both within the price, and destroys every
   machine but the one kept before anything is uploaded to it.
3. **Own machines only.** Act only on instance ids in the records, or on an instance with the exact
   label of a record's rent. `list` points out other instances labelled `crab-student-...`, never
   touching them.
4. **Never destroy work.** Code destroys a machine by itself only before its upload (`uploaded` false)
   or right after its setup failed. `start` on a stopped agent never destroys. `delete` checks for
   unsaved work first (5.3).
5. **Stops are bounded.** In every mode the watchdog stops the machine after `idle_minutes` without
   activity, done or not, always saving first. The dead-man switch stops it if the watchdog never runs
   or dies; setup refuses a machine with no key to stop itself; `list`/`status` stop a machine whose own
   stop call Vast has not taken after 5 minutes. Nothing on the machine destroys it.
6. **Secrets** (the Claude token, deploy keys, task secrets) never appear on command lines, in logs, the
   brief, records or git. They reach the machine only inside the upload stream, as 0600 files (and in
   its 0600 `~/.claude/settings.json`).
7. **The launcher** cannot ask the user anything, and a command outside `setup.ALLOW` would raise a
   permission prompt in the user's session. Whatever approves a pricier machine, destroys, discards or
   changes settings is in `setup.ASK`; editing `~/.crab-student` is denied (`setup.DENY`).
8. **The remote agent never hears "crab-student".** Its rules, prompts, the watchdog's messages, hook
   and command output describe the situation plainly; its commands are `agent-status`, `agent-attach`
   and `agent-handoff`.
9. **Modes.** Branch-only pushes, the hooks, the save before every stop, the idle stop and the dead-man
   switch hold in every mode, and every mode runs Claude Code without permission prompts. A hand-off
   never leaves two Claude sessions on one conversation (6.6).

## 2. Local files

```
~/.crab-student/
  config.json            settings that differ from the defaults (README lists them)
  secrets.json           {"names": [...]}: names of stored task secrets
  secrets/<key>          0600, only when no system keyring works
  agents/<name>.json     the record (3); agents/<name>.lock its OS lock
  agents/<name>.brief.md the brief
  agents/<name>.log      the launch's progress lines with the local time ("~ " marks a status line)
  agents/_keys-<owner>--<repo>.lock   the repo's deploy key lock (4.2)
  keys/<owner>--<repo>-<8 hex>(.pub)  the repo's deploy key (4.2)
  ssh/id_ed25519(.pub)   the key added to machines
  ssh/config             generated Host entries (crab-student-<name>), included from ~/.ssh/config
  ssh/known_hosts        machines' host keys, under HostKeyAlias crab-student-<name>
  ssh/known_hosts_racing throwaway host keys of machines racing for a launch
  ssh/github_known_hosts GitHub's pinned ed25519 host key
~/.config/vastai/vast_api_key   the Vast key (the vastai CLI's file)
~/.claude/settings.json         read for the model, effort and fast mode (4.4); init adds permission rules
~/.claude/skills/crab-student/SKILL.md   the skill
VS Code's user settings.json    remote.SSH.remotePlatform "linux" for agents' hosts (only if plain JSON)
```

Secrets live in the OS keyring, service `crab-student`: `claude-oauth-token` and `task-secret:<NAME>`,
falling back to `~/.crab-student/secrets/` when the keyring fails or drops values.

## 3. The agent record

One JSON file per agent, written only by the command holding the agent's lock. `phase`:

| phase | meaning |
|---|---|
| `pending` | waiting for approval: nothing rented, nothing on GitHub |
| `renting` | machines racing (`rents`) |
| `booting` | the machine kept (`instance_id`, its rent in `rent`) answers ssh; the upload comes next |
| `setup` | `uploaded` is true: the upload and setup are under way, or were cut off |
| `ready` | the launch finished; whether the machine runs comes from Vast |
| `failed` | `error` says why; it holds no machine |

Other fields: `req` (hours, gpus, gpu_ram_gb, gpu_models, cpus, ram_gb, disk_gb, min_down_mbps);
`mode` (follows the machine after a hand-off); `claude_settings`; `max_dph` (the most it may rent at
without asking again); `fake_claude`; `setup` (a command, `none`, or null to detect); `secrets` (names);
`session_id`; `repo` (slug, workspace, branch, start_sha, local_dir, carried, untracked, deploy_key
`{id, file, created}`, branch_created, dir on the machine); `offers` (the pick and up to four backups);
`failed_offers`; `rents` (while racing: `{label, offer_id, at, instance_id (null while unconfirmed),
running_at}`); `rent`, `instance_id`, `dph`, `disk_usd_day`, `machine`, `short`, `ssh`; `uploaded`;
`pending` (`{reason, max_dph}`); `warnings`; `last_start` (last rented or started, or seen running again
after a stop); `stopped_since`; `last_seen` (the last machine status, for display only); `image`,
`idle_minutes`, `notify_url` (copied at launch).

Names: 3-40 characters of a-z, 0-9 and "-". Branch `agent/<name>`; ssh alias `crab-student-<name>`;
rent label `crab-student-<name>-<8 hex>` (new per rent request); deploy key title
`crab-student <created>`; tmux session `agent` on the machine.

## 4. The launch (`launch.py`)

1. **Plan** (writes nothing): name, brief, secrets, mode and Claude's settings (4.4), repo (4.1),
   offers (4.3). `--dry-run` stops here.
2. **Record**, under the agent's lock: `pending` (exit 3) when no machine that fits is within the
   price, else `renting`.
3. **The race** (`Race`): two offers are rented at once and the first machine running and answering ssh
   is kept (`booting`), the others destroyed first; a machine that fails or does not start in time is
   destroyed and the next offer rented in its place. An unconfirmed rent keeps its slot while it is
   looked for by label: one not found when another machine wins becomes a warning, and when only such
   rents are left for 2 minutes the launch fails. Every offer failed: exit 4. Meanwhile the deploy key
   (4.2) and branch go on GitHub; a failure there destroys the launch's machines and fails it.
4. **Upload and setup** in one ssh call (6.1), `uploaded` saved true first. Setup's own failure destroys
   the machine and fails the launch (exit 5); a lost connection leaves `setup`.
5. **`start` on an unfinished launch**: `setup` starts the machine if it stopped and repeats the upload
   and setup, which resumes; `renting`/`booting` with machines destroys them (nothing was uploaded) and
   plans afresh; `failed` plans afresh. Planning afresh has the same price check (1.1).
   `approve` plans again up to the price it showed (higher prices: it waits again), then races.
6. **Progress**: plain lines on stderr, also in `agents/<name>.log` (a failure as `Launch failed: ...`).
   Once the record exists and the launch goes ahead, a window runs `crab-student open NAME` (5.4) unless
   `open_window` is `off`: a tab in the Windows Terminal window named crab-student (`wt -w crab-student
   new-tab`), or a macOS Terminal window; elsewhere the command is printed.

Several agents are several `launch` processes, each with its own record, lock and race. The launcher
runs `launch`, `start` and `approve` as background Bash calls (setup can outlast 10 minutes).

**4.1 The repo.** Default: the current repo's GitHub remote, with admin rights (for the deploy key) and
no branch `agent/<name>` yet. The starting commit is HEAD plus, as one extra commit, what
`git commit -a` would commit, built from a copy of the index (the user's checkout is untouched);
untracked files are not carried (a warning names them). It is pushed from the user's computer with the agent's deploy key.
`--no-repo`: the private `<login>/<workspace_repo>` (created if missing), the branch made through the
API.

**4.2 Deploy keys.** GitHub emails a repo's admins about every deploy key added, so a repo's agents
share one: a new agent takes the key another agent on the repo holds, else a new ed25519 key is added
read-write. It is removed from GitHub and `keys/` when the last agent holding it is deleted. Taking and
releasing happen under the repo's key lock, with the record saved inside it.

**4.3 Offers** (`offers.py`). Defaults: 0 GPUs, 8 cores, 16 GB RAM, `disk_gb`, `min_download_mbps`,
`min_reliability`. `fits` checks every offer again, with GPU models, `regions`, the host's lease (at
least `max(2 x hours, 24) + 1` hours left) and `BLOCKED` (countries where Anthropic's API is
unavailable). `rank` drops GPU offers clearly worse in speed per dollar; among similar prices it prefers,
for an interactive agent, hosts near `home`, then reliability and download speed. `fallbacks`: up to
four backups on other machines. All come from the offers within the price when any fits, else from all
(the launch waits for approval; backups then cost at most 1.25x the pick).

**4.4 Mode, Claude's settings and the result.** The mode is `--mode`, else the `mode` setting. Each of
`model`, `effortLevel`, `fastMode` is the flag, else the setting, else the user's
`~/.claude/settings.json`; without a chosen effort, its per-model levels (`modelSettings`) come along.
The result is `claude_settings` in the record and the machine's config (never `claude` flags).
`--setup "COMMAND"` replaces the detected dependency install, `--setup none` skips it. The result line
(`--json`: `result_line`, `warnings`, `mode`, `exit_code` and more), which the skill relies on:

```
NAME: working on 1x RTX 4090 (Quebec, CA) at $0.42/hr - colleague - branch agent/NAME - watch: crab-student open NAME
NAME: ready on 1x RTX 4090 (Quebec, CA) at $0.42/hr - interactive - branch agent/NAME - open it: crab-student open NAME --vscode
NAME: NOT started - needs your approval: REASON - would rent SHORT at $X/hr - approve: crab-student approve NAME (or: crab-student decline NAME)
NAME (dry run): would rent SHORT at $X/hr - MODE - no approval needed
```

## 5. Agents once launched (`agents.py`)

**5.1 Views** (`list`, `status`): one Vast instance list, and one `cs.py status` per running ready agent
(in parallel). States: approval; starting; interrupted (a launch cut off: nobody holds its lock);
failed; unknown (Vast or ssh did not answer); stopped (why, how long ago, the disk's cost per day);
needs you; done; working; interactive (Claude in VS Code: active, or idle for how long). A `status`
reporting another mode (after a hand-off) updates the record; a stopped machine seen running again moves
`last_start`. `list` warns on stderr about orphans: instances labelled `crab-student-...` that are no
record's machine or rent.

**5.2 stop** (a machine that received its files): `cs.py save --reason user --final` if it runs (best
effort), then Vast `stopped`. **start** (stopped): Vast `running`, `last_start` set, a wait for ssh that
never destroys. The boot starts the watchdog, which resumes the conversation.

**5.3 delete.** Pending: removed. A machine that received its files is checked first: running, with
`cs.py check`; stopped, with the stop note on its branch (6.7), counted only if `stopped_at` is at least
5 minutes after `last_start`. No such note, GitHub unreachable, or a failed check: refused (exit 3)
without `--force`. Refused too (exit 3, with the list and a copy command) for unpushed commits,
uncommitted files, files in `/root/outputs`, or untracked files of 100 MB+ outside git. Then: destroy
the machine and any racing rents' machines, release the deploy key, delete the branch only if it still
points at `start_sha`, forget the host key and the record.

**5.4 open.** On an agent that is not ready, `open NAME` first follows the launch log until the phase is
ready, failed or pending, or nobody holds the lock (cut off). Frontend `tmux`: `agent-attach` over `ssh
-t`. Frontend `vscode`: VS Code opens as with `--vscode`; then, on a terminal, it asks the machine's
`status` every 5 s and attaches once the frontend is `tmux`. `open --all`: the running tmux agents side
by side (Windows Terminal panes, else a local tmux session with prefix Ctrl-a). `open NAME --vscode`:
`code --remote ssh-remote+crab-student-<name>` on the repo folder; for a `vscode` frontend, 15 s later,
`vscode://anthropic.claude-code/open?prompt=<the brief>` fills the Claude panel without sending (a
brief over 5,000 characters becomes a pointer to `/root/BRIEF.md`).

## 6. The machine (`remote/cs.py`)

`D=/root/crab-student` (the onstart command names it); the repo at `/root/work/<repo>`; big outputs in
`/root/outputs`; the brief at `/root/BRIEF.md` (the agent never needs `D`).

**6.1 Upload and setup.** One ssh command, `mkdir -p D && tar -xzf - -C D --no-same-owner && cd D &&
<python> cs.py setup`, with a tar.gz on stdin: the `remote/` files (`fake_claude.py` only for
`--fake-claude`), `config.json` (0600: name, branch, repo_url, repo_dir, mode, claude_settings,
fake_claude, session_id, idle_minutes, notify_url, secret_names, setup, vscode_commit), `BRIEF.md`,
`claude.env` and `secrets.env` (0600, `export K=<quoted>` lines), `deploy_key` (0600),
`github_known_hosts`. Setup prints `step i/6 <label>` lines, then `{"ok": true, "warnings": [...]}` or
`{"ok": false, "step", "label", "error"}`; once `setup-done` exists it only restarts the watchdog
(`{"ok": true, "resumed": true}`). A setup run again (after a lost connection) waits on `setup.lock` for
the first; each step touches `heartbeat`, so the dead-man switch waits for a long step. The steps:

1. `environment`: the login profile `/etc/profile.d/agent.sh`, also run from `~/.bashrc` (PATH with the
   repo's `.venv`, `~/.local/bin`, `/venv/main`; the token and secrets; shells start in the repo); the
   commands `agent-attach`, `agent-status`, `agent-handoff` (interactive); the git hooks and identity;
   `.venv/`, `*.egg-info/`, `__pycache__/` in git's global ignore; jobs installing Claude Code, tmux and,
   when `vscode_commit` is set, VS Code's server with the Claude extension.
2. `repository`: clone the branch with the deploy key; a push refspec for the branch only.
3. `dependencies`: `setup`, or the first that fits the repo: `uv.lock` or `[tool.uv]` -> `uv sync`
   (installing uv if missing); `[project]` in `pyproject.toml` -> `pip install -e .`;
   `requirements*.txt` -> `pip install -r` each; `setup.py` -> `pip install -e .`. Waited for (30
   minutes at most); `deps.json` keeps `{command, exit_code}`; a failure is a warning in setup's result,
   never a failed setup.
4. `agent settings`: the brief to `/root/BRIEF.md`; autonomous and colleague: `~/.claude/CLAUDE.md` from
   `rules.md`, the mode's section and how the install went; interactive: `handoff.md` to
   `~/.claude/commands/`; every mode: Claude Code's and VS Code's settings (6.5).
5. `Claude Code`: wait for the install jobs; one tiny `claude -p` checks the token.
6. `starting the agent`: refused without the container's Vast key; `setup-done`, the watchdog; for tmux,
   wait for the conversation file. Last, a wait for VS Code's server, whose failure is only a warning.

**6.2 onstart** (run by Vast as root at every container start; each command ends in `;` or `&`): write
`CONTAINER_ID`, `CONTAINER_API_KEY`, `CS_VAST_URL` to `vast.env` (0600) and the start time to `booted`;
start the dead-man loop; start the watchdog if `setup-done` exists. **Dead-man loop:** every 60 s, if
the newer of `booted` and `heartbeat`'s mtime is over 45 minutes old, call Vast stop with the
container's key.

**6.3 The watchdog** (`cs.py watch`, one per container by flock; `status` and `agent-attach` start it
if it is not running). Every minute: touch `heartbeat`; while the frontend is `tmux`, keep the tmux
session `agent` (running `cs.py claude`) alive; count `used_seconds`. Busy = the conversation files
under `~/.claude/projects` changed (tmux or VS Code alike), the container's CPU or a GPU is busy.
Colleague: a nudge after half of `idle_minutes` while it says `needs-you`; autonomous and colleague: a
check-in after a long silence while it says `working`. Typed messages start with `Automated message
from the machine:`. **Stop** after `idle_minutes` idle, done or not: set `stopping`, notify, final save
(6.7), record `stop_sent_at`, call Vast stop with the container's key, retried every tick; `stop_reason`
is `idle` or `done`. A new container start resets the clocks. Notifications go to `notify_url`.

**6.4 Modes.** The machine's mode starts as the launch's and changes only by hand-off (6.6).

| | autonomous | colleague | interactive |
|---|---|---|---|
| Claude runs | in tmux | in tmux | in VS Code's Claude panel, driven by the user |
| rules (`~/.claude/CLAUDE.md`) | base + autonomous | base + colleague | none |
| report, `agent-status` | yes; `needs-you` refused | yes | no report; `agent-status` only shows the machine |
| watchdog reminders | check-in | nudge, check-in | none |
| `status` `frontend` | `tmux` | `tmux` | `vscode`, then `tmux` after hand-off |

**6.5 Claude Code.** In tmux: `claude --dangerously-skip-permissions`, `--session-id <id> "<first
prompt>"` the first time and `--resume <id>` after; a resume that fails at once starts a new session
(`D/session_id`) told to read its report. Its environment has the token, the task secrets, `AGENT_NAME`,
`AGENT_BRANCH` and the login profile's PATH, and none of `CLAUDECODE`, `CLAUDE_CODE_*`, `CONTAINER_*`.
`~/.claude/settings.json` (0600, every mode): `bypassPermissions`, deny rules for the ways around the
hooks, `env` with the token and task secrets (for the Claude Code VS Code starts), and
`claude_settings`. VS Code's machine settings: the Claude extension without permission prompts, the
Python interpreter, and a default terminal running `agent-attach`.

**6.6 Hand-off** (interactive only). The user types `/handoff [autonomous|colleague] [what to continue
with]`, or asks in words. The command has Claude commit and push, run `agent-handoff
--session=${CLAUDE_SESSION_ID} <mode> "<what to continue with>"` (default `colleague`), and end its
turn. `agent-handoff` refuses once handed off; the conversation is the `--session=` one, else
`CLAUDE_CODE_SESSION_ID` (the first that names a saved transcript; neither: refused). It writes the
mode's rules, removes `/handoff`, picks a new session id, writes `handoff.json` `{mode, from, prompt,
at}` (the machine's mode is now that mode), starts the tmux session and waits up to 90 s for Claude. In
tmux, `claude --resume <from> --fork-session --session-id <new id> "<hand-off prompt>"` continues the
whole conversation under the new id; if that fails at once, a new conversation starts from the brief
with what the user asked for. `status` then reports the new mode and `tmux`.

**6.7 Save before stopping.** On the agent's branch and not mid-merge or rebase: stage tracked changes
and untracked files up to 10 MB each (100 MB in all), commit `Autosave (<reason>)` unless the
pre-commit hook refuses, push. A **final** save (every stop: the watchdog's and `crab-student stop`'s)
puts the stop note `agent-runs/<name>/stopped.json` in the same commit: `{stopped_at (epoch seconds),
reason, outputs {files, bytes, sample}, large_files [{path, bytes}], not_committed (first 50 paths),
not_committed_count}`. A refused commit leaves no note. The note on the branch is the only record of a
stop; `delete` reads it.

**6.8 Commands over ssh** (`cd D && <python> cs.py ...`; each prints one JSON object last; a failure
prints `{"error": ...}` and exits 1):

- `status`: name, mode, frontend, agent `{state, message, at}` (`{}` until set), idle_seconds,
  used_hours, stopping, stop_reason, stop_sent_for, git `{branch, head, dirty, unpushed}`.
- `save --reason R [--final]`: committed, pushed, skipped, dirty, unpushed, error, note, note_committed.
- `check`: a save, then its fields (unpushed after a fetch) plus outputs and large_files.
- `send`: stdin typed into the pane as one bracketed paste; refused while Claude is in VS Code.

`open` runs `agent-attach` with `ssh -t`: `tmux -u attach` (waiting up to 30 s for the session), else a
login shell with a hint.

**6.9 The agent's tools.** `agent-status working|needs-you|done|failed "<message>"` writes `agent.json`;
`done` is refused until `agent-runs/<name>/REPORT.md` is committed and nothing is uncommitted or
unpushed. `agent-status` alone shows the status and the machine time. Hooks: `pre-commit` refuses staged
lines containing a secret's value (the token and deploy key too) and files over 50 MB; `pre-push`
refuses any ref but `refs/heads/<branch>`, and deleting it.
