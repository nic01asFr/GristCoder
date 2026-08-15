# CLAUDE.md

Instructions for AI assistants (Claude Code, Cursor, etc.) working on this codebase.

## What this project is

A single-file Python MCP server (`grist_coder.py`) + custom widget (`widget.html`) that turns a Grist document into a complete business application via AI. Alongside them, `harness/` (7 vanilla-JS modules, ~2700 lines) runs an LLM agent inside the widget so the document can build itself without an external MCP client. Its LLM config comes from the pod via `GET /llm-config` — the key stays server-side.

**4-layer app architecture** (all buildable from this MCP):
1. **Data** — Grist tables + column formulas
2. **UI** — HTML/React artefacts + Grist pages
3. **Logic** — grist_apply, grist_sql, grist_upsert
4. **Integrations** — grist_webhooks → external services

**Philosophy**: implement only what is optimal, low complexity, and easy to add given what already exists.

## Setup and run

```bash
python -m venv .venv && source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
cp .env.example .env
uvicorn grist_coder:app --port 8742 --reload
```

Or with Docker: `docker compose up -d`

## Code structure

Everything lives in `grist_coder.py` (~8200 lines). Key sections in order:

| Section | Role |
|---------|------|
| Module docstring | Version, auth model, tool count |
| `SERVER_INSTRUCTIONS` | System prompt sent to LLM on `initialize` |
| `SessionCtx` / `UserRegistry` | In-memory session store, keyed by `uid:{grist_user_id}` |
| SSE helpers (`_push`, `_notify_resource`) | Server-Sent Events fan-out to widget clients |
| Grist HTTP helpers (`grist_get/post/patch/put/delete`) | Wrappers for Grist REST API |
| `TOOLS` list | MCP tool definitions (34 tools) |
| `PROMPTS` list + `_prompt_messages()` | MCP prompts |
| `STATIC_RESOURCES` / `RESOURCE_TEMPLATES` | MCP resources + inline docs |
| `call_tool()` | Tool dispatch logic |
| `dispatch()` | MCP method router |
| FastAPI routes | `/mcp`, `/register`, `/webhook-receive/{docId}`, `/health`, `/` |

## Extension rules

- **Do not split** `grist_coder.py` into multiple files
- **New tool**: add entry to `TOOLS[]` + handle in `call_tool()`
- **New prompt**: add entry to `PROMPTS[]` + handle in `_prompt_messages()`
- **New resource**: add entry to `STATIC_RESOURCES[]` + handle in `_read_resource()`
- **New HTTP helper**: add `async def grist_X(ctx, path, ...)` next to existing helpers

## Auth model

Identity is based on `uid:{grist_user_id}` — stable numeric ID shared between widget and Claude Desktop sessions.

- **Widget**: `grist.docApi.getAccessToken()` → `POST /register` → session token
- **Claude Desktop**: `Authorization: Bearer <grist_key>` → `GET /api/profile/user` → same uid namespace

## Publishing rules (learned the hard way)

- **Never write the script closing sequence literally** in `widget.html` or in any
  artefact — even inside a comment. The HTML parser ends the script element as soon
  as it sees it, truncating everything after. Write `'<' + '/script>'`.
- **A published artefact must not reference the pod** (`/ai-proxy`, `/llm-proxy`,
  `/webhook-receive`, its URL). `artefact_publish` refuses it — the widget would die
  with the server.
- **npm imports are bundled at publish time** (esbuild, no Node). JSX is fine inside a
  `html` artefact. A screen-blank preview is expected for artefacts with imports —
  the browser does not resolve them.
- **JSX source cannot be written server-side** on WAF-protected instances: bare HTML
  tags trigger a 403 and escaping does not help. Write through `canvas_write` with an
  open widget, or use `h(...)` / `React.createElement`.
- **Meta tables** (`_grist_Views*`): `grist_apply` with `UpdateRecord`, never REST
  PATCH — and `customView` is a JSON **string**, not an object, or the document
  becomes unopenable.
- **canvas_write returns render errors** when a widget is open on that document —
  exceptions with line numbers, failed resources, and whether anything was rendered
  at all. Read them before declaring success; `session_info` returns the last one.
- **After deploying a change to `widget.html`, a real page reload is required.**
  The widget reconnects and re-registers by itself after a pod restart, which looks
  like it picked up the new code — it did not.
- **Session routing is per call**: pass `token=` to every tool when several documents
  are open. `session_select` only pins a default for one connection.

## Common errors

| Error | Cause | Fix |
|-------|-------|-----|
| `401` on widget | Expired access token | Widget auto-refreshes at 80% TTL |
| `401` on Claude Desktop | Wrong/missing Grist API key | Check `Authorization: Bearer` in config |
| `403` on webhook create | accessToken insufficient | Use `grist_webhooks` via MCP with owner API key |
| `Fragment introuvable` | `old_str` not exact in canvas_patch | Call `canvas_read()` first |
| No sessions | Widget not loaded | Open widget in Grist and wait for registration |
