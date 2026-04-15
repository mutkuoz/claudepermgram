# Setup guide

A long-form companion to the `README.md` quick-install — use this if `install.sh` doesn't work for your environment, or if you'd rather see each step.

## 1. Create a Telegram bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather).
2. Send `/newbot`. Pick a display name, then a username ending in `bot`.
3. BotFather replies with a token like `123456789:ABCdefGhIJKlmnOPQrstUvwxYZ0123456`. **Save it.**
4. (Optional but recommended) Send `/setprivacy` → `Disable` if you plan to use the bot in a group.

## 2. Find your chat ID

Telegram doesn't route updates to a bot until you've interacted with it from that chat.

1. In Telegram, open a chat with your new bot and send `/start` (or any message).
2. In a browser, load:
   ```
   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   ```
3. Look for the `"chat":{"id":…}` field. That number is your chat ID.
   - Private chats: positive integer (your user ID).
   - Groups / channels: negative integer (starts with `-100...` for supergroups).

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
# Contents to merge into ~/.claude/settings.json:
#   see config/settings.json for the exact shape.

# 3. Export credentials (also add these to your ~/.zshrc or ~/.bashrc)
export CLAUDE_TG_TOKEN="123456789:ABCdef..."
export CLAUDE_TG_CHAT_ID="987654321"
```

If you already have a `~/.claude/settings.json` with other hooks, merge ours into the existing `hooks.PreToolUse` array rather than overwriting — see `docs/troubleshooting.md` for a jq one-liner.

## 4. Verify

1. Open a new terminal (so the env vars load) and run:
   ```bash
   claude
   ```
2. Ask it to do something simple: "run `ls` for me".
3. A Telegram message with ✅ / ❌ buttons should arrive within a second.
4. Tap ✅ — the message updates to "✅ Approved" and Claude proceeds.
5. Try another action and tap ❌ — the message updates to "❌ Denied" and Claude reports the denial to you.

### Dry-run the hook without Claude Code

```bash
echo '{"tool_name":"Bash","tool_input":{"command":"ls"},"cwd":"/tmp","session_id":"test"}' \
  | python3 ~/.claude/hooks/telegram_approval.py
```

This sends one approval request and blocks on your response. Exit 0 = you tapped ✅, exit 2 = you tapped ❌ (or 300s elapsed).

## 5. Recommended tweaks

- **If you find the approval prompts too noisy for reads**, add `"Read"`, `"Glob"`, `"Grep"` to `EXCLUDED_TOOLS` in `~/.claude/hooks/telegram_approval.py` — or narrow the `matcher` regex in `~/.claude/settings.json` (see README).
- **If you run Claude Code over SSH on a server**, keep the defaults — this is exactly the use case the hook was built for. You can close your SSH session; Claude blocks until you tap a button from your phone.
- **Raise the timeout** (`CLAUDE_TG_TIMEOUT`) if you're often away from your phone for more than 5 minutes. Don't forget to raise the matching `timeout` in `settings.json`.
