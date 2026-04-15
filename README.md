# claude-telegram-approval

> Approve or deny every Claude Code tool call from Telegram instead of the terminal.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.8+-blue.svg)
![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey.svg)

A `PreToolUse` hook that intercepts every Claude Code tool call (Bash, Write, Edit, Read, …), sends you a Telegram message with ✅ Approve / ❌ Deny buttons, and blocks until you tap one. Perfect for running Claude Code in the background or on a remote machine and approving actions from your phone.

## Demo

![Demo screenshot](docs/demo.png)

<sub>Replace `docs/demo.png` with a real screenshot after first install — see *Customization* below.</sub>

## Quick install

```bash
git clone https://github.com/YOUR_USER/claude-telegram-approval.git
cd claude-telegram-approval
./install.sh
```

Or, once you've set up a bot and have a chat ID:

```bash
curl -fsSL https://raw.githubusercontent.com/YOUR_USER/claude-telegram-approval/main/install.sh -o /tmp/claude-tg-install.sh
bash /tmp/claude-tg-install.sh
```

The installer copies the hook into `~/.claude/hooks/`, merges the `PreToolUse` entry into `~/.claude/settings.json`, writes your credentials to your shell rc, and sends a test message.

## Prerequisites

- **Python 3.8+** (stdlib only — no pip installs)
- **Claude Code** installed and working (see [Anthropic docs](https://docs.claude.com/en/docs/claude-code))
- **A Telegram bot** — 60 seconds of setup, next section

## Telegram bot setup

1. Open Telegram, message [@BotFather](https://t.me/BotFather), send `/newbot`, and follow the prompts. Save the token it gives you — it looks like `123456789:ABCdef...`.
2. Open a chat with your new bot and send it any message (e.g. `/start`). Telegram won't route updates to you until you do.
3. In a browser, open:
   ```
   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   ```
   Find `"chat":{"id":…}` in the JSON — that number is your `CLAUDE_TG_CHAT_ID`. Positive for private chats, negative for groups.

Pass both values to `./install.sh` when it prompts, or export them manually:

```bash
export CLAUDE_TG_TOKEN="123456789:ABCdef..."
export CLAUDE_TG_CHAT_ID="987654321"
```

## Configuration

| Variable                | Description                                                   | Default | Required |
| ----------------------- | ------------------------------------------------------------- | ------- | -------- |
| `CLAUDE_TG_TOKEN`       | Bot token from @BotFather                                     | —       | yes      |
| `CLAUDE_TG_CHAT_ID`     | Chat ID where approval prompts are sent                       | —       | yes      |
| `CLAUDE_TG_TIMEOUT`     | Seconds to wait for a button press before auto-denying        | `300`   | no       |
| `CLAUDE_TG_FAIL_OPEN`   | `true` → approve on Telegram errors, `false` → deny           | `true`  | no       |

## How it works

```
  Claude Code                         Telegram                      You
      │                                   │                          │
      │──PreToolUse JSON (stdin)──┐       │                          │
      │                           ▼       │                          │
      │                    telegram_approval.py                      │
      │                           │                                  │
      │                           │──sendMessage──────────────────▶  │
      │                           │                                  │
      │      (hook blocks,        │◀──long poll getUpdates──┐        │
      │       up to 320s)         │                         │        │
      │                           │                         └────────┤
      │                           │                    user taps ✅/❌ │
      │                           │◀────callback_query─────│         │
      │                           │                        │         │
      │◀──exit 0  (approve)       │──answerCallbackQuery──▶│         │
      │   exit 2 + JSON (deny)    │──editMessageText──────▶│         │
      │                           │                                  │
```

See [`docs/how-it-works.md`](docs/how-it-works.md) for the full protocol with exact JSON payloads, and [`docs/setup.md`](docs/setup.md) for a manual install walkthrough.

## Customization

### Exclude low-risk tools from requiring approval

If you don't want buzzed every time Claude reads a file, either:

- **Edit the hook script** — open `~/.claude/hooks/telegram_approval.py` and set:
  ```python
  EXCLUDED_TOOLS = ["Read", "Glob", "Grep"]
  ```
- **Or narrow the matcher** in `~/.claude/settings.json`:
  ```json
  "matcher": "Bash|Write|Edit|MultiEdit|WebFetch|WebSearch|Agent"
  ```

Both approaches work; the matcher is faster because it skips invoking the hook entirely for excluded tools.

### Change the approval timeout

```bash
export CLAUDE_TG_TIMEOUT=600   # 10 minutes
```

Also bump the `timeout` field in `settings.json` to at least `CLAUDE_TG_TIMEOUT + 20`, otherwise Claude Code will kill the hook before it can time out itself.

### Fail open vs fail closed

By default, `CLAUDE_TG_FAIL_OPEN=true` — if Telegram is unreachable, the hook exits 0 so Claude isn't stuck. For a strict posture (deny on any hook error):

```bash
export CLAUDE_TG_FAIL_OPEN=false
```

### Record a new demo screenshot

After you've got it running, take a screenshot of the Telegram message and save it to `docs/demo.png`. Commit.

## Troubleshooting

Full guide: [`docs/troubleshooting.md`](docs/troubleshooting.md). Common causes:

- **Hook never fires** — run `claude /hooks` inside Claude Code to see whether the hook is registered; check `~/.claude/settings.json`.
- **Telegram 401/403** — token wrong, or you never sent `/start` to your bot from the target account.
- **Buttons appear but nothing happens** — likely a stale `getUpdates` offset; restart Claude Code (new hook process starts a fresh poll).

## Contributing

Bug reports and feature requests welcome — use the templates under [`.github/ISSUE_TEMPLATE/`](.github/ISSUE_TEMPLATE/). PRs that add new tool formatters or improve the install script are especially appreciated.

## License

MIT — see [LICENSE](LICENSE).
