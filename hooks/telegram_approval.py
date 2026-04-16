#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Claude Code PreToolUse hook: route tool-use approvals to Telegram.

Reads Claude Code's PreToolUse JSON from stdin, sends a Telegram message
with inline buttons, blocks on long-polling until the user responds (or
TIMEOUT_SECONDS elapses), then exits with the right code and JSON so
Claude Code honors the decision.

Decisions:
  Approve   -> exit 0 + permissionDecision=allow      (run without terminal)
  Terminal  -> exit 0 + permissionDecision=ask        (defer to terminal prompt)
  Deny      -> exit 2 + permissionDecision=deny       (block, reason delivered)

Special tools:
  AskUserQuestion  -> Telegram button per option; on tap, the selected
                      label is delivered back to Claude as the answer.
  ExitPlanMode     -> Plan body shown; Approve/Terminal/Reject with the
                      same feedback flow as regular denies.

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

# Overall patience for a button press before auto-denying.
# Keep a few seconds under the Claude Code hook `timeout` so we decide
# for ourselves instead of being killed mid-poll.
TIMEOUT_SECONDS = int(os.environ.get("CLAUDE_TG_TIMEOUT", "300"))

# How long to wait for a free-form denial reason after the user taps ❌.
# Set to 0 to disable the feedback prompt entirely.
FEEDBACK_WAIT_SECONDS = int(os.environ.get("CLAUDE_TG_FEEDBACK_WAIT", "60"))

# Show the "💻 Terminal" button that defers to Claude Code's normal
# in-terminal permission prompt. Great for "I'll decide at the keyboard."
ALLOW_TERMINAL = os.environ.get("CLAUDE_TG_ALLOW_TERMINAL", "true").lower() == "true"

# Ask for a deny reason after tapping ❌. Disable for plain yes/no UX.
ALLOW_FEEDBACK = os.environ.get("CLAUDE_TG_ALLOW_FEEDBACK", "true").lower() == "true"

# If True, any error reaching Telegram (bad token, network down, …) will
# exit 0 so Claude isn't stuck. Set to "false" for a strict fail-closed
# posture that denies on any hook error.
FAIL_OPEN = os.environ.get("CLAUDE_TG_FAIL_OPEN", "true").lower() == "true"

# Per-iteration long-poll timeout (seconds). Telegram holds the HTTP
# request open this long waiting for updates. 25s sits comfortably below
# most proxy/firewall idle timeouts.
POLL_TIMEOUT = 25

# Tools in this list are auto-approved without asking. Add e.g.
# "Read", "Glob", "Grep" to skip prompts for read-only operations.
EXCLUDED_TOOLS = []

# Tools that Claude Code auto-approves when `permission_mode` is
# "acceptEdits". We mirror that so Telegram doesn't buzz for things
# the user has already blanket-approved for the session.
ACCEPT_EDITS_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")

# Honor Claude Code's per-session permission_mode. When True (default),
# we silently approve tool calls that Claude would already auto-approve:
#   - permission_mode = "bypassPermissions" -> approve everything
#   - permission_mode = "acceptEdits"       -> approve file-edit tools
# Set to "false" for strict mode: always prompt on Telegram regardless.
RESPECT_MODE = os.environ.get("CLAUDE_TG_RESPECT_MODE", "true").lower() == "true"

# Telegram caps message bodies at 4096 chars; we truncate previews to
# stay well under the limit. Plan review gets a larger budget.
MAX_FIELD_LEN = 400
MAX_PLAN_LEN = 3500

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


# ---------- text helpers ----------

def _truncate(s, n=MAX_FIELD_LEN):
    if s is None:
        return ""
    s = str(s)
    if len(s) <= n:
        return s
    return s[:n] + "... [{} more chars]".format(len(s) - n)


def _esc(s):
    # HTML parse_mode — escape the three special chars Telegram requires.
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _code(s):
    return "<code>{}</code>".format(_esc(s))


# ---------- tool formatters ----------

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
    "Task": _format_agent,
}


def format_message(tool_name, tool_input, cwd, session_id):
    fmt = FORMATTERS.get(tool_name)
    body = fmt(tool_input) if fmt else _format_generic(tool_name, tool_input)
    header = "\U0001F916 <b>Claude Code needs approval</b>"
    footer = "\n<b>cwd:</b> {}\n<b>session:</b> {}".format(
        _code(cwd or "?"), _code((session_id or "?")[:8])
    )
    return "{}\n\n{}\n{}".format(header, body, footer)


def format_ask_user(question, extra_questions_count):
    lines = [
        "\u2753 <b>Claude is asking a question</b>",
        "",
        "<b>{}</b>".format(_esc(question.get("header", "Question"))),
        _esc(_truncate(question.get("question", ""), 500)),
    ]
    if extra_questions_count > 0:
        lines.append("")
        lines.append("<i>+ {} more question(s) — first shown</i>".format(extra_questions_count))
    options = question.get("options", []) or []
    if options:
        lines.append("")
        for i, opt in enumerate(options):
            lines.append("<b>{}.</b> {} — {}".format(
                i + 1,
                _esc(opt.get("label", "")),
                _esc(_truncate(opt.get("description", ""), 150)),
            ))
    return "\n".join(lines)


def format_plan(plan_text):
    return (
        "\U0001F4CB <b>Plan review</b>\n\n"
        "<pre>{}</pre>"
    ).format(_esc(_truncate(plan_text, MAX_PLAN_LEN)))


# ---------- exit helpers (Claude Code hook contract) ----------

def _emit_allow():
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": "Approved via Telegram",
        }
    }))
    sys.stdout.flush()


def approve():
    """Allow the tool call (skip further permission prompts)."""
    _emit_allow()
    sys.exit(0)


def ask_terminal():
    """Defer to Claude Code's default permission flow (terminal prompt)."""
    sys.exit(0)


def deny(reason):
    """Block the tool call. Emit hookSpecificOutput JSON, exit 2.

    The JSON tells Claude *why* so the model can explain to the user or
    adjust its plan. Exit code 2 is the block signal.
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


def answer_as_denial(text):
    """Abuse deny+reason as a carrier for AskUserQuestion answers.

    Claude reads the denial reason and treats it as the user's response
    to the question, then proceeds without calling AskUserQuestion again.
    """
    reason = (
        "The user answered via Telegram: \"{}\". "
        "Treat this as the answer and continue — do not call "
        "AskUserQuestion again for this question."
    ).format(text)
    deny(reason)


def fail_open_or_deny(err):
    if FAIL_OPEN:
        sys.stderr.write("[telegram_approval] {} - failing open\n".format(err))
        sys.exit(0)
    deny("Telegram hook error: {}".format(err))


# ---------- Telegram primitives ----------

def _send_message(text, inline_keyboard=None, reply_to_message_id=None):
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if inline_keyboard is not None:
        payload["reply_markup"] = {"inline_keyboard": inline_keyboard}
    if reply_to_message_id is not None:
        payload["reply_parameters"] = {
            "message_id": reply_to_message_id,
            "allow_sending_without_reply": True,
        }
    resp = _http_post("sendMessage", payload)
    if not resp.get("ok"):
        raise RuntimeError("sendMessage failed: {}".format(resp))
    return resp["result"]["message_id"], str(resp["result"]["chat"]["id"])


def _edit_message(chat_id, message_id, text):
    try:
        _http_post("editMessageText", {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })
    except Exception:
        pass  # best-effort


def _answer_cb(cq_id, text=None):
    try:
        payload = {"callback_query_id": cq_id}
        if text:
            payload["text"] = text
        _http_post("answerCallbackQuery", payload)
    except Exception:
        pass


def _decision_buttons(callback_id, kind="generic"):
    """Build the inline keyboard for Approve/Terminal/Deny-style prompts.

    kind="plan" uses "Reject" instead of "Deny" since rejecting a plan
    and denying a shell command feel different to the user.
    """
    deny_label = "\u274C Reject" if kind == "plan" else "\u274C Deny"
    approve_label = "\u2705 Approve plan" if kind == "plan" else "\u2705 Approve"
    row = [{"text": approve_label, "callback_data": "approve:{}".format(callback_id)}]
    if ALLOW_TERMINAL:
        row.append({"text": "\U0001F4BB Terminal", "callback_data": "terminal:{}".format(callback_id)})
    row.append({"text": deny_label, "callback_data": "deny:{}".format(callback_id)})
    return [row]


def _ask_buttons(callback_id, options):
    """One button per option; fall back to terminal for multi-select
    or overflow (>10 options)."""
    buttons = []
    # Two per row for readability, capped at 10 options total.
    capped = options[:10]
    row = []
    for i, opt in enumerate(capped):
        label = _truncate(opt.get("label", "Option {}".format(i + 1)), 40)
        row.append({"text": label, "callback_data": "ans:{}:{}".format(callback_id, i)})
        if len(row) == 2:
            buttons.append(row); row = []
    if row:
        buttons.append(row)
    if ALLOW_TERMINAL:
        buttons.append([{"text": "\U0001F4BB Ask in terminal", "callback_data": "terminal:{}".format(callback_id)}])
    return buttons


# ---------- polling ----------

class Poller:
    """Shared long-poll loop with a persistent offset.

    Telegram's `offset = last_update_id + 1` is an ack: every update
    below `offset` is marked consumed and never redelivered. We keep the
    offset sticky across multiple calls (e.g. button press followed by
    feedback reply) so updates never get lost or double-processed.
    """

    def __init__(self):
        self.offset = 0

    def poll(self, deadline, allowed):
        remaining = max(1, int(deadline - time.time()))
        poll = min(POLL_TIMEOUT, remaining)
        try:
            return _http_get("getUpdates", {
                "offset": self.offset,
                "timeout": poll,
                "allowed_updates": json.dumps(allowed),
            }, timeout=poll + 10)
        except (urllib.error.URLError, TimeoutError, OSError):
            return None

    def consume(self, updates):
        for u in updates:
            self.offset = max(self.offset, u["update_id"] + 1)


def wait_for_callback(poller, callback_id, deadline):
    """Wait for a callback_query whose data matches our callback_id.

    Returns a (action, extra, cq_id) tuple, or None on timeout.
    `extra` is whatever follows after the second colon in callback_data
    (used e.g. for ans:<id>:<option_index>).
    """
    while time.time() < deadline:
        resp = poller.poll(deadline, ["callback_query"])
        if resp is None:
            time.sleep(1); continue
        if not resp.get("ok"):
            time.sleep(1); continue
        updates = resp.get("result", [])
        poller.consume(updates)
        for u in updates:
            cq = u.get("callback_query")
            if not cq:
                continue
            data = cq.get("data", "") or ""
            parts = data.split(":", 2)
            if len(parts) < 2:
                continue
            action, cbid = parts[0], parts[1]
            extra = parts[2] if len(parts) > 2 else ""
            if cbid != callback_id:
                # Someone else's session — clear their spinner and skip.
                _answer_cb(cq["id"])
                continue
            return action, extra, cq["id"]
    return None


def wait_for_feedback_or_skip(poller, chat_id, prompt_msg_id, callback_id, deadline):
    """Wait for either:
      - a message replying to `prompt_msg_id` (returns that text), or
      - a `skip:<callback_id>` callback_query (returns None),
      - deadline expiry (returns None).
    """
    while time.time() < deadline:
        resp = poller.poll(deadline, ["message", "callback_query"])
        if resp is None:
            time.sleep(1); continue
        if not resp.get("ok"):
            time.sleep(1); continue
        updates = resp.get("result", [])
        poller.consume(updates)
        for u in updates:
            cq = u.get("callback_query")
            if cq:
                data = cq.get("data", "") or ""
                parts = data.split(":", 2)
                if len(parts) >= 2 and parts[1] == callback_id and parts[0] == "skip":
                    _answer_cb(cq["id"], "Skipped")
                    return None, cq["id"]
                # foreign callback — just acknowledge
                _answer_cb(cq["id"])
                continue
            msg = u.get("message")
            if not msg:
                continue
            if str(msg.get("chat", {}).get("id")) != str(chat_id):
                continue
            reply_to = msg.get("reply_to_message") or {}
            if reply_to.get("message_id") != prompt_msg_id:
                continue
            return msg.get("text", "") or "", None
    return None, None


# ---------- handler flows ----------

def handle_generic(tool_name, tool_input, cwd, session_id, callback_id):
    text = format_message(tool_name, tool_input, cwd, session_id)
    msg_id, chat_id = _send_message(text, inline_keyboard=_decision_buttons(callback_id))
    return _run_decision_flow(text, msg_id, chat_id, callback_id, kind="generic")


def handle_ask_user(tool_input, callback_id):
    questions = tool_input.get("questions") or []
    if not questions:
        # Nothing to route — let Claude handle it normally.
        ask_terminal()

    q0 = questions[0]
    if q0.get("multiSelect"):
        # Multi-select UX on inline keyboards is messy; just defer.
        ask_terminal()

    text = format_ask_user(q0, max(0, len(questions) - 1))
    options = q0.get("options") or []
    if not options or len(options) > 10:
        ask_terminal()

    msg_id, chat_id = _send_message(text, inline_keyboard=_ask_buttons(callback_id, options))

    poller = Poller()
    result = wait_for_callback(poller, callback_id, time.time() + TIMEOUT_SECONDS)
    if result is None:
        _edit_message(chat_id, msg_id, "{}\n\n<b>\u23F0 Timed out</b>".format(text))
        deny("Telegram question timed out after {}s".format(TIMEOUT_SECONDS))

    action, extra, cq_id = result
    if action == "terminal":
        _answer_cb(cq_id, "Deferred to terminal")
        _edit_message(chat_id, msg_id, "{}\n\n<b>\U0001F4BB Deferred to terminal</b>".format(text))
        ask_terminal()
    elif action == "ans":
        try:
            idx = int(extra)
            chosen = options[idx]
        except (ValueError, IndexError):
            _answer_cb(cq_id, "Invalid option")
            deny("Telegram returned invalid option index: {}".format(extra))
            return
        label = chosen.get("label", "option {}".format(idx + 1))
        _answer_cb(cq_id, "Answered: {}".format(label))
        _edit_message(
            chat_id, msg_id,
            "{}\n\n<b>\u2705 Answered: {}</b>".format(text, _esc(label)),
        )
        answer_as_denial(label)
    else:
        # "deny" or anything else — treat as denial of the question.
        _answer_cb(cq_id, "Denied")
        _edit_message(chat_id, msg_id, "{}\n\n<b>\u274C Denied</b>".format(text))
        deny("User declined to answer the question via Telegram")


def handle_plan(tool_input, callback_id):
    plan_text = tool_input.get("plan", "") or ""
    text = format_plan(plan_text)
    msg_id, chat_id = _send_message(text, inline_keyboard=_decision_buttons(callback_id, kind="plan"))
    _run_decision_flow(text, msg_id, chat_id, callback_id, kind="plan")


def _run_decision_flow(text, msg_id, chat_id, callback_id, kind):
    poller = Poller()
    deadline = time.time() + TIMEOUT_SECONDS
    result = wait_for_callback(poller, callback_id, deadline)

    if result is None:
        _edit_message(chat_id, msg_id, "{}\n\n<b>\u23F0 Timed out - denied</b>".format(text))
        deny("Approval timed out after {}s".format(TIMEOUT_SECONDS))

    action, _extra, cq_id = result

    if action == "approve":
        _answer_cb(cq_id, "\u2705 Approved")
        label = "\u2705 Approved" + (" (plan)" if kind == "plan" else "")
        _edit_message(chat_id, msg_id, "{}\n\n<b>{}</b>".format(text, label))
        approve()

    if action == "terminal":
        _answer_cb(cq_id, "Deferred to terminal")
        _edit_message(chat_id, msg_id, "{}\n\n<b>\U0001F4BB Deferred to terminal</b>".format(text))
        ask_terminal()

    # deny path — optionally collect a free-form reason
    _answer_cb(cq_id, "\u274C Denied")
    default_reason = (
        "User rejected the plan via Telegram" if kind == "plan"
        else "User denied the tool call via Telegram"
    )

    if not ALLOW_FEEDBACK or FEEDBACK_WAIT_SECONDS <= 0:
        _edit_message(chat_id, msg_id, "{}\n\n<b>\u274C Denied</b>".format(text))
        deny(default_reason)

    prompt = (
        "\U0001F5C3 Reply with a reason (what Claude should do instead), "
        "or tap Skip to deny without feedback."
    )
    skip_kb = [[{"text": "Skip (no reason)", "callback_data": "skip:{}".format(callback_id)}]]
    try:
        prompt_msg_id, _ = _send_message(
            prompt, inline_keyboard=skip_kb, reply_to_message_id=msg_id,
        )
    except (urllib.error.URLError, RuntimeError, TimeoutError, OSError):
        _edit_message(chat_id, msg_id, "{}\n\n<b>\u274C Denied</b>".format(text))
        deny(default_reason)
        return  # unreachable, kept for clarity

    feedback, _skip_cq = wait_for_feedback_or_skip(
        poller, chat_id, prompt_msg_id, callback_id,
        time.time() + FEEDBACK_WAIT_SECONDS,
    )

    if feedback:
        _edit_message(
            chat_id, msg_id,
            "{}\n\n<b>\u274C Denied with feedback:</b>\n<i>{}</i>".format(
                text, _esc(_truncate(feedback, 600)),
            ),
        )
        reason = "{}. User said: \"{}\"".format(default_reason, feedback.strip())
        deny(reason)
    else:
        _edit_message(chat_id, msg_id, "{}\n\n<b>\u274C Denied</b>".format(text))
        deny(default_reason)


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
    # Claude Code reports the session's permission mode in stdin.
    # Newer builds use snake_case; older ones used camelCase — accept both.
    permission_mode = (
        payload.get("permission_mode")
        or payload.get("permissionMode")
        or ""
    )

    if tool_name in EXCLUDED_TOOLS:
        approve()

    # If Claude Code would already auto-approve this tool, don't waste a
    # Telegram round-trip asking about it.
    if RESPECT_MODE:
        if permission_mode == "bypassPermissions":
            approve()
        if permission_mode == "acceptEdits" and tool_name in ACCEPT_EDITS_TOOLS:
            approve()

    if not TELEGRAM_TOKEN or not CHAT_ID:
        fail_open_or_deny("CLAUDE_TG_TOKEN or CLAUDE_TG_CHAT_ID not set")
        return

    # Per-invocation id — embedded in callback_data so concurrent
    # Claude Code sessions using the same bot don't steal each other's
    # button presses.
    callback_id = uuid.uuid4().hex[:12]

    try:
        if tool_name == "AskUserQuestion":
            handle_ask_user(tool_input, callback_id)
        elif tool_name == "ExitPlanMode":
            handle_plan(tool_input, callback_id)
        else:
            handle_generic(tool_name, tool_input, cwd, session_id, callback_id)
    except (urllib.error.URLError, RuntimeError, TimeoutError, OSError) as e:
        fail_open_or_deny("Telegram request failed: {}".format(e))


if __name__ == "__main__":
    main()
