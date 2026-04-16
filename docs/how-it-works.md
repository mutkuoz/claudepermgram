# How it works

A deep-dive into the `PreToolUse` hook protocol, the Telegram round-trip, and the decision flow with feedback, AskUserQuestion, and plan review.

## The flow at a glance

1. **Claude Code calls a tool.** The user asks Claude to do something; the model emits a tool call (`Bash`, `Write`, `Edit`, `AskUserQuestion`, `ExitPlanMode`, …). Before running it, Claude Code fires the `PreToolUse` hook.
2. **The hook script receives JSON on stdin.** Claude Code invokes `python3 $HOOK_PATH` and pipes the tool-call envelope to it.
3. **The script dispatches on `tool_name`.**
   - `AskUserQuestion` → one button per option.
   - `ExitPlanMode` → plan body + Approve / Terminal / Reject.
   - Everything else → generic Approve / Terminal / Deny.
4. **Sends a Telegram message** with an `inline_keyboard`. Every button carries a unique `callback_data` suffix (`approve:f0f2c1e4...`) so concurrent Claude Code sessions can't cross-fire.
5. **Long-polls `getUpdates`** with a 25-second server-side timeout, looping until the overall `CLAUDE_TG_TIMEOUT` (default 300s) elapses.
6. **User taps a button.** Telegram queues a `callback_query` update, which the next `getUpdates` call delivers.
7. **If Deny was tapped**, the script sends a follow-up message asking for an optional reason. It listens for up to `CLAUDE_TG_FEEDBACK_WAIT` (default 60s) for either:
   - a reply message → used verbatim as the denial reason, or
   - a `Skip` button tap → uses a default reason.
8. **`answerCallbackQuery`** clears the Telegram spinner on the user's client; **`editMessageText`** rewrites the original message to show the outcome (✅ Approved / 💻 Deferred / ❌ Denied with feedback / ⏰ Timed out).
9. **The script exits.** Decisions map to exit codes and JSON on stdout (see below).

## Stdin envelope

Claude Code sends the hook JSON that looks like:

```json
{
  "session_id": "abc123...",
  "transcript_path": "/Users/you/.claude/projects/.../transcript.jsonl",
  "cwd": "/Users/you/projects/demo",
  "hook_event_name": "PreToolUse",
  "tool_name": "Bash",
  "tool_input": {
    "command": "rm -rf build",
    "description": "clean the build dir",
    "run_in_background": false
  }
}
```

Different tools have different `tool_input` shapes — see the formatters near the top of `hooks/telegram_approval.py` for the full set.

## The three decisions

Every decision is expressed through an exit code plus JSON on stdout:

| Button       | Exit | stdout JSON                                                                                 | What Claude does                                            |
| ------------ | ---- | ------------------------------------------------------------------------------------------- | ----------------------------------------------------------- |
| ✅ Approve   | 0    | `{"hookSpecificOutput":{"permissionDecision":"allow", ...}}`                                | Run the tool immediately, no further permission prompt.     |
| 💻 Terminal  | 0    | *(empty)*                                                                                   | Fall through to the normal permission flow (terminal prompt). |
| ❌ Deny      | 2    | `{"hookSpecificOutput":{"permissionDecision":"deny","permissionDecisionReason":"...", "additionalContext":"..."}}` | Block the tool; reason is shown to the model so it can adapt. |
| ⏰ Timed out | 2    | same deny JSON with a timeout reason                                                        | Treated as a deny.                                          |

Exit code 2 is Claude Code's "block this action" signal. Exit 0 with explicit `"allow"` skips any downstream permission prompt; exit 0 with no JSON defers to Claude's default behavior (which is the in-terminal prompt for anything that isn't auto-approved).

## Deny with feedback

After you tap ❌, the hook doesn't immediately exit. It sends a follow-up Telegram message:

```
🗒 Reply with a reason (what Claude should do instead), or tap Skip to deny without feedback.
[ Skip (no reason) ]
```

It then polls for either:
- a **message** in the same chat whose `reply_to_message.message_id` matches the prompt — the text is used verbatim,
- a **callback_query** with `data = "skip:<callback_id>"` — uses the default reason,
- `CLAUDE_TG_FEEDBACK_WAIT` seconds of silence — uses the default reason.

The denial reason Claude sees becomes, for example:

```
User denied the tool call via Telegram. User said: "run it with -v instead"
```

Claude reads this and typically retries the tool with the user's requested modification.

## AskUserQuestion routing

When Claude calls `AskUserQuestion`, the stdin looks like:

```json
{
  "tool_name": "AskUserQuestion",
  "tool_input": {
    "questions": [{
      "question": "Which library should we use?",
      "header": "Library",
      "options": [
        {"label": "React", "description": "..." },
        {"label": "Vue",   "description": "..." }
      ],
      "multiSelect": false
    }]
  }
}
```

The hook shows the question in Telegram with one button per option (up to 10; two per row). Tapping a button sends back an exit-2 with a reason like:

```
The user answered via Telegram: "React". Treat this as the answer and continue —
do not call AskUserQuestion again for this question.
```

Claude reads the denial reason and treats it as the user's response. The actual `AskUserQuestion` tool never runs — no terminal prompt.

**Limitations**: multiSelect questions and questions with more than 10 options fall through to terminal (exit 0 with no JSON), where Claude Code's native UI handles them properly.

## Plan review (`ExitPlanMode`)

When Claude finishes building an implementation plan and calls `ExitPlanMode`, the hook shows the plan body with Approve / Terminal / Reject buttons. The plan text comes from `tool_input.plan`. Truncated to 3500 chars to fit Telegram's 4096-byte message cap.

- **Approve** → exit 0 + allow → plan mode exits, Claude starts implementing.
- **Terminal** → exit 0 → Claude Code's normal plan-approval UI (terminal).
- **Reject** → exit 2 + deny. Feedback prompt fires just like a regular deny — reply with "reconsider the DB migration step" and Claude revises.

## The `callback_id`

One bot can receive `callback_query` updates for any chat it's in. If you have two concurrent Claude Code sessions running, they'd both poll the same bot's update queue.

Each hook invocation generates a fresh `callback_id = uuid.uuid4().hex[:12]` and embeds it in every button's `callback_data`. When `getUpdates` returns a button press:

- If the suffix matches **this** invocation's id → act on it.
- If it matches a **different** session's id → silently acknowledge (so the other user's spinner clears) and keep polling.

Because Telegram delivers each update to whichever poller reads it first, sessions sometimes have to skip past each other's buttons. The `offset = last_update_id + 1` bump ensures no update is ever dropped — it just gets routed to whichever session owns it.

## Polling math

```
┌──────────────────────────────────────────────┐
│ deadline = now + CLAUDE_TG_TIMEOUT  (300s)   │
└──────────────────────────────────────────────┘
                    │
                    ▼
┌──────────────────────────────────────┐
│ while now < deadline:                │── no upd → deadline elapses → deny
│   poll = min(25, remaining)          │
│   getUpdates(offset=X, timeout=poll) │── update  ─────────────────▶ parse
└──────────────────────────────────────┘                               │
                                                                       ▼
                                                            matches our id? yes → act
                                                                          → no  → ack,
                                                                                  keep polling
```

- **Outer timeout (300s)** is your overall patience window.
- **Inner timeout (25s)** is Telegram's server-side long-poll. Larger values risk tripping reverse-proxy idle timeouts.
- When you tap ❌, a **second polling phase** kicks in for up to `CLAUDE_TG_FEEDBACK_WAIT` seconds, sharing the same `Poller.offset` so no updates slip past during the handoff.

## Permission-mode mirroring

Claude Code's session has a `permission_mode` (`default`, `acceptEdits`, `plan`, `bypassPermissions`). When `CLAUDE_TG_RESPECT_MODE=true` (the default), the hook short-circuits before any Telegram request for tools Claude would already auto-approve:

| `permission_mode`     | What the hook does                                                   |
| --------------------- | -------------------------------------------------------------------- |
| `default`             | Prompt via Telegram as usual.                                        |
| `acceptEdits`         | Silent allow for `Edit` / `Write` / `MultiEdit` / `NotebookEdit`. Everything else still prompts. |
| `bypassPermissions`   | Silent allow for every tool. Hook returns `permissionDecision: "allow"` without any Telegram round-trip. |
| `plan`                | Prompt as usual. Plan mode only allows `ExitPlanMode` anyway; that's exactly when you want Telegram review. |

Set `CLAUDE_TG_RESPECT_MODE=false` for strict mode — always ask on Telegram regardless of session mode.

## Failure modes

- **Network down, bad token, Telegram 5xx** → `FAIL_OPEN=true` (default) exits 0 so Claude isn't stuck. Set `FAIL_OPEN=false` for a strict posture that denies on error.
- **Hook process killed mid-poll** → Claude Code treats it as "no decision" and continues with normal permission flow. Keep the hook `timeout` in `settings.json` at least 20 seconds above `CLAUDE_TG_TIMEOUT` so the script times out itself first.
- **Concurrent sessions, user taps the wrong chat's button** → the other session's hook silently swallows it (thanks to the `callback_id` filter). Tap again on the right message.
- **Feedback reply arrives late** → once `CLAUDE_TG_FEEDBACK_WAIT` expires, the hook has already denied with the default reason. The stray reply is consumed by whichever hook picks up the next `getUpdates` call and is ignored.
