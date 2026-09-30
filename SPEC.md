# crab-student: protocols and invariants

The contracts between the parts: what is stored where, what crosses ssh, how machines start and stop,
and the rules that must hold. It describes behaviour, not code. (`docs/REQUIREMENTS.md` is what the
tool is for.)

## 1. Safety invariants

1. **Money.** Every rent and every start of a stopped worker passes the approval gate: a budget over
   `auto_approve_usd`, or more running machines than `max_running_machines`, records a `pending`
   action and exits 3 instead. `extend` passes the budget check. Only `approve` skips the gate, and it
   is an ask-rule command. Every worker has a time limit in running hours over all its starts.
2. **Never rent twice.** A rent request is never retried. Its label is saved in the record before the
   request is sent. A request whose outcome is unknown (timeout, connection error, 5xx) is resolved
   only by finding an instance with that exact label; if none appears, the launch stops and says so.
3. **Own machines only.** crab-student acts only on instance ids in its records, or on an instance
   carrying the exact label of one of its rents.
4. **Never destroy work.** Code destroys a machine by itself only before its upload (`uploaded` false:
   a boot that failed or timed out, a GitHub step that failed) or right after a first setup failed (the
   worker never ran). Setup on a machine where it was done before only resumes. `delete` checks any
   machine that received its files (5.4); a machine that cannot be checked needs `--force`.
5. **Stops are bounded.** The watchdog stops the machine at the time limit, after `idle_minutes`
   without activity, and 15 minutes after the worker is done and quiet, always saving first. Setup
   refuses a machine that has no key to stop itself. The dead-man switch stops it if the watchdog never
   runs or dies, and `list`/`status` finish a stop Vast did not take within 5 minutes. Nothing on the
   machine ever destroys one.
6. **Secrets** (the Claude token, deploy keys, task secrets) never appear on command lines, in logs,
   the brief, records or git. They reach the machine only inside the upload stream, as 0600 files.
7. **The launcher** (a background Claude Code agent) runs only allowed commands (`setup.ALLOW`);
   everything that spends beyond the gate, destroys, discards or changes settings is in `setup.ASK`, and
   editing crab-student's own files is denied (`setup.DENY`).

## 2. Local files

```
~/.crab-student/
  config.json            settings that differ from the defaults (README lists them)
  secrets.json           {"names": [...]}: names of stored task secrets
  secrets/<key>          0600, only when no system keyring works
  workers/<name>.json    the worker's record (3); workers/<name>.lock its OS lock
  workers/<name>.brief.md  the brief
  workers/_keys-<owner>--<repo>.lock   the repo's deploy key lock (4.3)
  keys/<owner>--<repo>-<8 hex>(.pub)   deploy keys, shared by workers on that repo (4.3)
  ssh/id_ed25519(.pub)   the key crab-student adds to machines
  ssh/config             generated Host entries (crab-student-<name>), included from ~/.ssh/config
  ssh/known_hosts        machines' host keys, under HostKeyAlias crab-student-<name>
  ssh/github_known_hosts GitHub's pinned ed25519 host key
~/.config/vastai/vast_api_key   the Vast key (the vastai CLI's file; XDG_CONFIG_HOME is honoured)
```

Secrets live in the OS keyring, service `crab-student`: `claude-oauth-token` (the Claude token for
workers) and `task-secret:<NAME>`. A keyring that fails or silently drops values falls back to
`~/.crab-student/secrets/`.

## 3. The worker record

One JSON file per worker, written only by the command that holds the worker's lock. `phase` says how
far the launch got (only the local side writes it):

| phase | meaning |
|---|---|
| `pending` | waiting for approval (`pending.action` = launch); nothing rented, nothing on GitHub |
| `renting` | about to rent, or a rent was sent (`rent.label`, `rent.offer_id`, `rent.at`) |
| `booting` | `instance_id` known, waiting for the machine to run and answer ssh |
| `setup` | `uploaded` is true: the upload and setup are under way (or were cut off) |
| `ready` | the launch finished; whether the machine runs now comes from Vast |
| `failed` | the launch failed; `error` says why; `instance_id` is null once the machine is destroyed |

Other fields: `req` (hours, gpus, gpu_ram_gb, gpu_models, cpus, ram_gb, disk_gb, min_down_mbps),
`budget_usd`, `auto_budget`, `limit_hours` (budget / the rented offer's $/hr),
`model`, `fake_claude`, `secrets` (names), `session_id`, `repo` (slug, workspace, branch, start_sha,
local_dir, carried, untracked, deploy_key `{id, file, created}` (4.3), branch_created, dir on the
machine), `offers` (the pick and up to two fallbacks), `failed_offers`, `instance_id`, `dph`, `machine`,
`short`, `ssh` (host, port), `uploaded`, `pending` (`{action: launch|start|extend, reason, hours?}`),
`warnings`, `last_seen` (the last status seen, for display only), `image`, `idle_minutes`, `notify_url`
(copied at launch). Decisions never rely on `last_seen`: `delete` checks the machine itself.

Names are 3-40 characters of a-z, 0-9 and "-". Branch `agent/<name>`; ssh alias `crab-student-<name>`;
rent label `crab-student-<name>-<8 hex>` (new for every rent request); deploy key title
`crab-student <created>`; tmux session `worker`.

## 4. The launch

1. Plan (writes nothing anywhere): check the name, the brief and the secrets; the repo (4.1); the
   offers and budget (4.2); `--dry-run` stops here.
2. Under a global lock: the gate; save the record (`renting`, or `pending` and exit 3).
3. `bring_up` (also what `approve` and `start` use for unfinished launches): for each offer not yet
   failed, save a new label, send one rent request; a definite refusal moves on; an unknown outcome is
   looked for by label for 2 minutes. Then the deploy key (4.3) and branch (once), then wait for the
   machine (every 5 s, at most 15 minutes from the rent): gone, `exited`/`offline`, or an error in
   `status_msg` twice in a row fail it, and it is destroyed and the next offer tried. Running with an
   address: add the ssh key, wait for ssh.
4. Upload and setup in one ssh call (6.1). Setup's own failure destroys the machine and fails the
   launch (exit 5); anything else (a lost connection, no answer within an hour) leaves `setup` for
   `start` to continue: it starts the machine again if it stopped (through the gate), and setup resumes.

**4.1 The repo.** Default: the current repo's GitHub remote (origin first; the configured URL, not an
insteadOf rewrite), which needs admin rights (for the deploy key); the branch must not exist. The
starting commit is HEAD plus, as one extra commit, what `git commit -a` would commit (staged and
unstaged changes to tracked files, including staged new files), built from a copy of the index so the
user's index, worktree, branches and stash are untouched. Untracked files are not carried (a warning
names them). The commit is pushed to `agent/<name>` from the user's computer with the worker's deploy
key (Windows' own OpenSSH, GitHub's pinned host key, no prompts). `--no-repo`: `<login>/<workspace_repo>`
(created private with a README if missing), the branch made through the API from its default head.

**4.2 Offers and budget.** Vast's search (on-demand, verified, rentable, not external, reliability,
cores, RAM, disk, download speed, a direct port; for GPU tasks the GPU count exactly, GPU memory and
`cuda_max_good` from the image tag) is re-checked on every result, plus GPU model substrings, `end_date`
and `regions` (the country code at the end of `geolocation`; continent names expand to countries).
Ranking: for GPU tasks, drop offers more than 20% worse in dlperf per dollar than the best offer within
1.5x the cheapest price; among the offers within 10% of the lowest price left, the fastest download
first; then price and id. Up to 2 fallbacks on other machines at most 1.25x the pick's price. Budget:
`ceil_cents(dph x max(budget_factor x hours, min_budget_hours))`; an explicit `--budget` instead keeps
only offers with `dph x hours <= budget`. A CPU task (0 GPUs) may rent a GPU host, with
`NVIDIA_DISABLE_REQUIRE=1` in its environment.

**4.3 Deploy keys.** GitHub emails a repo's admins about every deploy key added, so workers started on
a repo within 24 hours share one: a new worker takes the newest key another worker on the same repo
holds, if it was added less than 24 hours ago, its file is here and GitHub still has it; otherwise
crab-student makes a new ed25519 key in `keys/` and adds it (read-write). The key is removed from GitHub
and `keys/` when the last worker holding it is deleted (5.4). Taking and letting go of a key happen under
the repo's key lock, with the record saved inside it, so a delete never removes a key that a launch is
taking, and two deletes cannot each leave the key to the other. A machine's owner can copy its key, so
sharing is bounded: every key stops working once the workers it was given to are deleted.

## 5. Workers once launched

- **5.1 Views** (`list`, `status`): one Vast instance list, and one `cs.py status` over ssh per
  running ready worker (in parallel). States in order: approval, launching (lock held), rent unknown,
  interrupted, launch failed, unknown (Vast unreachable), gone, starting, stopped (with the reason:
  stopped by you, time limit, or stopped itself), unreachable, stopping, needs you, done / done* (not
  all pushed), failed, starting (no status yet), idle (10+ minutes), working. A worker whose status
  says it sent its stop call over 5 minutes ago while Vast still shows it running is stopped from here.
- **5.2 stop** (any machine that received its files): `cs.py save --reason user` (best effort), then
  Vast `stopped`. **start** (stopped): the time-limit check, the gate (machines, under the gate lock),
  Vast `running`, the boot wait (it tolerates `exited` while the host makes room, and never destroys),
  `set-limit`; if the machine reports the limit reached it is stopped again and that is remembered.
- **5.3 extend**: budget += hours x $/hr through the gate's budget check; `set-limit` now if running,
  else at the next start.
- **5.4 delete**: pending -> removed. A machine that received its files: running -> `cs.py check`,
  refused (exit 3, with the list and a copy command) for unpushed commits, uncommitted files, files in
  `/root/outputs`, or untracked files of 100 MB+ outside git; stopped or unreachable -> needs `--force`.
  Then destroy, remove the deploy key unless another worker holds it (4.3), remove the branch only if
  it still points at `start_sha`, forget the host key and the record.

## 6. The machine

`D=/root/crab-student`; the repo at `/root/work/<repo>`; big outputs in `/root/outputs`.

**6.1 Upload and setup.** One ssh command: `mkdir -p D && tar -xzf - -C D --no-same-owner && cd D &&
<python> cs.py setup`, with a tar.gz on stdin: the machine code (`cs.py`, `fake_claude.py`, `worker.md`),
`config.json` (0600: name, branch, repo_url, repo_dir, model, fake_claude, session_id, idle_minutes,
limit_hours, notify_url, secret_names), `BRIEF.md`, `claude.env` and `secrets.env` (0600, `export K=<quoted>`
lines), `deploy_key` (0600), `github_known_hosts`. Setup prints `step i/5 <label>` lines, then one JSON
object `{"ok": true}` or `{"ok": false, "step", "label", "error"}`; on a machine where setup was done
before, it only restarts the watchdog (`{"ok": true, "resumed": true}`). The steps: environment (profile,
`crab-status`, `crab-attach`, git hooks, identity; Claude Code and tmux installed in the background),
repository (clone the branch with the deploy key; `core.sshCommand`, `core.hooksPath`, a push refspec
for the branch only), worker settings (`~/.claude/CLAUDE.md` from `worker.md`, `settings.json`, the
onboarding and trust flags in `~/.claude.json`), Claude Code (wait for the installs; one tiny
`claude -p` call checks the token), starting the worker (refused without the container's Vast key;
`setup-done`, the watchdog; wait for the conversation file).

**6.2 onstart** (Vast runs it as root at every container start; each command ends in `;` or `&`, so it
works with or without line breaks): write `CONTAINER_ID`, `CONTAINER_API_KEY`, `CS_VAST_URL` to
`vast.env` (0600) and the start time to `booted`; start the dead-man loop; start the watchdog if
`setup-done` exists. **Dead-man loop:** every 60 s, if the newer of `booted` (its content, epoch
seconds) and `heartbeat`'s mtime is more than 2,700 s old, call Vast stop with the container's key.

**6.3 The watchdog** (`cs.py watch`, one per container by flock). Every 60 s: touch `heartbeat`; keep
the tmux session `worker` (running `cs.py claude`) alive; add the elapsed time (at most two ticks) to
`used_seconds` in `state.json`. Busy = the conversation files under `~/.claude/projects` grew, the
container's own cgroup CPU grew by 0.25 core or more, or a GPU is 10%+ busy. Rules: a nudge typed to
the worker after half of `idle_minutes` of quiet while it says `needs-you` (once per quiet stretch); a
check-in typed after 20 minutes without a word while it says `working` (it may have ended its turn
with a job running; once per silent stretch); a warning 10 minutes before the limit (again after an
extend); **stop** at `used >= limit` (not within 5 minutes of a start), 15 minutes after `done` with
nothing happening, or after `idle_minutes` of quiet. A stop sets `stopping`, notifies, saves (6.5),
records `stop_sent_at` and calls Vast stop with the container's key (from `vast.env`, else process 1's
environment), retried every tick. A new container start resets the idle and quiet clocks and the boot
grace, never `used_seconds`.
Notifications (`notify_url`): the worker entering needs-you, done or failed, and the watchdog's stops.

**6.4 Claude Code** runs in the tmux pane as `claude --dangerously-skip-permissions [--model M]` with
`--session-id <id> "<first prompt>"` the first time and `--resume <id>` after (the first resume in a
pane adds a restart note). A resume that fails at once starts a new session that reads the report.
Environment: without `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT` and `CONTAINER_*`; with `IS_SANDBOX=1`,
`DISABLE_AUTOUPDATER=1`, the token and task secrets. When Claude Code exits, the pane waits for Enter.

**6.5 Save before stopping**: on the worker's branch and not mid-merge or rebase: stage tracked changes
and untracked files up to 10 MB each (at most 2,000 files and 100 MB in all; the rest are reported as
skipped), commit `crab-student: autosave (<reason>)` unless the pre-commit hook refuses, push the
branch.

**6.6 Commands over ssh** (`cd D && <python> cs.py ...`; each prints one JSON object last; a failure
prints `{"error": ...}` and exits 1): `status` (name, worker {state, message, at}, idle_seconds,
used_hours, limit_hours, stopping, stop_reason, stop_sent_for, git {branch, head, dirty, unpushed});
`save --reason R`; `check` (save's fields plus outputs {files, bytes, sample} and large_files);
`set-limit H`; `send` (text on stdin, typed into the pane as one bracketed paste). `open` runs
`crab-attach` with `ssh -t` (`tmux -u attach`; a shell if the session is gone).

**6.7 The worker's tools.** `crab-status working|needs-you|done|failed "<message>"` writes
`worker.json`; `done` is refused until `agent-runs/<name>/REPORT.md` is committed, nothing is
uncommitted and nothing is unpushed. Hooks: `pre-commit` refuses staged lines containing a secret's
value (8+ characters) and files over 50 MB; `pre-push` refuses any ref but `refs/heads/<branch>`, and
deleting it. The worker's Claude Code settings deny the obvious ways around the hooks.
