#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Claude Code PreToolUse hook: route tool-use approvals to Telegram.

Reads Claude Code's PreToolUse JSON from stdin, sends a Telegram message
with inline Approve/Deny buttons, blocks on long-polling until the user
responds (or TIMEOUT_SECONDS elapses), then exits with the right code
and JSON so Claude Code honors the decision.

Exit-code protocol (per Claude Code hook spec):
  exit 0            allow the tool call to proceed
  exit 2 + JSON     block the tool call (permissionDecision=deny)
  other exit codes  treated as hook errors

Pure Python 3 stdlib — no pip installs required.
"""

import json
import os
import sys
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request

# =====================================================================
# CONFIG — all tunables live here, read from env vars with defaults
# =====================================================================

# Credentials (required; missing values trigger FAIL_OPEN behavior below)
TELEGRAM_TOKEN = os.environ.get("CLAUDE_TG_TOKEN", "")
CHAT_ID = os.environ.get("CLAUDE_TG_CHAT_ID", "")

# Wait up to this many seconds for a button press before auto-denying.
# Keep this a few seconds under the Claude Code hook `timeout` so we
# decide for ourselves instead of being killed mid-poll.
TIMEOUT_SECONDS = int(os.environ.get("CLAUDE_TG_TIMEOUT", "300"))

# If True, any error reaching Telegram (bad token, network down, etc.)
# results in exit 0 so Claude isn't stuck. Set to "false" for a strict
# fail-closed posture that denies on any error.
FAIL_OPEN = os.environ.get("CLAUDE_TG_FAIL_OPEN", "true").lower() == "true"

# Per-iteration long-poll timeout (seconds). Telegram keeps the HTTP
# request open this long waiting for updates. 25s sits comfortably
# below most proxy/firewall idle timeouts.
POLL_TIMEOUT = 25

# Tools in this list are auto-approved without asking. Add e.g.
# "Read", "Glob", "Grep" here to skip approval prompts for read-only
# operations. Leave empty to gate every tool call through Telegram.
EXCLUDED_TOOLS = []

# Telegram message bodies cap at 4096 chars; we truncate each field
# preview to stay comfortably under that limit.
MAX_FIELD_LEN = 400

# =====================================================================
# END CONFIG
# =====================================================================

API_BASE = "https://api.telegram.org/bot{}".format(TELEGRAM_TOKEN)


# ---------- tiny HTTP helpers (stdlib only) ----------

def _http_post(method, payload, timeout=30.0):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "{}/{}".format(API_BASE, method),
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_get(method, params, timeout):
    url = "{}/{}?{}".format(API_BASE, method, urllib.parse.urlencode(params))
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------- text formatting ----------

def _truncate(s, n=MAX_FIELD_LEN):
    if s is None:
        return ""
    s = str(s)
    if len(s) <= n:
        return s
    return s[:n] + "... [{} more chars]".format(len(s) - n)


def _esc(s):
    # HTML parse_mode — escape only the three special chars Telegram requires.
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _code(s):
    return "<code>{}</code>".format(_esc(s))


def _format_bash(inp):
    lines = ["\U0001F5A5\uFE0F <b>Bash</b>"]
    lines.append("<b>Command:</b>\n<pre>{}</pre>".format(_esc(_truncate(inp.get("command", "")))))
    if inp.get("description"):
        lines.append("<b>Description:</b> {}".format(_esc(_truncate(inp["description"], 200))))
    if inp.get("run_in_background"):
        lines.append("<b>Background:</b> yes")
    return "\n".join(lines)


def _format_write(inp):
    content = inp.get("content", "") or ""
    return (
        "\U0001F4DD <b>Write</b>\n"
        "<b>File:</b> {}\n"
        "<b>Bytes:</b> {}\n"
        "<b>Preview:</b>\n<pre>{}</pre>"
    ).format(_code(inp.get("file_path", "")), len(content), _esc(_truncate(content, 300)))


def _format_edit(inp):
    return (
        "\u270F\uFE0F <b>Edit</b>\n"
        "<b>File:</b> {}\n"
        "<b>Replace all:</b> {}\n"
        "<b>Old:</b>\n<pre>{}</pre>\n"
        "<b>New:</b>\n<pre>{}</pre>"
    ).format(
        _code(inp.get("file_path", "")),
        bool(inp.get("replace_all", False)),
        _esc(_truncate(inp.get("old_string", ""), 200)),
        _esc(_truncate(inp.get("new_string", ""), 200)),
    )


def _format_multiedit(inp):
    edits = inp.get("edits") or []
    return (
        "\u270F\uFE0F <b>MultiEdit</b>\n"
        "<b>File:</b> {}\n"
        "<b>Number of edits:</b> {}"
    ).format(_code(inp.get("file_path", "")), len(edits))


def _format_read(inp):
    lines = ["\U0001F440 <b>Read</b>", "<b>File:</b> {}".format(_code(inp.get("file_path", "")))]
    if "offset" in inp:
        lines.append("<b>Offset:</b> {}".format(inp["offset"]))
    if "limit" in inp:
        lines.append("<b>Limit:</b> {}".format(inp["limit"]))
    return "\n".join(lines)


def _format_glob(inp):
    lines = ["\U0001F50D <b>Glob</b>", "<b>Pattern:</b> {}".format(_code(inp.get("pattern", "")))]
    if inp.get("path"):
        lines.append("<b>Path:</b> {}".format(_code(inp["path"])))
    return "\n".join(lines)


def _format_grep(inp):
    lines = [
        "\U0001F50E <b>Grep</b>",
        "<b>Pattern:</b> {}".format(_code(_truncate(inp.get("pattern", ""), 200))),
    ]
    if inp.get("path"):
        lines.append("<b>Path:</b> {}".format(_code(inp["path"])))
    if inp.get("output_mode"):
        lines.append("<b>Mode:</b> {}".format(_esc(inp["output_mode"])))
    if inp.get("glob"):
        lines.append("<b>Glob:</b> {}".format(_code(inp["glob"])))
    return "\n".join(lines)


def _format_webfetch(inp):
    return (
        "\U0001F310 <b>WebFetch</b>\n"
        "<b>URL:</b> {}\n"
        "<b>Prompt:</b> {}"
    ).format(_code(inp.get("url", "")), _esc(_truncate(inp.get("prompt", ""), 200)))


def _format_websearch(inp):
    return "\U0001F50E <b>WebSearch</b>\n<b>Query:</b> {}".format(_code(inp.get("query", "")))


def _format_agent(inp):
    lines = ["\U0001F916 <b>Agent</b>"]
    if inp.get("description"):
        lines.append("<b>Task:</b> {}".format(_esc(_truncate(inp["description"], 150))))
    if inp.get("subagent_type"):
        lines.append("<b>Type:</b> {}".format(_code(inp["subagent_type"])))
    return "\n".join(lines)


def _format_generic(tool_name, inp):
    body = _truncate(json.dumps(inp, indent=2, ensure_ascii=False), 500)
    return "\U0001F6E0\uFE0F <b>{}</b>\n<pre>{}</pre>".format(_esc(tool_name), _esc(body))


FORMATTERS = {
    "Bash": _format_bash,
    "Write": _format_write,
    "Edit": _format_edit,
    "MultiEdit": _format_multiedit,
    "Read": _format_read,
    "Glob": _format_glob,
    "Grep": _format_grep,
    "WebFetch": _format_webfetch,
    "WebSearch": _format_websearch,
    "Agent": _format_agent,
    "Task": _format_agent,  # some Claude Code versions expose Agent as "Task"
}


def format_message(tool_name, tool_input, cwd, session_id):
    fmt = FORMATTERS.get(tool_name)
    body = fmt(tool_input) if fmt else _format_generic(tool_name, tool_input)
    header = "\U0001F916 <b>Claude Code needs approval</b>"
    footer = "\n<b>cwd:</b> {}\n<b>session:</b> {}".format(
        _code(cwd or "?"), _code((session_id or "?")[:8])
    )
    return "{}\n\n{}\n{}".format(header, body, footer)


# ---------- exit helpers (Claude Code hook contract) ----------

def approve():
    """Allow the tool call. Exit 0 with no stdout."""
    sys.exit(0)


def deny(reason):
    """Block the tool call. Emit hookSpecificOutput JSON, exit 2.

    The JSON tells Claude *why* it was blocked so the model can explain
    to the user. Exit code 2 is the actual block signal.
    """
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
            "additionalContext": reason,
        }
    }
    sys.stdout.write(json.dumps(payload))
    sys.stdout.flush()
    sys.exit(2)


def fail_open_or_deny(err):
    """Route to approve or deny based on FAIL_OPEN config."""
    if FAIL_OPEN:
        sys.stderr.write("[telegram_approval] {} - failing open\n".format(err))
        approve()
    else:
        deny("Telegram hook error: {}".format(err))


# ---------- Telegram interactions ----------

def send_request_message(text, callback_id):
    """Send the approval request with inline Approve/Deny buttons."""
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "\u2705 Approve", "callback_data": "approve:{}".format(callback_id)},
                {"text": "\u274C Deny", "callback_data": "deny:{}".format(callback_id)},
            ]],
        },
    }
    resp = _http_post("sendMessage", payload)
    if not resp.get("ok"):
        raise RuntimeError("sendMessage failed: {}".format(resp))
    return resp["result"]["message_id"], str(resp["result"]["chat"]["id"])


def wait_for_callback(callback_id, deadline):
    """Long-poll getUpdates until our callback arrives or deadline passes.

    Returns "approve" / "deny" on a matching button press, or None on
    timeout. We filter by a per-invocation `callback_id` so concurrent
    Claude Code sessions sharing the same bot don't cross-fire.
    """
    # `offset = last_update_id + 1` is Telegram's acknowledgement model:
    # every update with id < offset is marked consumed server-side.
    # Starting at 0 lets us pick up any callback_queries already queued.
    offset = 0
    while time.time() < deadline:
        remaining = max(1, int(deadline - time.time()))
        poll = min(POLL_TIMEOUT, remaining)
        try:
            # Per-request socket timeout must exceed Telegram's long-poll
            # window or urlopen will abort before the server responds.
            resp = _http_get(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": poll,
                    "allowed_updates": json.dumps(["callback_query"]),
                },
                timeout=poll + 10,
            )
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(1)
            continue

        if not resp.get("ok"):
            time.sleep(1)
            continue

        for update in resp.get("result", []):
            offset = max(offset, update["update_id"] + 1)
            cq = update.get("callback_query")
            if not cq:
                continue
            data = cq.get("data", "") or ""
            if ":" not in data:
                continue
            action, cbid = data.split(":", 1)

            if cbid != callback_id:
                # A different session's button — acknowledge to clear
                # that user's spinner, but leave the decision to its
                # own hook process.
                try:
                    _http_post("answerCallbackQuery", {"callback_query_id": cq["id"]})
                except Exception:
                    pass
                continue

            try:
                _http_post(
                    "answerCallbackQuery",
                    {
                        "callback_query_id": cq["id"],
                        "text": "\u2705 Approved" if action == "approve" else "\u274C Denied",
                    },
                )
            except Exception:
                pass
            return action
    return None


def edit_outcome(message_id, chat_id, original_text, outcome_label):
    """Rewrite the original message to show the final decision."""
    try:
        _http_post(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": "{}\n\n<b>{}</b>".format(original_text, outcome_label),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
    except Exception:
        # Best-effort — don't let a failed edit change the decision.
        pass


# ---------- main ----------

def main():
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        fail_open_or_deny("bad stdin JSON: {}".format(e))
        return

    tool_name = payload.get("tool_name", "?")
    tool_input = payload.get("tool_input", {}) or {}
    cwd = payload.get("cwd", "")
    session_id = payload.get("session_id", "")

    if tool_name in EXCLUDED_TOOLS:
        approve()

    if not TELEGRAM_TOKEN or not CHAT_ID:
        fail_open_or_deny("CLAUDE_TG_TOKEN or CLAUDE_TG_CHAT_ID not set")
        return

    # Per-invocation id — embedded in callback_data so concurrent
    # Claude Code sessions using the same bot don't steal each other's
    # button presses.
    callback_id = uuid.uuid4().hex[:12]
    text = format_message(tool_name, tool_input, cwd, session_id)

    try:
        message_id, chat_id = send_request_message(text, callback_id)
    except (urllib.error.URLError, RuntimeError, TimeoutError, OSError) as e:
        fail_open_or_deny("sendMessage error: {}".format(e))
        return

    deadline = time.time() + TIMEOUT_SECONDS
    result = wait_for_callback(callback_id, deadline)

    if result is None:
        edit_outcome(message_id, chat_id, text, "\u23F0 Timed out - denied")
        deny("Approval timed out after {}s".format(TIMEOUT_SECONDS))
    elif result == "approve":
        edit_outcome(message_id, chat_id, text, "\u2705 Approved")
        approve()
    else:
        edit_outcome(message_id, chat_id, text, "\u274C Denied")
        deny("User denied the tool call via Telegram")


if __name__ == "__main__":
    main()
