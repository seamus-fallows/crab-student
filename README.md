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
- For the window that follows a launch and for `open --all`: Windows Terminal on Windows (`open --all`
  needs `tmux` on macOS and Linux) (optional)
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
  permission rules (below) into `~/.claude`. Run `init` again after upgrading crab-student to update
  them.

## Launching

Ask your Claude Code in plain words: "run the ablation on a GPU with crab-student", "give me a machine
with Claude on it to work on this", "process the dataset on a remote machine". It starts a background
*launcher* (a fork of your session, so it has seen what you said; where forks are unavailable, a
background agent given your words) and your conversation carries on. The
launcher reads the relevant code, works out what the task needs (GPUs and GPU memory, cores, RAM,
disk), picks a machine, writes a brief for the agent, and runs `crab-student launch`. Anything you state yourself (the
GPU, the disk, the hours, the region, the name, the mode, the model) is used as you said it; only the
rest is estimated, with generous margins, since running out of disk or memory costs more than a little
extra does. You then see the brief and one line like:

```
ablation-lr: working on 1x RTX 4090 (Quebec, CA) at $0.42/hr - colleague - branch agent/ablation-lr - watch: crab-student open ablation-lr
```

You don't have to wait for that line to see what is happening: as soon as the launch starts, a window
opens (on Windows a tab per agent in one Windows Terminal window named crab-student; on macOS a Terminal
window) that shows its progress as it goes (the machines rented, the image download, the setup steps)
and then becomes the agent's session: it attaches to the agent's Claude Code. For an interactive agent
it opens VS Code instead and stays open, checking every few seconds: once you hand the agent over with
`/handoff`, it becomes the agent's terminal. If the launch fails, the window says why and what to do.
On other systems, or with `crab-student config open_window off`, run `crab-student open NAME`
yourself: on an agent that is still launching it shows the same progress. (Every launch also keeps its
progress, with times, in `~/.crab-student/agents/NAME.log`.)

You never type a price. The launcher picks the machine the way you would if you compared every offer:
it works out what will slow the task down (the GPU, the CPU, the network, the downloads, Claude's own
thinking time), browses `crab-student offers`, and favours speed over cost when the extra is in
proportion (a $5/hr machine for 1.5x the speed of a $2/hr one, yes; $100/hr for 1.5x, no). It picks
a machine and 2-4 backups on other hosts, and says in one line why. Set `home` to your country code
(`crab-student config home CA`) so it weighs how far a host is from you: you feel the lag when you
type to an agent. Every offer passes fixed rules first: a verified host at least `min_reliability`
reliable, a driver for the image's CUDA, no mining cards, enough disk and download speed, a lease long
enough, your `regions`, and never a country where Anthropic's API is unavailable (mainland China, Hong
Kong, Russia, Iran and the others missing from https://www.anthropic.com/supported-countries), since
Claude Code could not work there. A task that uses no GPU gets a machine with at least 24 vCPUs and
64 GB of RAM. If the pick costs more than `approve_above_usd_per_hour` an hour, nothing is rented until
you approve it (see Money and safety). `crab-student offers --gpus 1 --gpu-ram 24 --kinds` shows what is
on offer, grouped by kind of GPU (`--help` lists the filters), without renting anything. There is no
cost estimate and no time limit: you decide when a machine has done its job (see Money and safety).

**Two machines at once.** How long a new machine takes to start varies a lot (a host that must first
download the Docker image can take several minutes; one that has it cached, one or two). So a launch
rents the launcher's pick and its first backup (which the launcher makes nearly as good) at the same
time, keeps whichever is ready first (running and answering ssh), and
destroys the other at once, before anything is uploaded to it. A machine that has not started within 5
minutes, does not answer ssh within 3 minutes of starting, or fails is destroyed too and the next
backup rented in its place, so at most two are ever in flight for one launch.

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
`--effort` and `--fast` / `--no-fast`, else the `model`, `effort` and `fast_mode` settings, else what
your own Claude Code uses (from `~/.claude/settings.json`: its model, fast mode, and the effort you
picked with `/effort`, which Claude Code keeps for each model), so remote agents behave like your local
Claude. `status` shows what an agent got.

**Your repo.** The agent starts from your current commit on a new branch `agent/<name>`. Local
commits that are not on GitHub yet, and uncommitted changes to tracked files, go along (as one extra
commit on the agent's branch; your own checkout is not touched). Untracked files do not; the launch
warns about them. A task that has nothing to do with a repo (`--no-repo`) goes to a private
`crab-student-work` repo on your GitHub.

**Your project's dependencies.** Each machine starts from a Docker image (`image`; by default Ubuntu
with CUDA, Python and pip in `/venv/main`). Setup adds what the machine needs to host an agent (tmux,
Claude Code, the repo and its git hooks) and installs your project's dependencies before the agent
starts, so it does not spend its first minutes on them. It goes by the repo, the first that fits:

- `uv.lock`, or `[tool.uv]` in `pyproject.toml`: `uv sync` (installing uv first if needed), which makes
  the repo's own `.venv`;
- `[project]` in `pyproject.toml`: `pip install -e .`;
- `requirements*.txt`: `pip install -r` for each;
- `setup.py`: `pip install -e .`.

pip is the image's (`/venv/main/bin/python -m pip`). Setup waits for the install (up to 30 minutes),
so a launch can take a while. If it fails, the machine is set up anyway: the launch prints a warning,
and the agent is told, with the end of the install's output. Shells on the machine, the agent's Claude
Code and VS Code all use the repo's `.venv` once there is one, else `/venv/main`, and terminals start
in the repo. When the repo needs something else, the launch takes `--setup "COMMAND"` (e.g.
`--setup "pip install -r requirements-gpu.txt"`), or `--setup none` for no install; just say so when
you ask. Anything special the agent should know about (a system library, a particular CUDA or PyTorch
version, a private package index) goes into its brief.

**Several agents.** For a sweep, or several separate tasks, the launcher writes a brief for each and
runs one `crab-student launch` per agent, all at once. Each gets a machine (and a window tab) of its
own, they come up side by side, and you see one line per agent. Any that would cost over the approval
price wait for your OK while the rest go ahead.

## Watching and steering

```
crab-student open NAME           # the agent's Claude Code, in this terminal (while launching: its progress first)
crab-student open --all          # every running agent side by side
crab-student open NAME --vscode  # the machine in VS Code (Remote-SSH)
```

The window a launch opens is `open NAME`, so you only need these to come back later, or when no window
opened.

`open` attaches to the agent's own Claude Code session, which runs in tmux on the machine. Watch it,
or type to it like any Claude Code session. **Detach with Ctrl-b then d**: the agent keeps going.
Closing the window, losing the connection or a sleeping laptop do not stop it either; `open` again to
come back. In `open --all` on macOS/Linux the local layout uses Ctrl-a, so Ctrl-b still reaches the
agent. Agents whose Claude runs in VS Code (interactive, not handed over) are left out of `open --all`,
with a note. `open NAME` on one opens VS Code (as `--vscode` does), then waits, asking the machine
every few seconds: once you hand the agent over with `/handoff`, it attaches to it (Ctrl+C closes the
wait).

`open NAME --vscode` opens the machine's repo folder in VS Code. For an agent whose Claude runs in
tmux, VS Code's integrated terminal attaches to that session by itself, so you can watch and type to it
from VS Code too. For an interactive agent that you have not handed over yet, it waits 15 seconds for
VS Code to connect, then types the task into the Claude panel without sending it: read it, change it,
press Enter. That first connect is quick because, for interactive agents only and when VS Code is installed
on your computer, the machine downloads the matching VS Code server, with the Claude extension, while it
is being set up (setup waits for it; if that fails, the launch prints a warning and VS Code installs its
server itself, which takes a minute).

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
crab-student list            # every agent: mode, state, its own status line, time and money since its last start
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

**From your phone.** Turn on [Remote Control](https://code.claude.com/docs/en/remote-control) for your
own Claude Code session (`/remote-control`), then talk to it from the Claude app or claude.ai/code: it
runs `list`, `status`, `report`, `send` and `stop` for you. Agents themselves can't use Remote Control:
it refuses the long-lived token they sign in with.

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
  idle or you stop it. `list` shows what each one costs and how long it has run since it was last
  started.
- **Idle stop.** The machine stops itself after an hour (`idle_minutes`) with nothing happening: no
  CPU or GPU work (a running job always counts) and no activity in the conversation, whether it is in
  tmux or in VS Code. An open but unused session does not keep it up. This applies whether the agent
  has finished or not; `start` brings a stopped machine back, with its conversation. A colleague or
  autonomous agent that goes quiet mid-task gets one reminder to check its jobs and carry on.
- **Save before stopping.** Every stop commits and pushes what can safely be saved first.
- **Dead-man switch.** If the machine's own watchdog never starts or dies, the machine still stops
  itself within about 45 minutes. A machine without a key to stop itself is refused at setup, and one
  whose stop call Vast ignores is stopped by the next `list` or `status`.
- **Approval, by price per hour.** A launch rents without asking when the launcher's pick costs
  `approve_above_usd_per_hour` or less (see Settings for the default); backups that cost more are left
  out. If the pick costs more, it does not rent: it waits for `crab-student approve NAME`, which your
  Claude Code only runs with your OK, and which rents at up to the highest price among its offers
  (`crab-student decline NAME` drops it). The launcher is never told the approval price: it picks for
  the task, and crab-student enforces the price. Only a launch can ask for approval:
  there is no dollar budget and no cap on running machines, and starting a stopped agent needs no
  approval. The approval, the idle stop and the dead-man switch are what bound the bill.
- **No leases about to end.** A host that plans to take its machine back within a day plus an hour is
  never offered (`crab-student offers --hours H`, for a longer job, asks for twice H), so the machine is
  not taken away in the middle of a job.
- **Your machines only.** crab-student acts only on machines it rented and recorded, and never repeats
  a rent request whose outcome is unknown (so you never pay twice). Renting two machines for a launch is
  deliberate and bounded: at most two are in flight, both within the price you allow, and the one not
  kept is destroyed before anything is uploaded to it. If Vast never confirms a rent, the launch says
  so (with a warning, or by failing when nothing else came up), and if that machine shows up later,
  `crab-student list` points it out for you to destroy on Vast's website.
- **Nothing is destroyed automatically** once an agent has run on it: crab-student destroys a machine
  by itself only before anything was uploaded to it (it lost its launch's race, failed to start, or its
  launch was cut off and is being started again) or when its setup failed.
- **GitHub access** is a deploy key for the agent's one repo. GitHub emails you about every key
  added, so the agents on a repo share one, and it is removed from GitHub when the last agent using it
  is deleted. A deploy key can technically push to any branch of that repo, so git hooks on the machine
  keep the agent on `agent/<name>`; for extra safety, protect your `main` branch on GitHub (deploy keys
  obey it).
- **Permissions.** Agents run Claude Code without permission prompts, in every mode: the machine is
  disposable and the git hooks guard the repo.
- **Secrets** for tasks (a Hugging Face or W&B token, say): `crab-student secret set HF_TOKEN` (in a
  terminal; it asks for the value), then the launch attaches it with `--secret HF_TOKEN`. The agent
  sees it as an environment variable. Secret values never go on command lines, into logs, the brief
  or git (a pre-commit hook on the machine refuses commits containing them).

### What your Claude Code may do without asking

`init` adds these rules to `~/.claude/settings.json`. Allowed: `guide`, `offers`, `launch`, `list`,
`status`, `report`, `pull`, `send`, `open`, `stop`, `start`, `delete`, `decline`, `secret list` (a
`launch` over the price limit waits for `approve` by itself; restarting a stopped agent with `start`
only gives an agent you already have more machine time; `delete` refuses to destroy unsaved work unless
forced). Always asked first: `approve`, `delete --force`, `config`, `init`, `secret set`, `secret delete`.

The launcher runs in the background and cannot ask you anything, so it can use only the allowed
commands; a command outside that list would interrupt you with a permission prompt in your main
session.

## Settings

`crab-student config` shows them all; `crab-student config KEY VALUE` sets one; `--reset` restores the
default.

| Setting | Default | What it does |
|---|---|---|
| `approve_above_usd_per_hour` | 5 | A launch waits for `approve` when its pick costs more than this per hour (0: approve every launch) |
| `idle_minutes` | 60 | Idle stop after this long with nothing happening |
| `mode` | `colleague` | `autonomous`, `colleague` or `interactive` for launches that do not say |
| `model` | (yours) | The Claude model agents use; empty: your own `~/.claude/settings.json` `model`, else Claude Code's default |
| `effort` | (yours) | `low`, `medium`, `high` or `xhigh`; empty: the effort your own Claude Code uses |
| `fast_mode` | (yours) | `on` or `off`; empty: your `fastMode` |
| `image` | `vastai/base-image:cuda-12.8.1-auto` | Docker image (Ubuntu, CUDA, Python) |
| `disk_gb` | 64 | Disk when the launch does not say (the launcher usually does: 1.5x its estimate, at least 64 GB) |
| `min_reliability` | 0.98 | Only hosts at least this reliable |
| `min_download_mbps` | 200 | Only hosts that download at least this fast |
| `regions` | (anywhere) | Countries (`US`, `CA`, `DE`, ...) or continents (`north-america`, `europe`, `asia`, ...), comma-separated |
| `home` | (none) | Your country code (`CA`, `US`, `DE`, ...): the launcher weighs how far a host is from you |
| `workspace_repo` | `crab-student-work` | The repo for `--no-repo` tasks |
| `notify_url` | (none) | Where notifications go |
| `open_window` | `on` | `off`: a launch opens no window that follows it (`crab-student open NAME` still does) |

`mode`, `model`, `effort`, `fast_mode`, `idle_minutes`, `notify_url` and `image` apply to agents launched
after the change. `min_reliability`, `min_download_mbps` and `regions` are rules every offer must pass.

## How it works

There is no background service: each `crab-student` command does its job and exits, keeping one small
JSON record per agent in `~/.crab-student/`. It talks to Vast over its REST API, to GitHub through
`gh` and `git`, and to machines through `ssh`. On each machine, one script sets things up (the repo,
Claude Code and its settings, git hooks, your project's dependencies, the settings VS Code reads and,
for an interactive agent, VS Code's server) and runs a watchdog that keeps the agent's Claude Code
alive in tmux, measures activity, saves work, sends notifications and stops the machine. Vast's start
command carries the dead-man switch, so it works before anything else is installed.

The agent itself is never told about crab-student: it is told that it works on a rented cloud machine
that will be stopped when idle, and given plain commands (`agent-status` to say what state it is in,
`agent-handoff` to leave VS Code).

## Trouble

- **A launch was cut off** (Ctrl-C, a closed terminal; `list` says `interrupted`, and its window says
  it stopped): `crab-student start NAME` continues it. Cut off during setup, it carries on with the
  same machine; earlier, it destroys the machines (they hold nothing) and tries again with the offers
  that have not failed. If those are gone, the error says so: `crab-student start NAME --offers ID,ID`
  tries others (`crab-student offers` shows them).
  Closing the window that follows a launch does not stop the launch.
- **"Vast did not confirm the rent"**: crab-student never sends a rent request twice. If the launch
  failed for it, `crab-student start NAME` tries again. If `list` later warns about a
  machine that belongs to no agent, destroy it at https://cloud.vast.ai/instances/.
- **Setup failed**: the machine was destroyed (it held nothing) and the error says why; fix it and
  `crab-student start NAME` tries again. A refused Claude token: `crab-student init --claude-token`.
- **Claude in VS Code asks you to log in**: the extension could not find the token. In the Claude panel,
  log in with your subscription, or hand the agent over with `/handoff` (tmux always has the token).
- **Big outputs**: the agent keeps them in `/root/outputs` (not in git) and lists them in its report;
  copy them with `scp -F ~/.crab-student/ssh/config -r crab-student-NAME:/root/outputs .`
