#!/usr/bin/env bash
# Installer for claude-telegram-approval.
#
# - Copies the hook into ~/.claude/hooks/
# - Merges the PreToolUse entry into ~/.claude/settings.json
# - Prompts for Telegram token + chat ID and exports them in your shell rc
# - Sends a test Telegram message to confirm the bot is working
#
# Safe to run twice: every step checks before writing.

set -euo pipefail

# ---------- cosmetics ----------
if [ -t 1 ]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; BLU=$'\033[34m'; RST=$'\033[0m'
else
  BOLD=""; DIM=""; RED=""; GRN=""; YLW=""; BLU=""; RST=""
fi

info()  { printf "${BLU}==>${RST} %s\n" "$*"; }
ok()    { printf "${GRN}[ok]${RST} %s\n" "$*"; }
warn()  { printf "${YLW}[warn]${RST} %s\n" "$*"; }
fail()  { printf "${RED}[fail]${RST} %s\n" "$*" >&2; exit 1; }

# ---------- preflight ----------
OS="$(uname -s)"
case "$OS" in
  Darwin|Linux) ;;
  *) fail "Unsupported OS: $OS. Windows is not supported (Claude Code runs on macOS/Linux)." ;;
esac

command -v python3 >/dev/null 2>&1 || fail "python3 not found on PATH. Install Python 3 first."

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK_SRC="$REPO_ROOT/hooks/telegram_approval.py"
SETTINGS_SRC="$REPO_ROOT/config/settings.json"
[ -f "$HOOK_SRC" ]     || fail "Missing $HOOK_SRC — run this script from inside the repo clone."
[ -f "$SETTINGS_SRC" ] || fail "Missing $SETTINGS_SRC — run this script from inside the repo clone."

CLAUDE_DIR="$HOME/.claude"
HOOK_DST_DIR="$CLAUDE_DIR/hooks"
HOOK_DST="$HOOK_DST_DIR/telegram_approval.py"
SETTINGS_DST="$CLAUDE_DIR/settings.json"

printf "\n${BOLD}Claude Telegram Approval — installer${RST}\n"
printf "${DIM}Repo:${RST} %s\n" "$REPO_ROOT"
printf "${DIM}Target:${RST} %s\n\n" "$CLAUDE_DIR"

# ---------- 1. install hook script ----------
info "Installing hook script..."
mkdir -p "$HOOK_DST_DIR"
cp "$HOOK_SRC" "$HOOK_DST"
chmod +x "$HOOK_DST"
ok "Wrote $HOOK_DST"

# ---------- 2. merge settings.json ----------
info "Updating $SETTINGS_DST..."
if [ ! -f "$SETTINGS_DST" ]; then
  cp "$SETTINGS_SRC" "$SETTINGS_DST"
  ok "Created $SETTINGS_DST from template"
else
  MERGE_TMP="$(mktemp)"
  if command -v jq >/dev/null 2>&1; then
    # jq path — merge our PreToolUse entry in, skip if an identical
    # command string already exists.
    jq --slurpfile new "$SETTINGS_SRC" '
      .hooks = (.hooks // {}) |
      .hooks.PreToolUse = (.hooks.PreToolUse // []) |
      . as $root |
      ($new[0].hooks.PreToolUse[0]) as $entry |
      if any($root.hooks.PreToolUse[]?; .hooks[]?.command == $entry.hooks[0].command)
        then .
        else .hooks.PreToolUse += [$entry]
      end
    ' "$SETTINGS_DST" > "$MERGE_TMP"
  else
    # Python fallback — same semantics, no jq required.
    python3 - "$SETTINGS_DST" "$SETTINGS_SRC" "$MERGE_TMP" <<'PYEOF'
import json, sys
dst_path, src_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
with open(dst_path) as f:
    dst = json.load(f)
with open(src_path) as f:
    src = json.load(f)
entry = src["hooks"]["PreToolUse"][0]
target_cmd = entry["hooks"][0]["command"]
dst.setdefault("hooks", {}).setdefault("PreToolUse", [])
existing = dst["hooks"]["PreToolUse"]
already = any(
    any(h.get("command") == target_cmd for h in e.get("hooks", []))
    for e in existing
)
if not already:
    existing.append(entry)
with open(out_path, "w") as f:
    json.dump(dst, f, indent=2)
    f.write("\n")
PYEOF
  fi
  mv "$MERGE_TMP" "$SETTINGS_DST"
  ok "Merged PreToolUse hook into $SETTINGS_DST"
fi

# ---------- 3. prompt for credentials ----------
info "Telegram credentials"
current_token="${CLAUDE_TG_TOKEN:-}"
current_chat="${CLAUDE_TG_CHAT_ID:-}"

read -rp "Bot token (from @BotFather)${current_token:+ [keep existing]}: " TOKEN_IN || true
TOKEN="${TOKEN_IN:-$current_token}"
[ -n "$TOKEN" ] || fail "Bot token is required."
if ! printf '%s' "$TOKEN" | grep -Eq '^[0-9]+:[A-Za-z0-9_-]+$'; then
  warn "Token doesn't match the usual <digits>:<letters> shape — double-check it."
fi

read -rp "Chat ID (numeric, from getUpdates)${current_chat:+ [keep existing]}: " CHAT_IN || true
CHAT="${CHAT_IN:-$current_chat}"
[ -n "$CHAT" ] || fail "Chat ID is required."

# ---------- 4. persist env vars in shell rc ----------
info "Configuring shell..."
case "${SHELL:-}" in
  *zsh*)  RC="$HOME/.zshrc" ;;
  *bash*) if [ "$OS" = "Darwin" ]; then RC="$HOME/.bash_profile"; else RC="$HOME/.bashrc"; fi ;;
  *)      RC="$HOME/.profile" ;;
esac
touch "$RC"

upsert_export() {
  local name="$1" value="$2"
  # Remove any prior line setting this var, then append the new value.
  if grep -qE "^export ${name}=" "$RC" 2>/dev/null; then
    # Write filtered contents to a temp file (portable; macOS sed differs).
    local tmp; tmp="$(mktemp)"
    grep -vE "^export ${name}=" "$RC" > "$tmp" || true
    mv "$tmp" "$RC"
  fi
  printf 'export %s="%s"\n' "$name" "$value" >> "$RC"
}

upsert_export CLAUDE_TG_TOKEN "$TOKEN"
upsert_export CLAUDE_TG_CHAT_ID "$CHAT"
ok "Wrote CLAUDE_TG_TOKEN and CLAUDE_TG_CHAT_ID to $RC"

# Export for the test call we're about to make.
export CLAUDE_TG_TOKEN="$TOKEN"
export CLAUDE_TG_CHAT_ID="$CHAT"

# ---------- 5. test message ----------
info "Sending test message..."
TEST_BODY=$(python3 -c 'import json,sys; print(json.dumps({"chat_id": sys.argv[1], "text": "Claude Code Telegram approval installed \u2705"}))' "$CHAT")
TEST_RESP="$(curl -sS -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
  -H 'Content-Type: application/json' \
  --data "$TEST_BODY" || true)"

if printf '%s' "$TEST_RESP" | python3 -c 'import json,sys; sys.exit(0 if json.loads(sys.stdin.read()).get("ok") else 1)' 2>/dev/null; then
  TEST_OK=1
  ok "Telegram accepted the test message — check your chat."
else
  TEST_OK=0
  warn "Test message failed. Response was:"
  printf '  %s\n' "$TEST_RESP"
fi

# ---------- 6. summary ----------
printf "\n${BOLD}Summary${RST}\n"
printf "  hook         %s\n" "$HOOK_DST"
printf "  settings     %s\n" "$SETTINGS_DST"
printf "  shell rc     %s\n" "$RC"
printf "  token        %s\n" "${TOKEN:0:4}...${TOKEN: -4}"
printf "  chat id      %s\n" "$CHAT"
if [ "$TEST_OK" = "1" ]; then
  printf "  test msg     ${GRN}ok${RST}\n"
else
  printf "  test msg     ${RED}failed${RST}\n"
fi

printf "\nNext steps:\n"
printf "  1. ${BOLD}Open a new terminal${RST} (or \`source %s\`) so the env vars take effect.\n" "$RC"
printf "  2. Start Claude Code and try any tool call — the Approve/Deny buttons should pop up in Telegram.\n"
printf "  3. Trouble? See docs/troubleshooting.md\n"

if [ "$TEST_OK" != "1" ]; then
  exit 1
fi
