---
name: crab-student
description: Rent a machine on Vast.ai (GPU or CPU) and put a Claude Code agent on it to work on a task - training, experiments, data processing, big refactors, anything slow or heavy, or work the user wants to do themselves in VS Code on a bigger machine - and manage those agents - status, reports, results, steering, stopping, deleting. Use when the user asks to run something on a GPU or on a remote, rented or cloud machine, asks for a machine with Claude on it, mentions Vast or crab-student, or names a crab-student agent.
---

# crab-student

Agents are Claude Code sessions on rented Vast.ai machines, one per task, each on its own git branch
`agent/<name>`, writing its report to `agent-runs/<name>/REPORT.md` on that branch. There is no time
limit: a machine stops itself (saving first) after `idle_minutes` with no CPU or GPU work and no
conversation activity, and `list` shows what is running and what it costs. The user's setting
`approve_above_usd_per_hour` decides which machines need their approval.

## Modes

Pick the mode from the user's words; without a hint the setting `mode` decides (colleague unless
changed).

- **colleague**: works mostly on its own, asks only about important or hard-to-reverse choices, like a
  capable colleague.
- **autonomous**: "just do it", "don't bother me", "overnight": never asks; decides, and records each
  real decision with its reasons in the report.
- **interactive**: "I want to work on it myself", "give me a machine with Claude on it": a normal Claude
  Code session in VS Code on the machine that the user drives. No report; the user says when it is done.

The model, effort and fast mode of the agent's Claude come from the user's own Claude settings unless
they ask for others (`--model`, `--effort`, `--fast`).

## Launching

Do not estimate the task yourself. Start a background fork (the Agent tool, `subagent_type: "fork"`,
`run_in_background: true`). A fork sees this conversation, including the user's exact words, so the
prompt is only:

> Run `crab-student guide launch` and follow it for the task above.

If forks are not available (the Agent tool refuses "fork", as in some Claude Code environments), start a
general-purpose background agent instead, with the task in the user's words and the context in the
prompt (repo path, relevant files, data, secrets, the mode, model, price or hours they mentioned),
followed by the same instruction.

For several agents at once (a sweep, or several tasks), start ONE launcher with all of them: it writes a
brief for each and launches them side by side (one launch each), each on a machine of its own.

Keep talking with the user meanwhile. Tell them that once the launch starts, a window opens on their
screen (Windows Terminal, or Terminal on macOS; a tab per agent) that shows its progress and then the
agent itself; elsewhere, or with the setting `open_window` off, `crab-student open NAME` in their own
terminal does the same. A launch rents two machines at once and keeps the first that starts (the other
is destroyed at once). The launcher's permission prompts, if any, appear in this session: say what they
are for. When it finishes, show the user the brief(s) it gave, then the result line(s) and any
`Warning:` lines, exactly as returned.

If a result says a launch needs approval, tell the user the price per hour and why, and ask. Only after
an explicit yes in this conversation, run `crab-student approve NAME --json`, one
Bash call per agent with `run_in_background: true` (several can run at once). If one is cut off, run
`crab-student start NAME --json`, which tries again. If they say no: `crab-student decline NAME`.

## Interactive agents

When the machine is ready, the launch's window opens VS Code on it and, a few seconds later,
fills in the Claude panel with the task (not yet sent). The window then stays open and becomes the agent's terminal after the
hand-off. If no window opened (or they closed it), they run the command the result line ends with,
`crab-student open NAME --vscode`, in their own terminal (it waits about 15 seconds, so if you run it
yourself, a Bash timeout of 60000 ms is enough). They work with the agent there. When they are done and
want it to keep going on its own, they type `/handoff` in that chat (optionally `autonomous` or
`colleague`, and what to continue with), or ask for a hand-off in words. The agent commits, pushes and
carries on in tmux with the whole conversation, after which they can close VS Code and use everything
below. Before that, `send` does not apply, and `status` shows the agent as `interactive`.

The machine installs the project's dependencies during setup, before the agent starts (the launch's
`--setup` changes how); a failed install is a warning in the launch's result.

## Managing agents

| The user wants | Run |
|---|---|
| an overview | `crab-student list --json` |
| how one is doing | `crab-student status NAME --json` |
| its report | `crab-student report NAME` |
| its results in their repo | `crab-student pull NAME` |
| its big output files on this computer | `crab-student fetch NAME` (below) |
| to tell it something | `crab-student send NAME` with the text on stdin |
| to stop it (it saves first) | `crab-student stop NAME` |
| to start it again | `crab-student start NAME --json` |
| what the agents have cost | `crab-student spend` |
| what machines cost | `crab-student offers [--gpus N --gpu-ram GB] [--kinds]` |

Summarize the output in plain words; do not paste tables unless asked. A state of `needs you` means
the agent asked a question: show it, and pass the answer on with `send` (or the user can type to the
agent directly with `open`). An agent that is done stops its machine after the idle time; for a
follow-up, `start` it (its conversation resumes), then `send`.

## Waiting for agents

When you are to act on an agent's results, or to tell the user when it finishes, never write a polling
loop: run `crab-student wait NAME ... --json` as a Bash call with `run_in_background: true`. It ends
when one of those agents is done, failed, needs the user, stopped, cut off or deleted, and after 60
minutes otherwise (`--timeout MINUTES`). Act on what it says, then run it again with the names in its
`waiting`, until none is left. Without names it watches every agent there is, and ends at once while
any of them is in such a state.

- `unseen` in the result (exit code 1): an agent could not be seen for 10 minutes. Tell the user at
  once, with the reason.
- `vast_problem` in the result (most often Vast wants a fresh 2FA login): until the user fixes it in
  their own terminal, no machine can be started, stopped or deleted. Tell them at once.

When an agent is done and its report lists files in `/root/outputs`, run `crab-student fetch NAME`
at once, also as a background Bash call: its machine stops itself after the idle time, and a stopped
machine cannot always be started again soon. The files go into a
new folder `NAME-outputs` in the current folder (`NAME-outputs-2`, ... beside an earlier copy), and the
copy is checked. `--to DIR` (new or empty) asks the user first: the machine chooses the file names, so
never point it at a folder where files mean something (`.claude`, `.github`, a folder of programs).

The user can also get phone or chat notifications (the `notify_url` setting).

## Watching

The launch's window ends up attached to the agent. Any time later, the user runs `crab-student open NAME`
in their own terminal (or `open --all` for every running agent side by side, or `open NAME --vscode`
for VS Code, whose terminal attaches to the agent by itself); on an agent still launching it shows the
progress first. It attaches to the agent's Claude Code; detaching with Ctrl-b d leaves the agent
running. On Windows, running it yourself opens a new Windows Terminal window.

## Deleting

Delete when the user asks for it (suggest `pull` and reading the report first if they have not). `delete`
refuses (exit 3) when work would be lost and lists it: show that list and let the user decide about
`--force`. Never add
`--force` on your own. Files in `/root/outputs` that `fetch` copied do not count as lost while every
one is as it was copied, so after a fetch a plain `delete` is enough, also once the machine has
stopped. A stopped agent's disk is still billed
(`list` shows its disk cost per day and how long it has sat), and it can usually be deleted without
starting it again.

## Rules

- Run `delete` and `decline` only when the user asks. Never run these without the user's explicit OK in
  this conversation: `approve`, `delete --force`, `config KEY VALUE`, `init`. The permission prompts
  enforce this too.
- The user stores secrets themselves in a terminal (`crab-student secret set NAME`); you never see or
  handle secret values.
- Exit codes: 0 ok, 1 error, 2 usage, 3 needs approval / refused by a limit / would lose work, 4 no
  machine fits or the offers chosen can no longer be rented, 5 setup failed. The error on stderr says
  what to do.
- Windows: pass text on stdin, and never pass arguments that start with `/` (Git Bash rewrites them).
