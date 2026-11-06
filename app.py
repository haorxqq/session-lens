import json
import os
import platform
import re
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse
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

    Order: OPENCODE_DB env var → `opencode debug paths` (newer versions) /
    `opencode db path` (older versions) → known fallback locations.
    """
    env = os.environ.get("OPENCODE_DB")
    if env:
        return Path(env)

    def _run(args):
        out = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=5,
            shell=(SYSTEM == "Windows"),
        )
        return out if out.returncode == 0 else None

    # Newer versions print `db  <path>` under `opencode debug paths`.
    out = _run("opencode debug paths" if SYSTEM == "Windows"
               else ["opencode", "debug", "paths"])
    if out:
        for line in out.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "db":
                return Path(parts[-1])

    # Older versions: `opencode db path` prints the path directly.
    out = _run("opencode db path" if SYSTEM == "Windows"
               else ["opencode", "db", "path"])
    if out and out.stdout.strip():
        line = out.stdout.strip().splitlines()[-1].strip()
        # Reject help/usage text (a real path contains a path separator).
        if os.sep in line:
            return Path(line)

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


# Trae CLI keeps its data home under ~/.trae/cli on every platform:
# sessions/YYYY/MM/DD/rollout-*.jsonl plus a session_index.jsonl title log.
def _trae_dir() -> Path:
    if SYSTEM == "Windows":
        return Path(os.environ.get("USERPROFILE") or Path.home()) / ".trae" / "cli"
    return Path.home() / ".trae" / "cli"


TRAE_DIR = _trae_dir()


# Codex stores paginated thread history in SQLite under ~/.codex.
def _codex_dir() -> Path:
    if SYSTEM == "Windows":
        return Path(os.environ.get("USERPROFILE") or Path.home()) / ".codex"
    return Path.home() / ".codex"


CODEX_DIR = _codex_dir()


# GitHub Copilot CLI keeps sessions under ~/.copilot/session-state/<uuid>/.
def _copilot_dir() -> Path:
    if SYSTEM == "Windows":
        return Path(os.environ.get("USERPROFILE") or Path.home()) / ".copilot"
    return Path.home() / ".copilot"


COPILOT_DIR = _copilot_dir()


# VS Code Copilot Chat keeps per-workspace chat transcripts under the user dir.
def _vscode_user_dirs() -> list:
    if SYSTEM == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    elif SYSTEM == "Windows":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return [base / n / "User" for n in ("Code", "Code - Insiders", "VSCodium")]


VSCODE_USER_DIRS = [p for p in _vscode_user_dirs() if p.is_dir()]


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
    sessions.extend(load_trae_sessions())
    sessions.extend(load_codex_sessions())
    sessions.extend(load_copilot_sessions())

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
        tables = {
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "session_v2" in tables:
            rows = con.execute(
                "SELECT id, directory, title, time_created, time_updated "
                "FROM session_v2 WHERE time_archived IS NULL "
                "ORDER BY time_updated DESC"
            ).fetchall()
            for r in rows:
                n = con.execute(
                    "SELECT count(*) FROM session_message "
                    "WHERE session_id = ? AND type IN ('user','assistant')",
                    (r["id"],),
                ).fetchone()[0]
                if n == 0:
                    continue
                directory = r["directory"] or ""
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
            return sessions

        # ── Legacy schema (session / message / part) ──
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
        tables = {
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "session_v2" in tables:
            rows = con.execute(
                "SELECT type, data, time_created FROM session_message "
                "WHERE session_id = ? AND type IN ('user','assistant') "
                "ORDER BY seq",
                (session_id,),
            ).fetchall()
            for r in rows:
                try:
                    d = json.loads(r["data"])
                except json.JSONDecodeError:
                    continue
                blocks = []
                if r["type"] == "user":
                    text = (d.get("text") or "").strip()
                    if text:
                        blocks.append({"type": "text", "text": text})
                else:
                    for b in d.get("content") or []:
                        btype = b.get("type")
                        if btype == "text":
                            text = (b.get("text") or "").strip()
                            if text:
                                blocks.append({"type": "text", "text": text})
                        elif btype == "reasoning":
                            text = (b.get("text") or "").strip()
                            if text:
                                blocks.append({"type": "thinking", "text": text})
                        elif btype == "tool":
                            state = b.get("state") or {}
                            tool_input = state.get("input", {}) if isinstance(state, dict) else {}
                            blocks.append(
                                {
                                    "type": "tool_use",
                                    "name": b.get("name", ""),
                                    "input": tool_input,
                                }
                            )
                            content = state.get("content") if isinstance(state, dict) else None
                            if isinstance(content, list):
                                out = " ".join(
                                    c.get("text", "") for c in content
                                    if c.get("type") == "text"
                                ).strip()
                                if out:
                                    blocks.append(
                                        {"type": "tool_result", "text": out[:2000]}
                                    )
                if blocks:
                    created = (d.get("time") or {}).get("created", r["time_created"])
                    messages.append(
                        {
                            "role": r["type"],
                            "timestamp": ms_to_iso(created),
                            "blocks": blocks,
                        }
                    )
            return messages

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


# ── Trae CLI sessions: ~/.trae/cli/sessions/YYYY/MM/DD/rollout-*.jsonl ──
TRAE_ROLLOUT_PREFIX = "rollout-"


def _trae_rollout_files(session_id: str) -> list:
    """All rollout files for a session, ordered by filename (which embeds the
    rollout start time)."""
    root = TRAE_DIR / "sessions"
    if not root.is_dir():
        return []
    return sorted(root.rglob(f"{TRAE_ROLLOUT_PREFIX}*-{session_id}.jsonl"))


def _trae_shell_command(item: dict) -> str:
    """Human-readable command from a CommandExecution item's argv."""
    argv = item.get("command") or []
    if isinstance(argv, list) and argv:
        # The CLI wraps everything in `/bin/zsh -lc "<the real command>"`.
        if len(argv) >= 3 and argv[1] == "-lc":
            return argv[2]
        return " ".join(shlex.quote(str(a)) for a in argv)
    return ""


def _trae_item_blocks(item: dict) -> tuple:
    """Map a rollout item to (role, blocks). (None, []) when not a chat item."""
    itype = item.get("type")
    if itype == "UserMessage":
        blocks = []
        for c in item.get("content") or []:
            if c.get("type") == "text":
                text = (c.get("text") or "").strip()
                if text and not is_meta_content(text):
                    blocks.append({"type": "text", "text": text})
        return "user", blocks
    if itype == "AgentMessage":
        blocks = []
        for c in item.get("content") or []:
            if c.get("type") in ("Text", "text"):
                text = (c.get("text") or "").strip()
                if text:
                    blocks.append({"type": "text", "text": text})
        return "assistant", blocks
    if itype == "Reasoning":
        blocks = [
            {"type": "thinking", "text": t.strip()}
            for t in (item.get("raw_content") or [])
            if isinstance(t, str) and t.strip()
        ]
        return "assistant", blocks
    if itype == "CommandExecution":
        blocks = [
            {
                "type": "tool_use",
                "name": "shell",
                "input": {"command": _trae_shell_command(item)},
            }
        ]
        output = ((item.get("stdout") or "") + (item.get("stderr") or "")).strip()
        if output:
            blocks.append({"type": "tool_result", "text": output[:2000]})
        return "assistant", blocks
    return None, []


def parse_trae_messages(session_id: str) -> list:
    """Parse every rollout file of a Trae CLI session into unified messages."""
    messages = []
    for rf in _trae_rollout_files(session_id):
        try:
            with open(rf, "r", encoding="utf-8") as f:
                records = [json.loads(line) for line in f if line.strip()]
        except (OSError, json.JSONDecodeError):
            continue
        records.sort(key=lambda o: o.get("ordinal", 0))
        for obj in records:
            if obj.get("type") != "event_msg":
                continue
            payload = obj.get("payload") or {}
            if payload.get("type") != "item_completed":
                continue
            role, blocks = _trae_item_blocks(payload.get("item") or {})
            if role and blocks:
                messages.append(
                    {
                        "role": role,
                        "timestamp": obj.get("timestamp", ""),
                        "blocks": blocks,
                    }
                )
    return messages


def _trae_titles() -> dict:
    """Latest thread_name per session id from session_index.jsonl (last row wins)."""
    titles = {}
    idx = TRAE_DIR / "session_index.jsonl"
    if not idx.exists():
        return titles
    try:
        with open(idx, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sid, name = row.get("id"), row.get("thread_name")
                if sid and name:
                    titles[sid] = name
    except OSError:
        pass
    return titles


def _trae_entrypoints(originator: str | None, source: str | None) -> list:
    """Map a Trae CLI rollout's client origin to origin-badge tokens."""
    ep = (originator or "").lower()
    if "vscode" in ep or source == "vscode":
        return ["trae-vscode"]
    if "desktop" in ep:
        return ["trae-desktop"]
    if "tui" in ep or source == "cli":
        return ["trae-cli"]
    return []


def load_trae_sessions() -> list:
    """Load session metadata from Trae CLI rollout files."""
    root = TRAE_DIR / "sessions"
    if not root.is_dir():
        return []

    # Group rollout files by the session id embedded in their filename:
    # rollout-YYYY-MM-DDTHH-MM-SS-<uuid>.jsonl
    files_by_id = {}
    for rf in root.rglob(f"{TRAE_ROLLOUT_PREFIX}*.jsonl"):
        m = re.match(
            r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-(.+)$",
            rf.stem[len(TRAE_ROLLOUT_PREFIX):],
        )
        if not m:
            continue
        files_by_id.setdefault(m.group(1), []).append(rf)

    titles = _trae_titles()
    sessions = []
    for sid, files in files_by_id.items():
        files.sort()
        meta = {}
        cwd, created, updated, first_user = "", "", "", ""
        msg_count = 0
        for rf in files:
            try:
                with open(rf, "r", encoding="utf-8") as f:
                    records = [json.loads(line) for line in f if line.strip()]
            except (OSError, json.JSONDecodeError):
                continue
            records.sort(key=lambda o: o.get("ordinal", 0))
            for obj in records:
                ts = obj.get("timestamp", "")
                if ts and not created:
                    created = ts
                if obj.get("type") == "session_meta" and not meta:
                    meta = obj.get("payload") or {}
                if obj.get("type") != "event_msg":
                    continue
                payload = obj.get("payload") or {}
                if payload.get("type") != "item_completed":
                    continue
                item = payload.get("item") or {}
                role, blocks = _trae_item_blocks(item)
                if not role or not blocks:
                    continue
                updated = ts or updated
                if not cwd and item.get("cwd"):
                    cwd = item["cwd"]
                has_text = any(b["type"] == "text" for b in blocks)
                if has_text:
                    msg_count += 1
                if role == "user" and has_text and not first_user:
                    first_user = next(
                        b["text"] for b in blocks if b["type"] == "text"
                    )

        if msg_count == 0:
            continue
        cwd = cwd or meta.get("cwd", "")
        title = titles.get(sid, "")
        project = os.path.basename(cwd.rstrip("/")) if cwd else "trae"
        entrypoints = _trae_entrypoints(meta.get("originator"), meta.get("source"))
        sessions.append(
            {
                "id": sid,
                "source": "trae",
                "entrypoints": entrypoints,
                "project": project,
                "dir": cwd,
                "cwd": cwd,
                "title": title or first_user[:120] or sid[:8],
                "preview": first_user[:120],
                "timestamp": created,
                "last_timestamp": updated or created,
                "msg_count": msg_count,
                "path": str(TRAE_DIR),
            }
        )
    return sessions


_codex_copy_cache = {}


def _sqlite_ro(uri: str):
    """Open a SQLite URI read-only, returning None on any failure."""
    con = None
    try:
        con = sqlite3.connect(uri, uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        # Opening is lazy: a WAL database in a directory where SQLite cannot
        # create the -shm file only fails on the first query. Probe it so the
        # caller can fall back to an immutable/open-copy read.
        con.execute("PRAGMA schema_version").fetchone()
        return con
    except sqlite3.Error:
        if con is not None:
            try:
                con.close()
            except sqlite3.Error:
                pass
        return None


def _copy_sqlite_bundle(db_path: Path) -> Path | None:
    """Copy a SQLite DB (and its WAL/SHM sidecars) to a temp dir.

    Some environments (sandboxes, read-only mounts) allow reading the file but
    not creating the shared-memory/lock files SQLite needs. A private copy is
    the simplest way to read such a database without touching the original.
    """
    try:
        key = (str(db_path), db_path.stat().st_mtime_ns, db_path.stat().st_size)
    except OSError:
        return None
    cached = _codex_copy_cache.get(str(db_path))
    if cached and cached[0] == key:
        return cached[1]
    try:
        tmpdir = Path(tempfile.mkdtemp(prefix="session-lens-sqlite-"))
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(db_path) + suffix)
            if src.exists():
                shutil.copy2(src, tmpdir / (db_path.name + suffix))
        dest = tmpdir / db_path.name
        if cached:
            shutil.rmtree(cached[1].parent, ignore_errors=True)
        _codex_copy_cache[str(db_path)] = (key, dest)
        return dest
    except OSError:
        return None


def codex_connect(db_path: Path):
    """Open a Codex SQLite DB read-only, or return None if unavailable.

    Tries the original file first, then an immutable view (avoids WAL/SHM
    creation), then a private copy for environments that block SQLite locking.
    """
    if not db_path.exists():
        return None
    con = _sqlite_ro(f"file:{db_path}?mode=ro")
    if con:
        return con
    con = _sqlite_ro(f"file:{db_path}?mode=ro&immutable=1")
    if con:
        return con
    copy = _copy_sqlite_bundle(db_path)
    if copy:
        return _sqlite_ro(f"file:{copy}?mode=ro&immutable=1")
    return None


def _codex_state_db() -> Path | None:
    """Newest Codex state_*.sqlite, which holds thread metadata (cwd/title)."""
    candidates = []
    for p in CODEX_DIR.glob("state_*.sqlite"):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        candidates.append((mtime, p))
    return max(candidates)[1] if candidates else None


def _codex_history_db() -> Path | None:
    """Newest Codex thread_history_*.sqlite, which holds the item stream."""
    candidates = []
    for p in CODEX_DIR.glob("thread_history_*.sqlite"):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        candidates.append((mtime, p))
    return max(candidates)[1] if candidates else None


def _codex_entrypoints(source: str | None, originator: str | None) -> list:
    """Map a Codex thread's client origin to origin-badge tokens.

    Codex records two fields: `source` (rough channel: cli/vscode) and
    `originator` (concrete client id, sometimes NULL on older threads).
    """
    ep = (originator or "").lower()
    if "tui" in ep or source == "cli":
        return ["codex-cli"]
    if "chrome" in ep or "sidepanel" in ep:
        return ["codex-chrome"]
    if "work_desktop" in ep:
        return ["codex-work-desktop"]
    if "desktop" in ep:
        return ["codex-desktop"]
    if "vscode" in ep or source == "vscode":
        return ["codex-vscode"]
    return []


def _codex_item_blocks(item_type: str, d: dict) -> list:
    """Map a Codex thread item to unified blocks."""
    if item_type == "userMessage":
        blocks = []
        for c in d.get("content") or []:
            if c.get("type") == "text":
                text = (c.get("text") or "").strip()
                if text:
                    blocks.append({"type": "text", "text": text})
        return blocks
    if item_type == "agentMessage":
        text = (d.get("text") or "").strip()
        return [{"type": "text", "text": text}] if text else []
    if item_type == "reasoning":
        texts = [
            (x.get("text") or "").strip()
            for x in (d.get("content") or [])
            if isinstance(x, dict) and x.get("text")
        ]
        return [{"type": "thinking", "text": "\n".join(texts)}] if texts else []
    if item_type == "commandExecution":
        blocks = [
            {
                "type": "tool_use",
                "name": "shell",
                "input": {"command": d.get("command", "")},
            }
        ]
        output = (d.get("aggregatedOutput") or "").strip()
        if output:
            blocks.append({"type": "tool_result", "text": output[:2000]})
        return blocks
    if item_type == "webSearch":
        query = d.get("query") or ""
        blocks = [
            {"type": "tool_use", "name": "web_search", "input": {"query": query}}
        ]
        snippets = [
            r.get("snippet") or r.get("title") or ""
            for r in (d.get("results") or [])
            if isinstance(r, dict)
        ]
        text = "\n\n".join(s for s in snippets if s).strip()
        if text:
            blocks.append({"type": "tool_result", "text": text[:2000]})
        return blocks
    if item_type == "fileChange":
        changes = [
            {"path": c.get("path", ""), "kind": (c.get("kind") or {}).get("type", "")}
            for c in (d.get("changes") or [])
            if isinstance(c, dict)
        ]
        return [
            {
                "type": "tool_use",
                "name": "file_change",
                "input": {"changes": changes},
            }
        ] if changes else []
    if item_type == "mcpToolCall":
        name = f"{d.get('server', 'mcp')}.{d.get('tool', 'tool')}"
        blocks = [
            {"type": "tool_use", "name": name, "input": d.get("arguments") or {}}
        ]
        content = (d.get("result") or {}).get("content") or []
        text = "\n".join(
            c.get("text", "") for c in content
            if isinstance(c, dict) and c.get("type") == "text"
        ).strip()
        if text:
            blocks.append({"type": "tool_result", "text": text[:2000]})
        return blocks
    return []


def _load_codex_from_history(db_path: Path) -> list:
    """Fallback: derive Codex sessions by scanning thread_items directly."""
    con = codex_connect(db_path)
    if con is None:
        return []
    sessions = []
    try:
        thread_ids = [
            r[0] for r in con.execute("SELECT DISTINCT thread_id FROM thread_items")
        ]
        for tid in thread_ids:
            rows = con.execute(
                "SELECT item_type, item_json, created_at_ms FROM thread_items "
                "WHERE thread_id = ? ORDER BY rollout_ordinal",
                (tid,),
            ).fetchall()
            items = []
            cwd = ""
            for r in rows:
                try:
                    d = json.loads(r["item_json"])
                except json.JSONDecodeError:
                    continue
                if not cwd and r["item_type"] == "commandExecution":
                    cwd = d.get("cwd", "")
                items.append((r["item_type"], d, r["created_at_ms"]))

            first_user = next(
                (b["text"] for it, d, _ in items if it == "userMessage"
                 for b in _codex_item_blocks(it, d) if b["type"] == "text"),
                "",
            )
            msg_count = sum(
                1 for it, d, _ in items
                if it in ("userMessage", "agentMessage")
                and _codex_item_blocks(it, d)
            )
            if msg_count == 0:
                continue
            times = [t for _, _, t in items if t]
            project = os.path.basename(cwd.rstrip("/")) if cwd else "codex"
            sessions.append(
                {
                    "id": tid,
                    "source": "codex",
                    "entrypoints": [],
                    "project": project,
                    "dir": cwd,
                    "cwd": cwd,
                    "title": first_user[:120] or tid[:8],
                    "preview": first_user[:120],
                    "timestamp": ms_to_iso(min(times)) if times else "",
                    "last_timestamp": ms_to_iso(max(times)) if times else "",
                    "msg_count": msg_count,
                    "path": str(db_path),
                }
            )
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return sessions


def load_codex_sessions() -> list:
    """Load Codex sessions from state_*.sqlite + thread_history_*.sqlite."""
    if not CODEX_DIR.is_dir():
        return []
    history_db = _codex_history_db()
    if history_db is None:
        return []

    # Message counts for every thread in one pass.
    counts = {}
    con = codex_connect(history_db)
    if con is not None:
        try:
            for r in con.execute(
                "SELECT thread_id, count(*) FROM thread_items "
                "WHERE item_type IN ('userMessage', 'agentMessage') "
                "GROUP BY thread_id"
            ):
                counts[r[0]] = r[1]
        except sqlite3.Error:
            counts = {}
        finally:
            con.close()

    state_db = _codex_state_db()
    if state_db is None:
        return _load_codex_from_history(history_db)

    sessions = []
    con = codex_connect(state_db)
    if con is None:
        return _load_codex_from_history(history_db)
    try:
        rows = con.execute(
            "SELECT id, cwd, title, first_user_message, preview, source, "
            "originator, created_at, updated_at, created_at_ms, updated_at_ms, archived "
            "FROM threads ORDER BY updated_at DESC"
        ).fetchall()
        for r in rows:
            if r["archived"]:
                continue
            tid = r["id"]
            msg_count = counts.get(tid, 0)
            if msg_count == 0:
                continue
            cwd = r["cwd"] or ""
            first_user = (r["first_user_message"] or "").strip()
            title = (r["title"] or "").strip() or first_user[:120] or tid[:8]
            preview = (r["preview"] or "").strip() or first_user[:120]
            created = r["created_at_ms"] or (r["created_at"] * 1000 if r["created_at"] else 0)
            updated = r["updated_at_ms"] or (r["updated_at"] * 1000 if r["updated_at"] else 0)
            sessions.append(
                {
                    "id": tid,
                    "source": "codex",
                    "entrypoints": _codex_entrypoints(r["source"], r["originator"]),
                    "project": os.path.basename(cwd.rstrip("/")) if cwd else "codex",
                    "dir": cwd,
                    "cwd": cwd,
                    "title": title,
                    "preview": preview,
                    "timestamp": ms_to_iso(created),
                    "last_timestamp": ms_to_iso(updated or created),
                    "msg_count": msg_count,
                    "path": str(history_db),
                }
            )
    except sqlite3.Error:
        return _load_codex_from_history(history_db)
    finally:
        con.close()
    return sessions


def parse_codex_messages(session) -> list:
    """Parse all items of a Codex thread into unified messages."""
    con = codex_connect(Path(session["path"]))
    if con is None:
        return []
    messages = []
    try:
        rows = con.execute(
            "SELECT item_type, item_json, created_at_ms FROM thread_items "
            "WHERE thread_id = ? ORDER BY rollout_ordinal",
            (session["id"],),
        ).fetchall()
        for r in rows:
            try:
                d = json.loads(r["item_json"])
            except json.JSONDecodeError:
                continue
            blocks = _codex_item_blocks(r["item_type"], d)
            if not blocks:
                continue
            role = {
                "userMessage": "user",
                "agentMessage": "assistant",
                "reasoning": "assistant",
                "commandExecution": "assistant",
                "webSearch": "assistant",
                "fileChange": "assistant",
                "mcpToolCall": "assistant",
            }.get(r["item_type"])
            if role:
                messages.append(
                    {
                        "role": role,
                        "timestamp": ms_to_iso(r["created_at_ms"]),
                        "blocks": blocks,
                    }
                )
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return messages


# ── GitHub Copilot sessions ──
# CLI:    ~/.copilot/session-state/<uuid>/{workspace.yaml,events.jsonl}
# VS Code: <User>/workspaceStorage/<hash>/chatSessions/*.json(l)


def _read_simple_yaml(path: Path) -> dict:
    """Read the flat key: value YAML files Copilot writes (no PyYAML needed)."""
    data = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line or line.lstrip().startswith("#") or ":" not in line:
                continue
            key, value = line.split(":", 1)
            value = value.strip()
            if value[:1] == '"' and value[-1:] == '"':
                # Renames are written as JSON-style quoted scalars.
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    value = value[1:-1]
            elif value[:1] == "'" and value[-1:] == "'":
                value = value[1:-1].replace("''", "'")
            data[key.strip()] = value
    except OSError:
        pass
    return data


def _copilot_text(value) -> str:
    """Flatten Copilot text/content values into a plain string."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("value") or ""
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        return "\n".join(parts).strip()
    if isinstance(value, dict):
        text = value.get("text") or value.get("value") or ""
        return str(text).strip() if text else ""
    return ""


def _copilot_cli_event_blocks(event_type: str, data: dict) -> tuple:
    """Map one Copilot CLI events.jsonl record to (role, blocks)."""
    if event_type == "user.message":
        if data.get("source") == "memory":
            return None, []
        text = _copilot_text(data.get("content"))
        if not text or is_meta_content(text):
            return None, []
        return "user", [{"type": "text", "text": text}]
    if event_type == "assistant.message":
        text = _copilot_text(data.get("content"))
        if not text or "<session-title>" in text[:200]:
            return None, []
        return "assistant", [{"type": "text", "text": text}]
    if event_type in ("assistant.reasoning", "reasoning"):
        text = _copilot_text(
            data.get("content") or data.get("text") or data.get("summary")
        )
        return ("assistant", [{"type": "thinking", "text": text}]) if text else (None, [])
    if event_type == "tool.execution_start":
        name = data.get("toolName") or "tool"
        args = data.get("arguments")
        if args is None:
            args = {}
        return "assistant", [{"type": "tool_use", "name": name, "input": args}]
    if event_type == "tool.execution_complete":
        result = data.get("result")
        text = _copilot_text(result.get("content") if isinstance(result, dict) else result)
        if not text:
            text = _copilot_text(data.get("output"))
        return ("assistant", [{"type": "tool_result", "text": text[:2000]}]) if text else (None, [])
    return None, []


def parse_copilot_cli_messages(session) -> list:
    """Parse a Copilot CLI session's events.jsonl into unified messages."""
    session_dir = Path(session.get("path") or
                       (COPILOT_DIR / "session-state" / session["id"]))
    events_file = session_dir / "events.jsonl"
    messages = []
    if not events_file.exists():
        return messages
    try:
        with open(events_file, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                role, blocks = _copilot_cli_event_blocks(
                    obj.get("type", ""), obj.get("data") or {}
                )
                if role and blocks:
                    messages.append({
                        "role": role,
                        "timestamp": obj.get("timestamp", ""),
                        "blocks": blocks,
                    })
    except OSError:
        pass
    return messages


def load_copilot_cli_sessions() -> list:
    """Load Copilot CLI sessions that have an events.jsonl transcript."""
    root = COPILOT_DIR / "session-state"
    if not root.is_dir():
        return []
    sessions = []
    for session_dir in sorted(root.iterdir()):
        if not session_dir.is_dir() or session_dir.name.startswith("."):
            continue
        if not (session_dir / "events.jsonl").exists():
            continue
        meta = _read_simple_yaml(session_dir / "workspace.yaml")
        messages = parse_copilot_cli_messages(
            {"id": session_dir.name, "path": str(session_dir)}
        )
        if not messages:
            continue
        msg_count = sum(
            1 for m in messages
            if any(b.get("type") == "text" for b in m["blocks"])
        )
        if msg_count == 0:
            continue
        first_user = next(
            (b["text"] for m in messages if m["role"] == "user"
             for b in m["blocks"] if b["type"] == "text"),
            "",
        )
        cwd = meta.get("cwd", "")
        timestamps = [m["timestamp"] for m in messages if m.get("timestamp")]
        created = meta.get("created_at") or (timestamps[0] if timestamps else "")
        updated = meta.get("updated_at") or (timestamps[-1] if timestamps else "")
        sessions.append({
            "id": session_dir.name,
            "source": "copilot",
            "entrypoints": ["copilot-cli"],
            "flavor": "cli",
            "resumable": True,
            "project": os.path.basename(cwd.rstrip("/")) if cwd else "copilot",
            "dir": cwd,
            "cwd": cwd,
            "title": meta.get("name") or first_user[:120] or session_dir.name[:8],
            "preview": first_user[:120],
            "timestamp": created,
            "last_timestamp": updated or created,
            "msg_count": msg_count,
            "path": str(session_dir),
        })
    return sessions


def _vscode_uri_to_path(uri: str) -> str:
    """Convert a VS Code file:// URI or plain path to a filesystem path."""
    if not uri:
        return ""
    if uri.startswith("file://"):
        parsed = urlparse(uri)
        path = unquote(parsed.path)
        # Windows file:///C:/... -> C:/...
        if SYSTEM == "Windows" and path[:3].lower() in ("/c:", "/d:", "/e:"):
            path = path[1:]
        return path
    return uri


def _vscode_workspace_cwd(chat_file: Path) -> str:
    """Read the owning workspace's folder from workspaceStorage/<hash>/workspace.json."""
    candidates = [
        chat_file.parent.parent / "workspace.json",
        chat_file.parent.parent / "workspace.jsonc",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            return _vscode_uri_to_path(data.get("folder") or data.get("workspace") or "")
        except (OSError, json.JSONDecodeError):
            pass
    return ""


def _json_set(root, keys: list, value):
    """Apply a VS Code JSONL `kind: 1` update, e.g. ["requests", 0, "result"]."""
    if not keys:
        return
    cur = root
    for i, key in enumerate(keys):
        last = i == len(keys) - 1
        if isinstance(cur, list):
            if not isinstance(key, int) or not (0 <= key < len(cur)):
                return
            if last:
                cur[key] = value
                return
            cur = cur[key]
        elif isinstance(cur, dict):
            if last:
                cur[key] = value
                return
            nxt = keys[i + 1]
            if not isinstance(cur.get(key), (dict, list)):
                cur[key] = [] if isinstance(nxt, int) else {}
            cur = cur[key]
        else:
            return


def _read_vscode_chat(path: Path) -> dict | None:
    """Read a VS Code chat session, replaying the append-only .jsonl format."""
    try:
        if path.suffix.lower() == ".json":
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            return data if isinstance(data, dict) else None

        state = {}
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = obj.get("kind")
                keys = obj.get("k")
                value = obj.get("v")
                if kind == 0 and isinstance(value, dict):
                    state = dict(value)
                elif kind == 2 and keys == ["requests"] and isinstance(value, list):
                    state.setdefault("requests", []).extend(value)
                elif kind in (1, 2) and isinstance(keys, list):
                    _json_set(state, keys, value)
        return state
    except (OSError, json.JSONDecodeError):
        return None


def _vscode_response_blocks(resp: dict) -> list:
    """Map one VS Code chat response part to unified blocks."""
    if not isinstance(resp, dict):
        return []
    kind = resp.get("kind")
    if kind == "thinking":
        text = (resp.get("value") or "").strip()
        return [{"type": "thinking", "text": text}] if text else []
    if kind == "toolInvocationSerialized":
        name = resp.get("toolId") or "tool"
        detail = resp.get("toolSpecificData") or {}
        tool_input = {}
        if isinstance(detail, dict):
            command_line = detail.get("commandLine")
            if isinstance(command_line, dict):
                command = command_line.get("original") or command_line.get("forDisplay")
                if command:
                    tool_input["command"] = command
            elif isinstance(command_line, str) and command_line:
                tool_input["command"] = command_line
        message = resp.get("invocationMessage") or resp.get("pastTenseMessage")
        if isinstance(message, dict):
            message = message.get("value")
        if message:
            tool_input["message"] = message
        blocks = [{"type": "tool_use", "name": name, "input": tool_input}]
        output = None
        if isinstance(detail, dict):
            output = detail.get("output") or detail.get("result")
        if isinstance(output, str) and output.strip():
            blocks.append({"type": "tool_result", "text": output[:2000]})
        return blocks
    text = resp.get("value")
    if isinstance(text, str) and text.strip():
        return [{"type": "text", "text": text}]
    return []


def parse_copilot_vscode_messages(session) -> list:
    """Parse a VS Code Copilot Chat session into unified messages."""
    state = _read_vscode_chat(Path(session["path"]))
    if not state:
        return []
    messages = []
    for req in state.get("requests") or []:
        if not isinstance(req, dict) or req.get("hiddenFromTranscript"):
            continue
        message = req.get("message") or {}
        text = _copilot_text(message.get("text") if isinstance(message, dict) else message)
        if text:
            messages.append({
                "role": "user",
                "timestamp": ms_to_iso(req.get("timestamp")),
                "blocks": [{"type": "text", "text": text}],
            })
        blocks = []
        for resp in req.get("response") or []:
            blocks.extend(_vscode_response_blocks(resp))
        if blocks:
            messages.append({
                "role": "assistant",
                "timestamp": ms_to_iso(req.get("responseTimestamp") or req.get("timestamp")),
                "blocks": blocks,
            })
    return messages


def _vscode_chat_files():
    """Yield (chat_file, cwd) for every VS Code Copilot Chat transcript."""
    for user_dir in VSCODE_USER_DIRS:
        ws_root = user_dir / "workspaceStorage"
        if ws_root.is_dir():
            for chat_file in sorted(ws_root.glob("*/chatSessions/*")):
                if chat_file.suffix.lower() in (".json", ".jsonl"):
                    yield chat_file, _vscode_workspace_cwd(chat_file)
        empty = user_dir / "globalStorage" / "emptyWindowChatSessions"
        if empty.is_dir():
            for chat_file in sorted(empty.glob("*")):
                if chat_file.suffix.lower() in (".json", ".jsonl"):
                    yield chat_file, ""


def load_copilot_vscode_sessions() -> list:
    """Load VS Code Copilot Chat sessions, de-duplicating .json/.jsonl pairs."""
    found = {}
    for chat_file, cwd in _vscode_chat_files():
        state = _read_vscode_chat(chat_file)
        if not state or not state.get("requests"):
            continue
        sid = state.get("sessionId") or chat_file.stem
        messages = parse_copilot_vscode_messages(
            {"id": sid, "path": str(chat_file)}
        )
        msg_count = sum(
            1 for m in messages
            if any(b.get("type") == "text" for b in m["blocks"])
        )
        if msg_count == 0:
            continue
        first_user = next(
            (b["text"] for m in messages if m["role"] == "user"
             for b in m["blocks"] if b["type"] == "text"),
            "",
        )
        times = []
        for req in state.get("requests") or []:
            if not isinstance(req, dict):
                continue
            for key in ("timestamp", "responseTimestamp"):
                value = req.get(key)
                if isinstance(value, (int, float)):
                    times.append(value)
        created = ms_to_iso(state.get("creationDate")) or (
            ms_to_iso(min(times)) if times else ""
        )
        updated = ms_to_iso(state.get("lastMessageDate")) or (
            ms_to_iso(max(times)) if times else created
        )
        session = {
            "id": sid,
            "source": "copilot",
            "entrypoints": ["copilot-vscode"],
            "flavor": "vscode",
            "resumable": False,
            "project": os.path.basename(cwd.rstrip("/")) if cwd else "copilot",
            "dir": cwd,
            "cwd": cwd,
            "title": state.get("customTitle") or first_user[:120] or sid[:8],
            "preview": first_user[:120],
            "timestamp": created,
            "last_timestamp": updated or created,
            "msg_count": msg_count,
            "path": str(chat_file),
        }
        # The same chat can exist as both .json and .jsonl; keep the richer one.
        prev = found.get(sid)
        if prev is None or (msg_count, updated) > (prev["msg_count"], prev["last_timestamp"]):
            found[sid] = session
    return list(found.values())


def load_copilot_sessions() -> list:
    """All GitHub Copilot sessions (CLI + VS Code)."""
    sessions = load_copilot_cli_sessions() + load_copilot_vscode_sessions()
    # Guard against accidental id collisions across the two clients.
    deduped = {}
    for s in sessions:
        prev = deduped.get(s["id"])
        if prev is None or s["msg_count"] > prev["msg_count"]:
            deduped[s["id"]] = s
    return list(deduped.values())


def parse_copilot_messages(session) -> list:
    """Dispatch to the CLI or VS Code Copilot transcript parser."""
    if session.get("flavor") == "vscode" or "copilot-vscode" in session.get("entrypoints", []):
        return parse_copilot_vscode_messages(session)
    return parse_copilot_cli_messages(session)


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
        elif session.get("source") == "trae":
            msgs = parse_trae_messages(sid)
        elif session.get("source") == "codex":
            msgs = parse_codex_messages(session)
        elif session.get("source") == "copilot":
            msgs = parse_copilot_messages(session)
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
                "resumable": s.get("resumable", True),
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
    elif session.get("source") == "trae":
        messages = parse_trae_messages(session_id)
    elif session.get("source") == "codex":
        messages = parse_codex_messages(session)
    elif session.get("source") == "copilot":
        messages = parse_copilot_messages(session)
    else:
        messages = parse_session_messages(Path(session["path"]))
    return jsonify(
        {
            "id": session_id,
            "source": session.get("source", "claude"),
            "title": session["title"],
            "project": session["project"],
            "timestamp": session["timestamp"],
            "resumable": session.get("resumable", True),
            "messages": messages,
        }
    )


# session IDs are uuid / ses_xxx — safe across shells, so no per-shell quoting
RESUME_INNER = {
    "claude": lambda sid: f"claude --resume {sid}",
    "opencode": lambda sid: f"opencode --session {sid}",
    "trae": lambda sid: f"trae-cli resume {sid}",
    "codex": lambda sid: f"codex resume {sid}",
    "copilot": lambda sid: f"copilot --resume {sid}",
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
    if not session.get("resumable", True):
        return jsonify({
            "ok": False,
            "message": "This session is read-only (VS Code Copilot Chat cannot be resumed from the CLI).",
        }), 400

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
            f"UPDATE session_v2 SET title = '{title.replace(chr(39), chr(39) * 2)}' "
            f"WHERE id = '{session_id}';"
        )
        command = f"sqlite3 {shlex.quote(str(OPENCODE_DB))} {shlex.quote(sql)}"
        note = "Close opencode before running — the database may be locked while it's open."
    elif source == "trae":
        # The CLI itself renames by appending a new thread_name row to the
        # append-only session_index.jsonl; the last row for an id wins.
        idx_file = TRAE_DIR / "session_index.jsonl"
        updated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        rec = json.dumps(
            {"id": session_id, "thread_name": title, "updated_at": updated_at},
            ensure_ascii=False,
        )
        command = f"printf '%s\\n' {shlex.quote(rec)} >> {shlex.quote(str(idx_file))}"
        note = "Appends a title record to session_index.jsonl, where the Trae CLI stores titles (last record per session wins). Close Trae CLI before running, then hit Refresh."
    elif source == "copilot":
        if session.get("flavor") == "vscode":
            # VS Code stores a user rename as an append-only `kind: 1` update
            # to the chat transcript; older .json snapshots keep it inline.
            script = (
                "import pathlib,sys,json"
                ";p=pathlib.Path(sys.argv[1]);t=sys.argv[2];s=p.read_text(encoding='utf-8')"
                ";p.write_text(s+('' if s.endswith('\\n') else '\\n')"
                "+json.dumps({'kind':1,'k':['customTitle'],'v':t},ensure_ascii=False)+'\\n',encoding='utf-8')"
                " if p.suffix=='.jsonl' else p.write_text("
                "json.dumps({**json.loads(s),'customTitle':t},ensure_ascii=False),encoding='utf-8')"
            )
            command = (
                f"python3 -c {shlex.quote(script)} "
                f"{shlex.quote(session['path'])} {shlex.quote(title)}"
            )
            note = (
                "Appends a customTitle record to the VS Code chat session. "
                "Close that VS Code window first, then hit Refresh."
            )
        else:
            # Copilot CLI keeps the display name in workspace.yaml. The newer
            # Copilot app mirrors it in data.db, so update both when present.
            yaml_path = str(Path(session["path"]) / "workspace.yaml")
            db_path = str(COPILOT_DIR / "data.db")
            script = (
                "import pathlib,sys,json,re,sqlite3"
                ";y=pathlib.Path(sys.argv[1]);t=sys.argv[2];sid=sys.argv[3];db=pathlib.Path(sys.argv[4])"
                ";s=y.read_text(encoding='utf-8');n=json.dumps(t,ensure_ascii=False)"
                ";s=re.sub(r'^name:.*$',lambda m:'name: '+n,s,count=1,flags=re.M)"
                " if re.search(r'^name:',s,re.M) else s.rstrip('\\n')+'\\nname: '+n+'\\n'"
                ";s=re.sub(r'^user_named:.*$',lambda m:'user_named: true',s,count=1,flags=re.M)"
                " if re.search(r'^user_named:',s,re.M) else s.rstrip('\\n')+'\\nuser_named: true\\n'"
                ";y.write_text(s,encoding='utf-8')"
                ";c=sqlite3.connect(str(db)) if db.exists() else None"
                ";c and (c.execute(\"update sessions set title=?, title_source='user' where id=?\",(t,sid)),c.commit(),c.close())"
            )
            command = (
                f"python3 -c {shlex.quote(script)} {shlex.quote(yaml_path)} "
                f"{shlex.quote(title)} {shlex.quote(session_id)} {shlex.quote(db_path)}"
            )
            note = (
                "Updates name/user_named in Copilot's workspace.yaml (and data.db "
                "when the session has an entry there). Close Copilot CLI and the "
                "Copilot app first, then hit Refresh."
            )
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
    elif source == "trae":
        command = f"trae-cli delete {shlex.quote(session_id)}"
        note = "Permanently deletes the saved Trae CLI session (not recoverable)."
    elif source == "codex":
        command = f"codex delete {shlex.quote(session_id)}"
        note = "Permanently deletes the saved session (not recoverable)."
    elif source == "copilot":
        # Copilot has no non-interactive delete command: CLI sessions are a
        # directory under session-state/, VS Code sessions are one chat file
        # (plus a possible .json/.jsonl sibling). Move them to the Trash.
        paths = [session["path"]]
        if session.get("flavor") == "vscode":
            chat = Path(session["path"])
            for ext in (".json", ".jsonl"):
                sibling = chat.with_suffix(ext)
                if sibling.exists() and str(sibling) not in paths:
                    paths.append(str(sibling))
        quoted = [shlex.quote(p) for p in paths]
        if SYSTEM == "Darwin":
            command = "trash " + " ".join(quoted)
            note = (
                "Moves the Copilot session to the Trash (recoverable). Needs the "
                "`trash` CLI: brew install trash. Quit Copilot/VS Code first."
            )
        elif SYSTEM == "Windows":
            command = " ; ".join(
                f'Remove-Item -Recurse -Force "{p}"' for p in paths
            )
            note = (
                "Permanently deletes the Copilot session files. Quit "
                "Copilot/VS Code first."
            )
        else:
            command = "gio trash " + " ".join(quoted)
            note = (
                "Moves the Copilot session to the Trash (recoverable). Falls "
                "back to `rm -rf` if `gio` is unavailable. Quit Copilot/VS Code first."
            )
        if session.get("flavor") != "vscode":
            # The Copilot app also indexes CLI sessions in data.db; drop that
            # row too (FK ON DELETE CASCADE clears its checkpoints/usage).
            db_path = str(COPILOT_DIR / "data.db")
            script = (
                "import sqlite3,sys,os"
                ";p=sys.argv[1];sid=sys.argv[2]"
                ";c=sqlite3.connect(p) if os.path.exists(p) else None"
                ";c and (c.execute('PRAGMA foreign_keys=ON'),"
                "c.execute('DELETE FROM sessions WHERE id=?',(sid,)),c.commit(),c.close())"
            )
            command += (
                f" && python3 -c {shlex.quote(script)} "
                f"{shlex.quote(db_path)} {shlex.quote(session_id)}"
            )
            note += " Also removes its index row from data.db."
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
