# crab-student: invariants, contracts and flows

For whoever changes the code: what must always hold (1), what two parts must agree on (2), and the flows
that spend money, keep work and carry secrets (3). Everything else is in the code.

## 1. Safety invariants

1. **Money.** The CLI enforces `approve_above_usd_per_hour`, which the launcher is never told
   (`setup.guide`). A launch whose pick costs more rents nothing until `approve`, an ask rule
   (`launch.priced`, `launch.approve`); one that `start` tries again is priced again (`launch.replan`).
   Starting a stopped agent has no gate. There is no time limit, cost estimate or cap on machines: the
   approval, the idle stop and the dead-man switch bound the bill.
2. **A rent request is sent once.** `vast.rent` never retries; `vast.request`, which does, never rents.
   The rent's label is saved in the record first, and an unknown outcome is resolved only by finding that
   exact label, renting nothing else meanwhile (`launch.rent`, `launch.find_rent`). A launch holds at most
   one machine at a time (`launch.rent_machine`).
3. **Own machines only.** Code acts only on instance ids in the records, or on the instance with a
   record's exact rent label. `list` points out other instances labelled with this computer's install id,
   and never touches them (`agents.orphans`); a teammate's machines carry another install id.
4. **Never destroy work.** Code destroys a machine by itself only before its upload
   (`launch.drop_unuploaded`), or right after a failed setup (`launch.setup_machine`); `uploaded` is saved
   true before the upload begins. Nothing on the machine destroys it. `delete` checks for unsaved work
   first (3.2).
5. **Stops are bounded** (3.3). In every mode the watchdog stops the machine after `idle_minutes` without
   activity, done or not, saving first. The dead-man switch stops it when neither the container's start
   nor the watchdog's last completed tick is within 45 minutes. Both use the machine's own Vast key, which
   setup requires (`cs.setup_start`). If Vast has not taken the watchdog's stop after 5 minutes, the next
   `list` or `status` stops the machine from here (`agents.listing`; a failure shows in that agent's
   line): a net that needs someone to run them.
6. **Secrets** (the Claude token, deploy keys, task secrets) never go on command lines, into logs, the
   brief, records or git. Locally, the token and task secrets are in the system keyring, else in 0600
   files (`core.set_secret`). They reach the machine only inside the upload stream, as 0600 files, and in
   its 0600 `~/.claude/settings.json` (`launch.generated_files`). The machine's own Vast key stays out of
   the tmux agent's environment (`cs.inherited`) and off every command line: onstart writes the stop call's
   header to the 0600 `vast.header` with the shell's own printf, and the dead-man loop's curl reads it with
   `-H @vast.header` (`launch.ONSTART`). The pre-commit hook refuses any `-----BEGIN ... PRIVATE KEY` line
   and the token's and task secrets' values (`cs.cmd_hook`): best effort, since the agent is root. The host
   is root too, and can read it all.
7. **Permission rules** (`setup.ALLOW`, `ASK`, `DENY`). The launcher cannot ask the user anything: a
   command it runs that is not allowed raises a prompt in the user's session. Approving, forcing a delete,
   and changing settings or secrets are ask rules; a plain `delete` is allowed, since it refuses to lose
   work; editing `~/.crab-student` is denied. Only spelled-out flags parse (`allow_abbrev=False`), so
   `--fo` cannot dodge the rule for `--force`.
8. **The remote agent never hears "crab-student"**, in its rules, its prompts, the watchdog's messages,
   or the output of its commands and hooks (`test_nothing_the_agent_reads_names_the_tool`). Its commands
   are `agent-status`, `agent-attach` and `agent-handoff`, the machine's folder is `/root/.machine`, the
   workspace repo is `agent-workspace` by default, and the user's ssh key reaches the machine without its
   comment (`vast.add_ssh_key`).
9. **Every mode keeps the guards**: branch-only pushes, the hooks, the save before every stop, the idle
   stop and the dead-man switch. Every mode runs Claude Code without permission prompts. A hand-off forks
   into a new session id, so two Claude sessions never share a conversation (3.5).
10. **GitHub.** A machine's only GitHub access is its agent repo's deploy key. crab-student never writes
    to the user's GitHub repo; in the user's clone it adds only the starting commit, unreferenced, and on
    `pull` the remote `agents` and the fetched branch. It writes only to an agent repo that is private and
    marked (`github._exists`). The pre-push hook keeps each agent on its own branch: best effort, since
    the key can push to any branch of the agent repo.

## 2. Contracts

### 2.1 Local files

```text
~/.crab-student/
  config.json                 settings that differ from the defaults
  install_id                  6 hex digits, made once: this computer's part of every rent label
  secrets.json                {"names": [...]}: the stored task secrets
  secrets/<key>               a secret, only when no system keyring works
  agents/<name>.json          the record (2.2); agents/<name>.lock, its lock
  agents/<name>.brief.md      the brief
  agents/<name>.log           the launch's progress, "HH:MM:SS <line>", which open follows
  agents/_repo-<login>--<agent repo>.lock   held while an agent repo and its deploy key are made
  keys/<login>--<agent repo>(.pub)          an agent repo's deploy key
  ssh/id_ed25519(.pub)        the key added to machines
  ssh/config                  a Host crab-student-<name> entry per agent with an address
  ssh/known_hosts             machines' host keys, under HostKeyAlias crab-student-<name>
  ssh/github_known_hosts      GitHub's pinned ed25519 host key
```

`~/.ssh/config` starts with `Include ~/.crab-student/ssh/config`. The Vast key is the `vastai` CLI's
`~/.config/vastai/vast_api_key`; its 2FA session key, `vast_tfa_key` beside it, is sent instead while it
exists. The keyring service `crab-student` holds `claude-oauth-token` and `task-secret:<NAME>`. `init`
adds the permission rules to `~/.claude/settings.json` and the skill to `~/.claude/skills/crab-student/`.

### 2.2 The agent record

Only the command holding the agent's lock writes the record; `launch` and `approve` hold it to the end of
the launch. `list` shows a launch whose lock nobody holds as `interrupted`.

| `phase` | Meaning |
| --- | --- |
| `pending` | waiting for approval: nothing rented, nothing on GitHub |
| `renting` | renting its machine and waiting for it to boot; nothing uploaded |
| `setup` | `uploaded` is true: the upload and setup are under way, or were cut off |
| `ready` | the launch finished; whether the machine runs comes from Vast |
| `failed` | `error` says why; it holds no machine |

A record holds at most one machine: `label` (saved before the rent request), then `instance_id`, `ssh`,
the price, and `uploaded`, set true just before the upload; they are cleared together
(`launch.NO_MACHINE`). `offers` is the pick and up to four backups, in order; `failed_offers`, those given
up. `max_dph` is the most a retry may rent at without asking; `pending`, `{reason, max_dph}` while it
waits. `last_start` is when the machine was last rented or started, or seen on its way up again after a
stop. `repo.slug` is the agent repo, `repo.source` the user's repo (null for `--no-repo`).

### 2.3 Names

- **Agent**: 3-40 characters of a-z, 0-9 and "-", not starting or ending with "-". Its branch is
  `agent/<name>`, and its ssh host `crab-student-<name>` (also the `HostKeyAlias`).
- **Rent label**: `<owner>-<name>-<install id>-<4 hex>`, new for each rent request; `<owner>` is the
  `owner` setting, else `crab-student`. A label ending in `-<install id>-<4 hex>` is this computer's.
- **Agent repo**: `<login>/<repo>-agents`, where `<login>` is the `gh` login and `<repo>` the name of the
  user's repo (two repos with one name share it); for `--no-repo`, `<login>/<workspace_repo>`. Private,
  with a description that starts with `crab-student agent branches`.
- **Deploy key**: one per agent repo, read-write, titled `crab-student`. It is made the first time, added
  again when GitHub no longer has it, and never removed.
- **On the machine**: the tmux session `agent`, the repo in `/root/work/<repo>`, the brief in
  `/root/BRIEF.md`, big outputs in `/root/outputs`. On the branch: `agent-runs/<name>/REPORT.md`, and the
  stop note (2.8).

### 2.4 The result line and exit codes

`launch`, `approve` and `start` end with one line, which the launcher returns and the skill shows as it is:

```text
NAME: working on 1x RTX 4090 (Quebec, CA) at $0.42/hr - colleague - branch agent/NAME - watch: crab-student open NAME
NAME: ready on 1x RTX 4090 (Quebec, CA) at $0.42/hr - interactive - branch agent/NAME - open it: crab-student open NAME --vscode
NAME: NOT started - needs your approval: REASON - would rent SHORT at $X/hr - approve: crab-student approve NAME (or: crab-student decline NAME)
NAME (dry run): would rent SHORT at $X/hr - MODE - no approval needed
```

With `--json`, the launcher reads `result_line`, and `warnings`, which it shows as `Warning:` lines
(`launch.result`). `status --json` gives the same line for an agent that is working, interactive or
waiting for approval. Exit codes: 0 ok, 1 error, 2 usage, 3 needs approval or would lose work, 4 no
machine fits or the offers given are gone or all failed, 5 setup failed.

### 2.5 The upload and setup

One ssh call (`machine.upload_and_setup`), a tar.gz on stdin:
`mkdir -p D && tar -xzf - -C D --no-same-owner && cd D && { SETUP; }`. The bundle holds the `remote/`
files (`fake_claude.py` only for `--fake-claude`), `config.json` (`launch.machine_config`), `BRIEF.md`,
`claude.env` and `secrets.env` (the token and the task secrets as `export K=<quoted>` lines),
`deploy_key` and `github_known_hosts`.

`SETUP` (`machine.SETUP`) links the first Python 3.11 or newer among `python3` and
`/venv/main/bin/python` as `D/python` and runs `./python cs.py setup`; with none, it exits 1, saying the
image needs Python 3.11 or newer. Everything after runs `D/python`: the commands over ssh, the watchdog
onstart starts, and the scripts setup writes.

Setup prints `step i/6 <label>` as each step starts (the launch shows these), then one JSON line:

- `{"ok": true, "warnings": [...]}`: set up. A failed dependency install, or VS Code's server not in
  place, is a warning, never a failed setup.
- `{"ok": false, "step": i, "label": "...", "error": "..."}`: setup failed.
- `{"ok": true, "resumed": true}`: `setup-done` existed, so it only restarted the watchdog.

Without that line, ssh's exit 255 (a lost connection) or the call's 1-hour timeout leaves the outcome
unknown, and the launch stays in `setup` for `start`; any other exit is a failed setup. Each step is
bounded and touches `heartbeat` as it starts, so the dead-man switch waits for a long one; the dependency
install (`cs.deps_command`, as the README and the launcher's guide describe) gets 30 minutes.

### 2.6 Commands over ssh

`cd D && exec ./python cs.py <command>` (`machine.remote`). Each prints one JSON object on its last line;
a failure prints `{"error": "..."}` and exits 1. ssh's own exit 255 means the machine was not reached.

- `status` (for `list`, `status`, `open`, `send`): `name`, `mode`, `frontend` (`tmux`, or `vscode` while
  Claude works in VS Code), `agent` (`{state, message, at}`, empty until set), `idle_seconds`,
  `used_hours`, `stopping`, `stop_reason`, `stop_sent_for`, `git` (`{branch, head, dirty, unpushed}`).
  It starts the watchdog if that is not running.
- `save --reason R [--final]` (for `stop`, as `--reason user --final`): `committed`, `pushed`, `error`,
  and more.
- `check` (for `delete`): a save, then its fields (`unpushed` after a fetch), plus `outputs` and
  `large_files` as in the stop note.
- `send`: stdin typed into the agent's pane as one bracketed paste; refused while Claude is in VS Code.

`open` runs `agent-attach` over `ssh -t`, as VS Code's terminals on the machine do: it attaches to the
tmux session `agent`, waiting up to 30 s for it, else opens a login shell with a hint.

### 2.7 The machine's files

`D` is `/root/.machine`: `machine.REMOTE_DIR` on the local side, which onstart and the ssh commands use,
and `D` in cs.py. The agent never needs it.

| In `D` | Written by | Read by |
| --- | --- | --- |
| `python` | setup's command (2.5) | onstart, the ssh commands, the agent's commands, the hooks |
| `vast.env` | onstart, at every container start (0600): `CONTAINER_ID`, `CONTAINER_API_KEY`, `CS_VAST_URL` | the dead-man loop, the watchdog's stop call |
| `vast.header` | onstart, at every container start (0600): the stop call's `Authorization` header | the dead-man loop's curl, with `-H @vast.header` |
| `booted` | onstart: the container's start time | the dead-man loop; the watchdog, which resets its clocks when it changes |
| `heartbeat` | each setup step; each completed watchdog tick | the dead-man loop (its mtime) |
| `setup-done` | the end of setup | onstart, which then starts the watchdog; setup, which then only restarts it |
| `setup.lock`, `watch.lock` | setup, the watchdog | one setup, one watchdog at a time (flock) |
| `state.json` | the watchdog | `status` |
| `agent.json` | `agent-status`: `{state, message, at}` | the watchdog, `status` |
| `handoff.json` | `agent-handoff`: `{mode, from, to, prompt, at}` | all that asks the machine's mode; the tmux loop |
| `session_id` | `agent-handoff`; the tmux loop, giving up a conversation | the tmux loop |

The other uploaded files stay in `D`, with `hooks/` (git's `core.hooksPath`) and `logs/`. The watchdog
also watches the conversations under `~/.claude/projects`.

### 2.8 The stop note

Every final save (the watchdog's stop, and `crab-student stop`) puts `agent-runs/<name>/stopped.json` in
the same commit as the work it saves (`cs.save`):

```text
{stopped_at, reason, outputs: {files, bytes, sample}, large_files: [{path, bytes}], not_committed, not_committed_count}
```

`stopped_at` is in epoch seconds, `reason` is `idle`, `done` or `user`, and `not_committed` lists the
first 50 paths. A commit the pre-commit hook refuses carries no note. The note is the only record of what
a stopped machine still holds: `delete` reads it from GitHub (`agents._stop_note`), and trusts it only
when `stopped_at` is at least 5 minutes after the record's `last_start`.

## 3. Flows

### 3.1 Launch

1. **Plan** (`launch.launch`), writing nothing but the starting commit (`--dry-run` skips it, and stops here).
   gh's token must have the `workflow` scope whenever it shows scopes (`github.login`): GitHub refuses
   to push workflow files without it, through the deploy keys gh added too. Else exit 1, naming
   `gh auth refresh -h github.com -s workflow`, which `init` runs in a terminal.
   The agent repo is checked: it must not have the branch. The starting commit is HEAD plus one commit of
   what `git commit -a` would commit, built from a copy of the index; untracked files, Git LFS objects
   and relative-URL submodules are not carried. The offers given with `--offers` (1 to 5, the pick first)
   are looked up again (`offers.named`): one gone or failing the fixed rules (`offers.fixed`) is skipped,
   two on one machine are refused (exit 2), none left is exit 4.
2. **Approval** (`launch.priced`). A pick at or under `approve_above_usd_per_hour` goes ahead, without
   the backups that cost more. A pricier pick saves the record as `pending`, with `max_dph` the highest
   price among its offers, and exits 3. `approve` looks the offers up again and goes ahead with those at
   or under that price, which becomes the record's `max_dph`; all pricier by now: it waits again; none
   left: exit 4. `decline` removes the record.
3. **Record.** The record, brief and log are written under the agent's lock, held to the end. A launch
   that goes ahead opens a window running `crab-student open NAME`, unless `open_window` is `off`.
4. **One machine at a time** (`launch.rent_machine`). The offers are rented in order (invariant 2). Vast
   refusing one moves on to the next; no credit fails the launch. A machine is given up, and destroyed
   before the next offer is rented, when it fails (exited, offline, or an error in its status twice in a
   row), is not running 10 minutes after its rent, or does not answer ssh 3 minutes after it runs. Vast
   out of reach is waited out; Vast refusing a call ends the launch, leaving the machine for `start` or
   `delete`. Every offer failed: exit 4, naming `start NAME --offers`.
5. **GitHub**, while the first machine boots (`launch.prepare_repo`). Under the agent repo's lock, the
   repo is made if missing, and its deploy key added if GitHub lacks it. The branch is pushed from the
   user's clone with that key and `push.negotiate`, so only the first push sends the history; for the
   workspace, it is made through the API. A failure destroys the machine and fails the launch.
6. **Upload and setup** (2.5), `uploaded` saved true first. Set up: `ready`. Setup failed: the machine
   destroyed, `failed`, exit 5. Outcome unknown: the record stays in `setup`.
7. **`start` on an unfinished launch** (`launch.continue_launch`) waits up to 15 minutes for a launch at
   work. In `setup`, it starts the machine if it stopped, and runs the upload and setup again, which
   resumes. In `renting` or `failed`, it destroys the machine if there is one (by id, or by its label),
   then plans the rent again (`launch.replan`) with the offers that have not failed, or those of
   `--offers`, priced against the higher of `max_dph` and the setting; none left: exit 4. `--offers` once
   the machine has had its upload: exit 2.

### 3.2 Stop, start and delete

- **stop** (`agents.stop`), for a machine that received its files: if it runs,
  `cs.py save --reason user --final`, best effort (the disk is kept, so nothing is lost); then Vast
  `stopped`.
- **start** (`launch.start`), for a stopped agent that is set up: Vast `running`, `last_start` set, and a
  wait of up to 15 minutes for ssh that never destroys. At boot, onstart starts the watchdog, whose tmux
  session resumes the conversation with a note that Claude Code was restarted.
- **delete** (`agents.delete`): a pending agent is just removed. A machine that received its files is
  checked first, unless `--force`: a running one with `cs.py check`, a stopped one with its stop note. A
  failed check, no note to trust, or GitHub out of reach: refused (exit 3). Unpushed commits, uncommitted
  files, files in `/root/outputs`, or untracked files of 100 MB or more outside git: refused (exit 3),
  listed with a copy command. Then the machine is destroyed (for a launch cut off before Vast confirmed
  its rent, the one with its label), and its host key and record are forgotten. GitHub is left alone.

`list` and `status` ask Vast once, and every running machine (`cs.py status`) in parallel. They keep what
they learn in the record: the mode after a hand-off, a new address, and `last_start` when a stopped
machine is on its way up again.

### 3.3 The watchdog and the dead-man switch

The watchdog (`cs.py watch`, one per container) is started by onstart once `setup-done` exists, by setup,
and, when it is not running, by `status`, `agent-attach` and `agent-handoff`. Every minute it:

- keeps the tmux session `agent` (running `cs.py claude`) alive while Claude works in tmux;
- counts the machine time and looks for **activity**: the conversations under `~/.claude/projects` grew
  (in tmux or VS Code, subagents too), the container used a quarter of a core, or a GPU was 10% busy;
- types **reminders** into the pane, starting with `Automated message from the machine:`. A colleague
  whose status is `needs-you` gets the nudge after half of `idle_minutes` without activity; an autonomous
  or colleague agent whose status is `working` gets the check-in after 20 minutes without its
  conversation growing. Each comes again only after the agent's own doing (a status update, or CPU or GPU
  work), never for a growing conversation, which a reminder causes itself: an agent that cannot answer
  gets one, and its machine stops `idle_minutes` later;
- notifies `notify_url` when the status becomes `needs-you`, `done` or `failed`;
- **stops** the machine after `idle_minutes` without activity, done or not: it notifies, makes the final
  save (2.8), records `stop_sent_at`, and asks Vast to stop the machine with its own key, every minute
  until it stops (`stop_reason`: `done` if the agent said so, else `idle`);
- touches `heartbeat`, last; a tick that fails leaves it.

A new container start (a new `booted`) resets the machine time, the clocks, the reminders and the stop.
The **dead-man switch** is a loop in the onstart command, so it runs before anything is installed: every
60 s, when neither `booted` nor `heartbeat`'s mtime is within 45 minutes, it asks Vast to stop the
machine, with curl reading the key's header from `vast.header`.

### 3.4 Modes and Claude Code on the machine

The machine's mode starts as the launch's, and changes only by a hand-off (3.5).

| | autonomous | colleague | interactive |
| --- | --- | --- | --- |
| Claude runs | in tmux | in tmux | in VS Code's Claude panel, driven by the user |
| rules (`~/.claude/CLAUDE.md`) | base + autonomous | base + colleague | none; `/handoff` instead |
| `agent-status` | sets the status; `needs-you` refused | sets the status | only shows the machine |
| reminders | check-in | nudge, check-in | none |
| `status`'s `frontend` | `tmux` | `tmux` | `vscode`, then `tmux` after a hand-off |

`agent-status working|needs-you|done|failed "<message>"` writes `agent.json`; `done` is refused until the
report is committed and nothing is uncommitted or unpushed. The pre-commit hook refuses a staged line with
any `-----BEGIN ... PRIVATE KEY` (OpenSSH, PEM, PGP) or the value of the token or a task secret, and files
over 50 MB; the pre-push hook refuses any ref but `refs/heads/<branch>`, and deleting that.

In tmux, `cs.py claude` resumes the conversation after a restart; one that cannot be resumed is replaced
by a new one (a new `session_id`), told to read its report. `~/.claude/settings.json`, in every mode,
bypasses permissions, denies the ways around the hooks, and carries the token, the secrets and
`claude_settings`: the model, effort and fast mode never go on `claude`'s command line.

### 3.5 The hand-off

Interactive only. The user types `/handoff [autonomous|colleague] [what to continue with]`, or asks in
words. The command (`remote/handoff.md`) has Claude commit, push, run
`agent-handoff --session=${CLAUDE_SESSION_ID} <mode> "<what to continue with>"` (default `colleague`),
and end its turn.

`agent-handoff` (`cs.cmd_handoff`) refuses once handed off. The conversation is the `--session=` one,
else `CLAUDE_CODE_SESSION_ID`, the first that names a saved conversation (neither: refused). It writes the
mode's rules, removes `/handoff`, picks a new session id, and writes `handoff.json`
`{mode, from, to (the new id), prompt, at}`, which makes that mode the machine's. It starts the tmux
session, where Claude runs `claude --resume <from> --fork-session --session-id <to> "<hand-off prompt>"`:
the whole conversation, under the new id. The fork goes only into `to`: when it, or a later resume of it,
fails at once, a new conversation starts from the brief with what the user asked for, and the VS Code
conversation is never forked again. `status` then reports the new mode, and `tmux`.
