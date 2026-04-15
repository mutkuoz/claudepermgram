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
  BOLD=$'\033[1m'; DIM=$'\033[2m'; RST=$'\033[0m'
  RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; BLU=$'\033[34m'; CYN=$'\033[36m'; MAG=$'\033[35m'
else
  BOLD=""; DIM=""; RST=""; RED=""; GRN=""; YLW=""; BLU=""; CYN=""; MAG=""
fi

TOTAL_STEPS=5
CUR_STEP=0

banner() {
  printf "\n"
  printf "${CYN}╔════════════════════════════════════════════════════════════╗${RST}\n"
  printf "${CYN}║${RST}  ${BOLD}claude-telegram-approval${RST}   ${DIM}tool approvals on your phone${RST}  ${CYN}║${RST}\n"
  printf "${CYN}╚════════════════════════════════════════════════════════════╝${RST}\n"
}

step()  { CUR_STEP=$((CUR_STEP + 1)); printf "\n${BOLD}${BLU}[${CUR_STEP}/${TOTAL_STEPS}]${RST} ${BOLD}%s${RST}\n" "$*"; }
ok()    { printf "   ${GRN}\xE2\x9C\x94${RST} %s\n" "$*"; }
info()  { printf "   ${DIM}%s${RST}\n" "$*"; }
warn()  { printf "   ${YLW}!${RST} %s\n" "$*"; }
fail()  { printf "\n${RED}\xE2\x9C\x96 %s${RST}\n" "$*" >&2; exit 1; }

# ---------- preflight ----------
OS="$(uname -s)"
case "$OS" in
  Darwin|Linux) ;;
  *) banner; fail "Unsupported OS: $OS. Windows is not supported (Claude Code runs on macOS/Linux)." ;;
esac

command -v python3 >/dev/null 2>&1 || { banner; fail "python3 not found on PATH. Install Python 3 first."; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK_SRC="$REPO_ROOT/hooks/telegram_approval.py"
SETTINGS_SRC="$REPO_ROOT/config/settings.json"
[ -f "$HOOK_SRC" ]     || { banner; fail "Missing $HOOK_SRC — run this script from inside the repo clone."; }
[ -f "$SETTINGS_SRC" ] || { banner; fail "Missing $SETTINGS_SRC — run this script from inside the repo clone."; }

CLAUDE_DIR="$HOME/.claude"
HOOK_DST_DIR="$CLAUDE_DIR/hooks"
HOOK_DST="$HOOK_DST_DIR/telegram_approval.py"
SETTINGS_DST="$CLAUDE_DIR/settings.json"

banner
printf "${DIM}Repo:${RST}   %s\n" "$REPO_ROOT"
printf "${DIM}Target:${RST} %s\n" "$CLAUDE_DIR"
printf "${DIM}OS:${RST}     %s\n" "$OS"

# ---------- 1. install hook script ----------
step "Installing hook script"
mkdir -p "$HOOK_DST_DIR"
cp "$HOOK_SRC" "$HOOK_DST"
chmod +x "$HOOK_DST"
ok "wrote $HOOK_DST"

# ---------- 2. merge settings.json ----------
step "Updating ${DIM}$SETTINGS_DST${RST}"
if [ ! -f "$SETTINGS_DST" ]; then
  cp "$SETTINGS_SRC" "$SETTINGS_DST"
  ok "created settings.json from template"
else
  MERGE_TMP="$(mktemp)"
  if command -v jq >/dev/null 2>&1; then
    # jq path: merge our PreToolUse entry, skip if identical command already exists.
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
    info "merged via jq"
  else
    python3 - "$SETTINGS_DST" "$SETTINGS_SRC" "$MERGE_TMP" <<'PYEOF'
import json, sys
dst_path, src_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
with open(dst_path) as f: dst = json.load(f)
with open(src_path) as f: src = json.load(f)
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
    json.dump(dst, f, indent=2); f.write("\n")
PYEOF
    info "merged via python (jq not found)"
  fi
  mv "$MERGE_TMP" "$SETTINGS_DST"
  ok "hook registered in settings.json"
fi

# ---------- 3. prompt for credentials ----------
step "Telegram credentials"
info "Don't have a bot yet? See README \xE2\x86\x92 'Telegram bot setup'"
current_token="${CLAUDE_TG_TOKEN:-}"
current_chat="${CLAUDE_TG_CHAT_ID:-}"

printf "   ${BOLD}Bot token${RST}%s: " "${current_token:+ ${DIM}[enter to keep existing]${RST}}"
read -r TOKEN_IN || true
TOKEN="${TOKEN_IN:-$current_token}"
[ -n "$TOKEN" ] || fail "Bot token is required."
if ! printf '%s' "$TOKEN" | grep -Eq '^[0-9]+:[A-Za-z0-9_-]+$'; then
  warn "token doesn't match the usual <digits>:<letters> shape — double-check it"
fi

printf "   ${BOLD}Chat ID${RST}%s:   " "${current_chat:+ ${DIM}[enter to keep existing]${RST}}"
read -r CHAT_IN || true
CHAT="${CHAT_IN:-$current_chat}"
[ -n "$CHAT" ] || fail "Chat ID is required."
ok "credentials collected"

# ---------- 4. persist env vars in shell rc ----------
step "Configuring shell"
case "${SHELL:-}" in
  *zsh*)  RC="$HOME/.zshrc" ;;
  *bash*) if [ "$OS" = "Darwin" ]; then RC="$HOME/.bash_profile"; else RC="$HOME/.bashrc"; fi ;;
  *)      RC="$HOME/.profile" ;;
esac
touch "$RC"

upsert_export() {
  local name="$1" value="$2"
  if grep -qE "^export ${name}=" "$RC" 2>/dev/null; then
    local tmp; tmp="$(mktemp)"
    grep -vE "^export ${name}=" "$RC" > "$tmp" || true
    mv "$tmp" "$RC"
  fi
  printf 'export %s="%s"\n' "$name" "$value" >> "$RC"
}

upsert_export CLAUDE_TG_TOKEN "$TOKEN"
upsert_export CLAUDE_TG_CHAT_ID "$CHAT"
ok "wrote CLAUDE_TG_TOKEN + CLAUDE_TG_CHAT_ID to $RC"

export CLAUDE_TG_TOKEN="$TOKEN"
export CLAUDE_TG_CHAT_ID="$CHAT"

# ---------- 5. test message ----------
step "Sending test message"
TEST_BODY=$(python3 -c 'import json,sys; print(json.dumps({"chat_id": sys.argv[1], "text": "\u2705 Claude Code Telegram approval is installed and working."}))' "$CHAT")
TEST_RESP="$(curl -sS -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
  -H 'Content-Type: application/json' \
  --data "$TEST_BODY" || true)"

if printf '%s' "$TEST_RESP" | python3 -c 'import json,sys; sys.exit(0 if json.loads(sys.stdin.read()).get("ok") else 1)' 2>/dev/null; then
  TEST_OK=1
  ok "Telegram accepted the test message — check your chat"
else
  TEST_OK=0
  warn "test message failed — response:"
  printf '     ${DIM}%s${RST}\n' "$TEST_RESP"
fi

# ---------- summary ----------
TOKEN_MASK="${TOKEN:0:6}\xE2\x80\xA6${TOKEN: -4}"
printf "\n${BOLD}Summary${RST}\n"
printf "  ${CYN}hook${RST}      %s\n" "$HOOK_DST"
printf "  ${CYN}settings${RST}  %s\n" "$SETTINGS_DST"
printf "  ${CYN}shell rc${RST}  %s\n" "$RC"
printf "  ${CYN}token${RST}     %b\n" "$TOKEN_MASK"
printf "  ${CYN}chat id${RST}   %s\n" "$CHAT"
if [ "$TEST_OK" = "1" ]; then
  printf "  ${CYN}test msg${RST}  ${GRN}ok${RST}\n"
else
  printf "  ${CYN}test msg${RST}  ${RED}failed${RST}\n"
fi

printf "\n${BOLD}Next steps${RST}\n"
printf "  ${MAG}1.${RST} ${BOLD}Open a fresh terminal${RST} (or ${DIM}source %s${RST}) so env vars load.\n" "$RC"
printf "  ${MAG}2.${RST} Start ${BOLD}claude${RST} and try any tool call \xE2\x86\x92 buttons will pop up in Telegram.\n"
printf "  ${MAG}3.${RST} Trouble? ${DIM}docs/troubleshooting.md${RST}\n\n"

if [ "$TEST_OK" != "1" ]; then
  exit 1
fi
