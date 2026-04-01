# Contributing to GristCoderMCP

> **Language note**: The server code (`SERVER_INSTRUCTIONS`, tool descriptions, wizard labels, prompts) is written in **French** — this is intentional, as the primary audience is French public sector users. The documentation files (README, ARCHITECTURE, CONTRIBUTING) are in English for broader accessibility. Line numbers below are approximate landmarks — they may drift as the code evolves.

## Code structure

Everything is in two files:

| File | Lines | Role |
|------|-------|------|
| `grist_coder.py` | ~5300 | MCP server (tools, resources, prompts, HTTP routes, Grist API) |
| `widget.html` | ~2100 | Grist custom widget (editor, preview, wizard, SSE client) |

**Do not split `grist_coder.py` into multiple files.** This is intentional — the single-file design keeps deployment simple (copy 2 files + pip install).

## `grist_coder.py` — Map of sections

Read the file top to bottom. Each section is marked with a comment banner.

```
Line    Section                        What it controls
─────   ─────────────────────────────  ──────────────────────────────────────
1       Module docstring               Version, auth model, tool count
33      SERVER_INSTRUCTIONS            System prompt sent to LLM on initialize
                                       → Controls AI behavior, phases, rules
230     SessionCtx                     Per-document session state (canvas, plan,
                                       wizard cards, chat history)
276     UserRegistry                   Multi-user session store, keyed by uid
330     _queues / _push                SSE event fan-out (filtered by uid_key)
353     Grist HTTP helpers             grist_get/post/patch/put/delete wrappers
                                       → All Grist API calls go through these
410     SUBAGENT_PROMPTS               System prompts for each sub-agent role
540     TOOLS list                     MCP tool definitions (name, description,
                                       inputSchema) — 28 entries
911     CONTEXT_TOOLS                  Phase → tool filtering (qualifying, etc.)
942     PROMPTS list                   MCP prompt definitions — 8 entries
1050    _prompt_messages()             Prompt content generator
1100    STATIC_RESOURCES               MCP resource URIs — 10 docs
1150    RESOURCE_TEMPLATES             MCP resource URI templates — 7 patterns
1200    DOCS_SCHEMA                    Inline doc: column types, formulas
1400    DOCS_ARTEFACTS                 Inline doc: widget templates, bridge API
1600    DOCS_PLAYBOOK                  Inline doc: page creation recipes
        ...                            (more DOCS_* constants)
2560    DOCS_QUALIFICATION             Inline doc: app categories, questions
2700    DOCS_SERVICES_AI               Inline doc: 4 AI integration patterns
2786    EXAMPLES                       Pre-built domain schemas (crm, rh, stock...)
3280    _read_resource()               Resource content resolver
3700    _fetch_schema / _fetch_doc_structure
                                       Live Grist introspection helpers
3960    call_tool()                    Tool dispatch — THE main function
                                       Every tool call lands here
4900    dispatch()                     MCP JSON-RPC method router
5000    FastAPI routes                 /mcp (POST/GET/DELETE), /register,
                                       /wizard/{token}, /webhook-receive, /health, /
```

## How to add a new tool

1. **Define it** — add an entry to the `TOOLS` list (~line 540):

```python
{"name": "my_tool",
 "description": "What this tool does.",
 "inputSchema": {"type": "object",
                 "properties": {
                     "param1": {"type": "string", "description": "..."},
                 },
                 "required": ["param1"]}},
```

2. **Handle it** — add a block in `call_tool()` (~line 3960):

```python
if name == "my_tool":
    param1 = args["param1"]
    # ... do work ...
    return {"ok": True, "result": "...", "_next": "Suggested next action"}
```

3. **Add to phase filtering** (optional) — if the tool should only be available in certain phases, add its name to the appropriate set in `_QUALIFYING_TOOLS`, `_ASSESSING_TOOLS`, or `_DESIGNING_TOOLS` (~line 911).

4. **Document it** — add to the appropriate `DOCS_*` constant if the LLM needs guidance on when/how to use it.

## How to add a new resource

1. **Static resource** — add to `STATIC_RESOURCES` list (~line 1100):

```python
{"uri": "grist-coder://docs/my-topic",
 "name": "My Topic",
 "description": "When to read this",
 "mimeType": "text/plain"},
```

Then handle it in `_read_resource()`:

```python
if uri == "grist-coder://docs/my-topic":
    return DOCS_MY_TOPIC
```

2. **Template resource** (with variables like `{token}`) — add to `RESOURCE_TEMPLATES`:

```python
{"uriTemplate": "grist-coder://my-thing/{token}",
 "name": "My Thing",
 "description": "...",
 "mimeType": "application/json"},
```

Then handle the pattern in `_read_resource()`.

## How to add a new prompt

1. Add to `PROMPTS` list (~line 942):

```python
{"name": "my-prompt",
 "description": "What this prompt does",
 "arguments": [{"name": "arg1", "description": "...", "required": True}]},
```

2. Handle in `_prompt_messages()`:

```python
if name == "my-prompt":
    return [{"role": "user", "content": {"type": "text", "text": f"Do X with {args['arg1']}"}}]
```

## How to add a new wizard card type

1. **Server side** — in `call_tool()`, the `canvas_wizard` handler (~line 4182) processes the step dict and pushes it via SSE. Add validation for your new type there.

2. **Widget side** — in `widget.html`, the `_wzBuildCard()` function (~line 1434) renders each card type as HTML. Add a new `else if (step.type === 'my-type')` block.

3. **Response handling** — wizard responses come back via `POST /wizard/{token}`. The server unblocks the waiting `call_tool()` and returns the response to the LLM.

## How to add a new sub-agent role

1. Add the role name to the `SUBAGENT_PROMPTS` dict (~line 410):

```python
"my-role": """You are an expert in X. Given a task, you...""",
```

2. If the role should return structured JSON, add parsing logic in the `subagent_call` handler in `call_tool()`.

## How to modify the AI's behavior

### Change what the AI does at each phase

Edit `SERVER_INSTRUCTIONS` (~line 33). This is the system prompt injected on MCP `initialize`. It controls:
- The 4-phase workflow (qualify → design → build → verify)
- When to call which tools
- Quality standards (column types, sample data, completeness)
- Style rules (DSFR, responsive design)

### Change which tools are available per phase

Edit `CONTEXT_TOOLS` and the `_QUALIFYING_TOOLS` / `_ASSESSING_TOOLS` / `_DESIGNING_TOOLS` sets (~line 911).

### Change the documentation the AI reads

Edit the `DOCS_*` constants (DOCS_SCHEMA, DOCS_ARTEFACTS, etc.). These are plain text strings that the AI reads via MCP resources. They contain recipes, templates, and patterns.

### Change the pre-built domain examples

Edit the `EXAMPLES` dict (~line 2786). Each domain is a dict with `{tables, artefacts, pages}` containing Grist UserActions and sample data.

## `widget.html` — Map of sections

```
Line    Section                        What it controls
─────   ─────────────────────────────  ──────────────────────────────────────
1       CSS styles                     Widget layout, colors, wizard overlay
270     HTML structure                 Navbar, preview pane, editor pane,
                                       wizard overlay, status bar
296     GRIST_BRIDGE_SCRIPT            Injected into artefact iframes —
                                       provides grist.docApi.* to widgets
344     APP_RUNTIME_SCRIPT             Injected into iframes — app.navigate(),
                                       app.emit(), screenshot handler
400     prepareWidgetHTML()            Prepares HTML for iframe rendering:
                                       strips grist-plugin-api.js, injects bridge
417     Wizard preview script          Injected into wizard preview iframes
434     GristBridgeParent handler      Receives postMessage from artefact iframes,
                                       relays to real Grist API
555     DSFR comment                   (removed — artefacts import DSFR themselves)
560     Grist init + register          grist.ready(), SSE connection, auto-register
617     grist.onRecord handler         Row selection → relay to iframe bridge
700     SSE message handler            Receives canvas_updated, canvas_patched,
                                       wizard_step, chat_message, etc.
864     loadArtefacts()                Fetches Artefacts table, populates dropdown
908     renderArtSelect()              Builds artefact dropdown options
919     onArtSelect()                  User selects artefact → load + render
950     renderEmpty()                  Empty state (no artefact selected)
992     renderArt()                    Main render: prepares HTML, sets blob URL
                                       on iframe (was srcdoc, now blob)
1155    renderCurrent()                Re-render current artefact (after patch)
1171    saveArt()                      Save artefact code to Grist via browser
1188    autoSave()                     Debounced auto-save from editor changes
1315    Wizard card system             _wzCards, _wzOrder, showWizardCard(),
                                       _wzBuildCard(), closeWizardCard()
1550    Chat UI                        sendChat(), renderChatMessage()
1600    Wizard response submission     postWizardResponse() → POST /wizard/{token}
2000    Wizard preview rendering       For preview card type with live iframe
```

### Key widget behaviors

**Artefact rendering** (line 992): `renderArt()` calls `prepareWidgetHTML()` which injects the bridge script, then creates a blob URL and sets it as `frame.src`. This gives the iframe a real origin (not `null`) so CORS works for map tiles and external APIs.

**SSE sync** (line 700): The widget listens to server events. When the AI calls `canvas_write` or `canvas_patch`, the server pushes an SSE event → the widget reloads the code and re-renders the iframe.

**Grist bridge** (line 434): When an artefact iframe calls `grist.docApi.fetchTable('X')`, the iframe sends a `postMessage` to the parent widget. The parent relays it to the real `grist.docApi` and sends the result back. This is transparent to the artefact code.

## Running in development

```bash
# Start with hot-reload
uvicorn grist_coder:app --port 8742 --reload

# The server watches grist_coder.py for changes.
# For widget.html changes, refresh the Grist widget manually.
```

## Testing

There are currently no automated tests. Testing is done manually:

1. Open a Grist document with the widget
2. Connect Claude Desktop or Claude Code
3. Test the tool/feature you changed
4. Check the widget console (F12 → select iframe context) for errors
5. Check the server logs (uvicorn output) for Python errors

Adding a test suite (pytest + httpx test client) is a welcome contribution.
