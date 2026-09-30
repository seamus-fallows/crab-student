# crab-student: requirements

What the tool must do, for whoever builds it. It says nothing about how to structure the code: design
that yourself. Section 9 lists facts about the environment that are expensive to discover by trial and
error; they are facts, not design advice.

## 1. What it is

crab-student lets someone who uses Claude Code on their own computer hand a long or heavy task
(training a model, running experiments, processing data, a big refactor, anything slow) to Claude Code
running on a rented Vast.ai machine, with a GPU or CPU only, as the task needs. The user asks in plain
words in their own Claude Code session ("run the ablation on a GPU"); a few minutes later a remote
Claude Code is working on it on its own git branch, and the user can watch and steer it from any
terminal. Results come back as a git branch and a written report. The bill stays bounded without the
user having to think about it.

The name is a pun on "grad student": a crab (Claude Code's mascot is a crab) doing your research.

## 2. The user's experience

1. **Setup, once:** `crab-student init` walks the user through everything the tool needs (Vast API key,
   GitHub login, a Claude token for the remote workers, SSH key, the Claude Code integration). Clear,
   friendly, short text. Re-running it is safe and only asks for what is missing.
2. **Launching:** the user tells their Claude Code what to run. Claude Code (taught by a skill that
   init installs) starts the launch in the background so the user's conversation is not held up. It
   estimates what the task needs (GPUs and GPU memory, CPU cores, RAM, disk, how many hours) and writes
   a clear task brief for the remote worker. The tool picks a machine that fits, rents it and sets it
   up. The user never types a price: the budget defaults to about twice the estimated cost. The user's
   Claude Code shows them the brief it gave the worker and a one-line result (which machine, $/hr,
   estimated time and cost, the budget, the branch, how to watch it).
3. **Watching and steering:** `crab-student open NAME` puts the user in the worker's Claude Code
   session in their terminal: they see exactly what it is doing and can type to it like any Claude Code
   session. Closing the terminal, a dropped connection or a laptop sleep does not stop the worker;
   running the command again reattaches. `crab-student open --all` shows every running worker side by
   side in one window (split panes). Opening the machine in VS Code (Remote-SSH) must also be possible.
4. **Overview:** `crab-student list` shows every worker at a glance: its state (setting up, working,
   needs you, done, failed, idle, stopped...), its own one-line status message, running time, and money
   spent against its budget. `list --watch` keeps it on screen and alerts the user (terminal bell and a
   line) when a worker needs them, finishes or fails.
5. **Results:** the worker commits and pushes its work to branch `agent/<name>` as it goes and writes a
   REPORT.md (what was done, results with evidence, what files it left on the machine, caveats).
   `crab-student report NAME` shows the report; `crab-student pull NAME` fetches the branch into the
   user's local repo without touching their checkout.
6. **Lifecycle:** `stop` (saves work first), `start` again later, `extend` the time limit, `delete`
   (destroys the machine, but refuses if work would be lost unless forced).
7. The user's Claude Code can do all of the above for them ("how's the ablation doing?", "show me its
   report", "stop it") by running these commands.

## 3. The worker (the remote Claude Code)

- Runs the user's Claude Code, logged in with the user's Claude subscription token (no API key),
  without permission prompts (it is alone on a disposable machine), on the task brief.
- Works independently like a capable colleague: makes reasonable decisions itself; asks the user
  only about important, hard-to-reverse choices; if nobody answers, chooses sensibly and records the
  choice in the report. The user may be watching and may type to it at any time.
- Commits and pushes after every meaningful step (unpushed work can be lost), keeps notes, writes the
  report from a template, keeps big files and caches out of git, and runs long jobs so they survive
  (in the background with logs), checking on them.
- Declares a short status (working / needs you / done / failed, with a one-line message) that `list`
  shows. Before saying done, everything is committed and pushed.
- Pushes only its own branch. It cannot push elsewhere or rewrite the user's other branches.
- After the machine restarts (the user started it again, or the host rebooted), the worker resumes the
  same conversation and continues if it was in the middle of the task.

## 4. Choosing and renting the machine

- The launch states requirements (hours estimate, GPUs, GPU memory, allowed GPU models, CPU cores,
  RAM, disk, minimum download speed); the tool searches Vast and picks deterministically: the cheapest
  machine that meets the requirements (for GPU work, prefer better price for speed when the difference
  is clear), from reliable hosts, within the budget. It keeps a couple of fallback offers in case the
  first is taken or fails to boot.
- A `regions` setting restricts machines to chosen countries or continents (latency for interactive
  use and git pushes); empty means anywhere. Among similarly priced offers, prefer faster downloads
  (the image pull is a big part of start-up time).
- `crab-student offers ...` shows what would be picked, without renting (read-only).
- Launch should be fast once Vast has booted the machine (upload and setup in well under a minute on
  a normal host). Show progress while it launches.

## 5. Money and safety (non-negotiable)

1. **Approval gate.** A launch whose budget is over `auto_approve_usd` (default $50), or that would
   exceed `max_running_machines` (default 5), does not rent: it waits for `crab-student approve NAME`,
   which the user's Claude Code may only run with the user's explicit OK (a permission rule that asks).
   Starting a stopped worker and extending a time limit go through the same limits.
2. **Time limit.** Every worker has a limit in running hours over all its starts, from its budget. The
   machine stops itself when it is reached.
3. **Idle stop.** The machine stops itself after `idle_minutes` (default 60) with nothing happening:
   no GPU or CPU load, and the Claude Code session not progressing. A session left open but unused
   does not keep a machine up; a worker actually working does.
4. **Dead-man switch.** If the tool's own machinery on the machine never starts or dies, the machine
   still stops itself within about 45 minutes.
5. **Save before stopping.** Any stop (idle, time limit, user) first commits and pushes what can
   safely be saved.
6. **Own machines only.** The tool acts only on machines it rented and recorded. It must never rent
   twice by retrying a rent request whose outcome is unknown; such a machine is found by its unique
   label instead.
7. **Never destroy work automatically.** The tool destroys a machine by itself only when it holds
   nothing (it failed before receiving its files, or its setup failed). `delete` checks for unpushed
   work, output files and large untracked files first, and refuses unless forced; a machine that
   cannot be checked needs force.
8. **Secrets** (the Claude token, a deploy key, task secrets like an HF or W&B token) never appear on
   command lines, in logs, in the task brief or in git. Task secrets are stored locally by the user
   (`crab-student secret set NAME`, which always asks) and can be attached to a launch.
9. **GitHub access** per worker is limited to the one repo it works on (a per-worker deploy key with
   write access, removed on delete). No broad tokens on the machine.
10. **Claude Code permissions** on the user's own computer: the background launcher may run the
    read-only and in-budget commands without prompts; anything that spends beyond the gate, destroys,
    or changes settings asks the user.

## 6. Repos

- By default the worker works on the user's current repo (its GitHub remote): it starts from the
  user's current commit, on a new branch `agent/<name>`. If the user has local commits or uncommitted
  changes not on GitHub, say clearly what happens (either carry them over or ask the user to push
  first; pick one simple, well-explained behaviour).
- A task with no repo (`--no-repo`) goes to a private "workspace" repo the tool creates on the user's
  GitHub (`<login>/crab-student-work`), one branch per worker.
- Names: 3-40 characters, a-z 0-9 -. A taken name or an existing branch is refused with a clear message.

## 7. Other features

- `init`, `offers`, `launch`, `approve`, `decline`, `list [--watch]`, `status NAME`, `open NAME |
  --all [--vscode]`, `report`, `pull`, `stop`, `start`, `extend`, `delete`, `secret set|list|delete`,
  `config [KEY [VALUE]]`, and whatever the launcher needs to read its instructions.
- Optional notifications to the user's phone or chat (`notify_url`: ntfy.sh, Slack or Discord
  webhooks, or a generic JSON POST) when a worker needs them, finishes, fails or its machine stops.
- Settings with sensible defaults: `auto_approve_usd`, `max_running_machines`, budget factor (2x) and
  minimum budget hours, `idle_minutes`, the Docker image, disk size, minimum reliability, minimum
  download speed, `regions`, the Claude model for workers, the workspace repo, `notify_url`.
- `--json` on the commands the launcher and scripts use; `launch --dry-run`.
- A `--fake-claude` launch option that runs a stand-in for Claude Code, so the whole pipeline can be
  tested on a real machine without spending Claude usage.

## 8. Non-goals and quality bar

- **No** web dashboard, **no** always-running local background service, **no** worker "modes". The
  terminal is the interface; the user's Claude Code is the assistant for managing workers.
- **Small and clean.** This replaces an over-built predecessor. Aim for about 2,500 lines of source
  in total (local and machine side), short functions, no speculative features, no dead code.
- **Terminal text** is clear, short, well laid out and ASCII (Windows consoles). Errors say what
  happened and what to do. Exit codes: 0 ok, 1 error, 2 usage, 3 needs approval / refused by a limit /
  would lose work, 4 no machine fits, 5 setup failed.
- **Platforms:** Windows first (PowerShell and Git Bash), also macOS and Linux. Python 3.11+ locally,
  installable with `uv tool install`. On the machine: whatever the image has (Python 3.8+ standard
  library, bash); install anything else at setup.
- **Tests** never touch the network, Vast, GitHub, the real home directory, `~/.ssh`, `~/.claude` or
  the keyring. Behavioural tests, not tests of wording. One end-to-end test (CI, Linux) runs the whole
  pipeline against a fake Vast API and a fake GitHub with real SSH, tmux and the machine side, using
  the fake Claude.
- Docs: a README for users, a CLAUDE.md for future Claude Code sessions working on the code, and a
  short spec of the protocols and safety invariants.

## 9. Facts about the environment (learned the hard way)

**Vast.ai REST API** (base `https://console.vast.ai`, header `Authorization: Bearer <key>`; allow
overriding the base URL, e.g. env `VAST_URL`, so tests can point at a fake):
- Search offers: `POST /api/v0/bundles/` with a JSON query of filters `{field: {op: value}}` (ops
  `eq, gte, gt, lte, lt, in`) plus `type: "on-demand"`, `order: [["dph_total","asc"]]`, `limit` (max ~1000),
  `allocated_storage: <disk GB>` (so prices include the disk). Useful fields: `id, gpu_name, num_gpus,
  gpu_ram (MB), gpu_total_ram, dph_total ($/hr), dlperf, reliability (or reliability2), inet_down (Mbps),
  cuda_max_good, geolocation ("California, US" - the country code ends it), disk_space, cpu_cores_effective,
  cpu_ram (MB), cpu_ghz, verification ("verified"), rentable, direct_port_count, duration or end_date`.
  Vast compares GB as 1000 MB. Re-check results client-side: the server filter is not always exact.
- Looking up one offer by id: filtering on `id` does not work on the offers endpoint; use
  `ask_contract_id: {"eq": id}`.
- Rent: `PUT /api/v0/asks/<offer_id>/` with `{client_id: "me", image, env: {...}, disk, label, onstart,
  runtype: "ssh_direc ssh_proxy", cancel_unavail: true, ...}`; the new instance id is `new_contract`.
  **Not idempotent**: a timeout or 5xx may still have rented; never retry, find it by label.
- List your instances: `GET /api/v1/instances/` (paginated); one: `GET /api/v0/instances/<id>/`.
  Fields: `actual_status` (e.g. loading, running, exited), `intended_status`, `cur_state`, `ssh_host`,
  `ssh_port`, `public_ipaddr`, `ports` (direct SSH is the host's mapping of `22/tcp`), `label`,
  `status_msg` (shows image pull progress and boot errors).
- Start/stop: `PUT /api/v0/instances/<id>/` with `{"state": "running"|"stopped"}`. Destroy:
  `DELETE /api/v0/instances/<id>/` (a 404 means it is already gone). Add an SSH key to a running
  instance: `POST /api/v0/instances/<id>/ssh/` `{"ssh_key": "<public key>"}`. Account and credit:
  `GET /api/v0/users/current`.
- Inside the container Vast sets `CONTAINER_ID` and `CONTAINER_API_KEY` (a key scoped to that
  instance) in the environment of the onstart command (not necessarily in SSH sessions: save them to
  a 0600 file from onstart). With them the machine can stop itself via the API. Never destroy from the
  machine.
- The `onstart` command runs at every container start (including after a stop/start), as root.
  SSH is as root. The container's disk survives stop/start; a stopped machine still bills for storage.
- Machines sometimes fail to boot (e.g. "failed to set up container networking" in `status_msg`) or
  take many minutes to pull the image; handle both (give up on a machine after ~15 minutes and try the
  next offer; destroy a machine that never booted).
- Good default image: `vastai/base-image:cuda-12.8.1-auto` (Ubuntu, CUDA 12.8, Python in
  `/venv/main/bin` - not on PATH by default in SSH sessions - git, curl, SSH). On a GPU host whose
  driver is older than the image's CUDA, the container will not start with GPUs; for GPU searches
  require `cuda_max_good >= 12.8`. For CPU-only tasks, cheap GPU hosts are often cheaper than CPU-only
  hosts; renting them with env `NVIDIA_DISABLE_REQUIRE=1` lets the image start regardless of driver.
- The user's Vast key: the official `vastai` CLI keeps it in `~/.config/vastai/vast_api_key` (honour
  `XDG_CONFIG_HOME`); using that file lets the user switch accounts for all Vast tools at once.

**Claude Code on the machine:**
- Install: `curl -fsSL https://claude.ai/install.sh | bash` (installs to `~/.local/bin/claude`; retry
  once on failure). Takes ~15-25 s; can run in parallel with other setup.
- Auth without a browser: the user creates a long-lived token locally with `claude setup-token`
  (prints `sk-ant-oat01-...`, about 100 characters); on the machine set env
  `CLAUDE_CODE_OAUTH_TOKEN`. Keep it in a 0600 file and source it into the worker's environment.
- As root, `--dangerously-skip-permissions` is refused unless env `IS_SANDBOX=1` is set.
- First-run prompts (onboarding, the bypass-permissions warning, trusting the folder) block an
  unattended start; pre-accept them in `~/.claude.json`: top-level `hasCompletedOnboarding: true`,
  `bypassPermissionsModeAccepted: true`, and under `projects["<repo path>"]`:
  `hasTrustDialogAccepted: true`, `hasCompletedProjectOnboarding: true`. Set `DISABLE_AUTOUPDATER=1`.
- `claude [flags] "<prompt>"` starts the interactive screen with that first message.
  `--session-id <uuid>` names a new conversation; `--resume <uuid>` continues it (fails if that
  conversation was never saved); `--model <name>` picks the model. Conversations are saved as JSONL
  under `~/.claude/projects/<folder-slug>/<session-id>.jsonl`, which grows as the session works (a
  good "is it progressing" signal).
- `~/.claude/CLAUDE.md` is read as the user's global instructions (put the worker's rules there).
  Claude Code sets env `CLAUDECODE` / `CLAUDE_CODE_ENTRYPOINT` for commands it runs; remove them when
  starting another Claude Code from inside one, or it thinks it is nested.
- A single Bash tool call is cut off after ~10 minutes; long jobs must run in the background (e.g.
  `nohup ... > log 2>&1 &`) and be checked on.
- Small models (e.g. haiku) sometimes say "done" without committing or pushing; worth checking the
  branch is clean when a worker declares done.

**The user's side:**
- Windows: Claude Code's Bash tool is Git Bash, which rewrites arguments that start with `/` into
  Windows paths (use stdin or `MSYS_NO_PATHCONV=1`). The OpenSSH client (`ssh`) ships with Windows;
  `ssh -t` works for interactive sessions. Windows Terminal (`wt`) can open split panes
  (`wt new-tab ... ; split-pane ...`).
- Secrets locally: the OS keyring (the `keyring` package) with a 0600-file fallback when no keyring
  backend works.
- GitHub: the `gh` CLI gives the login and API access. Adding a deploy key needs admin rights on the
  repo; a 404 there usually means no admin rights.
- VS Code Remote-SSH: `code --remote ssh-remote+<ssh host alias> <remote folder>` opens a folder on a
  host from `~/.ssh/config`. Its remote server resolves its environment from a login shell.
- Claude Code permission rules live in `~/.claude/settings.json` (`permissions.allow` / `ask`, patterns
  like `Bash(crab-student list:*)`); a skill is `~/.claude/skills/<name>/SKILL.md` with a name and a
  description that tells Claude Code when to use it. A background launcher cannot answer permission
  prompts, so it may only run allowed commands.
