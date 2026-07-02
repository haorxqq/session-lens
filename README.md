# 👀 session-lens

**Your AI coding history is a goldmine — stop letting it rot in the terminal.**

Every session you've ever had with **Claude Code** or **opencode** is sitting on your disk: the bug you fixed at 2am, the shell one-liner you never wrote down, the architecture discussion you half-remember. Scrolling terminal backlog to find it? Painful. `session-lens` turns all of it into a fast, beautiful, searchable web UI — and lets you jump back into any conversation with one click.

No cloud. No database to set up. No build step. **One Python file + one HTML file**, reading the session data you already have.

![session-lens screenshot](assets/screen.png)

## Why you'll keep it open in a tab

- 🔍 **Full-text search across every conversation** — not just titles. That command you vaguely remember Claude running three weeks ago? Type any fragment and get highlighted match snippets instantly.
- 🗂️ **Everything in one place** — aggregates Claude Code (`~/.claude/projects/*.jsonl`) and opencode (SQLite) sessions side by side, with cascading filters: source → origin → project, plus sort by recency or message count.
- 💬 **Transcripts that are actually readable** — chat-style layout (you on the right, AI on the left), full Markdown + syntax highlighting, collapsible **Thinking** and **Tool result** blocks. Auto-scrolls to the latest message.
- ⚡ **One-click resume** — reopen any session in a real terminal, already `cd`'d into the original working directory. Or copy the command and paste it anywhere.
- 🏷️ **Knows where sessions came from** — CLI, VS Code, or Desktop badges per session (and yes, it correctly handles sessions you started in one and resumed in another).
- ⚙️ **Config inspector** (`/config`) — visualize your whole setup: MCP servers, models, providers, commands, skills, plugins. Every card labelled with the file it comes from, secrets automatically masked.
- ✏️🗑️ **Rename & delete — the safe way** — session-lens **never writes your data**. It generates the exact shell command and you run it yourself. Zero surprises.

## Quick start

```bash
pip install flask
python app.py
```

Open **http://localhost:5678**. That's it — it finds your sessions automatically.

Works on **macOS, Linux, Windows, and WSL**. Requires Python 3.10+.

## How it finds your data

| Source | Location |
|--------|----------|
| Claude Code | `~/.claude/projects/` (auto-resolved on every OS) |
| opencode | via `opencode db path` → `OPENCODE_DB` env var → known fallbacks |

Everything is read directly and read-only — nothing is copied, uploaded, or modified. Hit **Refresh** to re-scan after new conversations.

> **Using WSL?** Run `python app.py` inside WSL (where your session data lives) and open http://localhost:5678 from your Windows browser — WSL2 forwards localhost automatically.

### "Continue in terminal" by platform

| Platform | Opens in |
|----------|----------|
| macOS    | iTerm (via `osascript`) |
| Windows  | a new PowerShell window |
| WSL      | a Windows PowerShell window that re-enters WSL |
| Linux    | first available terminal (`gnome-terminal`, `konsole`, `xterm`, …) |

If no terminal can be launched, the command is copied to your clipboard instead — you're never stuck.

## Under the hood

- **`app.py`** — a small Flask backend that normalizes both sources into one message format and exposes a JSON API:
  - `GET /api/sessions` — session list metadata
  - `GET /api/sessions/<id>` — full conversation
  - `GET /api/search?q=…` — full-text search with match snippets
  - `POST /api/sessions/<id>/resume` — open the session in a terminal (platform-aware)
  - `GET /api/sessions/<id>/rename-command` / `delete-command` — generate safe, copy-paste commands (read-only)
  - `GET /api/config` — masked, source-labelled configuration overview
  - `GET /api/reload` — rebuild the in-memory cache
- **`index.html`** / **`config.html`** — single-page frontends, no build step, dependencies via CDN.

## License

MIT
