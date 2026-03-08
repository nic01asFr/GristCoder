# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A single-file Python app (`grist_coder.py`) that turns a Grist document into a **complete business app** via MCP:
- A **FastAPI HTTP server** implementing the MCP Streamable HTTP protocol (spec 2025-03-26)
- A **Grist custom widget** (`widget.html`, served at `/`) — IDE for writing artefacts and running code

**Philosophy**: implement only what is optimal, low complexity, and easy to add given what already exists.

**4-layer app architecture** (all buildable from this MCP):
1. **Data** — Grist tables + column formulas
2. **UI** — HTML/React artefacts + Grist pages
3. **Logic** — grist_apply (UserActions), grist_sql, grist_upsert
4. **Integrations** — grist_webhooks → external services (CRM, email, ERP, async AI)

## Setup and run

```bash
python -m venv .venv && .venv/Scripts/activate   # Windows
pip install -r requirements.txt
cp .env.example .env                              # set HOST_URL if needed
uvicorn grist_coder:app --port 8742 --reload
curl http://localhost:8742/health                  # -> {"ok":true,...}
```

Or with Docker: `docker compose build --no-cache && docker compose up -d`

## Auth model

Identity is based on **`uid:{grist_user_id}`** — stable numeric ID shared between widget and Claude Desktop sessions.

- **Widget**: `grist.docApi.getAccessToken({readOnly:false})` → `POST /register` → returns `gc-xxxxxx` session token
- **Claude Desktop**: `Authorization: Bearer <grist_key>` → `GET /api/profile/user` → same `uid:{userId}` namespace

accessToken (widget) = document-scoped permissions. Does **not** cover webhook create/update/delete (needs owner API key).

The Grist key and access token must **never** appear in any tool response.

## Claude Desktop config

File: `%APPDATA%\Claude\claude_desktop_config.json`
```json
{
  "mcpServers": {
    "grist-coder": {
      "type": "http",
      "url": "http://localhost:8742/mcp",
      "headers": { "Authorization": "Bearer <grist_key>" }
    }
  }
}
```
Restart Claude Desktop after any change.

## Architecture

Everything lives in `grist_coder.py`. Key sections in order:

| Section | Role |
|---------|------|
| `SERVER_INSTRUCTIONS` | System prompt sent to Claude on `initialize` — includes vision + 4-layer arch |
| `SessionCtx` / `UserRegistry` | In-memory session store, keyed by `uid:{grist_user_id}` |
| SSE helpers (`_push`, `_notify_resource`) | Server-Sent Events fan-out to widget clients |
| Grist HTTP helpers (`grist_get/post/patch/put/delete`) | Wrappers for Grist REST API calls |
| `TOOLS` list | MCP tool definitions (21 tools) |
| `PROMPTS` list + `_prompt_messages()` | MCP prompts (7 prompts) |
| `STATIC_RESOURCES` / `RESOURCE_TEMPLATES` | MCP resources + inline docs |
| `call_tool()` | Tool dispatch logic |
| `dispatch()` | MCP method router |
| FastAPI routes | `/mcp`, `/register`, `/webhook-receive/{docId}`, `/health`, `/` |

## HTTP endpoints

| Method | Route | Role |
|--------|-------|------|
| `POST` | `/mcp` | JSON-RPC MCP |
| `GET` | `/mcp` | SSE stream — canvas updates to widget |
| `DELETE` | `/mcp` | Closes an SSE session |
| `POST` | `/register` | Widget auto-registration |
| `POST` | `/webhook-receive/{docId}` | Receives Grist webhooks → SSE fan-out to widget (production only — HOST_URL must be public) |
| `GET` | `/` | Serves widget.html |
| `GET` | `/health` | Healthcheck JSON |

## MCP tools (21)

**Sessions**: `sessions_list`, `session_select`, `session_info`
**Canvas**: `canvas_read`, `canvas_write`, `canvas_patch`, `canvas_exec` (Python subprocess, 10s), `canvas_screenshot`
**Artefact**: `artefact_init`
**Grist read**: `grist_schema`, `grist_records`, `grist_sql`
**Grist write**: `grist_records_add`, `grist_records_patch`, `grist_upsert`, `grist_apply`
**Document**: `grist_views_list`, `grist_view_create`, `grist_view_add_widget`, `grist_section_configure`
**Webhooks**: `grist_webhooks` (list|create|update|delete)

## Extension rules

- **Do not split** `grist_coder.py` into multiple files
- **New tool**: add entry to `TOOLS[]` + handle in `call_tool()`
- **New prompt**: add entry to `PROMPTS[]` + handle in `_prompt_messages()`
- **New resource**: add entry to `STATIC_RESOURCES[]` + handle in `_read_resource()`
- **New HTTP helper**: add `async def grist_X(ctx, path, ...)` next to existing helpers

## Common errors

| Error | Cause | Fix |
|-------|-------|-----|
| `401` on widget | Expired access token | Widget auto-refreshes at 80% TTL (~12 min) |
| `401` on Claude Desktop | Wrong/missing Grist API key | Check `Authorization: Bearer` in config |
| `403` on webhook create | accessToken insufficient | Use `grist_webhooks` via MCP with owner API key |
| `Fragment introuvable` | `old_str` not exact | Call `canvas_read()` first |
| No sessions | Widget not loaded | Open widget in Grist and wait for auto-registration |
| `timeout 10s` | Infinite loop in canvas | Avoid loops without exit conditions |
| `[object Object]` in Grist | REST PATCH on `_grist_Views*` | Always use `grist_apply(["UpdateRecord", ...])` for meta tables |
