# crab-student: protocols and invariants

The contracts between the parts: what is stored where, what crosses ssh, how machines start and stop,
and the rules that must hold. It describes behaviour, not code.

## 1. Safety invariants

1. **Money.** A launch rents without asking only a machine that costs `approve_above_usd_per_hour` or
   less; when every machine that fits costs more, it records `pending` (with the price) and exits 3
   instead. Only `approve`, an ask-rule command, launches it, at up to the price it showed. There is no
   time limit and no cost estimate: what bounds the bill is the approval, the idle stop and the
   dead-man switch (item 5). Starting a stopped agent has no gate.
2. **Never rent twice.** A rent request is never retried. Its label is saved in the record (`rents`)
   before the request is sent. A request whose outcome is unknown (timeout, connection error, 5xx) is
   resolved only by finding an instance with that exact label; if none appears and no other machine
   wins, the launch stops and says so. Renting two offers at once for one launch (4, step 3) is deliberate
   and bounded, not a retry: at most two rents are in flight per launch (an unconfirmed one keeps its
   place), both within the price the launch may rent at, and every machine but the one kept is
   destroyed before anything is uploaded to it. No rent is left unaccounted for: an unconfirmed rent
   left behind when another machine won stays in `strays` until its machine is found by label and
   destroyed (4, 5.1, 5.3).
3. **Own machines only.** crab-student acts only on instance ids in its records, or on an instance
   carrying the exact label of one of its rents (`rents`, `strays`).
4. **Never destroy work.** Code destroys a machine by itself only before its upload (`uploaded` false:
   a machine that lost its launch's race, one that failed or timed out starting, a GitHub step that
   failed) or right after a first setup failed (the agent never ran). Starting a stopped agent again
   never destroys its machine. Setup on a machine where it was done before only resumes. `delete` checks any
   machine that received its files (5.3); a machine that cannot be checked needs `--force`.
5. **Stops are bounded.** In every mode the watchdog stops the machine after `idle_minutes` without
   activity, whether the agent is done or not, always saving first. Setup refuses a machine that has no
   key to stop itself. The dead-man switch stops it if the watchdog never runs or dies, and
   `list`/`status` finish a stop Vast did not take within 5 minutes. Nothing on the machine ever
   destroys one.
6. **Secrets** (the Claude token, deploy keys, task secrets) never appear on command lines, in logs,
   the brief, records or git. They reach the machine only inside the upload stream, as 0600 files (on
   the machine, the token and task secrets are also copied into the 0600 `~/.claude/settings.json`,
   6.5).
7. **The launcher** (a background Claude Code fork, or a general-purpose background agent when forks
   are unavailable) cannot ask the user anything, and a command outside its allowed list would raise a
   permission prompt in the user's main session. So it runs only allowed commands (`setup.ALLOW`);
   everything that approves a pricier machine, destroys, discards or changes settings is in
   `setup.ASK`, and editing crab-student's own files is denied (`setup.DENY`).
8. **The remote agent never hears "crab-student".** It has no context for the name. Everything the
   agent sees (its rules, its first prompt, the watchdog's messages, the hooks' and commands' output)
   describes the situation plainly; the machine's commands are `agent-status`, `agent-attach` and
   `agent-handoff`. (The tmux status bar and local output may use the name.)

## 2. Local files

```
~/.crab-student/
  config.json            settings that differ from the defaults (README lists them)
  secrets.json           {"names": [...]}: names of stored task secrets
  secrets/<key>          0600, only when no system keyring works
  agents/<name>.json     the agent's record (3); agents/<name>.lock its OS lock
  agents/<name>.brief.md   the brief
  agents/<name>.log      the launch's progress lines, each with the local time ("~ " marks a status line)
  agents/_keys-<owner>--<repo>.lock   the repo's deploy key lock (4.3)
  keys/<owner>--<repo>-<8 hex>(.pub)   deploy keys, shared by agents on that repo (4.3)
  ssh/id_ed25519(.pub)   the key crab-student adds to machines
  ssh/config             generated Host entries (crab-student-<name>), included from ~/.ssh/config
  ssh/known_hosts        machines' host keys, under HostKeyAlias crab-student-<name>
  ssh/known_hosts_racing throwaway host keys of machines racing for a launch (only the kept one's is trusted)
  ssh/github_known_hosts GitHub's pinned ed25519 host key
~/.config/vastai/vast_api_key   the Vast key (the vastai CLI's file; XDG_CONFIG_HOME is honoured)
~/.claude/settings.json         read by launches (never written by them) for the model, effort and fast mode to inherit (4.5)
```

Secrets live in the OS keyring, service `crab-student`: `claude-oauth-token` (the Claude token for
agents) and `task-secret:<NAME>`. A keyring that fails or silently drops values falls back to
`~/.crab-student/secrets/`.

## 3. The agent record

One JSON file per agent, written only by the command that holds the agent's lock. `phase` says how
far the launch got (only the local side writes it):

| phase | meaning |
|---|---|
| `pending` | waiting for approval: no machine that fits is within the price; nothing rented, nothing on GitHub |
| `renting` | machines racing: `rents`, each `{label, offer_id, at, instance_id (null while unconfirmed), running_at}` |
| `booting` | the machine kept (`instance_id`, its rent in `rent`) answered ssh; the repo step and the upload come next |
| `setup` | `uploaded` is true: the upload and setup are under way (or were cut off) |
| `ready` | the launch finished; whether the machine runs now comes from Vast |
| `failed` | the launch failed; `error` says why; `instance_id` is null once the machine is destroyed |

Other fields: `req` (hours or null, gpus, gpu_ram_gb, gpu_models, cpus, ram_gb, disk_gb,
min_down_mbps), `mode` (`autonomous`, `colleague` or `interactive`; follows the machine's mode after a
hand-off, 6.5), `claude_settings` (only the keys that are set: any of `model`, `effortLevel`,
`modelSettings`, `fastMode`; 4.5), `max_dph` (the most per hour it may rent at without asking again: the setting, or
the price approved), `fake_claude`, `setup` (`--setup`: the command that installs the project's dependencies, `none`,
or null to detect it on the machine; 6.9), `secrets` (names), `session_id`, `repo` (slug, workspace, branch,
start_sha, local_dir, carried, untracked, deploy_key `{id, file, created}` (4.3), branch_created, dir on
the machine), `offers` (the pick and up to two fallbacks, in the order they are rented),
`failed_offers`, `rents`, `strays` (unconfirmed rents the race left behind: `{label, offer_id, at}`),
`rent`, `instance_id`, `dph`,
`disk_usd_day` (the disk's cost per day, from the offer's storage price; billed while stopped too),
`machine`, `short`, `ssh` (host, port), `uploaded`, `pending` (`{reason, max_dph}`), `last_start`
(when crab-student last rented or started the machine), `stopped_since` (when `list` or `status` first
saw it stopped), `stop_note` (what it left behind, from `stop`; 5.3), `warnings`, `last_seen` (the last
status seen, for display only), `image`, `idle_minutes`, `notify_url` (copied at launch). Decisions
never rely on `last_seen`: `delete` checks the machine itself, or a stop note.

Names are 3-40 characters of a-z, 0-9 and "-". Branch `agent/<name>`; ssh alias `crab-student-<name>`;
rent label `crab-student-<name>-<8 hex>` (new for every rent request); deploy key title
`crab-student <created>`; tmux session `agent` on the machine (`agents` for the local `open --all`).

## 4. The launch

1. Plan (writes nothing anywhere): check the name, the brief and the secrets; the mode and Claude's
   settings (4.5); the repo (4.1); the offers (4.2); `--dry-run` stops here.
2. Under the agent's lock (the name must still be free): save the record (`renting`, or `pending` and
   exit 3 when no machine that fits is within `approve_above_usd_per_hour`).
3. `bring_up` (also what `approve` and `start` use for unfinished launches), the race: rent the first
   two offers not yet used, each with a new label saved in `rents` before its one rent request; a
   definite refusal fails that offer and the next is rented. Once a machine is known, the deploy key
   (4.3) and branch (once; a failure destroys every machine rented for the launch). Every 5 s, each
   machine in flight: an unconfirmed rent is looked for by its label; gone, `exited`/`offline`, an
   error in `status_msg` twice in a row, not running 5 minutes after its rent, or not answering ssh 3
   minutes after it runs fail it (ssh is tried at its address, its host key not kept): it is destroyed
   and the next unused offer rented in its place. Running with an address: add the ssh key, try ssh.
   The first machine that answers is kept (`booting`): the others are destroyed first, and an
   unconfirmed one not found goes to `strays` (the result then warns). With every offer failed the
   launch fails (exit 4; no credit: exit 1); with only unconfirmed rents left and none found for 2
   minutes, it stops and says so (`start` looks again, never sending them again). `start` continues
   from the record: the rents in flight keep their deadlines. Each bring-up destroys the machines of
   `strays` that have shown up, at its start and after setup.
4. Upload and setup in one ssh call (6.1). Setup's own failure destroys the machine and fails the
   launch (exit 5); anything else (a lost connection, no answer within an hour) leaves `setup` for
   `start` to continue: it starts the machine again if it stopped, and setup resumes. A failed launch
   tried again (`start`) plans afresh up to `max_dph`, and waits for approval if only pricier machines
   are left.
5. Progress: every line a launch prints (on stderr) also goes, with the time, to `agents/<name>.log`
   (the planning lines too, once the record exists; a failure as `Launch failed: <message>`). As soon as
   the record exists and the launch goes ahead (also for `approve` and a `start` that continues a
   launch), it opens a window running `crab-student open NAME` for each agent (5.4), unless the setting
   `open_window` is `off`: on Windows one Windows Terminal window (`wt -w new`) with a tab per agent, on
   macOS a Terminal window per agent (`osascript`). Elsewhere, or when none opens, it prints that
   command.

**4.1 The repo.** Default: the current repo's GitHub remote (origin first; the configured URL, not an
insteadOf rewrite), which needs admin rights (for the deploy key); the branch must not exist. The
starting commit is HEAD plus, as one extra commit, what `git commit -a` would commit (staged and
unstaged changes to tracked files, including staged new files), built from a copy of the index so the
user's index, worktree, branches and stash are untouched. Untracked files are not carried (a warning
names them). The commit is pushed to `agent/<name>` from the user's computer with the agent's deploy
key (Windows' own OpenSSH, GitHub's pinned host key, no prompts). `--no-repo`: `<login>/<workspace_repo>`
(created private with a README if missing), the branch made through the API from its default head.

**4.2 Offers.** What the launch asks for, when it does not say: 0 GPUs (a CPU task), 8 cores, 16 GB
RAM, `disk_gb` disk, `min_download_mbps`, `min_reliability`; `--hours` is optional. Vast's search
(on-demand, verified, rentable, not external, reliability, cores, RAM, disk, download speed, a direct
port; for GPU tasks the GPU count exactly, GPU memory and `cuda_max_good` from the image tag) is
re-checked on every result, plus GPU model substrings, `regions` (the country code at the end of
`geolocation`; continent names expand to countries), the countries where Anthropic's API is
unavailable (`offers.BLOCKED`, from https://www.anthropic.com/supported-countries: AF BY CN CU HK IR KP MM
MO RU SY VE YE never fit, whatever `regions` says; a host without a recognisable country code stays
allowed) and the host's lease: `end_date` must be at least
`max(2 x hours, 24) + 1` hours away (24 + 1 without `--hours`). Ranking: for GPU tasks, drop offers
more than 20% worse in dlperf per dollar than the best offer within 1.5x the cheapest price; among the
offers within 20% of the lowest price left, for an interactive agent (`--mode`, else the `mode`
setting) with `home` set: first those in the `home` country, then on its continent (`offers.REGIONS`),
then the rest; within that (and for every other launch), best first by reliability and download speed
together (100 x reliability + log2 of the download Mbps: a point of reliability counts as much as twice
the speed), then price and id; the rest after them, by price. Up to 4 fallbacks on other machines, at any price up to
`approve_above_usd_per_hour` (over it, when the launch needs approval: at most 1.25x the pick's price); the first is rented at the same time as the pick (4, step 3). The pick and its fallbacks come from the offers up to `approve_above_usd_per_hour` when
any fits, else from all of them (the launch then waits for approval, and `approve` plans again up to
the price it showed). A CPU task (0 GPUs) may rent a GPU host, with `NVIDIA_DISABLE_REQUIRE=1` in its
environment. The plan and the result carry no hour or cost estimate.

**4.3 Deploy keys.** GitHub emails a repo's admins about every deploy key added, so agents started on
a repo within 24 hours share one: a new agent takes the newest key another agent on the same repo
holds, if it was added less than 24 hours ago, its file is here and GitHub still has it; otherwise
crab-student makes a new ed25519 key in `keys/` and adds it (read-write). The key is removed from GitHub
and `keys/` when the last agent holding it is deleted (5.3). Taking and letting go of a key happen under
the repo's key lock, with the record saved inside it, so a delete never removes a key that a launch is
taking, and two deletes cannot each leave the key to the other. A machine's owner can copy its key, so
sharing is bounded: every key stops working once the agents it was given to are deleted.

**4.4 Several at once.** `launch --batch` reads the agents from stdin, each starting at a line
`=== NAME [flags]` (its flags over the batch's; secrets add up) and followed by its brief. All are
planned first, so any problem stops the batch before anything is rented; they share one starting
commit; each picks an offer no earlier agent in the batch picked, searched once per kind of machine.
Then each agent's backups are chosen among the offers that are no agent's pick, the first (the one it
races) one that no earlier agent races; an agent left without one races its pick alone. (A second
backup may be another agent's racer: renting a taken offer moves on to the next.) Those over
the price wait for approval; the rest are brought up side by side (one thread each, progress lines
prefixed `[name]`), a failure in one not stopping the others. The result
has each agent's result, all result lines, the warnings once each, and the worst exit code (a failure,
else 3 if any waits, else 0). `approve NAME ...` plans the named agents the same way (a machine each).

**4.5 Mode, Claude's settings and the result.** The mode is `--mode`, else the `mode` setting
(default `colleague`). For each of `model`, `effortLevel` and `fastMode` the value is the launch flag
(`--model`, `--effort`, `--fast`/`--no-fast`), else the setting (`model`, `effort`, `fast_mode`), else
the same key in the user's `~/.claude/settings.json` (a missing or invalid file gives nothing); a key
with no value is left out. Claude Code keeps the effort picked with `/effort` per model, under
`modelSettings.<model>.effortLevel`: without a chosen effort (flag or setting), those per-model levels
come along too (`modelSettings`, the effort levels only). The result is `claude_settings` in the record
and in the machine's `config.json`. A launch prints the brief and one result line, which the skill
relies on:

```
NAME: working on 1x RTX 4090 (Quebec, CA) at $0.42/hr - colleague - branch agent/NAME - watch: crab-student open NAME
NAME: ready on 1x RTX 4090 (Quebec, CA) at $0.42/hr - interactive - branch agent/NAME - open it: crab-student open NAME --vscode
NAME: NOT started - needs your approval: REASON - would rent SHORT at $X/hr - approve: crab-student approve NAME (or: crab-student decline NAME)
NAME (dry run): would rent SHORT at $X/hr - MODE - no approval needed
```

(`--json` gives `result_line`, `warnings`, `mode` and the rest.) An interactive agent's brief is the
task as the user would type it to Claude Code. `--setup "COMMAND"` (also on a batch's `===` lines) replaces
the machine's detection of how to install the project's dependencies, and `--setup none` skips the install
(6.9); the plan shows which.

## 5. Agents once launched

- **5.1 Views** (`list`, `status`): one Vast instance list, and one `cs.py status` over ssh per
  running ready agent (in parallel). States: approval; starting (a launch at work, Vast starting the
  machine, or no agent status yet); interrupted (a launch cut off, or a rent not confirmed); failed (the
  launch failed, the agent says so, or the machine no longer exists); unknown (Vast unreachable, or no
  answer over ssh); stopped (with the reason: stopped by you, idle stop, finished (idle after done), or
  "stopped itself" when the reason is not known here; how long ago; the disk's cost per day; or
  "stopping"); needs you (colleague mode only); done (" (not all pushed)" when
  git is not clean); working (idle for 10+ minutes shows in the message); interactive (Claude is in
  VS Code and has no status of its own: the message says "in VS Code, active" or "in VS Code, idle
  12m", from the conversation's activity). Views show the mode, the frontend (`tmux` or `vscode`), the
  record's `claude_settings` and (`status`) the dependency install (6.9). When `status` reports a mode other than the record's (after a
  hand-off), the record is updated. An agent whose status says it sent its stop call over 5 minutes ago
  while Vast still shows it running is stopped from here. For an agent not at work that has `strays`, a
  machine in Vast's list with one of those labels is destroyed and the label dropped.
- **5.2 stop** (any machine that received its files): `cs.py save --reason user --final` (best effort;
  a committed stop note is kept as the record's `stop_note`, with `unpushed` and `recorded_at`), then
  Vast `stopped`. **start** (stopped): Vast `running` (`last_start` set), the boot wait (it tolerates
  `exited` while the host makes room, and never destroys). The machine's boot then starts the
  watchdog, which resumes the agent's conversation.
- **5.3 delete**: pending -> removed. A machine that received its files: running -> `cs.py check`;
  stopped -> the stop note of its latest stop: the newer of the record's `stop_note` (if recorded after
  `last_start`) and `agent-runs/<name>/stopped.json` on its branch (if written 5+ minutes after
  `last_start`: a stop that could not push one leaves an older one); none, or GitHub unreachable ->
  needs `--force` (unreachable too). `list`/`status` seeing a stopped machine run again (started from
  Vast's website, say) moves `last_start` to then and drops `stop_note`. Refused (exit 3, with the list
  and a copy command) for unpushed commits, uncommitted files, files in `/root/outputs`, or untracked
  files of 100 MB+ outside git. Then destroy (with the machines of any `rents` in flight and of
  `strays`, by id or by label; a label not found is named in a warning),
  remove the deploy key unless another agent holds it (4.3), remove the branch only if it still points
  at `start_sha`, forget the host key and the record.
- **5.4 open.** `open NAME` on an agent that is not ready follows its launch first: its log so far, then
  each new line (the status line rewritten in place on a terminal), until the launch ends (the phase is
  ready, failed or pending, or nobody holds the agent's lock for 3 looks in a row: cut off). Ready: it
  goes on as below; otherwise it says what happened and what to do (exit 1). On a ready agent whose
  frontend is `tmux`: `agent-attach` over `ssh -t` (6.7). When the frontend is `vscode` (the machine's
  `status` says so, or, if it cannot be asked, the record's mode is interactive), it opens VS Code as
  `--vscode` does; then, on a terminal, it says the agent works in VS Code and that the window becomes
  the agent's terminal after `/handoff` (Ctrl+C closes it), and waits with one `cs.py wait-handoff 600`
  call after another (it stops waiting, and says why, when the machine cannot be reached); once the
  frontend is `tmux` it attaches as above. `open --all`: the agents that are running, side by side (Windows
  Terminal panes, else a local tmux session `agents` with prefix Ctrl-a); agents whose frontend is
  `vscode` are skipped, with a note. `open NAME --vscode`: VS Code opens
  `ssh-remote+crab-student-<name>` on the repo folder (needs the `code` command). When the frontend is
  `vscode` (an interactive agent), it waits a fixed 15 s for VS Code to connect, then opens
  `vscode://anthropic.claude-code/open?prompt=<the brief, URL-encoded>` through the OS URL handler, which
  fills the Claude panel without sending (a brief over 5,000 characters, the extension's limit, is
  replaced by a pointer to `/root/BRIEF.md`). While the dependency install is running or failed (6.9;
  its state comes from the same `cs.py status` call that told the frontend), a note saying so, and that
  `agent-status` shows it, follows the brief. VS Code's server and the Claude extension are already on the
  machine (6.9), so the connect is quick; nothing is printed for the user to paste.

## 6. The machine

`D=/root/crab-student`; the repo at `/root/work/<repo>`; big outputs in `/root/outputs`; the brief
moved to `/root/BRIEF.md`, where the agent reads it (the agent never needs `D`).

**6.1 Upload and setup.** One ssh command: `mkdir -p D && tar -xzf - -C D --no-same-owner && cd D &&
<python> cs.py setup`, with a tar.gz on stdin: the machine code (`cs.py`; `fake_claude.py` only for `--fake-claude`; the
agent's rules `rules.md` with `rules-autonomous.md` and `rules-colleague.md`; and `handoff.md`, the
`/handoff` command), `config.json` (0600: name, branch, repo_url, repo_dir, mode, claude_settings,
fake_claude, session_id, idle_minutes, notify_url, secret_names, setup (6.9), vscode_commit: the local
VS Code's commit for an interactive agent when VS Code is installed here, else null (6.9)), `BRIEF.md`, `claude.env` and `secrets.env` (0600,
`export K=<quoted>` lines), `deploy_key` (0600), `github_known_hosts`. Setup prints `step i/5 <label>`
lines, then one JSON object `{"ok": true, "warnings": [...]}` or `{"ok": false, "step", "label", "error"}`;
on a machine where setup was done before, it only restarts the watchdog (`{"ok": true, "resumed": true}`). The
steps: `environment` (when `vscode_commit` is set, VS Code's server and the Claude extension installed by a
job that setup waits for at its end (6.9); the login
profile `/etc/profile.d/agent.sh`, which puts the repo's `.venv/bin`, `~/.local/bin` and
`/venv/main/bin` first on PATH, sources `claude.env` and `secrets.env`, sets `VIRTUAL_ENV` to the repo's
`.venv` when it exists, else `/venv/main`, and starts interactive shells that begin in the home folder
in the repo; a line at the end of `~/.bashrc` runs it again, after any activation of the image's own;
`agent-attach` and `agent-status`, plus `agent-handoff` for interactive; git hooks; the git identity
`<name> (Claude Code agent) <name@agent.invalid>`; `.venv/`, `*.egg-info/` and `__pycache__/` added to
git's global ignore file `~/.config/git/ignore`, so what the dependency install leaves in the repo is
never committed; Claude Code and tmux installed by jobs that `Claude Code` waits for),
`repository` (clone the branch with the deploy key; `core.sshCommand`, `core.hooksPath`, a push refspec
for the branch only; the dependency install started in the background, 6.9), `agent settings`
(`BRIEF.md` moved to `/root/BRIEF.md`; autonomous and colleague: `~/.claude/CLAUDE.md` from `rules.md`
with the mode's section and the dependency install's state; interactive: `handoff.md` moved to
`~/.claude/commands/` (other modes delete it); all modes: `~/.claude/settings.json` and VS Code's
machine-level settings (6.5), the onboarding and trust flags in `~/.claude.json`), `Claude Code` (wait for the installs; one tiny `claude -p` call checks the
token), `starting the agent` (refused without the container's Vast key; `setup-done`, the watchdog;
autonomous and colleague: wait for the conversation file; interactive: nothing runs in tmux).

**6.2 onstart** (Vast runs it as root at every container start; each command ends in `;` or `&`, so it
works with or without line breaks): write `CONTAINER_ID`, `CONTAINER_API_KEY`, `CS_VAST_URL` to
`vast.env` (0600) and the start time to `booted`; start the dead-man loop; start the watchdog if
`setup-done` exists. **Dead-man loop:** every 60 s, if the newer of `booted` (its content, epoch
seconds) and `heartbeat`'s mtime is more than 2,700 s old, call Vast stop with the container's key.

**6.3 The watchdog** (`cs.py watch`, one per container by flock). Every 60 s (at once when a command
creates the file `wake`, as `agent-handoff` does): touch `heartbeat`; while the mode is autonomous or
colleague, keep the tmux session `agent` (running `cs.py claude`) alive; add the elapsed time (at most
two ticks) to `used_seconds` in `state.json` (shown as `used_hours`; nothing is decided from it). Busy =
the total size of the conversation files under `~/.claude/projects` (searched recursively, subagents'
too) changed, so a conversation in VS Code counts as much as one in tmux; the container's own cgroup
CPU grew by 0.25 core or more; or a GPU is 10%+ busy: a running job counts through its CPU or GPU.
Rules: in colleague mode, a nudge typed to the agent after half of `idle_minutes` of quiet while it says
`needs-you` (once per quiet stretch); in autonomous and colleague mode, a check-in typed after 20 minutes
without a word while it says `working` (it may have ended its turn with a job running; once per silent
stretch); in interactive mode neither. **Stop** after `idle_minutes` of quiet, whether the agent is done
or not. Typed messages start with `Automated message from the machine:`. A stop sets `stopping`,
notifies, saves (6.6, final), records `stop_sent_at` and calls Vast stop with the container's key (from
`vast.env`, else process 1's environment), retried every tick. The watchdog's `stop_reason` is `idle`,
or `done` (an idle stop while the agent's status was done); a stop by the user (`crab-student stop`) is
recorded locally and in its stop note (reason `user`). A new container start resets the idle and quiet
clocks and `used_seconds`, so `used_hours` is the machine time of the current start.
Notifications (`notify_url`): the agent entering needs-you, done or failed, and the watchdog's stops (a
generic JSON POST carries `agent`, `event`, `message` and `text`).

**6.4 Modes.** The machine's mode starts as the launch's and changes only by hand-off (6.5).

| | autonomous | colleague | interactive |
|---|---|---|---|
| Claude runs | in tmux, from the start | in tmux, from the start | in VS Code's Claude panel, driven by the user |
| rules (`~/.claude/CLAUDE.md`) | base + autonomous | base + colleague | none |
| report template, `agent-status` | yes; `needs-you` is refused with a message to decide and record the decision in the report | yes | no report; `agent-status` alone shows the machine (setting a state is refused) |
| watchdog reminders | check-in | nudge, check-in | none |
| `status` `frontend` | `tmux` | `tmux` | `vscode`, then `tmux` after hand-off |

The base rules say where the agent is (a rented cloud machine that stops when idle), where its work
must be (committed and pushed on its branch; big files in `/root/outputs`), how to keep long jobs
alive, and how to write the report. Colleague: decide ordinary things, ask only about important or
hard-to-reverse choices, record any decision made when nobody answers. Autonomous: never ask; record
each real decision with its reasons in the report. In every mode the hooks, the autosave before every
stop, the idle stop and the dead-man switch apply.

**6.5 Claude Code** in tmux runs as `claude --dangerously-skip-permissions` (the model, effort and fast
mode come from the settings file, never from flags) with `--session-id <id> "<first prompt>"` the first
time and `--resume <id>` after (the first resume in a pane adds a restart note). A resume that fails at
once starts a new session that reads the report. Environment: without `CLAUDECODE`, `CLAUDE_CODE_*`
(a hand-off runs inside VS Code's Claude) and `CONTAINER_*`; with `IS_SANDBOX=1`,
`DISABLE_AUTOUPDATER=1`, `AGENT_NAME`, `AGENT_BRANCH`, the token and task secrets, and the login
profile's PATH and `VIRTUAL_ENV` (6.1). When Claude Code
exits, the pane waits for Enter.

`~/.claude/settings.json` (0600, every mode): `permissions.defaultMode: "bypassPermissions"` and deny
rules for the obvious ways around the hooks; `env` with `IS_SANDBOX=1`, `DISABLE_AUTOUPDATER=1`, the
token and task secrets (so a Claude Code started by VS Code has them too); and `claude_settings` merged
over them (only `model`, `effortLevel`, `modelSettings` and `fastMode` are taken). VS Code's
machine-level settings (`~/.vscode-server/data/Machine/settings.json`, nothing in the user's repo):
`claudeCode.initialPermissionMode: "bypassPermissions"`, `claudeCode.allowDangerouslySkipPermissions:
true`, `python.defaultInterpreterPath` (the repo's `.venv/bin/python` when it exists, else
`/venv/main/bin/python`, else `python3`; written again when the dependency install ends, and it
overrides the user's own, a path on their computer), and a default terminal profile that runs `agent-attach`, so a new integrated terminal shows the
tmux session `agent` once there is one. The login profile also exports the token for the VS Code
server. Whether the extension picks these up is to be verified on a real machine.

**Hand-off** (interactive only): the user types `/handoff [autonomous|colleague] [what to continue
with]` in the Claude panel, or asks in words. The command (`~/.claude/commands/handoff.md`) has
Claude commit and push, then run `agent-handoff --session=${CLAUDE_SESSION_ID} <mode> "<what to
continue with>"` (default `colleague`), then end its turn. `agent-handoff` refuses once handed off. It
takes the conversation's id from `--session=` (if that transcript exists), else `CLAUDE_CODE_SESSION_ID`
(which Claude Code sets for its Bash commands), else the newest transcript. It writes that mode's rules
to `~/.claude/CLAUDE.md`, removes the `/handoff` command, picks a new session
id (`D/session_id`), writes `handoff.json` `{mode, from, prompt, at}` (the machine's mode is now its
mode), wakes the watchdog and waits up to 90 s for Claude in tmux. The watchdog starts `claude --resume
<from> --fork-session --session-id <new id> "<hand-off prompt>"`, so the tmux agent has the whole
conversation under a new session id and the two sessions never clash; the hand-off prompt starts with
`Automated message from the machine:` and has the agent read its new rules. If that fails at once, it
forks without `--session-id` and resumes the newest conversation begun since (never the VS Code one);
if that fails too, a new conversation starts from the brief, with what the user asked for. Later
restarts resume the fork. The command prints a line telling the VS Code conversation to stop and the
user that they can close VS Code. From then on the agent is a tmux agent of that mode; losing the VS
Code connection does not matter. `status` then reports the new mode and `tmux`, and the local record
follows (5.1).

**6.6 Save before stopping**: on the agent's branch and not mid-merge or rebase: stage tracked changes
and untracked files up to 10 MB each (at most 2,000 files and 100 MB in all; the rest are reported as
skipped), commit `Autosave (<reason>)` unless the pre-commit hook refuses, push the branch. A **final** save (before every stop: the watchdog's, and `crab-student stop`'s) also writes the
stop note `agent-runs/<name>/stopped.json` into the same commit (added even if ignored): `{stopped_at
(epoch seconds), reason, outputs {files, bytes, sample}, large_files [{path, bytes}], not_committed
(first 50 paths git would still list after the commit), not_committed_count}`. A refused commit commits
no note (and puts the note file back as it was).

**6.7 Commands over ssh** (`cd D && <python> cs.py ...`; each prints one JSON object last; a failure
prints `{"error": ...}` and exits 1): `status` (name, mode, frontend, agent {state, message, at} (`{}`
until the agent sets a status, so always for an interactive agent in VS Code), idle_seconds,
used_hours, stopping, stop_reason, stop_sent_for, git {branch, head, dirty, unpushed}, deps {state,
command, exit_code} (6.9)); `wait-handoff SECONDS` (waits until the frontend is `tmux` and the tmux session runs, or the
time is up: {frontend}); `save --reason R
[--final]` (committed, pushed, skipped, dirty, unpushed, error, note, note_committed); `check` (save's
fields plus outputs {files, bytes, sample} and large_files); `send` (text on stdin, typed into the pane
as one bracketed paste; refused while Claude is in VS Code). `open` runs `agent-attach` with `ssh -t`
(`tmux -u attach`; a login shell if the session is gone, or while Claude is in VS Code, with a hint).
There is no command that changes how long a machine may run.

**6.8 The agent's tools.** `agent-status working|needs-you|done|failed "<message>"` (autonomous and
colleague) writes `agent.json`; `agent-status` alone (every mode) shows the status, the dependency install
(6.9: running for how long, done, failed with the end of its log, cut off by a stop, or none) and the
machine time; `done` is refused until `agent-runs/<name>/REPORT.md` is committed,
nothing is uncommitted and nothing is unpushed, and `needs-you` is refused in autonomous mode.
`agent-handoff` and `/handoff` (interactive) are 6.5. Hooks: `pre-commit` refuses staged lines
containing a secret's value (8+ characters) and files over 50 MB; `pre-push` refuses any ref but
`refs/heads/<branch>`, and deleting it. The agent's Claude Code settings deny the obvious ways around
the hooks.

**6.9 Dependency install and VS Code's server.** The dependency install is the one background job: started
by setup and never waited for, so nothing in setup or the launch fails because of it, and setup running again
leaves it alone when it is done or under way. VS Code's server is a setup job: waited for, and only ever a
warning.

- **The dependency install** (`cs.py deps`, started in the `repository` step; output in
  `logs/deps.log`). The command is the config's `setup`, or, when it is null, the first that fits the
  repo: `uv.lock`, or `[tool.uv...]` in `pyproject.toml` -> uv installed with its official installer if
  missing, then `uv sync`; `[project]` in `pyproject.toml` -> `<python> -m pip install -e .`;
  `requirements*.txt` -> `<python> -m pip install -r` each; `setup.py` -> `<python> -m pip install -e .`.
  `<python>` is `/venv/main/bin/python` when it exists, else `python3`. `setup` `none`, or nothing that
  fits: no install (`{"state": "none", "skipped"}`). It runs `bash -c` in the repo with the agent's
  environment (6.5) without `VIRTUAL_ENV`, plus `PIP_ROOT_USER_ACTION=ignore`, `PIP_PROGRESS_BAR=off` and
  `TERM=dumb`. `D/deps.json`: `{state: running|done|failed|none, command, started, finished,
  exit_code}`; a `running` older than the container start was cut off by a stop and is not run again by
  itself (`agent-status` says to run it). When it ends, VS Code's interpreter path is written again
  (6.5).
- **VS Code's server** (only for an interactive agent, when VS Code is installed on the user's computer: the
  launch puts its commit, the second line of `code --version`, in the config as `vscode_commit`, else null;
  output in `logs/vscode.log`). In the `environment` step a job, started next to the Claude Code install,
  downloads `https://update.code.visualstudio.com/commit:<commit>/server-linux-<arch>/stable` (`<arch>` x64
  or arm64) and unpacks it straight into `~/.vscode-server/cli/servers/Stable-<commit>/server`, where
  Remote-SSH looks for it (skipped when `bin/code-server` is already there), then installs
  `anthropic.claude-code` with the server's own CLI (`bin/code-server --install-extension ... --extensions-dir
  ~/.vscode-server/extensions`). Setup waits for it, up to 10 minutes, at the very end, before it prints
  its result. A failure (or the wait running out) does not fail setup: it is a warning in the result's
  `warnings`, which the launch prints as a `Warning:` progress line and returns in its own `warnings`, and
  VS Code then installs its server itself when first opened, as it always does.
