# crab-student: protocols and invariants

What is stored where, what crosses ssh, how machines start and stop, and the rules that must hold. How
the code does it is in the modules named here.

## 1. Safety invariants

1. **Money.** The launcher picks the offers but is never told the price; the CLI checks it. A launch
   rents without asking only when its pick costs `approve_above_usd_per_hour` or less, and then rents
   no backup that costs more; otherwise it records `pending` and exits 3. Only `approve`, an ask-rule
   command, launches it, at up to the highest price among its offers. A launch that failed, or was cut
   off before its upload, tried again with `start` is checked again against the record's `max_dph` or
   the current setting, whichever is higher. Starting a stopped agent has no gate. No time limit, no
   cost estimate: the approval, the idle stop and the dead-man switch bound the bill.
2. **Never rent twice.** A rent request is never retried. Its label is saved in the record (`label`)
   before the request; an unknown outcome (timeout, connection error, 5xx) is resolved only by finding
   that exact label: the launch rents no other machine while it looks, and fails if the label has not
   shown up within 2 minutes. A launch has at most one machine at a time: one that does not come up is
   destroyed before the next offer is rented (4).
3. **Own machines only.** Act only on instance ids in the records, or on an instance with the exact
   label of a record's rent. `list` points out other instances labelled with this computer's install
   id, never touching them; a teammate's machines on a shared Vast account carry another id.
4. **Never destroy work.** Code destroys a machine by itself only before its upload (`uploaded` false:
   a machine that does not come up, one whose launch fails on GitHub, or one whose launch was cut off
   and is tried again by `start`) or right after its setup failed. `start` on a stopped agent never
   destroys. `delete` checks for unsaved work first (5.3).
5. **Stops are bounded.** In every mode the watchdog stops the machine after `idle_minutes` without
   activity, done or not, always saving first. The dead-man switch stops it if the watchdog never runs
   or dies; setup refuses a machine with no key to stop itself; `list`/`status` stop a machine whose own
   stop call Vast has not taken after 5 minutes. Nothing on the machine destroys it.
6. **Secrets** (the Claude token, deploy keys, task secrets) never appear on command lines, in logs, the
   brief, records or git. They reach the machine only inside the upload stream, as 0600 files (and in
   its 0600 `~/.claude/settings.json`).
7. **The launcher** cannot ask the user anything, and a command outside `setup.ALLOW` would raise a
   permission prompt in the user's session. Whatever approves a pricier machine, forces a delete,
   changes settings or secrets, or copies a machine's files into a folder of the caller's choice
   (`fetch --to`) is in `setup.ASK` (a plain `delete` is allowed: it refuses to lose work; a plain
   `fetch` too: it only fills a new folder `NAME-outputs`); editing `~/.crab-student` is denied
   (`setup.DENY`), and `fetch` refuses a folder inside it.
8. **The remote agent never hears "crab-student".** Its rules, prompts, the watchdog's messages, hook
   and command output describe the situation plainly; its commands are `agent-status`, `agent-attach`
   and `agent-handoff`.
9. **Modes.** Branch-only pushes, the hooks, the save before every stop, the idle stop and the dead-man
   switch hold in every mode, and every mode runs Claude Code without permission prompts. A hand-off
   never leaves two Claude sessions on one conversation (6.6).
10. **GitHub.** Agents can write only to their agent repo (4.1): its deploy key is the only GitHub access
    a machine gets. crab-student never writes to the user's repo (it reads the user's clone and pushes
    from it to the agent repo; `pull` writes only to the clone). Within the agent repo, the hooks keep
    each agent on its own branch (best effort: the key itself can push any branch there).

## 2. Local files

```
~/.crab-student/
  config.json            settings that differ from the defaults (README lists them)
  install_id             6 hex digits, made once: this computer's part of every rent label
  secrets.json           {"names": [...]}: names of stored task secrets
  secrets/<key>          0600, only when no system keyring works
  spend.jsonl            a line per deleted agent: {name, machine, created, deleted, hours, usd} (5.6)
  agents/<name>.json     the record (3); agents/<name>.lock its OS lock
  agents/<name>.brief.md the brief
  agents/<name>.log      the launch's progress lines with the local time ("~ " marks a status line)
  agents/_repo-<owner>--<repo>.lock   an agent repo's lock, held while making it and its key (4.2)
  keys/<owner>--<repo>(.pub)          an agent repo's deploy key (4.2)
  ssh/id_ed25519(.pub)   the key added to machines
  ssh/config             generated Host entries (crab-student-<name>), included from ~/.ssh/config
  ssh/known_hosts        machines' host keys, under HostKeyAlias crab-student-<name>
  ssh/github_known_hosts GitHub's pinned ed25519 host key
~/.config/vastai/vast_api_key   the Vast key (the vastai CLI's file)
~/.config/vastai/vast_tfa_key   the 2FA session key from `vastai tfa login`, sent instead while it exists;
                                removed when Vast answers it with "Session expired" (left there, the vastai
                                CLI would send it with the next `tfa login` too, which then fails)
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
| `renting` | renting its machine (`label`, then `instance_id`) and waiting for it to boot; the upload comes next |
| `setup` | `uploaded` is true: the upload and setup are under way, or were cut off |
| `ready` | the launch finished; whether the machine runs comes from Vast |
| `failed` | `error` says why; it holds no machine |

Other fields: `disk_gb`;
`mode` (follows the machine after a hand-off); `claude_settings`; `max_dph` (the most it may rent at
without asking again); `fake_claude`; `setup` (a command, `none`, or null to detect); `secrets` (names);
`session_id`; `repo` (slug: the agent repo; source: the user's repo, null for `--no-repo`; branch,
start_sha, local_dir, carried, untracked, branch_created, dir on the machine); `offers` (the launcher's
pick and up to four backups, in order: id, machine_id, dph_total, storage_cost, summary, short);
`failed_offers`; its machine: `label` (its rent's, saved before the request), `instance_id` (null until
Vast confirms the rent or the label is found), `dph`, `disk_usd_day`, `machine`, `short`, `ssh`,
`uploaded` (all null, and `uploaded` false, while it has none: a `failed` record never has one);
`pending` (`{reason, max_dph}`); `warnings`; `last_start` (last rented or started, or seen running again
after a stop); `stopped_since`; `last_seen` (the last machine status, for display and for what it cost);
`past_hours` and `stopped_hours` (machine hours before the last start, and hours it sat stopped before
it: 5.6); `fetched` (`{files, bytes, digest, to, at}`: the checked copy of `/root/outputs` on this
computer, 5.5); `image`, `idle_minutes`, `notify_url` (copied at launch).

Names: 3-40 characters of a-z, 0-9 and "-". Branch `agent/<name>`; ssh alias `crab-student-<name>`;
rent label `<owner>-<name>-<install id>-<4 hex>` (owner: the `owner` setting, else `crab-student`; new
per rent request); agent repo `<login>/<repo>-agents`; deploy key title `crab-student`; tmux session
`agent` on the machine.

## 4. The launch (`launch.py`)

1. **Plan** (writes nothing): name, brief, secrets, mode and Claude's settings (4.4), repo (4.1), the
   offers given with `--offers` (4.3). `--dry-run` stops here.
2. **Record**, under the agent's lock (held until the launch ends): `pending` (exit 3) when the pick
   costs more than the price, else `renting` (backups over the price dropped).
3. **The machine** (`rent_machine`): the offers are rented one at a time, in order, until a machine runs
   and answers ssh; one that fails, is not running 10 minutes after its rent, or does not answer ssh 3
   minutes after it runs is destroyed before the next offer is rented. A rent whose outcome is unknown
   is looked for by its label for 2 minutes: found, it is the launch's machine; not found, the launch
   fails, naming the label. Vast out of reach meanwhile is waited out (the timeouts decide). Every
   offer failed: exit 4. While the machine boots, the agent repo, its deploy key (4.2) and the branch go
   on GitHub; a failure there destroys the machine and fails the launch.
4. **Upload and setup** in one ssh call (6.1), `uploaded` saved true first. Setup's own failure destroys
   the machine and fails the launch (exit 5); a lost connection leaves `setup`.
5. **`start` on an unfinished launch**: `setup` starts the machine if it stopped and repeats the upload
   and setup, which resumes. Any other (`renting`, `failed`) is tried again, its machine destroyed first
   if it has one (by id, or found by its label; nothing was uploaded to it). Trying again takes the
   record's offers that have not failed, or those given with `start NAME --offers IDS`, looks them up
   again, and has the same price check (1.1); none left: exit 4, the error naming `start NAME --offers`.
   `--offers` on a launch that has its machine (set up, or cut off during setup): exit 2. `approve`
   looks the record's offers up again and rents those at or under the price it showed; all pricier by
   now: it waits again; none left: exit 4.
6. **Progress**: plain lines on stderr, also in `agents/<name>.log` (a failure as `Launch failed: ...`).
   Once the record exists and the launch goes ahead, a window runs `crab-student open NAME` (5.4) unless
   `open_window` is `off`: a tab in the Windows Terminal window named crab-student (`wt -w crab-student
   new-tab`), or a macOS Terminal window; elsewhere the command is printed.

Several agents are several `launch` processes, each with its own record, lock and machine. The launcher
runs `launch`, `start` and `approve` as background Bash calls (setup can outlast 10 minutes).

**4.1 The repo.** An agent's branch `agent/<name>` lives in its agent repo (`slug`), never in the user's
repo (`source`): `<login>/<repo>-agents` for the current repo's GitHub remote (`<login>`: the `gh`
login; two repos with one name share it), or the workspace `<login>/<workspace_repo>` for `--no-repo`
(`source` null). The branch must not exist there yet. The starting commit is HEAD plus, as one extra
commit, what `git commit -a` would commit, built from a copy of the index (the user's checkout is
untouched); untracked files are not carried (a warning names them), nor are Git LFS objects or
relative-URL submodules; `--committed-only`: HEAD alone. It is pushed from the user's computer with
the agent repo's deploy key and
`push.negotiate` (the agent repo's branch tips are agents' commits the clone never saw): only the first
push sends the history. In the workspace the branch is made through the API, from its default branch.
`pull` fetches the branch into the clone the agent started from (or `--repo PATH`), which gets the agent
repo as the remote `agents` the first time, in the transport of its GitHub remote (https or ssh); the
workspace has its own clone, `~/<workspace_repo>`.

**4.2 Agent repos and deploy keys.** An agent repo is made the first time, private, with a first commit
and the description `crab-student agent branches for <source>` (or `... for tasks without a repo`).
An existing repo of that name is used only if its description starts with `crab-student agent
branches` and it is private; else the launch fails before renting. Each agent repo has one read-write
deploy key, made the first time and kept in `keys/`; a launch adds it to GitHub again when GitHub no
longer has it. Making the repo and its key happens under the agent repo's lock, so parallel launches
wait for the first. Nothing is ever removed: the agent repo keeps every agent's branch.

**4.3 Offers** (`offers.py`). The launcher picks the machine (`data/launch.md` section 5, which
`guide launch` fills with the `home` country); the CLI only applies rules. Every offer, browsed or
named by a launch, passes `fixed`: rentable, verified, not external, reliability at least
`min_reliability`, a direct port, download at least `min_download_mbps`, room for the disk asked for, a
driver for the image's CUDA (`cuda_max_good`, CPU tasks too), not a mining card (GPU name `CMP...`),
not `BLOCKED` (countries where Anthropic's API is unavailable), the `regions` setting, and a lease of at
least `max(2 x hours, 24) + 1` hours. `crab-student offers` browses with filters (GPU count or range,
GPU memory, models, compute capability range, vCPUs, RAM, disk, down and up speed, max price, region,
hours; `--sort`, `--limit`, `--kinds` to group by kind of GPU, or by CPU model for a CPU task); prices
include the disk (`allocated_storage`). A task with no GPU (`--gpus 0`) gets machines with at least
`CPU_TASK_CPUS` (24) vCPUs and `CPU_TASK_RAM_GB` (64 GB). No offer fits: exit 4. `launch NAME --offers
A,B,C`: 1 to `MAX_OFFERS` (5) different ids, the pick first (else exit 2), looked up again by id
(`named`); one gone or failing `fixed` is skipped with a line, two on one machine are refused (exit 2),
none left: exit 4 and no record.

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
`last_start`. `list` warns on stderr about orphans: instances whose label ends in `-<install id>-<4 hex>`
whose id and label no record has (a rent Vast did not confirm that went through after all).

**Without Vast** (its API refuses or cannot be reached; `listing` returns the reason, and `list` warns
with it on stderr): every ready agent with an ssh address that is not known to be stopped is asked over
ssh, and its answer decides its state as usual. An agent already known to be stopped is shown so; any
other is `unknown`, the reason in its message. A machine's stuck stop is not taken over without Vast.
The first 2FA error after a session ended says how long ago that login was made (Vast does not say how
long one lasts).

**A look never hangs**: `list`, `status`, `spend` and each look of `wait` end the command with an error
and exit code 1 after `LOOK_SECONDS` (180), whatever call is stuck.

**5.2 stop** (a machine that received its files): `cs.py save --reason user --final` if it runs (best
effort), then Vast `stopped`. **start** (stopped): Vast `running`, `last_start` set, a wait for ssh that
never destroys. The boot starts the watchdog, which resumes the conversation.

**5.3 delete.** Pending: removed. A machine that received its files is checked first: running, with
`cs.py check`; stopped, with the stop note on its branch (6.7), counted only if `stopped_at` is at least
5 minutes after `last_start`. No such note, GitHub unreachable, or a failed check: refused (exit 3)
without `--force`. Refused too (exit 3, with the list and the copy commands) for unpushed commits,
uncommitted files, files in `/root/outputs`, or untracked files of 100 MB+ outside git. The files in
`/root/outputs` do not count when their fingerprint equals the record's `fetched.digest` (every file is
as it was copied): a running machine is asked for it now (5.5), a stopped one's is in its stop note
(6.7; a machine set up before notes had it cannot say, so its outputs count). The refusal names the
copy. Then: destroy the machine (or, for a launch cut off before Vast confirmed its rent, the machine
with its label), add the agent's cost to `spend.jsonl` (5.6), forget the host key and the record.
GitHub is left alone: the branch and the agent repo's deploy key stay.

**5.4 open.** On an agent that is not ready, `open NAME` first follows the launch log until the phase is
ready, failed or pending, or nobody holds the lock (cut off). Frontend `tmux`: `agent-attach` over `ssh
-t`. Frontend `vscode`: VS Code opens as with `--vscode`; then, on a terminal, it asks the machine's
`status` every 5 s and attaches once the frontend is `tmux`. `open --all`: the running tmux agents side
by side (Windows Terminal panes, else a local tmux session with prefix Ctrl-a). `open NAME --vscode`:
`code --remote ssh-remote+crab-student-<name>` on the repo folder; for a `vscode` frontend, 15 s later,
`vscode://anthropic.claude-code/open?prompt=<the brief>` fills the Claude panel without sending (a
brief over 5,000 characters becomes a pointer to `/root/BRIEF.md`).

**5.5 wait and fetch.** `wait [NAME ...] [--timeout MINUTES]` looks (5.1) every `WAIT_POLL` (60 s) at
the agents named, else at every agent there is at its first look (none: exit 1), so that an agent
already in an `ATTENTION` state ends it at once and none is passed over. It ends when one is in such a
state (done, failed, needs you, stopped, interrupted, approval) or was deleted, after the timeout
(default 60 minutes), or, with exit code 1, when one has been `unknown` for `UNSEEN_LIMIT` (10 minutes)
and nothing else is to report. `--json`: `{attention, unseen, deleted, waiting, timed_out,
vast_problem, result_line, exit_code}`, the first two as views, `waiting` the names for the next
`wait`. `vast_problem` is the last look's, or an ended 2FA login's while no new session key exists.

The machine's `/root/outputs` is read by `machine.OUTPUTS_SCRIPT`, sent on stdin to its Python in `D`
with the argument `state` or `copy` (no file on the machine changes for it). It goes through the files
`cs.files_under(/root/outputs)` lists (what `check` and the stop note count: regular files, a link to a
file as that file) and prints `{files, bytes, digest}` last on stderr; `digest` is a SHA-256 over each
file's path, size and time of last change. `copy` also writes the files to stdout as a tar, and takes
each file's size and time just before reading it, so a file changed during the copy or after it gives
another digest.

`fetch NAME [--to DIR]`, under the agent's lock and with ssh only. The default folder is the first of
`./NAME-outputs`, `NAME-outputs-2`, ... that does not exist or is empty. `DIR` must not exist or be
empty, and no folder may lie in `~/.crab-student` (exit 2): the machine chooses the names and contents.
All flags must be written out (`allow_abbrev=False`): the ask rules look for `--to` and `--force`. The local side writes regular files only, each under `DIR`, never over an existing file (a
name leading outside, one Windows cannot hold, or one that is the same file as another there is
refused), with its mtime. The copy counts only if nothing was refused and the files and bytes found in
`DIR` afterwards equal those sent; then the record's `fetched` keeps `{files, bytes, digest, to, at}`.
A stream that was cut off removes `DIR` again (exit 1); a copy with refused files stays, unrecorded.
While bytes flow, the script touches `D/copying` every half minute, which the watchdog takes for
activity (6.3), so the idle stop does not cut a long copy (a machine set up before that ignores it).

**5.6 What an agent cost** (`agents.spend`, a lower estimate). The machine counts `used_hours` from each
container start. When a new start is known (`start`, a stopped machine seen running, or `used_hours`
more than `CLOCK_SLACK` lower than last seen, which also moves `last_start`), `core.new_session` moves
the hours last seen to `past_hours` and the time since `stopped_since` to `stopped_hours`. Hours =
`past_hours` + the current `used_hours`; dollars = hours x `dph` + stopped hours (the current stop too)
x `disk_usd_day` / 24. `list` and `status` show both; `delete` appends them (as last seen; nothing for
an agent never seen running) to `spend.jsonl`; `spend` lists agents and deleted agents, with the
total.

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
4. `Claude Code`: wait for the install jobs; one tiny `claude -p` checks the token.
5. `agent settings` (after the install, whose own run of Claude Code writes `~/.claude.json`): the brief to
   `/root/BRIEF.md`; autonomous and colleague: `~/.claude/CLAUDE.md` from `rules.md`, the mode's section
   and how the install went; interactive: `handoff.md` to `~/.claude/commands/`; every mode: Claude
   Code's and VS Code's settings and its onboarding and trust flags (6.5).
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
under `~/.claude/projects` changed (tmux or VS Code alike), the container's CPU or a GPU is busy, or
the outputs are being copied off the machine (`D/copying` touched within the last two minutes: 5.5).
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
`~/.claude/settings.json` (0600, every mode): `bypassPermissions` with its warning accepted
(`skipDangerousModePermissionPrompt`), deny rules for the ways around the
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
reason, outputs {files, bytes, sample, digest}, large_files [{path, bytes}], not_committed (first 50
paths), not_committed_count}`. `digest` is `cs.fingerprint` of the outputs: the SHA-256 over each
file's path, size and time of last change that `machine.OUTPUTS_SCRIPT` takes too (the two must stay
the same formula). A refused commit leaves no note. The note on the branch is the only record of a
stop; `delete` reads it.

**6.8 Commands over ssh** (`cd D && <python> cs.py ...`; each prints one JSON object last; a failure
prints `{"error": ...}` and exits 1):

- `status`: name, mode, frontend, agent `{state, message, at}` (`{}` until set), idle_seconds,
  used_hours, stopping, stop_reason, stop_sent_for, git `{branch, head, dirty, unpushed}`.
- `save --reason R [--final]`: committed, pushed, skipped, dirty, unpushed, error, note, note_committed.
- `check`: a save, then its fields (unpushed after a fetch) plus outputs (as in the stop note) and
  large_files.
- `send`: stdin typed into the pane as one bracketed paste; refused while Claude is in VS Code.

`open` runs `agent-attach` with `ssh -t`: `tmux -u attach` (waiting up to 30 s for the session), else a
login shell with a hint.

**6.9 The agent's tools.** `agent-status working|needs-you|done|failed "<message>"` writes `agent.json`;
`done` is refused until `agent-runs/<name>/REPORT.md` is committed and nothing is uncommitted or
unpushed. `agent-status` alone shows the status and the machine time. Hooks: `pre-commit` refuses staged
lines containing a secret's value (the token and deploy key too) and files over 50 MB; `pre-push`
refuses any ref but `refs/heads/<branch>`, and deleting it.
