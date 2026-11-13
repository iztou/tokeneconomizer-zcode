"""ZCode: a session stored as rows of the sqlite database under `~/.zcode`.

The desktop app and its CLI keep every conversation in one database --
`~/.zcode/cli/db/db.sqlite` (the `~/.zcode/v2/db.sqlite` the older surface names stays empty on
current installs; both are probed, and `ZCODE_DB_PATH` overrides either). Verified on
2026-11-12 against app 3.17.2 (`ZCODE_APP_VERSION`; the database's own `session.version` column
reports the runtime's `0.17.1`); stdlib `sqlite3`, opened with a read-only URI so a busy writer
never breaks a fold. The tables this reads:

  * `session`     -- `id` (`sess_...`), `directory` (the repository), `title`, `version`,
                     `parent_id` (a sub-workflow session's parent), `time_created`/
                     `time_updated` in epoch milliseconds;
  * `message`     -- `data` JSON: `role` (`user`/`assistant`), `semantics.kind`
                     (`user_prompt`, `system_reminder`, `todo_reminder`,
                     `background_notification`, `compact_summary`, `fork_notice` -- only the
                     first is a person's prompt), `tokens`, `modelId`, `path.cwd`;
  * `part`        -- one message's content, `data.type` one of `text`, `reasoning`,
                     `step-start`, `step-finish`, `tool`. A `tool` part holds the whole call:
                     `callID`, `tool`, and `state` (`input`, `output`, `error` -- the text of a
                     failed call, whose output is null -- `status`
                     running/completed/error/cancelled, `time`);
  * `model_usage` -- one row per model call: `model_id`, `input_tokens`, `output_tokens`,
                     `reasoning_tokens`, `cache_read_input_tokens`,
                     `cache_creation_input_tokens`, `status`.

`zcode_session_to_claude` writes a session out as Claude transcript lines (assistant messages
with `tool_use` blocks, a following `user` line with their `tool_result`s, prompts as `user`
text), which the readers already handle. Tool names are Claude's on this host; `Agent` is read
as `Task` and `ApplyPatch` as `Edit`, per the host's aliases.
"""
import json
import os
import re
import sqlite3
import time

# A session id names files (`folded.json` keys, `sessions/<id>.json`), so a log's id is taken
# only when it is a plain token: ZCode's are `sess_` plus a UUID, sometimes with a workflow
# suffix (`sess_dwf-dwfrun-...-actor_1_1`).
SESSION_ID_RE = re.compile(r"sess_[A-Za-z0-9][A-Za-z0-9_-]{0,127}")

# Message `semantics.kind` values that are not a person's prompt. A message with no semantics
# at all (older installs) counts as a prompt when it carries plain text (see below).
NOT_PROMPTS = ("system_reminder", "todo_reminder", "background_notification", "compact_summary",
               "fork_notice")
SYSTEM_PREFIXES = ("<system-reminder", "<local-command", "[Request interrupted")

_TOOL_NAMES = {"agent": "Task", "task": "Task", "applypatch": "Edit", "apply_patch": "Edit"}

_EPOCH_MS = 1000.0


def claude_tool_name(name):
    """A ZCode tool name as the equivalent Claude Code one, or unchanged."""
    return _TOOL_NAMES.get((name or "").lower(), name)


def zcode_home(env=None):
    env = os.environ if env is None else env
    return env.get("ZCODE_HOME") or os.path.join(os.path.expanduser("~"), ".zcode")


def zcode_db_paths(env=None):
    """The session databases, operative first: the CLI's `db/db.sqlite`, then the `v2` one the
    older surface names (empty on current installs). `ZCODE_DB_PATH` replaces the list."""
    env = os.environ if env is None else env
    if env.get("ZCODE_DB_PATH"):
        return [os.path.abspath(env["ZCODE_DB_PATH"])]
    home = zcode_home(env)
    return [os.path.join(home, "cli", "db", "db.sqlite"),
            os.path.join(home, "v2", "db.sqlite")]


def zcode_connect(env=None):
    """A read-only connection to the first database that has sessions in it, or None (no
    database, an unreadable file, a locked one). Read-only URI mode: a fold must never write
    into the agent's own database, and a WAL writer never blocks a reader."""
    for path in zcode_db_paths(env):
        try:
            if os.path.getsize(path) == 0:
                continue
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
            con.execute("select 1 from session limit 1").fetchone()
            return con
        except (OSError, sqlite3.Error):
            continue
    return None


def zcode_safe_session_id(sid):
    """A log's id when it is a plain token (safe as a file name), else None."""
    return sid if isinstance(sid, str) and SESSION_ID_RE.fullmatch(sid) else None


def zcode_sessions(env=None):
    """Every session in the database, oldest first: `{id, directory, title, version, parent,
    started, updated, mtime, size}`. `started`/`updated`/`mtime` are epoch seconds (the
    database keeps milliseconds); `mtime` mirrors `updated`, and `size` is 0 -- the log is rows,
    not a file. Sub-workflow sessions carry `parent` and are read with everything else."""
    con = zcode_connect(env)
    if con is None:
        return []
    try:
        rows = con.execute("select id, directory, title, version, parent_id, time_created,"
                           " time_updated from session order by time_created").fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        con.close()
    out = []
    for sid, directory, title, version, parent, created, updated in rows:
        started = created / _EPOCH_MS if isinstance(created, (int, float)) else None
        mtime = updated / _EPOCH_MS if isinstance(updated, (int, float)) else started
        out.append({"id": sid, "directory": directory if isinstance(directory, str) else None,
                    "title": title, "version": version, "parent": parent,
                    "started": started, "updated": mtime, "mtime": mtime or 0.0, "size": 0})
    return out


def zcode_messages(con, session_id):
    """A session's messages in order: `(data, parts)` with `parts` the parsed `part` rows, or
    None when the session has none (an unknown id, a pruned database)."""
    try:
        rows = con.execute("select id, data from message where session_id=?"
                           " order by time_created, sequence", (session_id,)).fetchall()
    except sqlite3.Error:
        return None
    if not rows:
        return None
    out = []
    for mid, data in rows:
        try:
            body = json.loads(data) if data else {}
        except ValueError:
            body = {}
        parts = []
        try:
            for (pdata,) in con.execute("select data from part where message_id=?"
                                        " order by time_created, sequence", (mid,)):
                try:
                    part = json.loads(pdata) if pdata else {}
                except ValueError:
                    part = {}
                if isinstance(part, dict):
                    parts.append(part)
        except sqlite3.Error:
            parts = []
        if isinstance(body, dict):
            out.append((body, parts))
    return out


def _is_prompt(body, parts):
    """A person's prompt: `semantics.kind` says `user_prompt`; a message from before that field
    existed counts when it carries plain text that is not an injected reminder."""
    if (body.get("role") or "") != "user":
        return False
    kind = ((body.get("semantics") or {}).get("kind") if isinstance(body.get("semantics"), dict)
            else None)
    if kind in NOT_PROMPTS:
        return False
    texts = [p.get("text") or "" for p in parts if isinstance(p, dict) and p.get("type") == "text"]
    joined = "\n".join(t for t in texts if t)
    if kind == "user_prompt":
        return True
    return kind is None and bool(joined.strip()) and not joined.lstrip().startswith(SYSTEM_PREFIXES)


def zcode_prompt_count(con, session_id):
    """How many prompts the session ran; None when its messages cannot be read."""
    msgs = zcode_messages(con, session_id)
    if msgs is None:
        return None
    return sum(1 for body, parts in msgs if _is_prompt(body, parts))


def zcode_session_directory(con, session_id):
    """The directory the session ran in (the `session` row's `directory`), or None."""
    try:
        row = con.execute("select directory from session where id=?", (session_id,)).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row and isinstance(row[0], str) and row[0] else None


def _blocks(parts):
    """A message's parts as Claude content blocks: `tool` parts become `tool_use` (the input is
    `state.input`), `text` parts become text, reasoning and step markers are dropped."""
    blocks = []
    for p in parts:
        kind = p.get("type")
        if kind == "tool":
            state = p.get("state") if isinstance(p.get("state"), dict) else {}
            blocks.append({"type": "tool_use", "id": str(p.get("callID") or ""),
                           "name": claude_tool_name(p.get("tool")),
                           "input": state.get("input") if isinstance(state.get("input"), dict)
                           else {}})
        elif kind == "text" and (p.get("text") or "").strip():
            blocks.append({"type": "text", "text": p["text"]})
    return blocks


def _results(parts):
    """The `tool_result` blocks for a message's tool parts, from `state.output` -- or, for a
    part that failed, `state.error`, which is where a failed call carries its text (the output
    is null then; dropping the error left every failed result an empty string, and no reader
    could see what had failed)."""
    out = []
    for p in parts:
        if p.get("type") != "tool":
            continue
        state = p.get("state") if isinstance(p.get("state"), dict) else {}
        output = state.get("output")
        if output is None and state.get("status") in ("error", "cancelled"):
            output = state.get("error")
        out.append({"type": "tool_result", "tool_use_id": str(p.get("callID") or ""),
                    "content": output if isinstance(output, str) else json.dumps(output)
                    if output is not None else "",
                    "is_error": state.get("status") in ("error", "cancelled")})
    return out


def _iso(body):
    """The message's time as the ISO stamp a Claude transcript line carries (Claude Code's
    readers date a log's head and turn spans by it), or None."""
    created = ((body.get("time") or {}).get("created") if isinstance(body.get("time"), dict)
               else None)
    if not isinstance(created, (int, float)) or isinstance(created, bool):
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created / _EPOCH_MS))


def zcode_session_to_claude(con, session_id, cwd, out_path):
    """Write a ZCode session as Claude Code transcript lines, for `load_trajectory`.

    Every line carries the session's `directory` (ZCode records no per-call working directory),
    its id, and the message's timestamp. Returns the number of tool calls written, None when
    the session has no messages (an unknown id, an unreadable database) -- and 0 is a real,
    empty session.
    """
    msgs = zcode_messages(con, session_id)
    if msgs is None:
        return None
    n = 0
    with open(out_path, "w", encoding="utf-8") as out:
        for body, parts in msgs:
            role = body.get("role")
            stamp = _iso(body)
            if role == "assistant":
                blocks = _blocks(parts)
                if not blocks:
                    continue
                message = {"role": "assistant", "content": blocks}
                if isinstance(body.get("modelId"), str) and body["modelId"]:
                    message["model"] = body["modelId"]
                record = {"type": "assistant", "message": message}
                n += sum(1 for b in blocks if b.get("type") == "tool_use")
            elif role == "user" and _is_prompt(body, parts):
                text = "\n".join(p.get("text") or "" for p in parts
                                 if isinstance(p, dict) and p.get("type") == "text")
                record = {"type": "user", "message": {"role": "user",
                                                      "content": [{"type": "text",
                                                                   "text": text}]}}
            else:
                continue
            if cwd:
                record["cwd"] = cwd
            if stamp:
                record["timestamp"] = stamp
            record["sessionId"] = session_id
            out.write(json.dumps(record) + "\n")
            if role == "assistant":
                # The result of each call rides the same `tool` part (`state.output`); the
                # readers expect it as the next user line, as Claude Code writes it.
                results = _results(parts)
                if results:
                    answer = {"type": "user", "message": {"role": "user",
                                                          "content": results}}
                    if cwd:
                        answer["cwd"] = cwd
                    if stamp:
                        answer["timestamp"] = stamp
                    answer["sessionId"] = session_id
                    out.write(json.dumps(answer) + "\n")
    return n


def zcode_session_usage(con, session_id):
    """Token breakdown of a session from its `model_usage` rows, completed ones only (a
    running row holds zeros). Returns the four fields `pricing.effective_tokens` reads plus
    `model` (the most-used `model_id`) and `requests`. Without rows every count is None, so
    the ledger says `no_usage` rather than guessing."""
    out = {"input_tokens": None, "cache_read_input_tokens": None,
           "cache_creation_input_tokens": None, "output_tokens": None,
           "requests": 0, "model": None}
    try:
        rows = con.execute("select model_id, input_tokens, output_tokens,"
                           " cache_read_input_tokens, cache_creation_input_tokens"
                           " from model_usage where session_id=? and status='completed'",
                           (session_id,)).fetchall()
    except sqlite3.Error:
        return out
    if not rows:
        return out
    models = {}
    for model_id, tin, tout, tread, tcreate in rows:
        out["input_tokens"] = (out["input_tokens"] or 0) + (tin or 0)
        out["output_tokens"] = (out["output_tokens"] or 0) + (tout or 0)
        out["cache_read_input_tokens"] = (out["cache_read_input_tokens"] or 0) + (tread or 0)
        out["cache_creation_input_tokens"] = (out["cache_creation_input_tokens"] or 0) \
            + (tcreate or 0)
        models[model_id] = models.get(model_id, 0) + 1
    out["requests"] = len(rows)
    out["model"] = max(models, key=models.get)
    return out
