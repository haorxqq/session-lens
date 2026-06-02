import json
import os
import re
import shlex
import sqlite3
import subprocess
from pathlib import Path
from datetime import datetime
from flask import Flask, jsonify, send_from_directory, abort

app = Flask(__name__, static_folder=None)

BASE_DIR = Path(__file__).resolve().parent

PROJECTS_DIR = Path.home() / ".claude" / "projects"
OPENCODE_DB = Path.home() / ".local" / "share" / "opencode" / "opencode.db"


def ms_to_iso(ms) -> str:
    """Convert a millisecond epoch timestamp to an ISO string."""
    if not ms:
        return ""
    try:
        return datetime.fromtimestamp(ms / 1000).isoformat()
    except (ValueError, OSError, OverflowError):
        return ""


def dir_to_project_name(dir_name: str) -> str:
    """Convert directory name like -Users-hrx-Documents-prj-foo to readable name."""
    # Remove leading dash
    name = dir_name.lstrip("-")
    # Replace - with /
    parts = name.split("-")
    # Try to find the meaningful part (after common prefixes)
    try:
        idx = parts.index("prj")
        return "/".join(parts[idx + 1 :]) or dir_name
    except ValueError:
        pass
    try:
        idx = parts.index("Documents")
        return "/".join(parts[idx + 1 :]) or dir_name
    except ValueError:
        pass
    return parts[-1] if parts else dir_name


def extract_text_from_content(content):
    """Extract plain text from message content (list of blocks or string)."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        texts = []
        for block in content:
            if block.get("type") == "text":
                text = block.get("text", "").strip()
                if text and not text.startswith("<"):
                    texts.append(text)
        return " ".join(texts)
    return ""


def is_meta_content(content):
    """Check if message content is only meta/system content."""
    if isinstance(content, str):
        return content.strip().startswith("<")
    if isinstance(content, list):
        for block in content:
            if block.get("type") == "text":
                text = block.get("text", "").strip()
                if text and not text.startswith("<"):
                    return False
            elif block.get("type") in ("tool_use", "tool_result", "thinking"):
                return False
        return True
    return True


def parse_session_metadata(jsonl_path: Path) -> dict | None:
    """Parse just the metadata from a session file (fast scan)."""
    title = None
    first_timestamp = None
    last_timestamp = None
    first_user_msg = None
    cwd = None
    msg_count = 0

    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                ts = obj.get("timestamp")
                if ts:
                    if not first_timestamp:
                        first_timestamp = ts
                    last_timestamp = ts

                if not cwd:
                    cwd = obj.get("cwd", "")

                obj_type = obj.get("type")

                if obj_type == "ai-title" and obj.get("aiTitle"):
                    title = obj["aiTitle"]

                if obj_type in ("user", "assistant") and not obj.get("isMeta"):
                    content = obj.get("message", {}).get("content", "")
                    if not is_meta_content(content):
                        msg_count += 1
                        if obj_type == "user" and not first_user_msg:
                            text = extract_text_from_content(content)
                            if text:
                                first_user_msg = text[:120]
    except Exception:
        return None

    if msg_count == 0 and not title:
        return None

    session_id = jsonl_path.stem
    dir_name = jsonl_path.parent.name
    project = dir_to_project_name(dir_name)

    return {
        "id": session_id,
        "source": "claude",
        "project": project,
        "dir": dir_name,
        "cwd": cwd or "",
        "title": title or first_user_msg or session_id[:8],
        "preview": first_user_msg or "",
        "timestamp": first_timestamp or "",
        "last_timestamp": last_timestamp or "",
        "msg_count": msg_count,
        "path": str(jsonl_path),
    }


def load_all_sessions():
    """Load metadata for all sessions across every source."""
    sessions = []

    if PROJECTS_DIR.exists():
        for project_dir in sorted(PROJECTS_DIR.iterdir()):
            if not project_dir.is_dir():
                continue
            for jsonl_file in sorted(project_dir.glob("*.jsonl")):
                meta = parse_session_metadata(jsonl_file)
                if meta:
                    sessions.append(meta)

    sessions.extend(load_opencode_sessions())

    # Sort by last timestamp descending
    sessions.sort(key=lambda s: s.get("last_timestamp", ""), reverse=True)
    return sessions


def opencode_connect():
    """Open the opencode SQLite DB read-only, or return None if unavailable."""
    if not OPENCODE_DB.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{OPENCODE_DB}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        return con
    except sqlite3.Error:
        return None


def load_opencode_sessions() -> list:
    """Load session metadata from the opencode database."""
    con = opencode_connect()
    if con is None:
        return []
    sessions = []
    try:
        projects = {
            r["id"]: r["worktree"]
            for r in con.execute("SELECT id, worktree FROM project")
        }
        rows = con.execute(
            "SELECT id, project_id, directory, title, time_created, time_updated "
            "FROM session WHERE time_archived IS NULL ORDER BY time_updated DESC"
        ).fetchall()
        for r in rows:
            n = con.execute(
                "SELECT count(*) FROM message WHERE session_id = ?", (r["id"],)
            ).fetchone()[0]
            if n == 0:
                continue
            directory = r["directory"] or projects.get(r["project_id"], "") or ""
            project = os.path.basename(directory.rstrip("/")) or directory or "opencode"
            sessions.append(
                {
                    "id": r["id"],
                    "source": "opencode",
                    "project": project,
                    "dir": directory,
                    "cwd": directory,
                    "title": r["title"] or r["id"],
                    "preview": "",
                    "timestamp": ms_to_iso(r["time_created"]),
                    "last_timestamp": ms_to_iso(r["time_updated"]),
                    "msg_count": n,
                    "path": "",
                }
            )
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return sessions


def parse_opencode_messages(session_id: str) -> list:
    """Parse all messages for an opencode session into the unified block format."""
    con = opencode_connect()
    if con is None:
        return []
    messages = []
    try:
        parts_by_msg = {}
        for p in con.execute(
            "SELECT message_id, data FROM part WHERE session_id = ? ORDER BY time_created",
            (session_id,),
        ):
            try:
                parts_by_msg.setdefault(p["message_id"], []).append(
                    json.loads(p["data"])
                )
            except json.JSONDecodeError:
                continue

        msg_rows = con.execute(
            "SELECT id, data, time_created FROM message WHERE session_id = ? "
            "ORDER BY time_created",
            (session_id,),
        ).fetchall()

        for m in msg_rows:
            try:
                mdata = json.loads(m["data"])
            except json.JSONDecodeError:
                continue
            role = mdata.get("role", "assistant")
            blocks = []
            for pd in parts_by_msg.get(m["id"], []):
                ptype = pd.get("type")
                if ptype == "text":
                    text = (pd.get("text") or "").strip()
                    if text:
                        blocks.append({"type": "text", "text": text})
                elif ptype == "reasoning":
                    text = (pd.get("text") or "").strip()
                    if text:
                        blocks.append({"type": "thinking", "text": text})
                elif ptype == "tool":
                    state = pd.get("state") or {}
                    tool_input = state.get("input", {}) if isinstance(state, dict) else {}
                    blocks.append(
                        {
                            "type": "tool_use",
                            "name": pd.get("tool", ""),
                            "input": tool_input,
                        }
                    )
                    output = state.get("output") if isinstance(state, dict) else None
                    if output:
                        blocks.append(
                            {"type": "tool_result", "text": str(output)[:2000]}
                        )

            if blocks:
                messages.append(
                    {
                        "role": role,
                        "timestamp": ms_to_iso(m["time_created"]),
                        "blocks": blocks,
                    }
                )
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return messages


def parse_session_messages(jsonl_path: Path) -> list:
    """Parse all messages from a session for display."""
    messages = []
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                obj_type = obj.get("type")
                if obj_type not in ("user", "assistant"):
                    continue
                if obj.get("isMeta"):
                    continue

                content = obj.get("message", {}).get("content", "")
                if is_meta_content(content):
                    continue

                blocks = []
                if isinstance(content, list):
                    for block in content:
                        btype = block.get("type")
                        if btype == "text":
                            text = block.get("text", "")
                            if text.strip():
                                blocks.append({"type": "text", "text": text})
                        elif btype == "thinking":
                            thinking = block.get("thinking", "")
                            if thinking.strip():
                                blocks.append({"type": "thinking", "text": thinking})
                        elif btype == "tool_use":
                            tool_input = block.get("input", {})
                            blocks.append(
                                {
                                    "type": "tool_use",
                                    "name": block.get("name", ""),
                                    "input": tool_input,
                                }
                            )
                        elif btype == "tool_result":
                            result_content = block.get("content", "")
                            if isinstance(result_content, list):
                                result_text = " ".join(
                                    b.get("text", "")
                                    for b in result_content
                                    if b.get("type") == "text"
                                )
                            else:
                                result_text = str(result_content)
                            if result_text.strip():
                                blocks.append(
                                    {"type": "tool_result", "text": result_text[:2000]}
                                )
                elif isinstance(content, str) and content.strip():
                    blocks.append({"type": "text", "text": content})

                if blocks:
                    messages.append(
                        {
                            "role": obj_type,
                            "timestamp": obj.get("timestamp", ""),
                            "blocks": blocks,
                        }
                    )
    except Exception as e:
        messages.append(
            {
                "role": "system",
                "timestamp": "",
                "blocks": [{"type": "text", "text": f"Error reading session: {e}"}],
            }
        )
    return messages


# Cache sessions list in memory
_sessions_cache = None


def get_sessions():
    global _sessions_cache
    if _sessions_cache is None:
        _sessions_cache = load_all_sessions()
    return _sessions_cache


@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/api/sessions")
def api_sessions():
    sessions = get_sessions()
    # Return lightweight metadata only
    return jsonify(
        [
            {
                "id": s["id"],
                "source": s.get("source", "claude"),
                "project": s["project"],
                "cwd": s.get("cwd", ""),
                "title": s["title"],
                "preview": s["preview"],
                "timestamp": s["timestamp"],
                "last_timestamp": s["last_timestamp"],
                "msg_count": s["msg_count"],
            }
            for s in sessions
        ]
    )


@app.route("/api/sessions/<session_id>")
def api_session_detail(session_id):
    sessions = get_sessions()
    session = next((s for s in sessions if s["id"] == session_id), None)
    if not session:
        abort(404)
    if session.get("source") == "opencode":
        messages = parse_opencode_messages(session_id)
    else:
        messages = parse_session_messages(Path(session["path"]))
    return jsonify(
        {
            "id": session_id,
            "source": session.get("source", "claude"),
            "title": session["title"],
            "project": session["project"],
            "timestamp": session["timestamp"],
            "messages": messages,
        }
    )


RESUME_CMD = {
    "claude": lambda sid: f"claude --resume {shlex.quote(sid)}",
    "opencode": lambda sid: f"opencode --session {shlex.quote(sid)}",
}


@app.route("/api/sessions/<session_id>/resume", methods=["POST"])
def api_session_resume(session_id):
    session = next((s for s in get_sessions() if s["id"] == session_id), None)
    if not session:
        abort(404)
    source = session.get("source", "claude")
    builder = RESUME_CMD.get(source)
    if not builder:
        return jsonify({"ok": False, "message": f"Unsupported source: {source}"}), 400

    cwd = session.get("cwd", "")
    inner = builder(session_id)
    shell_cmd = f"cd {shlex.quote(cwd)} && {inner}" if cwd else inner

    def osa_escape(s):
        return s.replace("\\", "\\\\").replace('"', '\\"')

    script = f'''
tell application "iTerm"
  activate
  create window with default profile
  tell current session of current window
    write text "{osa_escape(shell_cmd)}"
  end tell
end tell'''
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception as e:
        return jsonify({"ok": False, "message": str(e), "command": shell_cmd}), 500
    return jsonify({"ok": True, "command": shell_cmd})


@app.route("/api/reload")
def api_reload():
    global _sessions_cache
    _sessions_cache = None
    sessions = get_sessions()
    return jsonify({"count": len(sessions)})


if __name__ == "__main__":
    print("Loading sessions...")
    sessions = get_sessions()
    print(f"Loaded {len(sessions)} sessions")
    print("Starting server at http://localhost:5678")
    app.run(host="0.0.0.0", port=5678, debug=False)
