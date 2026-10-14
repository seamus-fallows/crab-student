# crab-student

Rent a [Vast.ai](https://vast.ai) machine, with a GPU or CPU only, and put a Claude Code agent on it,
straight from your own Claude Code session:

> run the learning-rate ablation on a GPU

A few minutes later a remote Claude Code, the *agent*, is at work on its own git branch. You can watch it
and type to it from a terminal or from VS Code, and it writes a report as it goes. Its machine stops itself
after an hour with nothing happening, so you don't pay for an idle GPU. An agent can work alone, work like
a colleague who asks about the big decisions, or be a Claude Code session you drive yourself.

(A crab, because Claude Code's mascot is one. A student, because it does your research.)

## What you need

- [Claude Code](https://claude.com/claude-code) and a Claude subscription. Agents use it through a
  long-lived token, not an API key.
- Python 3.11+, and [uv](https://docs.astral.sh/uv/) or [pipx](https://pipx.pypa.io) to install
  crab-student (plain pip works too).
- git, and the GitHub CLI [`gh`](https://cli.github.com), logged in to your GitHub account.
- An OpenSSH client. Windows has one built in.
- A Vast.ai account with some credit, and its [API key](https://cloud.vast.ai/manage-keys/).
- Optional: Windows Terminal on Windows, for the window that follows a launch and for `open --all`; tmux
  on macOS and Linux, for `open --all`.
- Optional: [VS Code](https://code.visualstudio.com) with its Remote - SSH extension and the `code`
  command, for `open --vscode` and interactive agents.

## Install and set up

The repo is private, so you need access to it; `gh auth setup-git` lets git fetch it with your `gh` login.

```bash
uv tool install git+https://github.com/seamus-fallows/crab-student
crab-student init
```

Other ways to install:

- With pipx: `pipx install git+https://github.com/seamus-fallows/crab-student`.
- With plain pip: `pip install git+https://github.com/seamus-fallows/crab-student`, in a virtual
  environment whose `bin` (or `Scripts`) folder is on your PATH.
- From a clone: `uv tool install -e .` (or `pip install -e .`). The installed `crab-student` then runs the
  clone's code.

To upgrade, run the same install command again with `--force` (pip: `--force-reinstall`), then
`crab-student init` again.

`init` walks you through everything once. Running it again is safe: it only asks for what is missing.

- **Vast key.** It uses the key file the `vastai` CLI keeps (`~/.config/vastai/vast_api_key`), so
  switching accounts there switches crab-student too. If there is none, it asks for a key and saves it
  there. If your key requires 2FA, log in with `vastai tfa login --method-type totp -c <code>`.
  crab-student uses that session too, and says when it needs a new one. Vast ends a session after a
  while and does not say after how long; when yours ends, the error says how long ago you had logged in
  (see [Trouble](#trouble) for what still works without it).
- **GitHub.** It uses your `gh` login, and runs `gh auth login` if you are not logged in.
- **SSH key.** It makes a key just for your agents' machines, and adds one `Include` line to
  `~/.ssh/config`, so `ssh crab-student-NAME` and VS Code reach them.
- **Claude token.** You run `claude setup-token` in another terminal and paste the token. It is kept in
  your system keyring.
- **Claude Code.** It installs a skill, so your Claude Code knows how to use crab-student, and the
  permission rules listed under [Money and safety](#money-and-safety).

## Your first launch

Ask your Claude Code in plain words: "run the ablation on a GPU with crab-student", "give me a machine
with Claude on it to work on this", "process the dataset on a remote machine".

Your Claude Code starts a *launcher*, a subagent that works in the background while your conversation
carries on. It is a fork of your session, so it has seen what you said; where forks are not available,
it is a subagent given your words. The launcher reads the relevant code and works out what the task
needs: GPUs and their memory, cores, RAM, disk. It picks a machine and a few backups, writes a brief for
the agent, and runs `crab-student launch`.

What you state yourself is used as you said it: the GPU, the disk, the hours, the region, the name, the
mode, the model. The rest is estimated with generous margins, since running out of disk or memory costs
more than a little extra does. You never type a price.

As soon as the launch starts, a window opens that shows its progress (the machine rented, the image
download, the setup steps) and then becomes the agent's session. On Windows it is a tab in one Windows
Terminal window named crab-student; on macOS, a Terminal window. If the launch fails, the window says why
and what to do. Elsewhere, or with `crab-student config open_window off`, run `crab-student open NAME`
to see the same. Each launch also logs its progress in `~/.crab-student/agents/NAME.log`.

When the launch is done, your Claude Code shows you the brief it wrote and one line:

```text
ablation-lr: working on 1x RTX 4090 (Quebec, CA) at $0.42/hr - colleague - branch agent/ablation-lr - watch: crab-student open ablation-lr
```

If the machine costs more than you allow, nothing is rented: you are asked to approve it first (see
[Money and safety](#money-and-safety)).

### Modes

Say which in your request. Without one, the `mode` setting decides (`colleague` unless you change it).

- `colleague`: the agent works mostly on its own and asks only about important, hard-to-reverse choices,
  like a capable colleague. If nobody answers, it makes the call itself and records it.
- `autonomous` ("just do it, don't bother me"): the agent works alone and never asks. It records each
  real decision, with its reasons, in its report.
- `interactive` ("I want to work on it myself", "give me a machine with Claude on it"): no agent runs by
  itself. The machine gets a normal Claude Code session that you drive in VS Code's Claude panel, and you
  can hand it over when you are done (see [Interactive agents](#interactive-agents)).

The branch protection, the secret and big-file checks, the save before every stop, the idle stop and the
dead-man switch are the same in every mode.

### Model, effort and fast mode

The agent uses the model, effort and fast mode you ask for, else the `model`, `effort` and `fast_mode`
settings, else what your own Claude Code uses: its `~/.claude/settings.json`, including the effort you
picked with `/effort`. `crab-student status NAME` shows what an agent got.

### Secrets

If the task needs a token, such as one for Hugging Face or W&B, store it once in a terminal:
`crab-student secret set HF_TOKEN` asks for the value. The launcher attaches the stored secrets a task
needs, and the agent sees each as an environment variable.

### Several agents

For a sweep, or several separate tasks, the launcher writes a brief for each and runs one launch per
agent, all at once. Each gets its own machine and window tab, and you see one line per agent. Any that
need your approval wait while the rest go ahead.

## Watching and steering

```bash
crab-student open NAME           # the agent's Claude Code in this terminal (while launching: its progress first)
crab-student open --all          # every running agent side by side
crab-student open NAME --vscode  # the machine in VS Code (Remote - SSH)
```

The window a launch opens runs `open NAME`, so you need these only to come back later, or when no window
opened.

`open` attaches to the agent's own Claude Code session, which runs in tmux on the machine. Watch it, or
type to it as to any Claude Code session. **Detach with Ctrl-b then d**: the agent keeps going. Closing
the window, losing the connection or a sleeping laptop don't stop it either; `open` again to come back.
In `open --all` on macOS and Linux, the local layout uses Ctrl-a, so Ctrl-b still reaches the agent.

`open NAME --vscode` opens the machine's repo folder in VS Code. Its terminal attaches to the agent's tmux
session, so you can watch and type to the agent from VS Code too.

When a colleague asks something, its state becomes `needs you`. Answer in `open`, with
`crab-student send NAME "use the 2024 data"`, or through your Claude Code. If nobody answers for a
while, it makes the call itself and records it in its report.

### Interactive agents

For an interactive agent, the launch's window opens VS Code instead. 15 seconds later it types the task
into the Claude panel, without sending it: read it, change it, press Enter. The first connect is quick,
because setup puts VS Code's server and the Claude extension on the machine when VS Code is on your
computer. If that fails, the launch warns you, and VS Code installs its server itself, which takes a
minute.

When you have done as much as you want, type `/handoff` in the Claude panel, optionally with a mode and
what to continue with (`/handoff autonomous finish the sweep and write the report`), or ask for a
hand-off in words. The agent commits and pushes, and the machine continues the same conversation in
tmux, as a `colleague` (the default) or `autonomous` agent. You can then close VS Code: from now on it is
an ordinary agent. The window that opened VS Code waits meanwhile, and becomes the agent's terminal after
the hand-off (Ctrl+C closes it). `open --all` leaves out interactive agents until they are handed over.

### From your phone

Turn on [Remote Control](https://code.claude.com/docs/en/remote-control) for your own Claude Code
session (`/remote-control`), then talk to it from the Claude app or claude.ai/code. It runs `list`,
`status`, `report`, `send` and `stop` for you. Agents themselves can't use Remote Control: it refuses the
long-lived token they sign in with.

## Keeping track and getting results

```bash
crab-student list            # every agent: mode, state, its own status line, its machine time and what it has cost
crab-student status NAME     # one agent in detail
crab-student wait [NAME ...] # come back when an agent needs you
crab-student report NAME     # its report
crab-student pull NAME       # fetch its branch into your clone (your checkout is not touched)
crab-student fetch NAME      # copy its /root/outputs into a new folder here, and check the copy
crab-student spend           # what your agents have cost, deleted ones too
```

| State | Meaning |
| --- | --- |
| `approval` | waiting for your OK |
| `starting` | its launch is at work, or its machine or agent is starting |
| `interrupted` | its launch was cut off: `crab-student start NAME` continues it |
| `working`, `needs you`, `done`, `failed` | what the agent says; `failed` also when its launch failed or its machine is gone |
| `interactive` | Claude is in VS Code; the message says whether you are active there |
| `stopped` | why, how long ago, and what its disk costs per day |
| `unknown` | neither Vast nor the machine answered; the message says why |

`status` also shows the mode, where Claude runs, and the model, effort and fast mode the agent got.
`list` and `status` give up with an error after 3 minutes rather than hang.

An autonomous or colleague agent writes `agent-runs/<name>/REPORT.md` on its branch: a summary, results
with evidence, what it did, the decisions it made without you, the files it left on the machine, and
caveats. An interactive agent writes none until you hand it over.

`pull` fetches into the clone the agent was launched from (`--repo PATH` for another). The first time, it
adds the agent repo to that clone as the remote `agents`, reached the way the clone reaches GitHub (https
or ssh), so your own credentials work. Merging the branch, or pushing it to your repo for a pull request,
is up to you. A `--no-repo` agent's branch goes into `~/agent-workspace`, cloned the first time.

**Waiting.** `crab-student wait NAME ...` returns when one of those agents is `done`, `failed`, `needs
you`, `stopped`, `interrupted`, waiting for `approval` or deleted, and after an hour otherwise
(`--timeout MINUTES`), so whoever watches always hears back; your Claude Code runs it in the background
instead of polling. It says which agents are still at work, for the next `wait`. Without names it watches
every agent you have, so it returns at once while any of them is in one of those states: none that
finished while nobody looked is passed over. It looks once a minute. If an agent stays `unknown` for 10
minutes it stops waiting and exits with code 1, saying why. When Vast cannot be asked (your 2FA session
ended, say), running machines are still asked over ssh, and the result carries Vast's reason.

**Big outputs.** Unless you say where they should go when you ask for the task (a Hugging Face repo, for
example), the agent keeps big outputs on the machine, in `/root/outputs`, and lists them in its report.
`crab-student fetch NAME` copies them into a new folder `./NAME-outputs` while the machine runs (a later
fetch goes beside it, into `NAME-outputs-2`, and so on). The machine chooses the file names and what is in
them, so fetch only writes into a folder that is new or empty, never replaces a file, and your Claude Code
must ask you before using another folder (`--to DIR`). A link to a file arrives as that file. The copy is
checked: the folder then holds as many files and bytes as the machine sent. `delete` afterwards no longer
counts those files as work that would be lost while every one is still as it was copied (same paths,
sizes and times of last change): a running machine is asked, and a stopped one said so in its stop note,
so it can be deleted without starting it. Fetch when the agent is done, before its machine stops itself
(see [Stopping and deleting](#stopping-and-deleting)). A copy under way counts as activity, so the idle
stop does not cut a long one. (Agents launched before fetch existed: a copy does not keep their machine
up, and once stopped they cannot say whether their outputs changed, so `delete` names your copy and
leaves `--force` to you.)

**What it cost.** TIME and SPENT in `list` cover every start of an agent, not only the last one: its
machine hours at the machine's price, plus its disk for the time it sat stopped. `crab-student spend` adds
the agents you deleted (each is written to `~/.crab-student/spend.jsonl` when it goes) and a total. They
count only what this computer saw, so they run low rather than high; Vast's bill decides.

**Sharing with a collaborator.** Agents' branches live in each person's own private `<repo>-agents`. To
share one, `pull` it and push the branch to your shared repo, or invite your collaborator to your agent
repo on GitHub.

**Notifications.** `crab-student config notify_url URL` sends "needs you", "done", "failed" and "stopped"
to your phone or chat: ntfy.sh, a Slack or Discord webhook, or any URL that takes a JSON POST.

## Stopping and deleting

```bash
crab-student stop NAME           # save (commit and push) its work, then stop the machine
crab-student start NAME          # start it again; the agent resumes its conversation
crab-student delete NAME         # destroy the machine and forget the agent
```

A stopped machine keeps its disk, and Vast bills for that, so delete agents you are done with. `list`
and `status` show how long each stopped agent has sat and what its disk costs per day.

Before a machine stops, by itself or with `stop`, it saves its work and pushes a note of what it leaves
behind. With that note, `delete` checks a stopped agent for work that would be lost, without starting
it: unpushed commits, files in `/root/outputs`, large files outside git. If it finds any, it refuses and
lists them, unless you add `--force`. If it cannot tell what is on the machine, it asks you to `start` it
first, or to use `--force`. The branch stays in your agent repo; `pull` it first if you want it in your
clone, since `pull` needs the agent.

While a machine is stopped, Vast can rent its GPUs to someone else. `start` then waits until they are
free, and until then nothing in `/root/outputs` can be reached.

## Money and safety

- **Approval.** A launch rents without asking when the launcher's pick costs `approve_above_usd_per_hour`
  or less ($5 by default); backups that cost more are left out. If the pick costs more, nothing is
  rented: the launch waits for `crab-student approve NAME`, which your Claude Code runs only with your OK,
  and which rents at up to the highest price among its offers. `crab-student decline NAME` drops it. The
  launcher is never told the approval price: it picks for the task, and crab-student enforces the price.
  Starting a stopped agent needs no approval.
- **No time limit.** There is no budget, cost estimate or cap on running machines: a machine runs until
  it goes idle or you stop it. `list` shows how long each one has run and about what it has cost. The
  approval, the idle stop and the dead-man switch are what bound the bill.
- **Idle stop.** A machine stops itself after an hour (`idle_minutes`) with nothing happening: no CPU or
  GPU work, no activity in the conversation, whether in tmux or in VS Code, and no copy of its outputs
  under way. A running job always counts; an open but unused session does not. This holds whether the
  agent has finished or not. A
  colleague or autonomous agent that goes quiet mid-task gets a reminder to check its jobs and carry on.
- **Save before stopping.** Every stop first commits and pushes what can safely be saved.
- **Dead-man switch.** If the machine's watchdog never starts, dies or keeps failing, the machine still
  stops itself within about 45 minutes. A machine with no key to stop itself is refused at setup, and one
  whose stop call Vast ignores is stopped by the next `list` or `status`.
- **No leases about to end.** A host that will take its machine back within a day and an hour is never
  offered, and for a job you say may run longer, the lease must last twice as long.
- **Your machines only.** crab-student acts only on the machines it rented and recorded. It never
  repeats a rent request whose outcome is unknown, so you never pay twice. If Vast never confirms a rent
  and no machine with its label shows up within 2 minutes, the launch fails and says so; if that machine
  shows up later, `crab-student list` points it out for you to destroy on Vast's website. Collaborators
  who share your Vast account are safe: every machine's label carries an id of the computer that rented
  it, so `list` never points out theirs.
- **Nothing destroyed automatically** once an agent has run on a machine. crab-student destroys a machine
  by itself only before anything was uploaded to it, or when its setup failed.
- **GitHub access.** Agents never get access to your repo, and crab-student never writes to it. An agent
  can push only to its agent repo (see [Agent repos](#agent-repos)), and git hooks on the machine keep it
  on its own branch there.
- **Permissions.** Agents run Claude Code without permission prompts, in every mode: the machine is
  disposable, and the git hooks guard the repo.
- **Secrets.** Task secrets and the Claude token never go on command lines, into logs, the brief or git.
  A pre-commit hook on the machine refuses commits that contain them, or any private key.
- **The host.** The host of a rented machine has root and can read what's on it, including your Claude
  token and task secrets; verified hosts reduce that risk but don't remove it.

### What your Claude Code may do without asking

`init` adds these rules to `~/.claude/settings.json`. Allowed: `guide`, `offers`, `launch`, `list`,
`status`, `wait`, `spend`, `report`, `pull`, `fetch`, `send`, `open`, `stop`, `start`, `delete`,
`decline` and `secret list`. A launch over the price waits for `approve` by itself, `start` only gives
an agent you already have more machine time, `delete` refuses to destroy unsaved work unless forced, and
`fetch` only fills a new folder `NAME-outputs`. Always asked first: `approve`, `delete --force`,
`fetch --to`, `config`, `init`, `secret set` and `secret delete`.

The launcher runs in the background and cannot ask you anything, so it uses only the allowed commands;
any other would interrupt you with a permission prompt in your session.

## Settings

`crab-student config` shows them all, `crab-student config KEY VALUE` sets one, and
`crab-student config KEY --reset` restores its default.

| Setting | Default | What it does |
| --- | --- | --- |
| `approve_above_usd_per_hour` | 5 | A launch waits for `approve` when its pick costs more than this per hour (0: approve every launch) |
| `idle_minutes` | 60 | Idle stop after this long with nothing happening |
| `mode` | `colleague` | `autonomous`, `colleague` or `interactive` for launches that do not say |
| `model` | (yours) | The Claude model agents use; empty: your own `~/.claude/settings.json` `model`, else Claude Code's default |
| `effort` | (yours) | `low`, `medium`, `high` or `xhigh`; empty: the effort your own Claude Code uses |
| `fast_mode` | (yours) | `on` or `off`; empty: your `fastMode` |
| `image` | `vastai/base-image:cuda-12.8.1-auto` | Docker image (Ubuntu, CUDA, Python 3.11 or newer) |
| `disk_gb` | 64 | Disk when the launch does not say (the launcher usually does: 1.5x its estimate, at least 64 GB) |
| `min_reliability` | 0.98 | Only hosts at least this reliable |
| `min_download_mbps` | 200 | Only hosts that download at least this fast |
| `regions` | (anywhere) | Countries (`US`, `CA`, `DE`, ...) or continents (`north-america`, `europe`, `asia`, ...), comma-separated |
| `home` | (none) | Your country code (`CA`, `US`, `DE`, ...): the launcher weighs how far a host is from you |
| `owner` | (none) | How your machines' labels on Vast start, e.g. `SFALLOWS` (`SFALLOWS-train-lr-a1b2c3-1f2e`); empty: `crab-student` |
| `workspace_repo` | `agent-workspace` | The private repo on your GitHub for `--no-repo` tasks (made the first time) |
| `notify_url` | (none) | Where notifications go |
| `open_window` | `on` | `off`: a launch opens no window that follows it (`crab-student open NAME` still does) |

`mode`, `model`, `effort`, `fast_mode`, `idle_minutes`, `notify_url` and `image` apply to agents
launched after the change. `min_reliability`, `min_download_mbps` and `regions` are rules every offer
must pass.

## How it works

There is no background service: each `crab-student` command does its job and exits, keeping one small
JSON record per agent in `~/.crab-student/`. It talks to Vast over its REST API, to GitHub through `gh`
and `git`, and to machines through `ssh`. On each machine, one script sets things up (the repo, Claude
Code and its settings, git hooks, your project's dependencies, and VS Code's server for an interactive
agent). It then runs a watchdog that keeps the agent's Claude Code alive in tmux, measures activity,
saves work, sends notifications and stops the machine. The dead-man switch is part of Vast's start
command, so it works before anything else is installed.

The agent is never told about crab-student. It is told that it works on a rented cloud machine that stops
when idle, and it has plain commands: `agent-status` to say what state it is in, and `agent-handoff` to
leave VS Code.

### Picking the machine

The launcher picks the way you would if you compared every offer. It works out what will slow the task
down (the GPU, the CPU, the network, the downloads, Claude's own thinking time), browses
`crab-student offers`, and favours speed over cost when the extra is in proportion: a $5/hr machine for
1.5x the speed of a $2/hr one, yes; $100/hr for 1.5x, no. It picks a machine and 2-4 backups on other
machines, and says in one line why. Set `home` to your country code (`crab-student config home CA`) so it
weighs how far a host is from you: you feel the lag when you type to an agent.

`crab-student offers --gpus 1 --gpu-ram 24 --kinds` shows what is on offer, grouped by kind of GPU,
without renting anything; `--help` lists the filters.

### The offer rules

Every offer passes fixed rules first: a verified host at least `min_reliability` reliable, direct ports,
a driver for the image's CUDA, no mining cards, enough disk and download speed, a lease long enough, your
`regions`, and never a country where Anthropic's API is unavailable. That rules out mainland China, Hong
Kong, Russia, Iran and the others missing from
[Anthropic's supported countries](https://www.anthropic.com/supported-countries), since Claude Code could
not work there. A task that uses no GPU gets a machine with at least 24 vCPUs and 64 GB of RAM.

### Fallback machines

A launch rents one machine at a time. One that fails, has not started within 10 minutes (a host may
first have to download the Docker image), or does not answer ssh within 3 minutes of starting is
destroyed, before anything is uploaded to it, and the next backup is rented in its place.

### Agent repos

The agent starts from your current commit, on a new branch `agent/<name>`. Local commits that are not on
GitHub yet go along, and so do uncommitted changes to tracked files, as one extra commit on the agent's
branch; your own checkout is not touched. Untracked files don't, and the launch warns about them. With
`--committed-only` the uncommitted changes stay behind too: say so when other work is going on in the
checkout, or its half-finished edits become the agent's starting point.

The branch lives in the project's *agent repo*, `<repo>-agents`, a private repo on your own GitHub
account. The first launch makes it and sends it your repo's history; later ones send little. Each agent
repo has one deploy key, added the first time and kept (GitHub emails you when it is added). That key is
the only GitHub access a machine gets. A task that has nothing to do with a repo (`--no-repo`) goes to a
private `agent-workspace` repo instead.

### Dependencies

Each machine starts from a Docker image (`image`; by default Ubuntu with CUDA, Python and pip in
`/venv/main`). Before the agent starts, setup installs your project's dependencies, the first way that
fits the repo:

- `uv.lock`, or `[tool.uv]` in `pyproject.toml`: `uv sync` (installing uv first if needed), which makes
  the repo's own `.venv`;
- `[project]` in `pyproject.toml`: `pip install -e .`;
- `requirements*.txt`: `pip install -r` for each;
- `setup.py`: `pip install -e .`.

pip is the image's (`/venv/main/bin/python -m pip`). Setup waits for the install, up to 30 minutes, so a
launch can take a while. If the install fails, the machine is set up anyway: the launch prints a
warning, and the agent is told, with the end of the install's output. Shells, the agent's Claude Code
and VS Code use the repo's `.venv` once there is one, else `/venv/main`, and terminals start in the repo.

When the repo needs something else, say so when you ask: the launch takes `--setup "COMMAND"` (e.g.
`--setup "pip install -r requirements-gpu.txt"`), or `--setup none` for no install. Anything special the
agent should know about (a system library, a particular CUDA or PyTorch version, a private package
index) goes into its brief.

## Trouble

- **A launch was cut off** (Ctrl-C, a closed terminal; `list` says `interrupted`):
  `crab-student start NAME` continues it. Cut off during setup, it carries on with the same machine. Cut
  off earlier, it destroys the machine, which holds nothing, and tries again with the offers that have
  not failed, looked up and priced again. If those are gone, the error says so, and
  `crab-student start NAME --offers ID,ID` tries others (`crab-student offers` shows them). Closing the
  window that follows a launch does not stop the launch.
- **"Vast did not confirm the rent"**: crab-student never sends a rent request twice. If the launch
  failed for it, `crab-student start NAME` tries again. If `list` later warns about a machine that
  belongs to no agent, destroy it at <https://cloud.vast.ai/instances/>.
- **Setup failed**: the machine was destroyed (it held nothing), and the error says why. Fix that, then
  `crab-student start NAME` tries again. For a refused Claude token, run
  `crab-student init --claude-token` first.
- **Claude in VS Code asks you to log in**: the extension could not find the token. Log in with your
  subscription in the Claude panel, or hand the agent over with `/handoff` (tmux always has the token).
- **Git LFS files and submodules with relative URLs** don't reach the agent repo, so the agent doesn't
  get them: say in the brief where it can download what it needs.
- **"Vast wants a fresh 2FA login"**: your Vast session ended. Run `vastai tfa login --method-type totp
  -c <code>` (crab-student removes the ended session's key, which would otherwise make that very
  command fail with "Session expired"). Until you do, nothing can be rented, started, stopped or deleted,
  but your agents carry on, their machines still stop themselves when idle, and `list`, `status`, `wait`
  and `fetch` still work over ssh.
- **A stopped machine does not start** ("resources are currently unavailable"): its host is busy; Vast
  starts it when there is room, which can take hours. That is why outputs are best fetched before a
  machine stops. Work that only needs the branch can go on a new agent meanwhile.
- **Big outputs** in another place than `/root/outputs`: `scp -F ~/.crab-student/ssh/config -r
  crab-student-NAME:PATH .`
