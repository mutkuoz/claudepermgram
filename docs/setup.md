# Setup guide

A long-form companion to the `README.md` quick-install — use this if `install.sh` doesn't work for your environment, or if you'd rather see each step.

## 1. Create a Telegram bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather).
2. Send `/newbot`. Pick a display name, then a username ending in `bot`.
3. BotFather replies with a token like `123456789:ABCdefGhIJKlmnOPQrstUvwxYZ0123456`. **Save it.**
4. (Optional but recommended) Send `/setprivacy` → `Disable` if you plan to use the bot in a group.

## 2. Find your chat ID

Telegram doesn't route updates to a bot until you've interacted with it from that chat.

1. Open a chat with your new bot and send `/start` (or any message).
2. In a browser, load:
   ```
   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   ```
3. Look for the `"chat":{"id":…}` field. That number is your chat ID.
   - Private chats: positive integer (your user ID).
   - Groups / channels: negative integer (supergroups start with `-100`).

If `getUpdates` returns `{"ok":true,"result":[]}`, you haven't sent a message to the bot yet — do that first.

## 3. Install

### Automatic (recommended)

```bash
git clone https://github.com/YOUR_USER/claude-telegram-approval.git
cd claude-telegram-approval
./install.sh
```

The installer asks for your token and chat ID, writes them to your shell rc, copies the hook into `~/.claude/hooks/`, merges the config into `~/.claude/settings.json`, and sends a test message.

### Manual

```bash
# 1. Copy the hook
mkdir -p ~/.claude/hooks
cp hooks/telegram_approval.py ~/.claude/hooks/
chmod +x ~/.claude/hooks/telegram_approval.py

# 2. Add the hook to settings.json (create the file if it doesn't exist)
#    See config/settings.json for the exact shape.

# 3. Export credentials (also add these to your ~/.zshrc or ~/.bashrc)
export CLAUDE_TG_TOKEN="123456789:ABCdef..."
export CLAUDE_TG_CHAT_ID="987654321"
```

If you already have a `~/.claude/settings.json` with other hooks, merge ours into the existing `hooks.PreToolUse` array rather than overwriting — see `docs/troubleshooting.md` for a jq one-liner.

## 4. Tuning

All config is optional beyond `CLAUDE_TG_TOKEN` and `CLAUDE_TG_CHAT_ID`. See the README Configuration table for the full list. Common tweaks:

```bash
# Trim the button set to just [✅] [❌] — no Terminal defer, no feedback prompt
export CLAUDE_TG_ALLOW_TERMINAL=false
export CLAUDE_TG_ALLOW_FEEDBACK=false

# Longer patience (pair with matching settings.json `timeout`)
export CLAUDE_TG_TIMEOUT=900
export CLAUDE_TG_FEEDBACK_WAIT=180

# Strict: deny on any Telegram error
export CLAUDE_TG_FAIL_OPEN=false
```

If you'd rather set these in `~/.claude/settings.json` directly (avoids shell rc issues), add an `env` block at the top level:

```json
{
  "env": {
    "CLAUDE_TG_TOKEN": "…",
    "CLAUDE_TG_CHAT_ID": "…",
    "CLAUDE_TG_TIMEOUT": "600"
  },
  "hooks": { ... }
}
```

Claude Code injects these into every hook process regardless of how it was launched.

## 5. Verify

1. Open a new terminal (so env vars load) and run `claude`.
2. Ask it to do something simple: "run `ls` for me".
3. A Telegram message with three buttons arrives within a second.
4. Tap **✅ Approve** — the message updates, Claude proceeds.
5. Ask for another action, tap **💻 Terminal** — Claude Code's normal terminal prompt appears.
6. Ask for a third action, tap **❌ Deny** — a follow-up message asks for a reason. Reply "use `ls -la` instead" and watch Claude retry with that instruction.

### Dry-run the hook without Claude Code

```bash
echo '{"tool_name":"Bash","tool_input":{"command":"ls"},"cwd":"/tmp","session_id":"test"}' \
  | python3 ~/.claude/hooks/telegram_approval.py
```

This sends one approval request and blocks on your response. Exit 0 = approve (or terminal), exit 2 = deny.

### Dry-run plan review

```bash
echo '{"tool_name":"ExitPlanMode","tool_input":{"plan":"# Demo plan\n\nStep 1: do X\nStep 2: do Y"}}' \
  | python3 ~/.claude/hooks/telegram_approval.py
```

### Dry-run AskUserQuestion

```bash
echo '{"tool_name":"AskUserQuestion","tool_input":{"questions":[{"question":"Pick one","header":"Pick","multiSelect":false,"options":[{"label":"A","description":""},{"label":"B","description":""}]}]}}' \
  | python3 ~/.claude/hooks/telegram_approval.py
```

Exit 2 with a `"permissionDecisionReason"` containing the chosen label = success.
