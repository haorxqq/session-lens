# session-lens

A local web viewer for **Claude Code** & **opencode** conversation history — browse, search, filter by source, and resume any session right in your terminal.

The command line makes it hard to revisit past AI coding conversations. `session-lens` reads your local session history from both Claude Code and opencode, presents it as a searchable list, and renders each conversation as a clean chat transcript.

![session-lens screenshot](assets/screen.png)

## Features

- **Two sources, one place** — aggregates Claude Code (`~/.claude/projects/*.jsonl`) and opencode (`~/.local/share/opencode/opencode.db`) sessions side by side.
- **Cascading filters** — filter by source (Claude / opencode), then by project; plus full-text search over titles.
- **Readable transcripts** — your messages on the right, the assistant on the left; Markdown rendering, syntax highlighting, collapsible **Thinking** and **Tool result** blocks.
- **Session metadata** — title, session ID (click to copy), working directory, message count, and timestamps.
- **Resume in your terminal** — one click opens a new iTerm window, `cd`s into the original working directory, and runs the resume command:
  - Claude Code → `claude --resume <id>`
  - opencode → `opencode --session <id>`

## Requirements

- Python 3.10+ (uses `X | None` type syntax)
- [Flask](https://flask.palletsprojects.com/)
- macOS + [iTerm](https://iterm2.com/) for the "Continue in iTerm" button (the rest works anywhere)

```bash
pip install flask
```

## Usage

```bash
python app.py
```

Then open http://localhost:5678

The app reads session files directly — nothing is copied or modified. Use the **Refresh** button to re-scan after new conversations.

## How it works

- `app.py` — Flask backend. Scans both sources, normalizes them into a unified message format (`text` / `thinking` / `tool_use` / `tool_result` blocks), and exposes a small JSON API:
  - `GET /api/sessions` — session list metadata
  - `GET /api/sessions/<id>` — full conversation
  - `POST /api/sessions/<id>/resume` — open the session in iTerm
  - `GET /api/reload` — rebuild the in-memory cache
- `index.html` — single-page frontend (no build step; dependencies loaded via CDN).

## License

MIT
