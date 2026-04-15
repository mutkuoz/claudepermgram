# Troubleshooting

## The hook never fires

**Check Claude Code sees it.** Inside a `claude` session, run `/hooks`. You should see a `PreToolUse` entry whose command points to `telegram_approval.py`.

If not:

1. Confirm `~/.claude/settings.json` exists and contains the `hooks.PreToolUse` block. Paste its contents into a JSON validator — Claude Code silently ignores the whole file if it's invalid JSON.
2. Confirm the hook file exists and is executable:
   ```bash
   ls -l ~/.claude/hooks/telegram_approval.py
   ```
3. Restart `claude` — hooks are loaded at session start.

## Hook fires but no Telegram message arrives

Almost always env-var scope. The hook runs, can't find `CLAUDE_TG_TOKEN` / `CLAUDE_TG_CHAT_ID`, logs `CLAUDE_TG_TOKEN or CLAUDE_TG_CHAT_ID not set - failing open` to stderr, and exits 0 silently so Claude proceeds normally.

Check whether the variables are visible to Claude Code:

```bash
# Before launching claude, in the same terminal:
env | grep CLAUDE_TG
```

If they're missing:

- Bash only reads `.bashrc` for **interactive non-login shells**. If your terminal launches a login shell, or you started Claude Code from a GUI launcher, `.bashrc` isn't sourced. Put the exports in `~/.bash_profile` too, or move them to `~/.profile`.
- Or set them in `~/.claude/settings.json` under a top-level `env` block (applies regardless of how Claude Code is launched):
  ```json
  { "env": { "CLAUDE_TG_TOKEN": "...", "CLAUDE_TG_CHAT_ID": "..." }, "hooks": { ... } }
  ```

## "python3: command not found" in Claude Code logs

Your shell sees `python3`, but Claude Code's hook runner may have a stripped `PATH`. Use an absolute path in `settings.json`:

```json
"command": "/usr/bin/python3 $HOME/.claude/hooks/telegram_approval.py"
```

Find the right path with `which python3`.

## Telegram returns 401 Unauthorized

The bot token is wrong. Re-copy it from @BotFather (send `/token` to list your bots' tokens). No spaces, no quotes, shape is `<digits>:<letters/underscores/dashes>`.

## Telegram returns 403 Forbidden / "bot can't initiate conversation"

You haven't messaged your bot yet. Open the chat with your bot in Telegram and send `/start`, then retry.

## Chat ID seems wrong — message goes to the wrong chat, or nothing arrives

1. Re-run `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser after sending the bot a fresh message.
2. Look for the `"chat":{"id":…}` of the message you just sent.
3. For group chats, the ID is negative (often `-100…`). For private chats, it's a positive integer matching your user ID.

Alternative: message [@userinfobot](https://t.me/userinfobot) — it replies with your user ID, which is also your private-chat ID for bots.

## Buttons appear but don't do anything

In order of probability:

1. **Stale `getUpdates` poller.** Another process is consuming updates first. Check:
   ```bash
   pgrep -fa telegram_approval.py
   ```
2. **A webhook is set on your bot.** When a webhook exists, `getUpdates` silently returns nothing:
   ```bash
   curl "https://api.telegram.org/bot<TOKEN>/deleteWebhook"
   ```
3. **Concurrent Claude Code sessions.** Multiple hooks share the bot's update queue. The per-invocation `callback_id` prevents cross-fire, but you may have to tap each message explicitly.

## The deny-feedback prompt doesn't respond to my reply

- Make sure you're using Telegram's **Reply** feature (swipe or long-press the prompt → Reply). A plain new message won't be recognized — the script filters on `reply_to_message.message_id`.
- The feedback window is `CLAUDE_TG_FEEDBACK_WAIT` seconds (default 60). After that, the hook denies with the default reason.
- If you want to disable the feedback prompt entirely, `export CLAUDE_TG_ALLOW_FEEDBACK=false`.

## AskUserQuestion falls through to terminal even when I answer in Telegram

`multiSelect: true` questions and questions with more than 10 options are deliberately deferred to terminal — inline keyboards don't handle multi-select cleanly. This is by design. If Claude asked a single-select question with ≤10 options and it still fell through, check the hook's stderr for an error.

## The plan review message is cut off

Telegram caps message bodies at 4096 characters. The script truncates plan text to 3500 chars; anything beyond is elided with a `... [N more chars]` suffix. If your plans are routinely longer, the right answer is to split them — but you can also bump `MAX_PLAN_LEN` in `~/.claude/hooks/telegram_approval.py`.

## Approval times out immediately

Three things to check:

- **System clock** — `timedatectl status` (Linux) or `date` (macOS). Skew of more than a few minutes breaks the HTTPS handshake to `api.telegram.org`.
- **`CLAUDE_TG_TIMEOUT`** — setting it to 0 makes every request time out instantly. Default is 300.
- **Claude Code hook `timeout`** — must be higher than `CLAUDE_TG_TIMEOUT + ~20`. Default in our `settings.json` is 320.

## Merging with an existing settings.json that already has hooks

`install.sh` handles this automatically (jq if present, Python fallback otherwise). By hand:

```bash
jq '.hooks.PreToolUse += [
  {
    matcher: "Bash|Write|Edit|MultiEdit|Read|Glob|Grep|WebFetch|WebSearch|Agent|AskUserQuestion|ExitPlanMode",
    hooks: [{ type: "command",
              command: "python3 $HOME/.claude/hooks/telegram_approval.py",
              timeout: 320 }]
  }
]' ~/.claude/settings.json > /tmp/s.json && mv /tmp/s.json ~/.claude/settings.json
```

Order within `PreToolUse` matters when matchers overlap — putting this hook first means it runs before any other side-effectful hook.

## The test Telegram message arrives, but Claude-triggered ones don't

See [*Hook fires but no Telegram message arrives*](#hook-fires-but-no-telegram-message-arrives). Short version: the installer wrote exports to your shell rc, but Claude Code was launched from a shell that didn't source it. Open a fresh terminal, or set the vars in `~/.claude/settings.json` → `env`.

## Hook runs but Claude still shows the normal terminal approval prompt

Means the hook exited 0 with no JSON — which is exactly what **💻 Terminal** does by design. If you tapped ✅ Approve and still got a terminal prompt, the hook likely hit an error early and took the `FAIL_OPEN=true` path. Check Claude Code's logs for stderr lines starting with `[telegram_approval]`.
