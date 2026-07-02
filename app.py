import json
import os
import platform
import re
import shlex
import shutil
import sqlite3
import subprocess
from pathlib import Path
from datetime import datetime
from flask import Flask, jsonify, request, send_from_directory, abort

app = Flask(__name__, static_folder=None)

BASE_DIR = Path(__file__).resolve().parent

# ── Platform detection ──
SYSTEM = platform.system()  # 'Darwin', 'Windows', 'Linux'


def _detect_wsl() -> bool:
    """True when running inside Windows Subsystem for Linux."""
    if SYSTEM != "Linux":
        return False
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


IS_WSL = _detect_wsl()

# Claude Code stores per-project session files under ~/.claude/projects on every
# platform; Path.home() resolves correctly on macOS, Linux/WSL and Windows.
PROJECTS_DIR = Path.home() / ".claude" / "projects"


def _resolve_opencode_db() -> Path:
    """Locate the opencode SQLite DB across platforms.

    Order: OPENCODE_DB env var → `opencode db path` (authoritative) → known
    fallback locations (XDG on macOS/Linux, %LOCALAPPDATA% on Windows).
    """
    env = os.environ.get("OPENCODE_DB")
    if env:
        return Path(env)
    try:
        cmd = "opencode db path" if SYSTEM == "Windows" else ["opencode", "db", "path"]
        out = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5,
            shell=(SYSTEM == "Windows"),
        )
        path = out.stdout.strip().splitlines()[-1].strip() if out.stdout.strip() else ""
        if path:
            return Path(path)
    except Exception:
        pass
    candidates = [Path.home() / ".local" / "share" / "opencode" / "opencode.db"]
    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        candidates.append(Path(localappdata) / "opencode" / "opencode.db")
        candidates.append(Path(localappdata) / "opencode" / "data" / "opencode.db")
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


OPENCODE_DB = _resolve_opencode_db()


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
    entrypoints = []  # ordered, unique — a session can be resumed from different clients
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
                ep = obj.get("entrypoint")
                if ep and ep not in entrypoints:
                    entrypoints.append(ep)

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
        "entrypoints": entrypoints,
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
                    "entrypoints": [],
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
_fulltext_cache = {}  # session_id -> lowercased fulltext (lazy)


def get_sessions():
    global _sessions_cache
    if _sessions_cache is None:
        _sessions_cache = load_all_sessions()
    return _sessions_cache


def _session_fulltext(session):
    """Concatenated text + thinking content of a session (lazy, cached)."""
    sid = session["id"]
    if sid in _fulltext_cache:
        return _fulltext_cache[sid]
    try:
        if session.get("source") == "opencode":
            msgs = parse_opencode_messages(sid)
        else:
            msgs = parse_session_messages(Path(session["path"]))
        parts = [
            b.get("text", "")
            for m in msgs
            for b in m["blocks"]
            if b["type"] in ("text", "thinking")
        ]
        text = "\n".join(parts)
    except Exception:
        text = ""
    _fulltext_cache[sid] = text
    return text


# ── Config inspection (Claude Code + opencode) ──
CLAUDE_DIR = Path.home() / ".claude"
CLAUDE_JSON = Path.home() / ".claude.json"
OPENCODE_CONFIG_DIR = Path.home() / ".config" / "opencode"

_SECRET_HINTS = ("authorization", "token", "apikey", "api_key", "secret", "password", "bearer", "key")


def _mask(value):
    s = str(value)
    return "••••" if len(s) <= 8 else s[:4] + "…" + s[-3:]


def _mask_secrets(d):
    """Mask values whose key looks like a credential."""
    if not isinstance(d, dict):
        return d
    return {
        k: (_mask(v) if any(h in k.lower() for h in _SECRET_HINTS) else v)
        for k, v in d.items()
    }


def _short_path(p):
    """Abbreviate the home dir to ~ for display."""
    p, home = str(p), str(Path.home())
    return "~" + p[len(home):] if p.startswith(home) else p


def _strip_jsonc(text):
    """Strip // and /* */ comments from JSONC, preserving them inside strings."""
    out = []
    i, n = 0, len(text)
    in_str = esc = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
        elif c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _read_md_meta(path):
    """Pull a name + description from a .md file: YAML frontmatter if present,
    otherwise the first non-heading line as the description."""
    name, desc = path.stem, ""
    try:
        text = path.read_text()
    except Exception:
        return {"name": name, "description": ""}
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    k, v = k.strip().lower(), v.strip()
                    if k == "name" and v:
                        name = v
                    elif k == "description" and v:
                        desc = v
            text = text[end + 4:]
    if not desc:
        desc = next(
            (l.strip() for l in text.splitlines()
             if l.strip() and not l.lstrip().startswith("#") and not l.strip() == "---"),
            "",
        )
    return {"name": name, "description": desc}


def _scan_commands():
    d = CLAUDE_DIR / "commands"
    return [_read_md_meta(p) for p in sorted(d.glob("*.md"))] if d.is_dir() else []


def _skills_in(base, source):
    found = []
    for skill_md in sorted(Path(base).glob("*/SKILL.md")):
        m = _read_md_meta(skill_md)
        if m["name"] in ("SKILL", ""):
            m["name"] = skill_md.parent.name
        m["from"] = source
        found.append(m)
    return found


def _scan_skills():
    out = []
    # user-level skills
    sd = CLAUDE_DIR / "skills"
    if sd.is_dir():
        out.extend(_skills_in(sd, "user"))
    # skills provided by enabled plugins
    enabled = {}
    sp = CLAUDE_DIR / "settings.json"
    if sp.exists():
        try:
            enabled = json.loads(sp.read_text()).get("enabledPlugins", {})
        except Exception:
            pass
    ip = CLAUDE_DIR / "plugins" / "installed_plugins.json"
    if ip.exists():
        try:
            for pname, insts in (json.loads(ip.read_text()).get("plugins") or {}).items():
                if not enabled.get(pname):
                    continue  # only enabled plugins are active
                for inst in insts:
                    skdir = Path(inst.get("installPath", "")) / "skills"
                    if skdir.is_dir():
                        out.extend(_skills_in(skdir, pname.split("@")[0]))
        except Exception:
            pass
    return out


def _scan_agents():
    d = CLAUDE_DIR / "agents"
    return [_read_md_meta(p) for p in sorted(d.glob("*.md"))] if d.is_dir() else []


def read_claude_config():
    settings = {}
    sp = CLAUDE_DIR / "settings.json"
    if sp.exists():
        try:
            settings = json.loads(sp.read_text())
        except Exception:
            settings = {}
    mcp, skills, projects = {}, {}, 0
    if CLAUDE_JSON.exists():
        try:
            d = json.loads(CLAUDE_JSON.read_text())
            for name, cfg in (d.get("mcpServers") or {}).items():
                cfg = dict(cfg)
                if "headers" in cfg:
                    cfg["headers"] = _mask_secrets(cfg["headers"])
                if "env" in cfg:
                    cfg["env"] = _mask_secrets(cfg["env"])
                mcp[name] = cfg
            skills = d.get("skillUsage") or {}
            projects = len(d.get("projects") or {})
        except Exception:
            pass
    plugins = {}
    ip = CLAUDE_DIR / "plugins" / "installed_plugins.json"
    if ip.exists():
        try:
            plugins = json.loads(ip.read_text()).get("plugins") or {}
        except Exception:
            pass
    commands = _scan_commands()
    skill_files = _scan_skills()
    agents = _scan_agents()
    return {
        "available": bool(settings or mcp or plugins or commands or skill_files),
        "settings": {
            "model": settings.get("model") or "default",
            "theme": settings.get("theme"),
            "effortLevel": settings.get("effortLevel"),
            "permissions": settings.get("permissions"),
            "enabledPlugins": settings.get("enabledPlugins"),
        },
        "mcp": mcp,
        "commands": commands,
        "skills": skill_files,
        "skillUsage": skills,
        "agents": agents,
        "plugins": plugins,
        "projectCount": projects,
        "sources": {
            "settings": _short_path(CLAUDE_DIR / "settings.json"),
            "mcp": _short_path(CLAUDE_JSON),
            "commands": _short_path(CLAUDE_DIR / "commands") + "/",
            "skills": "~/.claude/skills/ + enabled plugins",
            "agents": _short_path(CLAUDE_DIR / "agents") + "/",
            "plugins": _short_path(CLAUDE_DIR / "plugins" / "installed_plugins.json"),
        },
    }


def read_opencode_config():
    out = {"available": False, "providers": {}, "mcp": {}, "agents": [], "commands": [], "models": []}
    cfg_file = next(
        (OPENCODE_CONFIG_DIR / n for n in ("opencode.jsonc", "opencode.json")
         if (OPENCODE_CONFIG_DIR / n).exists()),
        None,
    )
    if cfg_file:
        try:
            d = json.loads(_strip_jsonc(cfg_file.read_text()), strict=False)
            for name, c in (d.get("provider") or {}).items():
                out["providers"][name] = {
                    "label": c.get("name", name),
                    "npm": c.get("npm", ""),
                    "models": list((c.get("models") or {}).keys()),
                    "baseURL": (c.get("options") or {}).get("baseURL", ""),
                }
            out["mcp"] = {
                name: (_mask_secrets(dict(c)) if isinstance(c, dict) else c)
                for name, c in (d.get("mcp") or {}).items()
            }
            for field in ("agent", "command"):
                out[field + "s"] = [
                    {"name": n, "description": (c.get("description", "") if isinstance(c, dict) else "")}
                    for n, c in (d.get(field) or {}).items()
                ]
            out["available"] = True
        except Exception:
            pass
    try:
        r = subprocess.run(
            "opencode models" if SYSTEM == "Windows" else ["opencode", "models"],
            capture_output=True, text=True, timeout=10,
            shell=(SYSTEM == "Windows"),
        )
        out["models"] = [m.strip() for m in r.stdout.splitlines() if m.strip()]
        if out["models"]:
            out["available"] = True
    except Exception:
        pass
    cfg_display = _short_path(cfg_file) if cfg_file else "~/.config/opencode/opencode.jsonc"
    out["sources"] = {
        "providers": cfg_display,
        "mcp": cfg_display,
        "models": "$ opencode models",
    }
    return out


@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/config")
def config_page():
    return send_from_directory(BASE_DIR, "config.html")


@app.route("/api/config")
def api_config():
    return jsonify({"claude": read_claude_config(), "opencode": read_opencode_config()})


@app.route("/api/sessions")
def api_sessions():
    sessions = get_sessions()
    # Return lightweight metadata only
    return jsonify(
        [
            {
                "id": s["id"],
                "source": s.get("source", "claude"),
                "entrypoints": s.get("entrypoints", []),
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


@app.route("/api/search")
def api_search():
    """Full-text search across session bodies. Returns [{id, snippet}]."""
    q = (request.args.get("q") or "").strip().lower()
    if not q:
        return jsonify([])
    hits = []
    for s in get_sessions():
        text = _session_fulltext(s)
        idx = text.lower().find(q)
        if idx == -1:
            if q in s["title"].lower() or q in s["project"].lower():
                hits.append({"id": s["id"], "snippet": ""})
            continue
        start = max(0, idx - 40)
        seg = text[start:idx + len(q) + 60].replace("\n", " ").strip()
        hits.append({"id": s["id"], "snippet": ("…" if start else "") + seg + "…"})
    return jsonify(hits)


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


# session IDs are uuid / ses_xxx — safe across shells, so no per-shell quoting
RESUME_INNER = {
    "claude": lambda sid: f"claude --resume {sid}",
    "opencode": lambda sid: f"opencode --session {sid}",
}


def _build_command(source: str, sid: str, cwd: str, target: str) -> str:
    """Build a `cd <cwd> && <resume>` command for the given shell target.

    target: 'posix' (bash/zsh) or 'powershell'.
    """
    inner = RESUME_INNER[source](sid)
    if not cwd:
        return inner
    if target == "powershell":
        quoted = "'" + cwd.replace("'", "''") + "'"
        return f"cd {quoted}; {inner}"
    return f"cd {shlex.quote(cwd)} && {inner}"


def launch_resume_terminal(source: str, sid: str, cwd: str) -> str:
    """Open a new terminal window running the resume command.

    Returns the command that was launched. Raises on failure.
    """
    if SYSTEM == "Darwin":
        cmd = _build_command(source, sid, cwd, "posix")
        esc = cmd.replace("\\", "\\\\").replace('"', '\\"')
        script = (
            'tell application "iTerm"\n'
            "  activate\n"
            "  create window with default profile\n"
            "  tell current session of current window\n"
            f'    write text "{esc}"\n'
            "  end tell\n"
            "end tell"
        )
        subprocess.run(
            ["osascript", "-e", script],
            check=True, capture_output=True, text=True, timeout=15,
        )
        return cmd

    if SYSTEM == "Windows":
        cmd = _build_command(source, sid, cwd, "powershell")
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        # Passing cmd as a single -Command arg lets Windows handle the quoting.
        subprocess.Popen(
            ["powershell", "-NoExit", "-Command", cmd],
            creationflags=flags,
        )
        return cmd

    if IS_WSL:
        # From WSL, pop a Windows PowerShell window that re-enters WSL and runs it.
        posix = _build_command(source, sid, cwd, "posix")
        subprocess.Popen(
            ["cmd.exe", "/c", "start", "powershell", "-NoExit", "-Command",
             f'wsl.exe -e bash -lc "{posix}"'],
        )
        return posix

    # Plain Linux: try common terminal emulators.
    posix = _build_command(source, sid, cwd, "posix")
    for term, args in (
        ("x-terminal-emulator", ["-e", "bash", "-lc", posix]),
        ("gnome-terminal", ["--", "bash", "-lc", posix]),
        ("konsole", ["-e", "bash", "-lc", posix]),
        ("xterm", ["-e", "bash", "-lc", posix]),
    ):
        if shutil.which(term):
            subprocess.Popen([term] + args)
            return posix
    raise RuntimeError("No supported terminal emulator found")


@app.route("/api/sessions/<session_id>/resume", methods=["POST"])
def api_session_resume(session_id):
    session = next((s for s in get_sessions() if s["id"] == session_id), None)
    if not session:
        abort(404)
    source = session.get("source", "claude")
    if source not in RESUME_INNER:
        return jsonify({"ok": False, "message": f"Unsupported source: {source}"}), 400

    cwd = session.get("cwd", "")
    # Command we'd hand the user as a fallback (posix form is the common case).
    fallback = _build_command(source, session_id, cwd, "posix")
    try:
        command = launch_resume_terminal(source, session_id, cwd)
    except Exception as e:
        # Return the command so the frontend can offer copy-to-clipboard.
        return jsonify({"ok": False, "message": str(e), "command": fallback}), 500
    return jsonify({"ok": True, "command": command})


@app.route("/api/sessions/<session_id>/rename-command")
def api_session_rename_command(session_id):
    """Generate (but do NOT run) a shell command that renames the session at
    its source. session-lens never writes data itself — the user runs it."""
    session = next((s for s in get_sessions() if s["id"] == session_id), None)
    if not session:
        abort(404)
    title = (request.args.get("title") or "").strip()
    if not title:
        return jsonify({"ok": False, "message": "Title is required"}), 400

    source = session.get("source", "claude")
    note = ""
    if source == "claude":
        rec = json.dumps(
            {"type": "ai-title", "aiTitle": title, "sessionId": session_id},
            ensure_ascii=False,
        )
        command = f"printf '%s\\n' {shlex.quote(rec)} >> {shlex.quote(session['path'])}"
    elif source == "opencode":
        sql = (
            f"UPDATE session SET title = '{title.replace(chr(39), chr(39) * 2)}' "
            f"WHERE id = '{session_id}';"
        )
        command = f"sqlite3 {shlex.quote(str(OPENCODE_DB))} {shlex.quote(sql)}"
        note = "Close opencode before running — the database may be locked while it's open."
    else:
        return jsonify({"ok": False, "message": f"Unsupported source: {source}"}), 400

    return jsonify({"ok": True, "command": command, "note": note})


@app.route("/api/sessions/<session_id>/delete-command")
def api_session_delete_command(session_id):
    """Generate (but do NOT run) a shell command that deletes the session at
    its source. session-lens never touches the data — the user runs it."""
    session = next((s for s in get_sessions() if s["id"] == session_id), None)
    if not session:
        abort(404)

    source = session.get("source", "claude")
    if source == "claude":
        path = shlex.quote(session["path"])
        if SYSTEM == "Darwin":
            command = f"trash {path}"
            note = "Moves the file to the Trash (recoverable). Needs the `trash` CLI: brew install trash."
        elif SYSTEM == "Windows":
            command = f"Remove-Item {path}"
            note = "Permanently deletes the session file."
        else:
            command = f"gio trash {path}"
            note = "Moves the file to the Trash (recoverable). Falls back to `rm` if `gio` is unavailable."
    elif source == "opencode":
        command = f"opencode session delete {shlex.quote(session_id)}"
        note = "Permanently deletes the session from opencode (not recoverable)."
    else:
        return jsonify({"ok": False, "message": f"Unsupported source: {source}"}), 400

    return jsonify({"ok": True, "command": command, "note": note})


@app.route("/api/reload")
def api_reload():
    global _sessions_cache
    _sessions_cache = None
    _fulltext_cache.clear()
    sessions = get_sessions()
    return jsonify({"count": len(sessions)})


if __name__ == "__main__":
    print("Loading sessions...")
    sessions = get_sessions()
    print(f"Loaded {len(sessions)} sessions")
    print("Starting server at http://localhost:5678")
    app.run(host="0.0.0.0", port=5678, debug=False)
