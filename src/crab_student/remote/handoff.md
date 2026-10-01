---
description: Hand this conversation over to an agent that keeps working on its own in a terminal on this machine, so the user can close VS Code. Use when the user asks to hand off, to leave it running, or to let it carry on alone.
argument-hint: "[autonomous|colleague] [what to continue with]"
---

The user wants to hand this conversation over. An agent on this machine continues it in a terminal
(tmux), with the whole conversation so far, and works without VS Code; the user can close VS Code and
look in later.

The user's words: $ARGUMENTS

1. The mode, from their words: `autonomous` (works alone and never asks) or `colleague` (works on its
   own and asks only about important or hard-to-reverse choices). If they don't say, it is `colleague`.
2. What to continue with, from their words. If they don't say, write one or two sentences from this
   conversation: the task, where it stands, and the next steps.
3. Commit your changes and push them (`git push`), so the agent starts from a saved state.
4. Run: `agent-handoff --session=${CLAUDE_SESSION_ID} <mode> "<what to continue with>"`
5. When it succeeds, tell the user in one line that the agent has taken over and that they can close
   VS Code, then end your turn. Don't do more work here: the agent continues from this point, and two
   sessions changing the same repo would get in each other's way.
