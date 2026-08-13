# GristCoderMCP — How it works

## What is this?

GristCoderMCP connects an AI assistant (Claude, or any MCP-compatible LLM) to a [Grist](https://www.getgrist.com/) document. Through a conversation, the AI can **build a complete business application** inside your Grist document — data structure, user interfaces, pages, and integrations — without you writing a single line of code.

You describe what you need. The AI builds it. Everything stays in your Grist document.

---

## What the AI can do

### Structure a document

The AI can create and organize an entire Grist document:

| Action | How | Example |
|--------|-----|---------|
| **Create tables** | `grist_apply(["AddTable", ...])` | "Create a Clients table with name, email, phone, status" |
| **Define columns** | Types: Text, Numeric, Date, Bool, Choice, Ref | "Add a 'Total HT' formula column that sums the line items" |
| **Set up references** | `Ref:OtherTable` with `visibleCol` | "Link each Invoice to its Client" |
| **Write formulas** | Python column formulas (Grist native) | "Calculate days remaining = due date minus today" |
| **Add sample data** | `grist_records_add` / `BulkAddRecord` | Generates 3-5 realistic rows per table |
| **Create pages** | `grist_view_create` | "Create a page showing Clients + their Invoices" |
| **Configure widgets** | `grist_section_configure` | Link a custom widget to a table with row selection |
| **Link sections** | Linked widgets (master → detail) | Click a client row → invoice list filters automatically |

### Build widgets (artefacts)

The AI writes interactive widgets stored in your Grist document. Each widget is an HTML/JS application rendered in an iframe, with **live access to your Grist data**.

#### Widget types

| Type | What it produces | Typical use |
|------|-----------------|-------------|
| **html** | Raw HTML + CSS + JS | Dashboards, forms, data displays, maps |
| **react** | React 18 components (JSX via Babel CDN) | Complex interactive UIs, multi-view apps |
| **app** | React + built-in router | Full single-page applications with navigation |
| **markdown** | Rendered Markdown | Reports, documentation, guides |
| **mermaid** | Diagrams (flowchart, ER, sequence, gantt) | Schema visualization, process flows |
| **python** | Executed Python script (10s sandbox) | Data processing, calculations, CSV generation |
| **sql** | SQL query with syntax highlighting | Analytical queries on the document |
| **svg** | Inline SVG graphics | Icons, illustrations, custom visuals |

#### What widgets can do

Every widget gets an automatic **Grist bridge** — it can read and write your data:

```
Widget capabilities:
├── Read data     grist.docApi.fetchTable('Clients')     → get all rows
├── Write data    grist.docApi.applyUserActions([...])    → add/update/delete rows
├── React to selection  grist.onRecord(callback)          → update when user clicks a row
├── Navigate      grist.setCursorPos({rowId, sectionId})  → move cursor programmatically
└── Fetch APIs    fetch('https://api.example.com/...')    → call external services
```

#### Examples of widgets the AI can build

| Widget | Description |
|--------|-------------|
| **Dashboard** | KPI cards + charts (Chart.js) + data tables, reactive to filters |
| **Map** | Leaflet/MapLibre map with markers from a table, click to select, import from external APIs |
| **Kanban board** | Drag-and-drop columns by status, writes back to Grist on drop |
| **Calendar** | FullCalendar view of dated records, click to edit |
| **Detail card** | Rich display of a single record, reactive to row selection |
| **Data entry form** | Validated form that creates new records |
| **Report** | Markdown/HTML printable report generated from data |
| **Import wizard** | Fetch external API (SIRENE, data.gouv, geocoding), preview, import to Grist |
| **Chart** | Chart.js / lightweight D3 visualizations |
| **3D viewer** | Three.js / MapLibre GL for spatial data |

### In production — what users see

Once the AI has built the application, **end users interact with a normal Grist document**. They never see the AI or the code:

```
What the AI builds          What the user sees
─────────────────────       ──────────────────
Tables + columns        →   Editable spreadsheet grids
Formulas                →   Auto-computed values
Custom widgets          →   Interactive dashboards, maps, forms
Linked pages            →   Click a row → detail updates
Webhooks                →   Actions trigger external effects (email, CRM, etc.)
```

The Grist document **is** the application. It can be shared, exported, and used offline — all the widgets and data travel with the document.

---

## How the AI works with the user

### Guided phases

The AI follows a structured workflow. It doesn't jump to writing code immediately — it first understands, then plans, then builds:

```
Phase 1 — QUALIFY          "What do you need?"
  │  Wizard: input, choice, form
  │  AI reads qualification docs, asks structured questions
  │  Output: clear need + app category
  │
Phase 2 — DESIGN           "Here's the plan — agree?"
  │  Wizard: confirm (markdown plan)
  │  AI proposes tables, widgets, pages
  │  User validates or amends
  │
Phase 3 — BUILD            "Building… follow the progress"
  │  Wizard: progress bar
  │  AI creates tables → data → widgets → pages
  │  Each step visible in real-time
  │
Phase 4 — VERIFY & DELIVER "Done — here's what was built"
     Wizard: confirm (delivery summary)
     AI checks quality, screenshots widgets
     User gets a working application
```

### Interactive wizard

The AI communicates with the user **through the widget interface**, not just through the chat. It can show:

- **Input fields** — collect free-text needs, with suggestion chips
- **Choice cards** — pick between options (app type, architecture)
- **Forms** — structured data collection (text, numbers, selects, toggles)
- **Confirmations** — review a plan in Markdown, accept or reject
- **Progress tracking** — see each build step (tables ✓, widgets ✓, pages ●)
- **Live previews** — see a widget rendering before it's saved
- **Data import** — fetch external API, preview table, import with one click

Multiple cards can be visible at once (e.g., progress bar + a question form).

### Chat

The widget includes a chat component. The user can send messages during operations. The AI can reply, ask follow-up questions, or wait for input.

### Sub-agents

For complex tasks, the AI can delegate to specialized sub-agents:

| Sub-agent | When it's called |
|-----------|-----------------|
| **data-architect** | Design table schema for a business domain |
| **ui-designer** | Design widget UI with DSFR components |
| **page-architect** | Plan page layouts and widget linkages |
| **ux-navigator** | Decide what to do next after user responds |

---

## Contextual resources

The AI doesn't work blind. At each phase, it reads **contextual resources** that contain recipes, templates, and live document state:

### Documentation (always available)
| Resource | Content |
|----------|---------|
| `docs/schema` | How to create tables, columns, types, formulas |
| `docs/artefacts` | Widget templates, Grist bridge API reference |
| `docs/playbook` | How to create pages, link widgets, choose layouts |
| `docs/wizard` | Wizard step types and schemas |
| `docs/qualification` | App categories, qualifying questions |
| `docs/formulas` | Grist Python formula recipes |
| `docs/services-geo` | Geocoding, maps (BAN, OSM, IGN, Leaflet) |
| `docs/services-data` | Open data APIs (SIRENE, DVF, data.gouv) |
| `docs/services-ai` | AI integration patterns |

### Live document context (per session)
| Resource | Content |
|----------|---------|
| `plan/{token}` | Current project plan + phase + next recommended action |
| `context/{token}` | Full snapshot: schema, FK graph, artefacts, pages, **quality audit**, **plan vs reality delta** |
| `schema-diagram/{token}` | Auto-generated Mermaid ER diagram |
| `code/{token}` | Source code of all artefacts |
| `context/{token}/page/{id}` | Page-level detail: source table, linked sections, configured widget |

### Pre-built examples (8 domains)
| Domain | Tables included |
|--------|----------------|
| `crm` | Clients, Contacts, Opportunities, Activities |
| `rh` | Departments, Employees, Leaves, Planning |
| `stock` | Categories, Products, Movements |
| `projets` | Projects, Tasks, Members |
| `immobilier` | Properties, Leases, Interventions |
| `association` | Members, Events, Contributions |
| `restaurant` | Menu, Orders, Tables, Staff |
| `formation` | Courses, Sessions, Students, Evaluations |

---

## What works well today

| Feature | Status | Notes |
|---------|--------|-------|
| Create tables with correct types and references | **Solid** | Handles Ref, formulas, visibleCol |
| Write and patch widget code | **Solid** | HTML, React, Markdown, Mermaid, Python, SQL, SVG |
| Live preview in iframe | **Solid** | Blob URL rendering, Grist bridge injected |
| Screenshot capture | **Solid** | html2canvas inside iframe |
| Create pages with linked widgets | **Solid** | Master-detail, dashboard layouts |
| Read/write Grist data from widgets | **Solid** | Bridge works for fetchTable, applyUserActions, onRecord |
| Maps (Leaflet) | **Solid** | DOM-based markers, works in iframe |
| Maps (MapLibre GL) | **Solid** | WebGL, fixed with blob URL (was broken with srcdoc) |
| Contextual tool filtering (6 phases) | **Works** | Prevents premature actions |
| Pre-built domain examples | **Works** | 8 domains with schema + data + widget specs |
| Docker deployment | **Works** | Single command, localhost |

## What is in beta

| Feature | Status | What needs work |
|---------|--------|-----------------|
| **Wizard overlay** | Beta | Functional but UI is rough — needs styling polish, better animations, mobile layout. Card stacking can feel cramped. |
| **Phase transitions** | Beta | Sometimes the AI needs a tool not yet available in the current phase. The transition prompts could be smoother. |
| **Chat** | Beta | Basic — no message persistence (lost on refresh), no message history across sessions. |
| **Sub-agents** | Beta | Work well with Claude Desktop (sampling support). Fallback mode (Claude Code, other clients) works but is less reliable — the LLM must manually adopt the sub-agent role. |
| **Data-import wizard card** | Beta | Works for simple APIs but error handling is minimal. Malformed API responses can break the import. |
| **canvas_patch** | Beta | The string matching (`old_str`) is fragile — whitespace differences cause "Fragment not found" errors. Should be improved with fuzzy matching or line-based patching. |
| **`canvas_exec`** | Beta | Executes arbitrary Python in a subprocess (10s timeout). No sandboxing beyond timeout — should not be exposed publicly. |
| **Webhook receiver** | Beta | Works with a public URL but useless on localhost without a tunnel (ngrok). |

## What is missing or planned

| Feature | Status | Description |
|---------|--------|-------------|
| **Session persistence** | Not implemented | Plans and sessions are in-memory — server restart loses everything. Should be stored in a Grist table. |
| **Automated tests** | None | No test suite. Testing is manual with real Grist documents. |
| **HTTPS** | Not included | Requires a reverse proxy (nginx, caddy) for production. |
| **Rate limiting** | Not included | No protection against abuse. Needed for multi-user deployment. |
| **Undo/rollback** | Not implemented | No way to undo a table creation or widget write. Grist has its own undo, but the AI doesn't track it. |
| **Version control for artefacts** | Not implemented | Only the latest code is stored. No diff history inside the MCP server. |
| **Multi-language** | French-centric | Server instructions, wizard labels, and prompts are in French. Internationalization not implemented. |

---

## For non-technical users

### What is Grist?

[Grist](https://www.getgrist.com/) is an open-source spreadsheet-database. Think of it as Excel meets a database — you get familiar spreadsheets, but with proper data types, relations between tables, and custom widgets. It runs in a web browser.

### What does GristCoderMCP add?

It connects an AI assistant to your Grist document. Instead of manually creating tables, writing formulas, and building interfaces, you **describe what you want in plain language** and the AI builds it for you.

**Example conversation:**

> **You:** "I need a CRM to track my clients and their orders"
>
> **AI:** Shows a form asking about your business (B2B? B2C? How many clients?)
>
> **You:** Fill in the form
>
> **AI:** Shows a plan: "I'll create tables Clients, Orders, Products with these columns..."
>
> **You:** "Looks good, but add a Notes column to Clients"
>
> **AI:** Builds everything — tables, sample data, a dashboard, detail pages — you see the progress in real-time
>
> **You:** Open the finished application in Grist, start using it

### What you get

- **Your data stays in Grist** — no external service stores your data
- **Interactive widgets** — dashboards, maps, forms, kanban boards — all inside Grist
- **Everything is editable** — you can modify tables, formulas, and widget code after the AI builds them
- **Shareable** — the Grist document can be shared with colleagues who don't need the AI at all

### What you need

1. A Grist instance (self-hosted, or [grist.numerique.gouv.fr](https://grist.numerique.gouv.fr) for French public sector)
2. GristCoderMCP running on your machine (Docker or Python)
3. An MCP-compatible AI client (Claude Desktop, Claude Code, or similar)
4. A Grist API key (from Profile → API in Grist)

---

## Technical summary

```
Architecture:
┌─────────────────────────────────────────────────────┐
│  AI Client (Claude Desktop / Claude Code)            │
│  ← MCP protocol (Streamable HTTP, JSON-RPC) →       │
├─────────────────────────────────────────────────────┤
│  GristCoderMCP Server (Python, FastAPI, port 8742)   │
│  ├── 34 tools (contextual, phase-filtered)           │
│  ├── 8 prompts (explore, build, patch, debug...)     │
│  ├── 17 resources (docs + live context + examples)   │
│  ├── 7 sub-agent roles (architect, designer, ...)    │
│  ├── Wizard engine (8 card types, multi-card)        │
│  ├── SSE streaming (real-time widget sync)           │
│  └── Grist REST API client (read/write documents)    │
├─────────────────────────────────────────────────────┤
│  Grist Custom Widget (widget.html, served at /)      │
│  ├── Ace editor (code editing)                       │
│  ├── Iframe preview (blob URL, Grist bridge)         │
│  ├── Wizard overlay (interactive cards + chat)       │
│  └── SSE listener (real-time updates from server)    │
├─────────────────────────────────────────────────────┤
│  Grist Instance (your data)                          │
│  ├── Tables, columns, formulas, references           │
│  ├── Artefacts table (widget source code)            │
│  ├── Pages with linked widget sections               │
│  └── REST API (read/write via access token or key)   │
└─────────────────────────────────────────────────────┘
```

**Single file server**: `grist_coder.py` (~8200 lines) — intentional, keeps deployment simple.

**No database**: all persistent state lives in Grist. Server sessions are in-memory (lost on restart).

**Auth**: identity is `uid:{grist_user_id}` — derived from Grist API key (Claude Desktop) or widget access token. Sessions are isolated per user.
