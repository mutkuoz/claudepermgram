# How it works

A deep-dive into the `PreToolUse` hook protocol, the Telegram round-trip, and why the script is shaped the way it is.

## The flow

1. **Claude Code calls a tool.** The user asks Claude to do something; the model emits a tool call (Bash, Write, Edit, …). Before actually running it, Claude Code fires the `PreToolUse` hook.
2. **The hook script receives JSON on stdin.** Claude Code invokes `python3 $HOOK_PATH` and pipes the tool-call envelope to it.
3. **The script formats the action.** `telegram_approval.py` picks a per-tool formatter (Bash → 🖥️, Write → 📝, …) and builds a human-readable HTML summary.
4. **Sends a Telegram message.** `POST /bot<TOKEN>/sendMessage` with an `inline_keyboard` of two buttons, each carrying a unique `callback_data` suffix like `approve:f0f2c1e48b9d`.
5. **Long-polls `getUpdates`.** The script sits in a loop, calling `getUpdates` with a 25-second server-side `timeout`. The outer loop caps total wait at `CLAUDE_TG_TIMEOUT` (default 300s).
6. **User taps a button.** Telegram queues a `callback_query` update keyed by the bot, which the next `getUpdates` call delivers.
7. **The script acknowledges and edits.** `answerCallbackQuery` clears the spinner on the user's Telegram client; `editMessageText` rewrites the original message body to show ✅ Approved / ❌ Denied / ⏰ Timed out.
8. **The script exits.** Approve → exit 0. Deny / timeout → exit 2 with a JSON body describing the decision. Claude Code aborts or continues the tool call accordingly.

## The stdin envelope

Claude Code sends the hook something like:

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

Different tools have different `tool_input` shapes — see the formatters in `hooks/telegram_approval.py` for the full set.

## The deny response

On user denial or timeout, the script writes this JSON to stdout and exits with code 2:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "User denied the tool call via Telegram",
    "additionalContext": "User denied the tool call via Telegram"
  }
}
```

Exit code 2 is Claude Code's universal "block this action" signal. The JSON gives the model a clean reason string it can surface in its response to the user.

On approve, the script exits 0 with no stdout, which tells Claude Code "I have no opinion, proceed with normal permission flow."

## Polling, in detail

```
  ┌──────────────────────────────────────────────┐
  │ deadline = now + CLAUDE_TG_TIMEOUT (300s)    │
  └──────────────────────────────────────────────┘
                       │
                       ▼
  ┌─────────────────────────────┐         ┌──────────┐
  │ while now < deadline:       │────────▶│ timeout  │
  │   poll = min(25, remaining) │ no upd  │ → deny   │
  │   getUpdates(offset=X,      │         └──────────┘
  │              timeout=poll)  │
  │                             │──upd──▶ ┌──────────┐
  └─────────────────────────────┘         │ match    │
                                          │ cb id?   │
                                          └──────────┘
                                            ├ yes → answerCallbackQuery,
                                            │       editMessageText, exit
                                            └ no  → ack foreign cb, keep polling
```

- **Outer timeout (300s)** is the overall patience window; when it elapses the user gets a "⏰ Timed out" banner and Claude is told to deny.
- **Inner timeout (25s)** is Telegram's server-side long-poll — keeps the connection open just that long, then returns empty so we can loop. Anything larger risks hitting idle timeouts in common reverse proxies.
- **`offset = last_update_id + 1`** is Telegram's ack model: every update with an id below `offset` is marked consumed and will never be redelivered. We bump it every time we see a new update so we don't re-process the same button press.

## Why the per-invocation `callback_id`

One bot can receive `callback_query` updates for any chat it's in. If you have two concurrent Claude Code sessions running (e.g. two terminals, two projects), both would be polling the same bot and competing for updates.

Solution: each invocation generates a fresh `callback_id = uuid.uuid4().hex[:12]` and embeds it in every button's `callback_data`. When `getUpdates` returns a button press:

- If the `callback_data` suffix matches **this** invocation's id → act on it.
- If it matches a **different** session's id → acknowledge the callback (so the other user's spinner clears) but keep polling.

Because Telegram delivers each update to whichever poller reads it first, sessions will sometimes have to "skip past" each other's buttons. The `offset` bump ensures no update is ever dropped — it just gets routed to whichever session owns it.

## Why exit 2 + JSON

Claude Code's hook contract recognizes several signals:

| Exit code | Stdout                     | Effect                                    |
| --------- | -------------------------- | ----------------------------------------- |
| 0         | (empty)                    | Proceed with normal permission flow       |
| 0         | `hookSpecificOutput` JSON  | Honor the `permissionDecision` in JSON    |
| 2         | (anything)                 | Block the tool; show stderr/JSON to model |

We use exit 2 + JSON for deny so the block is unambiguous and the model still gets a structured reason. Approve is plain exit 0 — we don't override Claude Code's own permission flow; we just don't block it.

## Failure modes

- **Network down, bad token, Telegram 5xx** → `FAIL_OPEN=true` (default) exits 0 so Claude isn't stuck waiting on a bot that can't be reached. Set `FAIL_OPEN=false` for a strict posture that denies on error.
- **Hook process killed mid-poll** → Claude Code treats it as "no decision" and continues with normal permission flow (i.e. the terminal prompt the hook was supposed to replace). Set the hook `timeout` in `settings.json` higher than `CLAUDE_TG_TIMEOUT` so the script times out itself first.
- **Concurrent sessions, user taps the wrong chat's button** → the other session's hook will swallow and ignore it thanks to the `callback_id` filter. Just tap again on the right message.
