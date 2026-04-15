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

## "python3: command not found" in Claude Code logs

Your shell sees `python3`, but Claude Code's hook runner may have a stripped `PATH`. Use an absolute path in `settings.json`:

```json
"command": "/usr/bin/python3 $HOME/.claude/hooks/telegram_approval.py"
```

Find the right path with `which python3`.

## Telegram returns 401 Unauthorized

The bot token is wrong. Re-copy it from @BotFather (send `/token` to list your bots' tokens). No spaces, no quotes, and it should match the shape `<digits>:<letters/underscores/dashes>`.

## Telegram returns 403 Forbidden / "bot can't initiate conversation"

You haven't sent a message to your bot yet. Open the chat with your bot in Telegram and send `/start`, then try again.

## Chat ID seems wrong — message goes to the wrong chat, or nothing arrives

1. Re-run `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser after sending the bot a fresh message.
2. Look for the `"chat":{"id":…}` of the message **you** just sent.
3. For group chats, the ID is negative (often starting with `-100...`). For private chats, it's a positive integer matching your user ID.

Alternative: message [@userinfobot](https://t.me/userinfobot) — it replies with your user ID, which is also your private-chat ID for bots.

## The message appears but buttons don't do anything

Likely causes, in order of probability:

1. **Stale `getUpdates` offset from a previous session.** Another poller (another `telegram_approval.py` process, or a webhook you set up earlier) is consuming updates first. Kill any stray processes:
   ```bash
   pgrep -fa telegram_approval.py
   ```
   Then make sure you haven't set a webhook — if you ever did, `getUpdates` silently returns nothing:
   ```bash
   curl "https://api.telegram.org/bot<TOKEN>/deleteWebhook"
   ```
2. **Concurrent Claude Code sessions.** Multiple hook processes share the same bot's update queue. The per-invocation `callback_id` prevents cross-fire, but you may have to tap each message explicitly. Keep sessions separated by chat (use different group chats per project if this annoys you).
3. **Outdated hook script.** Early versions of this project didn't use a unique `callback_id`. Re-run `./install.sh` to refresh the copy under `~/.claude/hooks/`.

## Approval times out immediately

Three things to check:

- **System clock** — `timedatectl status` (Linux) or `date` (macOS). If it's off by more than a few minutes, the HTTPS handshake to `api.telegram.org` may fail and the script falls through to the timeout path.
- **`CLAUDE_TG_TIMEOUT`** — if you `export CLAUDE_TG_TIMEOUT=0` by accident, every request times out instantly. Default is 300.
- **Claude Code hook `timeout`** — if the `timeout` field in `settings.json` is lower than `CLAUDE_TG_TIMEOUT`, Claude Code kills the hook before it finishes polling. Keep `settings.json`'s timeout at least 20 seconds above `CLAUDE_TG_TIMEOUT`.

## Merging with an existing settings.json that already has hooks

`install.sh` handles this automatically (jq if present, Python fallback otherwise), but if you're merging by hand, the structure is:

```json
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "...existing matcher...", "hooks": [ /* existing hook */ ] },
      { "matcher": "Bash|Write|Edit|MultiEdit|Read|Glob|Grep|WebFetch|WebSearch|Agent",
        "hooks": [
          { "type": "command",
            "command": "python3 $HOME/.claude/hooks/telegram_approval.py",
            "timeout": 320 }
        ]
      }
    ]
  }
}
```

Order matters if the matchers overlap — Claude Code runs hooks in the order they appear. For approval, putting this hook first means it runs before any other side-effectful hook you might have.

Jq one-liner for manual merge:

```bash
jq '.hooks.PreToolUse += [
  {
    matcher: "Bash|Write|Edit|MultiEdit|Read|Glob|Grep|WebFetch|WebSearch|Agent",
    hooks: [{ type: "command",
              command: "python3 $HOME/.claude/hooks/telegram_approval.py",
              timeout: 320 }]
  }
]' ~/.claude/settings.json > /tmp/s.json && mv /tmp/s.json ~/.claude/settings.json
```

## The test Telegram message arrives, but Claude-triggered ones don't

Almost always env-var scope:

- The installer wrote `export CLAUDE_TG_TOKEN=...` to your shell rc, but you're running Claude Code from a terminal that was open before the rc changed. Solution: open a new terminal, or `source ~/.zshrc` (or `.bashrc`).
- You set the vars in your shell but launched Claude Code from a macOS Finder / Dock icon — Finder doesn't inherit shell env. Launch Claude Code from the terminal instead, or define the env vars at the OS level (`launchctl setenv` on macOS).

## Hook runs but Claude still shows the normal terminal approval prompt

Claude Code falls back to the default permission flow when the hook exits 0 with no JSON. That's the expected behavior on approve — the terminal prompt isn't bypassed, it's been pre-approved via your Telegram tap. If you see it anyway, the hook likely errored out early and took the `FAIL_OPEN=true` path. Check Claude Code's hook logs (look for stderr lines starting with `[telegram_approval]`).
