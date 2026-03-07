# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A single-file Python app (`grist_coder.py`) that combines two things:
- A **FastAPI HTTP server** implementing the MCP Streamable HTTP protocol (spec 2025-03-26)
- A **Grist custom widget** (served as inline HTML at `/`) that lets users write/run Python code ("canvas") inside Grist

The canvas concept mirrors Claude Code: `canvas_patch` = `str_replace`, `canvas_read` = read file, `canvas_exec` = run bash.

## Setup and run

```bash
python -m venv .venv && .venv/Scripts/activate   # Windows
pip install -r requirements.txt
cp .env.example .env                              # set HOST_URL if needed
uvicorn grist_coder:app --port 8742 --reload
curl http://localhost:8742/health                  # -> {"ok":true,...}
```

Or with Docker: `docker compose up`

## Auth model (v4.3)

Identity is based on **`uid:{grist_user_id}`** — stable numeric ID shared between widget and Claude Desktop sessions. No separate API key, no manual entry.

- **Widget**: calls `grist.docApi.getAccessToken({readOnly:false})` → gets a short-lived JWT → `POST /register {accessToken, docId, siteUrl, userId?}` → server decodes JWT payload to extract `userId` → returns `arto-xxxxxx` session token. No Grist key ever entered by the user.
- **Claude Desktop**: `Authorization: Bearer <grist_key>` → server calls `GET /api/profile/user` to resolve `userId` → same `uid:{userId}` namespace → sees the same sessions as the widget.

Grist API calls from the server use `?auth={accessToken}` (query param) for widget sessions, `Authorization: Bearer {grist_key}` for Claude Desktop sessions. These are mutually exclusive in Grist's auth model.

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

## Manual test sequence (PowerShell)

```powershell
$H = @{"Content-Type"="application/json";"Authorization"="Bearer <grist_key>"}
$base = "http://localhost:8742"

# Register a widget session first
Invoke-RestMethod -Uri "$base/register" -Method POST -Headers @{"Content-Type"="application/json"} `
  -Body '{"gristKey":"<grist_key>","docId":"DOC001","docTitle":"Test","siteUrl":"http://localhost"}'

# MCP initialize
Invoke-RestMethod -Uri "$base/mcp" -Method POST -Headers $H `
  -Body '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","clientInfo":{"name":"test","version":"1.0"},"capabilities":{}}}'

# sessions_list -> canvas_write -> canvas_exec -> canvas_patch (see README for full sequence)
```

## Architecture

Everything lives in `grist_coder.py`. Key sections in order:

| Section | Role |
|---------|------|
| `SERVER_INSTRUCTIONS` | System prompt sent to Claude on `initialize` |
| `SessionCtx` / `UserRegistry` | In-memory session store, keyed by `uid:{grist_user_id}` |
| SSE helpers (`_push`, `_notify_resource`) | Server-Sent Events fan-out to widget clients |
| Grist HTTP helpers (`grist_get/post/patch/put`) | Wrappers for Grist REST API calls |
| `TOOLS` list | MCP tool definitions (13 tools) |
| `PROMPTS` list + `_prompt_messages()` | MCP prompts (6 prompts) |
| `STATIC_RESOURCES` / `RESOURCE_TEMPLATES` + content | MCP resources + inline docs (GUIDE, GRIST_API, CANVAS_PATTERNS) |
| `call_tool()` | Tool dispatch logic |
| `dispatch()` | MCP method router |
| FastAPI routes | `/mcp` POST/GET/DELETE, `/register`, `/health`, `/` |
| `WIDGET` | Inline HTML/CSS/JS for the Grist widget |

## HTTP endpoints

| Method | Route | Role |
|--------|-------|------|
| `POST` | `/mcp` | JSON-RPC MCP (tools, initialize, prompts, resources) |
| `GET` | `/mcp` | SSE stream — pushes canvas updates to the widget |
| `DELETE` | `/mcp` | Closes an SSE session |
| `POST` | `/register` | Widget auto-registration (`{accessToken, docId, docTitle, siteUrl, userId?}`) |
| `GET` | `/` | Serves the Grist widget HTML |
| `GET` | `/health` | Healthcheck JSON |

## MCP tools (13)

**Sessions**: `sessions_list`, `session_select`, `session_info`
**Canvas**: `canvas_read`, `canvas_write`, `canvas_patch`, `canvas_exec` (subprocess, 10s timeout)
**Grist read**: `grist_schema`, `grist_records`, `grist_sql`
**Grist write**: `grist_records_add`, `grist_records_patch`, `grist_upsert`

## Extension rules

- **Do not split** `grist_coder.py` into multiple files
- **New tool**: add entry to `TOOLS[]` + handle in `call_tool()`
- **New prompt**: add entry to `PROMPTS[]` + handle in `_prompt_messages()`
- **New resource**: add entry to `STATIC_RESOURCES[]` + handle in `_read_resource()`

## Common errors

| Error | Cause | Fix |
|-------|-------|-----|
| `401` on widget | Expired access token | Widget auto-refreshes at 80% TTL (~12 min); reload if stuck |
| `401` on Claude Desktop | Wrong/missing Grist API key | Check `Authorization: Bearer` in `claude_desktop_config.json` |
| `userId introuvable` | JWT decode failed + no fallback | Check that `grist.docApi.getAccessToken()` returns a valid JWT |
| `Fragment introuvable` | `old_str` not exact | Call `canvas_read()` first to copy the exact fragment |
| No sessions | Widget not loaded | Open widget in Grist and wait for auto-registration |
| `timeout 10s` | Infinite loop in canvas | Avoid loops without exit conditions |
