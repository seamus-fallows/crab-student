# crab-student

Rent a [Vast.ai](https://vast.ai) machine, with a GPU or CPU only, and put a Claude Code *agent* on it,
straight from your own Claude Code session:

> run the learning-rate ablation on a GPU

A few minutes later a remote Claude Code (an *agent*) is on it, on its own git branch `agent/<name>`.
You can watch and steer it from any terminal or from VS Code, it writes a report as it goes, and its
machine stops when nothing has been happening for an hour, so an idle machine does not keep billing.
The agent can work alone, work like a colleague who asks about the big decisions, or be a Claude Code
session you drive yourself on the machine (see Modes).

(A crab, because Claude Code's mascot is one. A student, because it does your research.)

## What you need

- [Claude Code](https://claude.com/claude-code) and a Claude subscription (agents use it through a
  long-lived token; no API key)
- Python 3.11+, and [uv](https://docs.astral.sh/uv/) or [pipx](https://pipx.pypa.io) to install it
  (plain pip works too)
- git, the GitHub CLI [`gh`](https://cli.github.com), and an OpenSSH client (Windows has one built in)
- A Vast.ai account with some credit, and its [API key](https://cloud.vast.ai/manage-keys/)
- For `open --all`: Windows Terminal on Windows, or `tmux` on macOS and Linux (optional)
- For `open --vscode` and interactive agents: [VS Code](https://code.visualstudio.com) with its Remote -
  SSH extension and the `code` command (optional)

## Install and set up

```
uv tool install git+https://github.com/seamus-fallows/crab-student
crab-student init
```

Other ways to install:

- With pipx: `pipx install git+https://github.com/seamus-fallows/crab-student`.
- With plain pip: `pip install git+https://github.com/seamus-fallows/crab-student`. Use a virtual
  environment whose `bin` (or `Scripts`) folder is on your PATH.
- From a clone: `uv tool install -e .` (or `pip install -e .`). The installed `crab-student` then runs
  the clone's code.

The repo is private, so you need access to it. `gh auth setup-git` lets git use your `gh` login to
fetch it.

To upgrade:

1. Run the same install command again with `--force`. For pip, use `--force-reinstall`.
2. Run `crab-student init` again.

`init` walks you through everything once and is safe to run again (it only asks for what is
missing):

- **Vast key.** It uses the key file the `vastai` CLI keeps (`~/.config/vastai/vast_api_key`), so
  switching accounts there switches crab-student too. If there is none, it asks for one and saves it.
- **GitHub.** It uses your `gh` login.
- **SSH key.** It makes a key just for crab-student's machines and adds one `Include` line to
  `~/.ssh/config`, so `ssh crab-student-<name>` and VS Code can reach your agents' machines.
- **Claude token.** You run `claude setup-token` in another terminal and paste the token; it is kept
  in your system keyring.
- **Claude Code.** It installs a skill (so your Claude Code knows how to use crab-student) and
  permission rules (below) into `~/.claude`, and checks that your Claude Code is new enough for the
  background launcher (v2.1.232 or later, which can fork your session). Run `init` again after
  upgrading crab-student to update them.
- **VS Code.** If VS Code's `remote.SSH.defaultExtensions` setting does not list the Claude Code
  extension, it offers to add `anthropic.claude-code`, so the extension installs itself on every
  machine you open. (If it cannot edit your settings file safely, it prints what to add instead.)

## Launching

Ask your Claude Code in plain words: "run the ablation on a GPU with crab-student", "give me a machine
with Claude on it to work on this", "process the dataset on a remote machine". It starts a background
*launcher* (a fork of your session, so it has seen what you said) and your conversation carries on. The
launcher reads the relevant code, estimates what the task needs (GPUs and GPU memory, cores, RAM,
disk), writes a brief for the agent, and runs `crab-student launch`. Anything you state yourself (the
GPU, the disk, the hours, the region, the name, the mode, the model) is used as you said it; only the
rest is estimated, with generous margins, since running out of disk or memory costs more than a little
extra does. You then see the brief and one line like:

```
ablation-lr: working on 1x RTX 4090 (Quebec, CA) at $0.42/hr - colleague - branch agent/ablation-lr - watch: crab-student open ablation-lr
```

You never type a price. crab-student picks a reliable machine that fits, cheapest first (for GPU work,
a faster GPU when it clearly pays for itself; among similar prices, the most reliable and fastest
downloading). If every machine that fits costs more than `approve_above_usd_per_hour` an hour, nothing
is rented until you approve it (see Money and safety). `crab-student offers --gpus 1 --gpu-ram 24`
shows what would be picked, without renting anything. There is no cost estimate and no time limit: you
decide when a machine has done its job (see Money and safety).

**Modes.** `crab-student launch --mode ...`, or the `mode` setting (default `colleague`), or just say
it in your request:

- `colleague`: the agent works mostly on its own and asks only about important, hard-to-reverse
  choices, like a capable colleague. If nobody answers, it makes the call itself and records it.
- `autonomous`: the agent works alone and never asks. It decides, and records each real decision with
  its reasons in the report. ("just do it, don't bother me")
- `interactive`: no agent runs by itself. The machine gets a normal Claude Code session that you
  drive, in VS Code's Claude panel on the machine. ("I want to work on it myself", "give me a machine
  with Claude on it") You hand it over when you are done (see Watching and steering).

The branch protection, the secret and big-file checks, the save before every stop, the idle stop and
the dead-man switch are the same in every mode.

**Claude's settings.** The agent uses the model, effort and fast mode you ask for with `--model`,
`--effort` and `--fast` / `--no-fast`, else the `model`, `effort` and `fast_mode` settings, else your
own `~/.claude/settings.json` (`model`, `effortLevel`, `fastMode`), so remote agents behave like your
local Claude. `status` shows what an agent got.

**Your repo.** The agent starts from your current commit on a new branch `agent/<name>`. Local
commits that are not on GitHub yet, and uncommitted changes to tracked files, go along (as one extra
commit on the agent's branch; your own checkout is not touched). Untracked files do not; the launch
warns about them. A task that has nothing to do with a repo (`--no-repo`) goes to a private
`crab-student-work` repo on your GitHub.

**Your project's dependencies.** Each machine starts from a Docker image (`image`; by default Ubuntu
with CUDA, Python and pip). Setup adds only what the machine needs to host an agent: tmux, Claude Code,
the repo and its git hooks. The agent then installs your project's dependencies itself, as the first
part of its task:

- It installs them the way your repo does, e.g. `requirements.txt`, `pyproject.toml`, `uv.lock` or
  `environment.yml`, with `pip`, `uv` or `apt-get`.
- It can install uv or conda itself if your repo needs them.

If setting up needs anything special, such as a system library, a particular CUDA or PyTorch
version, or a private package index, say so when you ask. That goes into the agent's brief.

**Several agents.** For a sweep, or several separate tasks, the launcher writes a brief for each and
launches them together (`crab-student launch --batch`, which reads the agents from stdin, each
starting with a line `=== NAME [flags]` followed by its brief). Each gets a machine of its own, they
come up side by side, and you see one line per agent. Any that would cost over the approval price wait
for your OK while the rest go ahead.

## Watching and steering

```
crab-student open NAME           # the agent's Claude Code, in this terminal
crab-student open --all          # every running agent side by side
crab-student open NAME --vscode  # the machine in VS Code (Remote-SSH)
```

`open` attaches to the agent's own Claude Code session, which runs in tmux on the machine. Watch it,
or type to it like any Claude Code session. **Detach with Ctrl-b then d**: the agent keeps going.
Closing the window, losing the connection or a sleeping laptop do not stop it either; `open` again to
come back. In `open --all` on macOS/Linux the local layout uses Ctrl-a, so Ctrl-b still reaches the
agent. Agents whose Claude runs in VS Code (interactive, not handed over) are left out of `open --all`,
with a note; `open NAME` on one says to use `--vscode`.

`open NAME --vscode` opens the machine's repo folder in VS Code. For an agent whose Claude runs in
tmux, VS Code's integrated terminal attaches to that session by itself, so you can watch and type to it
from VS Code too. For an interactive agent that you have not handed over yet, it also opens the Claude
panel with the task typed in but not sent: read it, change it, press Enter.

When a colleague-mode agent asks (its state becomes `needs you`), answer in `open`, or with
`crab-student send NAME "use the 2024 data"`, or through your Claude Code. If nobody answers for a
while, it makes the call itself and records it in its report. An autonomous agent never asks.

**Handing an interactive agent over.** When you have worked with the agent in VS Code as far as you
want, type `/handoff` in its Claude panel, optionally with a mode and what to continue with
(`/handoff autonomous finish the sweep and write the report`), or just ask for a hand-off in words. The
agent commits and pushes, and the machine then continues that same conversation, as a new session,
in tmux as a `colleague` (the default) or `autonomous` agent. The VS Code conversation is told to
stop, and you can close VS Code: from then on it is an ordinary tmux agent, and losing the VS Code
connection does not matter.

## Keeping track

```
crab-student list            # every agent: state, its own status line, time, money spent
crab-student status NAME     # one agent in detail
crab-student report NAME     # its REPORT.md
crab-student pull NAME       # fetch its branch into your repo (your checkout is not touched)
```

An agent's state is `approval` (waiting for your OK), `starting`, `interrupted` (a launch that was cut
off: `start NAME` continues it), `working`, `needs you`, `interactive` (Claude is in VS Code; the
message says whether you are active there or for how long it has been idle), `done`, `failed`,
`stopped` (with why, how long ago, and what its disk costs per day) or `unknown` (Vast or the machine
did not answer). `status` also shows the mode, where Claude runs (tmux or VS Code) and the model,
effort and fast mode the agent got.

(`pull` for a `--no-repo` agent uses `~/crab-student-work`, cloning it the first time, unless you give
`--repo PATH`.)

An autonomous or colleague agent writes `agent-runs/<name>/REPORT.md` on its branch: a summary,
results with evidence, what it did, the decisions it made without you (in autonomous mode, every real
decision with its reasons), the files it left on the machine, and caveats. An interactive agent has
no report until you hand it over.

Optional phone or chat notifications: `crab-student config notify_url <url>` (ntfy.sh, a Slack or
Discord webhook, or any URL that takes a JSON POST) for "needs you", "done", "failed" and "stopped".

## Stopping, starting, deleting

```
crab-student stop NAME           # saves (commits and pushes) its work, then stops the machine
crab-student start NAME          # starts it again; the agent resumes its conversation
crab-student delete NAME         # destroys the machine and forgets the agent
```

A stopped machine keeps its disk, and Vast bills for it, so delete agents you are done with: `list` and
`status` show how long each stopped agent has been sitting and what its disk costs per day. Before a
machine stops, by itself or with `stop`, it saves its work and writes a note of what it leaves behind
(`agent-runs/<name>/stopped.json`, committed and pushed with that last save). So `delete` can usually
check a stopped agent for work that would be lost (unpushed commits, files in `/root/outputs`, large
files outside git) without starting it again, and refuses, listing them, unless you add `--force`. If
it cannot tell what is on the machine, it asks you to `start` it first (or to use `--force`). The
branch stays on GitHub.

## Money and safety

- **You are responsible for your machines.** There is no time limit: a machine runs until it goes
  idle or you stop it. `list` shows what each one costs and how long it has run.
- **Idle stop.** The machine stops itself after an hour (`idle_minutes`) with nothing happening: no
  CPU or GPU work (a running job always counts) and no activity in the conversation, whether it is in
  tmux or in VS Code. An open but unused session does not keep it up. This applies whether the agent
  has finished or not; `start` brings a stopped machine back, with its conversation. A colleague or
  autonomous agent that goes quiet mid-task gets one reminder to check its jobs and carry on.
- **Save before stopping.** Every stop commits and pushes what can safely be saved first.
- **Dead-man switch.** If the machine's own watchdog never starts or dies, the machine still stops
  itself within about 45 minutes. A machine without a key to stop itself is refused at setup, and one
  whose stop call Vast ignores is stopped by the next `list` or `status`.
- **Approval, by price per hour.** A launch rents without asking when a machine that fits costs
  `approve_above_usd_per_hour` or less (see Settings for the default). If every machine that fits costs
  more, it does not rent: it waits for `crab-student approve NAME [NAME ...]`, which your Claude Code
  only runs with your OK (`crab-student decline NAME` drops it). Only a launch can ask for approval:
  there is no dollar budget and no cap on running machines, and starting a stopped agent needs no
  approval. The approval, the idle stop and the dead-man switch are what bound the bill.
- **No leases about to end.** A host that plans to take its machine back within a day (or, when the
  launch gives `--hours`, within twice that many hours if longer) plus an hour is not picked, so the
  machine is not taken away in the middle of a job.
- **Your machines only.** crab-student acts only on machines it rented and recorded, and never repeats
  a rent request whose outcome is unknown (so you never pay twice).
- **Nothing is destroyed automatically** once an agent has run on it: crab-student destroys a machine
  by itself only when it failed to boot or its first setup failed.
- **GitHub access** is a deploy key for the agent's one repo. GitHub emails you about every key
  added, so agents started on the same repo within 24 hours share one (start 100 at once: one email),
  and it is removed from GitHub when the last agent using it is deleted. A deploy key can technically
  push to any branch of that repo, so git hooks on the machine keep the agent on `agent/<name>`; for
  extra safety, protect your `main` branch on GitHub (deploy keys obey it).
- **Permissions.** Agents run Claude Code without permission prompts, in every mode: the machine is
  disposable and the git hooks guard the repo.
- **Secrets** for tasks (a Hugging Face or W&B token, say): `crab-student secret set HF_TOKEN` (in a
  terminal; it asks for the value), then the launch attaches it with `--secret HF_TOKEN`. The agent
  sees it as an environment variable. Secret values never go on command lines, into logs, the brief
  or git (a pre-commit hook on the machine refuses commits containing them).

### What your Claude Code may do without asking

`init` adds these rules to `~/.claude/settings.json`. Allowed: `guide`, `offers`, `launch`, `list`,
`status`, `report`, `pull`, `send`, `open`, `stop`, `start`, `secret list` (a `launch` over the price
limit waits for `approve` by itself; restarting a stopped agent with `start` only gives an agent you
already have more machine time). Always asked first: `approve`, `decline`, `delete`, `config`, `init`,
`secret set`, `secret delete`.

The launcher runs in the background and cannot ask you anything, so it can use only the allowed
commands; a command outside that list would interrupt you with a permission prompt in your main
session.

## Settings

`crab-student config` shows them all; `crab-student config KEY VALUE` sets one; `--reset` restores the
default.

| Setting | Default | What it does |
|---|---|---|
| `approve_above_usd_per_hour` | 5 | A launch waits for `approve` when every machine that fits costs more than this per hour (0: approve every launch) |
| `idle_minutes` | 60 | Idle stop after this long with nothing happening |
| `mode` | `colleague` | `autonomous`, `colleague` or `interactive` for launches that do not say |
| `model` | (yours) | The Claude model agents use; empty: your own `~/.claude/settings.json` `model`, else Claude Code's default |
| `effort` | (yours) | `low`, `medium`, `high` or `xhigh`; empty: your `effortLevel` |
| `fast_mode` | (yours) | `on` or `off`; empty: your `fastMode` |
| `image` | `vastai/base-image:cuda-12.8.1-auto` | Docker image (Ubuntu, CUDA, Python) |
| `disk_gb` | 50 | Disk when the launch does not say |
| `min_reliability` | 0.98 | Only hosts at least this reliable |
| `min_download_mbps` | 200 | Only hosts that download at least this fast |
| `regions` | (anywhere) | Countries (`US`, `CA`, `DE`, ...) or continents (`north-america`, `europe`, `asia`, ...), comma-separated |
| `workspace_repo` | `crab-student-work` | The repo for `--no-repo` tasks |
| `notify_url` | (none) | Where notifications go |

`mode`, `model`, `effort`, `fast_mode`, `idle_minutes`, `notify_url` and `image` apply to agents launched
after the change. When a launch does not say, it asks for 8 cores, 16 GB of RAM and `disk_gb` of disk.

## How it works

There is no background service: each `crab-student` command does its job and exits, keeping one small
JSON record per agent in `~/.crab-student/`. It talks to Vast over its REST API, to GitHub through
`gh` and `git`, and to machines through `ssh`. On each machine, one script sets things up (the repo,
Claude Code and its settings, git hooks, the settings VS Code reads) and runs a watchdog that keeps the
agent's Claude Code alive in tmux, measures activity, saves work, sends notifications and stops the
machine. Vast's start command carries the dead-man switch, so it works before anything else is
installed.

The agent itself is never told about crab-student: it is told that it works on a rented cloud machine
that will be stopped when idle, and given plain commands (`agent-status` to say what state it is in,
`agent-handoff` to leave VS Code).

## Trouble

- **A launch was cut off** (Ctrl-C, a closed terminal; `list` says `interrupted`):
  `crab-student start NAME` continues it (for a batch, once for each agent that is not yet working).
- **`list` says `interrupted` and "the rent was not confirmed"**: Vast did not confirm a rent.
  crab-student never rents again by itself; `crab-student start NAME` looks for the machine again, or
  `delete` gives up.
- **Setup failed**: the machine was destroyed (it held nothing) and the error says why; fix it and
  `crab-student start NAME` tries again. A refused Claude token: `crab-student init --claude-token`.
- **Claude in VS Code asks you to log in**: the extension could not find the token. In the Claude panel,
  log in with your subscription, or hand the agent over with `/handoff` (tmux always has the token).
- **Big outputs**: the agent keeps them in `/root/outputs` (not in git) and lists them in its report;
  copy them with `scp -F ~/.crab-student/ssh/config -r crab-student-NAME:/root/outputs .`
