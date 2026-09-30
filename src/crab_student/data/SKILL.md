---
name: crab-student
description: Run long or heavy tasks (training, experiments, data processing, big refactors, anything slow) on a rented Vast.ai machine, GPU or CPU, with a remote Claude Code worker, and manage those workers - status, reports, results, steering, stopping, deleting. Use when the user asks to run something on a GPU or on a remote, rented or cloud machine, mentions Vast or crab-student, or names a crab-student worker.
---

# crab-student

Workers are Claude Code sessions on rented Vast.ai machines, one per task, each on its own git branch
`agent/<name>`, writing its report to `agent-runs/<name>/REPORT.md` on that branch. Money is bounded
by a time limit (twice the estimate), an idle stop, and the user's approval for any machine over
approve_above_usd_per_hour ($5/hr unless they changed it).

## Launching

Do not estimate the task yourself. Start the launcher in the background (the Agent tool,
`subagent_type: "crab-student-launcher"`, `run_in_background: true`) with this prompt:

> The task, in the user's words: <...>. Context: <repo path, relevant files, data, secrets, any
> deadline or price limit the user mentioned>.

If that agent does not exist, use a general-purpose background agent and start the prompt with "You
are the crab-student launcher. Run `crab-student guide launch` and follow it exactly."

For several workers at once (a sweep, or several tasks), start ONE launcher with all of them: it writes
a brief for each and launches them together, each on a machine of its own.

Keep talking with the user meanwhile. When the launcher finishes, show the user the brief(s) it gave,
then the result line(s) and any `Warning:` lines, exactly as returned.

If a result says a launch needs approval, tell the user the price per hour and why, and ask. Only after
an explicit yes in this conversation, run `crab-student approve NAME [NAME ...] --json` (Bash timeout
600000 ms). If it is cut off, run `crab-student start NAME --json` for each, which continues it. If
they say no: `crab-student decline NAME`.

## Managing workers

| The user wants | Run |
|---|---|
| an overview | `crab-student list --json` |
| how one is doing | `crab-student status NAME --json` |
| its report | `crab-student report NAME` |
| its results in their repo | `crab-student pull NAME` |
| to tell it something | `crab-student send NAME` with the text on stdin |
| to stop it (it saves first) | `crab-student stop NAME` |
| to start it again | `crab-student start NAME --json` |
| more time | `crab-student extend NAME HOURS --json` |
| what machines cost | `crab-student offers --hours H [--gpus N --gpu-ram GB]` |

Summarize the output in plain words; do not paste tables unless asked. A state of `needs you` means
the worker asked a question: show it, and pass the answer on with `send` (or the user can type to the
worker directly with `open`). A finished worker's machine stops itself after 15 quiet minutes; for a
follow-up, `start` it (its conversation resumes), then `send`.

## Watching

Tell the user to run `crab-student open NAME` in their own terminal (or `open --all` for every
running worker side by side, or `open NAME --vscode`). It attaches to the worker's Claude Code;
detaching with Ctrl-b d leaves the worker running. On Windows, running it yourself opens a new
Windows Terminal window.

## Deleting

Always ask the user first, and suggest `pull` and reading the report before. `delete` refuses (exit 3)
when work would be lost and lists it: show that list and let the user decide about `--force`. Never
add `--force` on your own. A stopped worker's disk is still billed (a few cents a day; `list` shows how
long it has sat), and it can usually be deleted without starting it again.

## Rules

- Never run these without the user's explicit OK in this conversation: `approve`, `decline`,
  `delete`, `config KEY VALUE`, `init`. The permission prompts enforce this too.
- The user stores secrets themselves in a terminal (`crab-student secret set NAME`); you never see or
  handle secret values.
- Exit codes: 0 ok, 1 error, 2 usage, 3 needs approval / refused by a limit / would lose work, 4 no
  machine fits, 5 setup failed. The error on stderr says what to do.
- Windows: pass text on stdin, and never pass arguments that start with `/` (Git Bash rewrites them).
