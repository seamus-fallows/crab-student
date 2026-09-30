# crab-student: requirements

What the tool must do, for whoever builds it. It says nothing about how to structure the code: design
that yourself. Section 9 lists facts about the environment that are expensive to discover by trial and
error; they are facts, not design advice.

## 1. What it is

crab-student lets someone who uses Claude Code on their own computer rent a Vast.ai machine, with a GPU
or CPU only, as the task needs, and put a Claude Code *agent* on it to work on a task: a long or heavy
one (training a model, running experiments, processing data, a big refactor, anything slow), or work
the user wants to do themselves on a bigger machine. The user asks in plain words in their own Claude
Code session ("run the ablation on a GPU", "give me a machine with Claude on it"); a few minutes later a
remote Claude Code is on it on its own git branch, and the user can watch and steer it from any
terminal or from VS Code. Results come back as a git branch and a written report. The user is
responsible for their machines; the tool keeps an idle machine from billing.

("Agent", not "worker", everywhere: "worker" sounds diminutive.)

The name is a pun on "grad student": a crab (Claude Code's mascot is a crab) doing your research.

## 2. The user's experience

1. **Setup, once:** `crab-student init` walks the user through everything the tool needs (Vast API key,
   GitHub login, a Claude token for the remote agents, SSH key, the Claude Code integration, and
   offering VS Code's `remote.SSH.defaultExtensions` so the Claude extension installs on machines by
   itself). Clear, friendly, short text. Re-running it is safe and only asks for what is missing.
2. **Launching:** the user tells their Claude Code what to run. Claude Code (taught by a skill that
   init installs) starts the launch in a background fork of the session, so the launcher sees the
   user's exact words and the conversation is not held up. Whatever the user states (GPU, hours, disk,
   region, repo, name, mode, model, effort, fast mode) is used as stated; the launcher estimates only
   the rest, generously (disk, cores, RAM and download speed are a small part of the price; running
   out costs far more), and writes a clear task brief for the agent. The tool picks a machine that
   fits, rents it and sets it up. The user never types a price: the launch asks the user only when
   every machine that fits costs more than a set price per hour (section 5). There is no time limit
   and no cost estimate. The user's Claude Code shows them the brief it gave the agent and a one-line
   result (which machine, $/hr, the mode, the branch, how to watch or open it). Several agents (a
   sweep, or several tasks) can be launched in one go: one brief and one result line each, every agent
   on a machine of its own, started side by side.
3. **Modes** (`--mode`, default from the setting `mode`, itself `colleague`): `autonomous` works alone
   and never asks, recording each real decision with its reasons in the report; `colleague` works
   mostly on its own and asks only about important or hard-to-reverse choices; `interactive` is a normal
   Claude Code session the user drives in VS Code's Claude extension on the machine, with no agent
   rules, report or reminders, until the user hands it off (`/handoff [autonomous|colleague] [what to
   continue with]`, or asking in words): Claude commits and pushes, and the conversation continues in
   tmux as a fork under a new session id, so the user can close VS Code. The safety parts (section 5)
   hold in every mode.
4. **Watching and steering:** `crab-student open NAME` puts the user in the agent's Claude Code
   session in their terminal: they see exactly what it is doing and can type to it like any Claude Code
   session. Closing the terminal, a dropped connection or a laptop sleep does not stop the agent;
   running the command again reattaches. `crab-student open --all` shows every running agent side by
   side in one window (split panes). `open NAME --vscode` opens the machine in VS Code (Remote-SSH, the
   repo folder): its terminal attaches to the agent's tmux session, and for an interactive agent the
   Claude panel opens with the task typed in but not sent.
5. **Overview:** `crab-student list` shows every agent at a glance: its mode, its state (approval,
   starting, interrupted, working, needs you, interactive, done, failed, stopped, unknown), its own
   one-line status message, running time, and money spent. A stopped agent also shows how long it has
   been sitting and what its disk costs per day (`status` shows the same, plus the model, effort and
   fast mode the agent got). Alerts when an agent needs the user, finishes or fails are the
   notifications in section 7.
6. **Results:** the agent commits and pushes its work to branch `agent/<name>` as it goes and writes a
   REPORT.md (what was done, results with evidence, its decisions, what files it left on the machine,
   caveats). `crab-student report NAME` shows the report; `crab-student pull NAME` fetches the branch
   into the user's local repo without touching their checkout.
7. **Lifecycle:** `stop` (saves work first), `start` again later, `delete` (destroys the machine, but
   refuses if work would be lost unless forced). A stopped machine keeps its disk and Vast keeps billing
   for it, so the user can see how long each stopped agent has been sitting and what its disk costs. A
   machine records what it leaves behind that git does not hold when it stops, so a stopped agent can
   usually be deleted without starting it again.
8. The user's Claude Code can do all of the above for them ("how's the ablation doing?", "show me its
   report", "stop it") by running these commands.

## 3. The agent (the remote Claude Code)

- Runs the user's Claude Code, logged in with the user's Claude subscription token (no API key),
  without permission prompts in every mode (the machine is disposable; git hooks guard the repo), on
  the task brief. Its model, effort and fast mode come from the launch's flags, else the settings, else
  the user's own Claude Code settings, so it behaves like the user's local Claude.
- Never hears the name "crab-student": it has no context for it. Its rules, prompts and the machine's
  messages describe the situation plainly (a rented cloud machine; messages from the watchdog start
  with `Automated message from the machine:`), and its commands are `agent-status` and
  `agent-handoff`.
- In colleague mode, works independently like a capable colleague: makes reasonable decisions itself;
  asks the user only about important, hard-to-reverse choices; if nobody answers, chooses sensibly and
  records the choice in the report. In autonomous mode it never asks and records every real decision.
  The user may be watching and may type to it at any time.
- Commits and pushes after every meaningful step (unpushed work can be lost), keeps notes, writes the
  report from a template, keeps big files and caches out of git, and runs long jobs so they survive
  (in the background with logs), checking on them.
- Declares a short status (working / needs you / done / failed, with a one-line message) that `list`
  shows. Before saying done, everything is committed and pushed.
- Pushes only its own branch. It cannot push elsewhere or rewrite the user's other branches.
- After the machine restarts (the user started it again, or the host rebooted), the agent resumes the
  same conversation and continues if it was in the middle of the task.

## 4. Choosing and renting the machine

- The launch states requirements (GPUs, GPU memory, allowed GPU models, CPU cores (default 8), RAM
  (16 GB), disk (50 GB), minimum download speed (200 Mbps), and optionally the hours when a job may run
  more than a day); the tool searches Vast and picks deterministically: a machine that meets the
  requirements, from reliable hosts (0.98), among the cheapest (for GPU work, prefer better price for
  speed when the difference is clear), and among those at or under the price that needs no approval
  (section 5) when any is that cheap. A host whose lease ends within max(2 x hours, 24) + 1 hours is
  not picked. It keeps a couple of fallback offers in case the first is taken or fails to boot.
- Agents launched in one go are planned together before anything is rented (a problem with one stops
  the batch), each with a different machine, and then brought up side by side; a failure in one does
  not stop the others.
- A `regions` setting restricts machines to chosen countries or continents (latency for interactive
  use and git pushes); empty means anywhere. Among offers within 20% of the lowest price, prefer the
  more reliable and faster-downloading ones together (the image pull is a big part of start-up time;
  a host that fails costs more than the difference).
- `crab-student offers ...` shows what would be picked, without renting (read-only).
- Launch should be fast once Vast has booted the machine (upload and setup in well under a minute on
  a normal host). Show progress while it launches.

## 5. Money and safety (non-negotiable)

1. **Approval by price.** A launch rents without asking when a machine that fits costs
   `approve_above_usd_per_hour` or less. When every machine that fits costs more, it does not rent: it
   waits for `crab-student approve NAME [NAME ...]`, which the user's Claude Code may only run with the
   user's explicit OK (a permission rule that asks). In a batch, the agents over the price wait while
   the others launch. Only a launch can ask for approval: there is no dollar budget per agent and no
   cap on running machines, and starting a stopped agent needs no approval. The skill and the docs name
   the setting without a number (the launcher's guide gets the current value filled in).
2. **No time limit.** The user is responsible for their machines: there is no time limit, no `extend`
   and no cost estimate. The approval, the idle stop and the dead-man switch are what bound the bill.
3. **Idle stop.** The machine stops itself after `idle_minutes` (default 60) with nothing happening,
   whether the agent is done or not: no GPU or CPU load (a running job always counts), and no
   conversation activity, wherever the conversation is (tmux, or VS Code for an interactive agent). A
   session left open but unused does not keep a machine up; an agent actually working does.
4. **Dead-man switch.** If the tool's own machinery on the machine never starts or dies, the machine
   still stops itself within about 45 minutes.
5. **Save before stopping.** Any stop (idle, user) first commits and pushes what can safely be saved,
   and records with it what the machine still holds that git does not (output files, large files), so
   a stopped agent can be checked for unsaved work without starting it.
6. **Own machines only.** The tool acts only on machines it rented and recorded. It must never rent
   twice by retrying a rent request whose outcome is unknown; such a machine is found by its unique
   label instead.
7. **Never destroy work automatically.** The tool destroys a machine by itself only when it holds
   nothing (it failed before receiving its files, or its setup failed). `delete` checks for unpushed
   work, output files and large untracked files first (a stopped machine from what it recorded when it
   stopped), and refuses unless forced; a machine that cannot be checked needs force, or, if it is
   stopped, starting first.
8. **Secrets** (the Claude token, a deploy key, task secrets like an HF or W&B token) never appear on
   command lines, in logs, in the task brief or in git. Task secrets are stored locally by the user
   (`crab-student secret set NAME`, which always asks) and can be attached to a launch.
9. **GitHub access** per agent is limited to the one repo it works on (a deploy key with write
   access, shared only by agents started on that repo within 24 hours, so that 100 agents do not mean
   100 emails from GitHub; removed when the last of them is deleted). No broad tokens on the machine.
10. **Claude Code permissions** on the user's own computer: the launcher may run the read-only
    commands and the ones that launch, start or stop agents without prompts (a launch over the price
    waits by itself); anything that approves a pricier machine, destroys or changes settings asks the
    user. The launcher runs in the background: it cannot ask the user questions (it states its
    assumptions in its result), and a command outside its list would interrupt the user with a
    permission prompt, so it uses only those.

## 6. Repos

- By default the agent works on the user's current repo (its GitHub remote): it starts from the
  user's current commit, on a new branch `agent/<name>`. If the user has local commits or uncommitted
  changes not on GitHub, say clearly what happens (either carry them over or ask the user to push
  first; pick one simple, well-explained behaviour).
- A task with no repo (`--no-repo`) goes to a private "workspace" repo the tool creates on the user's
  GitHub (`<login>/crab-student-work`), one branch per agent.
- Names: 3-40 characters, a-z 0-9 -. A taken name or an existing branch is refused with a clear message.

## 7. Other features

- `init`, `offers`, `launch [--batch]`, `approve NAME [NAME ...]`, `decline`, `list`, `status NAME`,
  `open NAME | --all [--vscode]`, `report`, `pull`, `stop`, `start`, `delete`, `send`,
  `secret set|list|delete`, `config [KEY [VALUE]]`, and whatever the launcher needs to read its
  instructions.
- Optional notifications to the user's phone or chat (`notify_url`: ntfy.sh, Slack or Discord
  webhooks, or a generic JSON POST) when an agent needs them, finishes, fails or its machine stops.
- Settings with sensible defaults: `approve_above_usd_per_hour`, `idle_minutes`, the Docker image,
  disk size, minimum reliability, minimum download speed, `regions`, the mode, the Claude model,
  effort and fast mode for agents (empty: as the user's own Claude Code), the workspace repo,
  `notify_url`.
- Remote Control for agents: not now (it refuses the long-lived token agents use; a decision is
  pending).
- `--json` on the commands the launcher and scripts use; `launch --dry-run`.
- A `--fake-claude` launch option that runs a stand-in for Claude Code, so the whole pipeline can be
  tested on a real machine without spending Claude usage.

## 8. Non-goals and quality bar

- **No** web dashboard, **no** always-running local background service. The terminal (and VS Code,
  for interactive agents) is the interface; the user's Claude Code is the assistant for managing agents.
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
  `CLAUDE_CODE_OAUTH_TOKEN`. Keep it in a 0600 file and source it into the agent's environment (the
  `env` of `~/.claude/settings.json` also carries it, for a Claude Code that VS Code starts).
- As root, `--dangerously-skip-permissions` is refused unless env `IS_SANDBOX=1` is set.
- First-run prompts (onboarding, the bypass-permissions warning, trusting the folder) block an
  unattended start; pre-accept them in `~/.claude.json`: top-level `hasCompletedOnboarding: true`,
  `bypassPermissionsModeAccepted: true`, and under `projects["<repo path>"]`:
  `hasTrustDialogAccepted: true`, `hasCompletedProjectOnboarding: true`. Set `DISABLE_AUTOUPDATER=1`.
- `claude [flags] "<prompt>"` starts the interactive screen with that first message.
  `--session-id <uuid>` names a new conversation; `--resume <uuid>` continues it (fails if that
  conversation was never saved); `--resume <uuid> --fork-session` continues it under a new id, and
  `--session-id` is accepted with `--resume` only together with `--fork-session` (v2.1.285);
  `--model <name>` picks the model, `--effort <level>` the effort for the session. Conversations are
  saved as JSONL under `~/.claude/projects/<folder-slug>/<session-id>.jsonl` (subagents' in folders
  below), which grows as the session works (a good "is it progressing" signal).
- Settings (`~/.claude/settings.json`): `model`, `fastMode` (bool), and the effort picked with
  `/effort`, which Claude Code keeps per model under `modelSettings.<model>.effortLevel` (`low`,
  `medium`, `high`, `xhigh`); a top-level `effortLevel` in the user's settings is only a default.
  `permissions.defaultMode: "bypassPermissions"` skips prompts for every session. The env var
  `CLAUDE_CODE_EFFORT_LEVEL` overrides effort outright (even `/effort`).
- `~/.claude/CLAUDE.md` is read as the user's global instructions (put the agent's rules there).
  Claude Code sets env `CLAUDECODE`, `CLAUDE_CODE_SESSION_ID` (the current conversation's id) and
  other `CLAUDE_CODE_*` variables for commands it runs; remove them when starting another Claude Code
  from inside one, or it thinks it is nested. A command file (`~/.claude/commands/<name>.md`) gets
  `$ARGUMENTS` and `${CLAUDE_SESSION_ID}` substituted.
- A single Bash tool call is cut off after ~10 minutes; long jobs must run in the background (e.g.
  `nohup ... > log 2>&1 &`) and be checked on.
- Small models (e.g. haiku) sometimes say "done" without committing or pushing; worth checking the
  branch is clean when an agent declares done.

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
  host from `~/.ssh/config`. Its remote server resolves its environment from a login shell, and reads
  machine-level settings from `~/.vscode-server/data/Machine/settings.json` on the host. The user's
  `remote.SSH.defaultExtensions` lists extensions it installs on every host. The Claude extension's
  panel opens pre-filled (not sent) from `vscode://anthropic.claude-code/open?prompt=<url-encoded>`
  (up to about 5,000 characters). On Windows `code` is a batch file, which mangles `%` in arguments:
  open such URLs through the OS URL handler instead.
- Background subagents cannot ask the user questions, and their permission prompts appear in the
  user's main session (Claude Code v2.1.186+). A fork (Agent tool, `subagent_type: "fork"`) sees the
  conversation it was started from.
- Claude Code permission rules live in `~/.claude/settings.json` (`permissions.allow` / `ask`, patterns
  like `Bash(crab-student list:*)`); a skill is `~/.claude/skills/<name>/SKILL.md` with a name and a
  description that tells Claude Code when to use it. A background launcher's permission prompts would
  interrupt the user in the main session, so it runs only allowed commands.
