---
name: crab-student
description: Run long or heavy tasks (training, experiments, data processing, big refactors, anything slow) on a rented Vast.ai machine, GPU or CPU, with a remote Claude Code worker, and manage those workers - status, reports, results, steering, stopping, deleting. Use when the user asks to run something on a GPU or on a remote, rented or cloud machine, mentions Vast or crab-student, or names a crab-student worker.
---

# crab-student

Workers are Claude Code sessions on rented Vast.ai machines, one per task, each on its own git branch
`agent/<name>`, writing its report to `agent-runs/<name>/REPORT.md` on that branch. Money is bounded
by a budget (about 2x the estimate), a time limit, an idle stop and an approval gate.

## Launching

Do not estimate the task yourself. Start a background agent (the Agent tool, `run_in_background:
true`) with this prompt:

> You are the crab-student launcher. Run `crab-student guide launch` and follow it exactly. The task,
> in the user's words: <...>. Context: <repo path, relevant files, data, secrets, any deadline or
> budget the user mentioned>.

Keep talking with the user meanwhile. When the launcher finishes, show the user the brief it gave the
worker, then the result line and any `Warning:` lines, exactly as returned.

If the result says the launch needs approval, tell the user the amount and why, and ask. Only after an
explicit yes in this conversation, run `crab-student approve NAME --json` (Bash timeout 600000 ms). If
it is cut off, run `crab-student start NAME --json`, which continues it. If they say no:
`crab-student decline NAME`.

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
worker directly with `open`).

## Watching

Tell the user to run `crab-student open NAME` in their own terminal (or `open --all` for every
running worker side by side, or `open NAME --vscode`). It attaches to the worker's Claude Code;
detaching with Ctrl-b d leaves the worker running. On Windows, running it yourself opens a new
Windows Terminal window.

## Deleting

Always ask the user first, and suggest `pull` and reading the report before. `delete` refuses (exit 3)
when work would be lost and lists it: show that list and let the user decide about `--force`. Never
add `--force` on your own.

## Rules

- Never run these without the user's explicit OK in this conversation: `approve`, `decline`,
  `delete`, `config KEY VALUE`, `init`. The permission prompts enforce this too.
- The user stores secrets themselves in a terminal (`crab-student secret set NAME`); you never see or
  handle secret values.
- Exit codes: 0 ok, 1 error, 2 usage, 3 needs approval / refused by a limit / would lose work, 4 no
  machine fits, 5 setup failed. The error on stderr says what to do.
- Windows: pass text on stdin, and never pass arguments that start with `/` (Git Bash rewrites them).
