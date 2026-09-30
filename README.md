# crab-student

Hand a long or heavy task to Claude Code running on a rented [Vast.ai](https://vast.ai) machine, with
a GPU or CPU only, straight from your own Claude Code session:

> run the learning-rate ablation on a GPU

A few minutes later a remote Claude Code (a *worker*) is on it, on its own git branch `agent/<name>`.
You can watch and steer it from any terminal, it writes a report as it goes, and it stops its machine
when it is done or idle, so the bill stays small and bounded.

(A crab, because Claude Code's mascot is one. A student, because it does your research.)

## What you need

- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- git, the GitHub CLI [`gh`](https://cli.github.com), and an OpenSSH client (Windows has one built in)
- A Vast.ai account with some credit, and its [API key](https://cloud.vast.ai/manage-keys/)
- A Claude subscription (workers use it through a long-lived token; no API key)
- For `open --all`: Windows Terminal on Windows, or `tmux` on macOS and Linux (optional)

## Install and set up

```
uv tool install git+https://github.com/<you>/crab-student      # or, from a clone: uv tool install -e .
crab-student init
```

`init` walks you through everything once and is safe to run again (it only asks for what is
missing):

- **Vast key.** It uses the key file the `vastai` CLI keeps (`~/.config/vastai/vast_api_key`), so
  switching accounts there switches crab-student too. If there is none, it asks for one and saves it.
- **GitHub.** It uses your `gh` login.
- **SSH key.** It makes a key just for crab-student's machines and adds one `Include` line to
  `~/.ssh/config`, so `ssh crab-student-<name>` and VS Code can reach your workers.
- **Claude token.** You run `claude setup-token` in another terminal and paste the token; it is kept
  in your system keyring.
- **Claude Code.** It installs a skill (so your Claude Code knows how to use crab-student) and
  permission rules (below) into `~/.claude`.

## Launching

Ask your Claude Code in plain words: "run the ablation on a GPU with crab-student", "process the
dataset on a remote machine". It starts a background *launcher* so your conversation carries on. The
launcher reads the relevant code, estimates what the task needs (GPU memory, cores, RAM, disk,
hours), writes a brief for the worker, and runs `crab-student launch`. You then see the brief and one
line like:

```
ablation-lr: working on 1x RTX 4090 (Quebec, CA) at $0.42/hr - est. 3h ~ $1.26 - budget $2.52 (stops after 6h00m) - branch agent/ablation-lr - watch: crab-student open ablation-lr
```

You never type a price. crab-student picks the cheapest reliable machine that fits (for GPU work, a
faster GPU when it clearly pays for itself; among similar prices, the one that downloads fastest), and
the budget is about twice the estimate. `crab-student offers --hours 3 --gpus 1 --gpu-ram 24` shows
what would be picked, without renting anything.

**Your repo.** The worker starts from your current commit on a new branch `agent/<name>`. Local
commits that are not on GitHub yet, and uncommitted changes to tracked files, go along (as one extra
commit on the worker's branch; your own checkout is not touched). Untracked files do not; the launch
warns about them. A task that has nothing to do with a repo (`--no-repo`) goes to a private
`crab-student-work` repo on your GitHub.

## Watching and steering

```
crab-student open NAME           # the worker's Claude Code, in this terminal
crab-student open --all          # every running worker side by side
crab-student open NAME --vscode  # the machine in VS Code (Remote-SSH)
```

`open` attaches to the worker's own Claude Code session, which runs in tmux on the machine. Watch it,
or type to it like any Claude Code session. **Detach with Ctrl-b then d**: the worker keeps going.
Closing the window, losing the connection or a sleeping laptop do not stop it either; `open` again to
come back. In `open --all` on macOS/Linux the local layout uses Ctrl-a, so Ctrl-b still reaches the
worker.

Workers work on their own like a capable colleague: they decide ordinary things themselves and ask only
about important, hard-to-reverse choices. When one asks (its state becomes `needs you`), answer in
`open`, or with `crab-student send NAME "use the 2024 data"`, or through your Claude Code. If nobody
answers for a while, it makes the call itself and records it in its report.

## Keeping track

```
crab-student list            # every worker: state, its own status line, time, money spent / budget
crab-student list --watch    # the same, kept on screen; rings when a worker needs you, finishes or fails
crab-student status NAME     # one worker in detail
crab-student report NAME     # its REPORT.md
crab-student pull NAME       # fetch its branch into your repo (your checkout is not touched)
```

(`pull` for a `--no-repo` worker uses `~/crab-student-work`, cloning it the first time, unless you give
`--repo PATH`.)

Each worker writes `agent-runs/<name>/REPORT.md` on its branch: a summary, results with evidence,
what it did, the decisions it made without you, the files it left on the machine, and caveats.

Optional phone or chat notifications: `crab-student config notify_url <url>` (ntfy.sh, a Slack or
Discord webhook, or any URL that takes a JSON POST) for "needs you", "done", "failed" and "stopped".

## Stopping, starting, deleting

```
crab-student stop NAME           # saves (commits and pushes) its work, then stops the machine
crab-student start NAME          # starts it again; the worker resumes its conversation
crab-student extend NAME HOURS   # more machine time
crab-student delete NAME         # destroys the machine and forgets the worker
```

A stopped machine keeps its disk (Vast still bills a little for storage), so delete workers you are
done with. `delete` first checks the machine for work that would be lost (unpushed commits, files in
`/root/outputs`, large files outside git) and refuses, listing them, unless you add `--force`. The
branch stays on GitHub.

## Money and safety

- **Budget and time limit.** Each worker gets a budget (about 2x the estimate) and a time limit in
  machine hours from it. The machine stops itself when the limit is reached (after a 10-minute
  warning to the worker).
- **Idle stop.** The machine stops itself after an hour (`idle_minutes`) with nothing happening: no
  CPU or GPU work and no progress in the conversation. An open but unused session does not keep it up.
  A worker that has finished stops 15 minutes after it is done (`start` brings it back, with its
  conversation). A worker that goes quiet mid-task gets one reminder to check its jobs and carry on.
- **Save before stopping.** Every stop commits and pushes what can safely be saved first.
- **Dead-man switch.** If crab-student's own machinery on the machine never starts or dies, the
  machine still stops itself within about 45 minutes. A machine without a key to stop itself is refused
  at setup, and one whose stop call Vast ignores is stopped by the next `list` or `status`.
- **Approval.** A launch with a budget over `auto_approve_usd` ($50), or beyond `max_running_machines`
  (5), does not rent: it waits for `crab-student approve NAME`, which your Claude Code only runs with
  your OK.
- **Your machines only.** crab-student acts only on machines it rented and recorded, and never repeats
  a rent request whose outcome is unknown (so you never pay twice).
- **Nothing is destroyed automatically** once a worker has run on it: crab-student destroys a machine
  by itself only when it failed to boot or its first setup failed.
- **GitHub access** is one deploy key per worker, for its one repo, removed on delete. A deploy key
  can technically push to any branch of that repo, so crab-student's git hooks keep the worker on
  `agent/<name>`; for extra safety, protect your `main` branch on GitHub (deploy keys obey it).
- **Secrets** for tasks (a Hugging Face or W&B token, say): `crab-student secret set HF_TOKEN` (in a
  terminal; it asks for the value), then the launch attaches it with `--secret HF_TOKEN`. The worker
  sees it as an environment variable. Secret values never go on command lines, into logs, the brief
  or git (a pre-commit hook on the machine refuses commits containing them).

### What your Claude Code may do without asking

`init` adds these rules to `~/.claude/settings.json`. Allowed: `guide`, `offers`, `launch`, `list`,
`status`, `report`, `pull`, `send`, `open`, `stop`, `start`, `extend`, `secret list` (launch, start and
extend stay within the limits above by themselves). Always asked first: `approve`, `decline`,
`delete`, `config`, `init`, `secret set`, `secret delete`.

## Settings

`crab-student config` shows them all; `crab-student config KEY VALUE` sets one; `--reset` restores the
default.

| Setting | Default | What it does |
|---|---|---|
| `auto_approve_usd` | 50 | Budgets above this wait for `approve` |
| `max_running_machines` | 5 | More running machines than this wait for `approve` |
| `budget_factor` | 2 | Budget = this x the estimated cost |
| `min_budget_hours` | 2 | The budget always covers at least this many machine hours |
| `idle_minutes` | 60 | Idle stop after this long with nothing happening |
| `image` | `vastai/base-image:cuda-12.8.1-auto` | Docker image (Ubuntu, CUDA, Python) |
| `disk_gb` | 50 | Disk when the launch does not say |
| `min_reliability` | 0.98 | Only hosts at least this reliable |
| `min_download_mbps` | 200 | Only hosts that download at least this fast |
| `regions` | (anywhere) | Countries (`US`, `CA`, `DE`, ...) or continents (`north-america`, `europe`, `asia`, ...), comma-separated |
| `model` | (Claude Code's default) | The Claude model workers use |
| `workspace_repo` | `crab-student-work` | The repo for `--no-repo` tasks |
| `notify_url` | (none) | Where notifications go |

`idle_minutes`, `notify_url`, `image` and `model` apply to workers launched after the change.

## How it works

There is no background service: each `crab-student` command does its job and exits, keeping one small
JSON record per worker in `~/.crab-student/`. It talks to Vast over its REST API, to GitHub through
`gh` and `git`, and to machines through `ssh`. On each machine, one script sets things up (the repo,
Claude Code and its settings, git hooks) and runs a watchdog that keeps the worker's Claude Code alive
in tmux, measures activity, saves work, sends notifications and stops the machine. Vast's start
command carries the dead-man switch, so it works before anything else is installed.

## Trouble

- **A launch was cut off** (Ctrl-C, a closed terminal): `crab-student start NAME` continues it.
- **`list` says `rent unknown`**: Vast did not confirm a rent. crab-student never rents again by itself;
  `crab-student start NAME` looks for the machine again, or `delete` gives up.
- **Setup failed**: the machine was destroyed (it held nothing) and the error says why; fix it and
  `crab-student start NAME` tries again. A refused Claude token: `crab-student init --claude-token`.
- **Big outputs**: the worker keeps them in `/root/outputs` (not in git) and lists them in its report;
  copy them with `scp -F ~/.crab-student/ssh/config -r crab-student-NAME:/root/outputs .`
