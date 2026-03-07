"""
GRIST CODER · MCP Server v5.0 · streamable HTTP spec 2025-03-26
────────────────────────────────────────────────────────────────
Le document Grist = la codebase du projet.
Le widget = split vertical : Ace editor (code) | iframe (rendu live).
Le LLM via MCP orchestre : schema relationnel Grist + artefacts HTML/JS.

AUTH  : widget -> grist.docApi.getAccessToken() -> POST /register -> gc-xxx
        Claude Desktop -> Bearer <grist_key> -> uid:user_id stable
TOOLS : sessions(3) canvas(5) artefact(1) grist-r(3) grist-w(3) = 15
"""

import asyncio, base64, hashlib, json, os, subprocess, sys, time, uuid
from collections import deque
from contextlib import asynccontextmanager
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

load_dotenv()
HOST_URL = os.getenv("HOST_URL", "http://localhost:8742")
MCP_VER  = "2025-03-26"

# ── SERVER INSTRUCTIONS ───────────────────────────────────────────────────────

SERVER_INSTRUCTIONS = """
Tu es connecte a Grist Coder MCP v5 - service de developpement d apps Grist.

CONCEPT FONDAMENTAL
Le document Grist ouvert dans le widget = ta codebase complete.
Comme un repo Git dans Claude Code :
  Tables de donnees (Batiments, Interventions...)  = modele de donnees
  Table Artefacts (widgets HTML/JS)               = fichiers source
  Schema relationnel (Ref:, formules, visibleCol) = architecture
  grist_sql() cross-tables                        = requetes sur le projet

OUTILS DISPONIBLES (15)
Sessions : sessions_list, session_select, session_info
Canvas   : canvas_read, canvas_write, canvas_patch, canvas_exec, canvas_screenshot
Artefact : artefact_init
Grist R  : grist_schema, grist_records, grist_sql
Grist W  : grist_records_add, grist_records_patch, grist_upsert

WORKFLOW OPTIMAL - CONSTRUIRE UNE APP COMPLETE
  1. sessions_list()                     -> identifier le document
  2. artefact_init()                     -> creer table Artefacts (9 colonnes)
  3. grist_schema()                      -> tables existantes du document
  4. resources/read widget-patterns      -> patterns de code artefact
  5. resources/read app-patterns         -> architecture + schema relationnel
  6. Concevoir le schema metier (tables, types, refs)
  7. Creer les tables via REST /tables
     -> Ordre : tables sans refs d abord, puis Ref:TableId, puis visibleCol
  8. Pour chaque artefact :
     a. canvas_write(htmlCode)           -> ecrire dans le buffer
     b. grist_upsert('Artefacts', [...]) -> persister dans Grist
     c. canvas_screenshot()             -> valider le rendu visuel
  9. session_info()                      -> verifier l etat du projet

REGLES CANVAS
  canvas_read() AVANT canvas_patch() - old_str doit etre exact
  canvas_patch >> canvas_write - preserve l historique
  Le widget voit chaque patch via SSE en temps reel

RESOURCES DISPONIBLES
  grist-coder://docs/guide              -> ce guide
  grist-coder://docs/grist-api          -> API Grist : REST schema + widget API + SQL
  grist-coder://docs/widget-patterns    -> coder un artefact (appContext, safeLoad, types)
  grist-coder://docs/app-patterns       -> schema relationnel + architecture app complete
  grist-coder://canvas/{token}          -> code Python actuel (subscribable)
  grist-coder://schema/{token}          -> tables du document
  grist-coder://artefacts/{token}       -> artefacts IsDoc=true du projet (contexte vivant)
""".strip()

# ── SESSION CTX ───────────────────────────────────────────────────────────────

class SessionCtx:
    def __init__(self, doc_id, doc_title, site_url, grist_key="", access_token=""):
        self.doc_id       = doc_id
        self.doc_title    = doc_title or doc_id
        self.site_url     = site_url.rstrip("/")
        self.grist_key    = grist_key
        self.access_token = access_token
        self.canvas       = ""
        self.history      = deque(maxlen=50)
        self.created_at   = time.time()
        self.last_seen    = time.time()
        self.token        = "gc-" + uuid.uuid4().hex[:6]
        self.subscribers: set[str] = set()

    def touch(self): self.last_seen = time.time()

    def meta(self):
        age = int(time.time() - self.last_seen)
        return {
            "token":        self.token,
            "doc_id":       self.doc_id,
            "doc_title":    self.doc_title,
            "site_url":     self.site_url,
            "canvas_sha":   hashlib.sha1(self.canvas.encode()).hexdigest()[:8] if self.canvas else None,
            "canvas_lines": len(self.canvas.splitlines()) if self.canvas else 0,
            "last_seen":    f"{age}s ago" if age < 3600 else f"{age//3600}h ago",
        }


class UserRegistry:
    def __init__(self):
        self._users: dict[str, dict] = {}
        self._grist_key_to_uid: dict[str, str] = {}

    def get_uid_for_grist_key(self, gk):
        return self._grist_key_to_uid.get(gk)

    def provision(self, uid_key, *, grist_key="", access_token="", site=""):
        if uid_key not in self._users:
            self._users[uid_key] = {"sessions": {}, "site": site,
                                    "grist_key": grist_key, "access_token": access_token}
        else:
            u = self._users[uid_key]
            if grist_key:    u["grist_key"]    = grist_key
            if access_token: u["access_token"] = access_token
            if site:         u["site"]         = site
        if grist_key:
            self._grist_key_to_uid[grist_key] = uid_key

    def get_api_token(self, uid_key):
        u = self._users.get(uid_key, {})
        return u.get("grist_key") or u.get("access_token") or ""

    def register_session(self, uid_key, doc_id, doc_title, site_url,
                         grist_key="", access_token=""):
        user = self._users[uid_key]
        for s in user["sessions"].values():
            if s.doc_id == doc_id:
                if grist_key:    s.grist_key    = grist_key
                if access_token: s.access_token = access_token
                s.touch(); return s
        ctx = SessionCtx(doc_id, doc_title, site_url,
                         grist_key=grist_key, access_token=access_token)
        user["sessions"][ctx.token] = ctx
        return ctx

    def resolve(self, uid_key, token):
        user = self._users.get(uid_key)
        if not user: return None
        sessions = user["sessions"]
        if not sessions: return None
        if token: return sessions.get(token)
        if len(sessions) == 1:
            ctx = next(iter(sessions.values())); ctx.touch(); return ctx
        return max(sessions.values(), key=lambda s: s.last_seen)

    def list_sessions(self, uid_key):
        user = self._users.get(uid_key)
        return [s.meta() for s in user["sessions"].values()] if user else []


registry  = UserRegistry()
_token_to_uid: dict[str, str] = {}
_screenshot_waiters: dict[str, asyncio.Future] = {}

# ── SSE ───────────────────────────────────────────────────────────────────────

_queues: dict[str, asyncio.Queue] = {}

def _push(uid_key, event):
    event["_user"] = uid_key
    for q in _queues.values():
        try: q.put_nowait(event)
        except asyncio.QueueFull: pass

def _notify_resource(uid_key, uri):
    _push(uid_key, {"type": "mcp_notification",
                    "method": "notifications/resources/updated",
                    "params": {"uri": uri}})

# ── GRIST HTTP HELPERS ────────────────────────────────────────────────────────

def _jwt_payload(token):
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.b64decode(part.replace("-", "+").replace("_", "/")))
    except Exception:
        return {}

def _gh(ctx):
    h = {"Content-Type": "application/json"}
    if ctx.grist_key:
        h["Authorization"] = f"Bearer {ctx.grist_key}"
    return h

def _aq(ctx):
    return {"auth": ctx.access_token} if ctx.access_token else {}

def _base(ctx): return f"{ctx.site_url}/api/docs/{ctx.doc_id}"

async def grist_get(ctx, path):
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{_base(ctx)}/{path}", headers=_gh(ctx), params=_aq(ctx))
        r.raise_for_status(); return r.json()

async def grist_post(ctx, path, body):
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"{_base(ctx)}/{path}", headers=_gh(ctx), params=_aq(ctx),
                         content=json.dumps(body))
        r.raise_for_status(); return r.json()

async def grist_patch(ctx, path, body):
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.patch(f"{_base(ctx)}/{path}", headers=_gh(ctx), params=_aq(ctx),
                          content=json.dumps(body))
        r.raise_for_status(); return r.json()

async def grist_put(ctx, path, body):
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.put(f"{_base(ctx)}/{path}", headers=_gh(ctx), params=_aq(ctx),
                        content=json.dumps(body))
        r.raise_for_status(); return r.json()

async def fetch_grist_user_profile(site_url, bearer_token):
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{site_url}/api/profile/user",
                            headers={"Authorization": f"Bearer {bearer_token}"})
            if r.status_code == 200: return r.json()
    except Exception:
        pass
    return None

# ── ARTEFACTS TABLE SCHEMA ────────────────────────────────────────────────────

ARTEFACTS_TABLE_DEF = {
    "tables": [{
        "id": "Artefacts",
        "columns": [
            {"id": "Nom",          "fields": {"type": "Text",     "label": "Nom"}},
            {"id": "Type",         "fields": {"type": "Choice",   "label": "Type",
                                              "widgetOptions": json.dumps({"choices": [
                                                  "app","grist","html","react","markdown",
                                                  "svg","mermaid","python","sql","component"]})}},
            {"id": "Code",         "fields": {"type": "Text",     "label": "Code"}},
            {"id": "Description",  "fields": {"type": "Text",     "label": "Description"}},
            {"id": "Dependencies", "fields": {"type": "Text",     "label": "Dependencies"}},
            {"id": "IsDoc",        "fields": {"type": "Bool",     "label": "IsDoc"}},
            {"id": "Icon",         "fields": {"type": "Text",     "label": "Icon"}},
            {"id": "Output",       "fields": {"type": "Text",     "label": "Output"}},
            {"id": "UpdatedAt",    "fields": {"type": "DateTime", "label": "Mis a jour"}},
        ]
    }]
}

# ── TOOLS ─────────────────────────────────────────────────────────────────────

TOOLS = [
    {"name": "sessions_list",
     "description": "Liste tous les widgets actifs. Appeler EN PREMIER.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True}},

    {"name": "session_select",
     "description": "Selectionne une session par token. Inutile si une seule session.",
     "inputSchema": {"type": "object",
                     "properties": {"token": {"type": "string"}},
                     "required": ["token"]}},

    {"name": "session_info",
     "description": "Infos session : tables, artefacts IsDoc, etat canvas. Inclut contexte projet.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True}},

    {"name": "canvas_read",
     "description": "Lit le code complet du canvas. Appeler AVANT tout canvas_patch.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True}},

    {"name": "canvas_write",
     "description": "Reecrit integralement le canvas. Preferer canvas_patch pour modifications ciblees.",
     "inputSchema": {"type": "object",
                     "properties": {"code": {"type": "string"}},
                     "required": ["code"]}},

    {"name": "canvas_patch",
     "description": "Remplace precisement un fragment unique. Equivalent str_replace Claude Code.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "old_str": {"type": "string", "description": "Fragment exact a remplacer"},
                         "new_str": {"type": "string", "description": "Fragment de remplacement"}},
                     "required": ["old_str", "new_str"]}},

    {"name": "canvas_exec",
     "description": "Execute le canvas Python (subprocess isole, timeout 10s).",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"openWorldHint": True}},

    {"name": "canvas_screenshot",
     "description": "Capture le panneau de rendu du widget (html2canvas). Timeout 15s. Retourne image/png base64.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True}},

    {"name": "artefact_init",
     "description": "Cree la table Artefacts (9 colonnes) si elle n existe pas. Appeler apres sessions_list.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"idempotentHint": True}},

    {"name": "grist_schema",
     "description": "Schema complet du document : tables, colonnes, types, formules, refs.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True}},

    {"name": "grist_records",
     "description": "Enregistrements d une table (max 100). Supporte filtre et tri.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "table_id": {"type": "string"},
                         "limit":    {"type": "integer", "default": 50},
                         "filter":   {"type": "object",  "description": "Ex: {\"Statut\": [\"actif\"]}"},
                         "sort":     {"type": "string",  "description": "Ex: 'Score' ou '-Score'"}},
                     "required": ["table_id"]},
     "annotations": {"readOnlyHint": True}},

    {"name": "grist_sql",
     "description": "Requete SELECT SQLite. Ideal pour agregations et jointures cross-tables.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "query": {"type": "string"},
                         "args":  {"type": "array", "items": {}}},
                     "required": ["query"]},
     "annotations": {"readOnlyHint": True}},

    {"name": "grist_records_add",
     "description": "Cree de nouvelles lignes dans une table Grist.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "table_id": {"type": "string"},
                         "records":  {"type": "array", "items": {"type": "object"},
                                      "description": "Liste de {fields: {Col: val}}"}},
                     "required": ["table_id", "records"]}},

    {"name": "grist_records_patch",
     "description": "Met a jour des lignes existantes par leur id.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "table_id": {"type": "string"},
                         "records":  {"type": "array", "items": {"type": "object"},
                                      "description": "Liste de {id: rowId, fields: {Col: val}}"}},
                     "required": ["table_id", "records"]}},

    {"name": "grist_upsert",
     "description": "Cree ou met a jour des lignes selon une cle metier (require). Ideal pour sync artefacts.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "table_id": {"type": "string"},
                         "records":  {"type": "array", "items": {"type": "object"},
                                      "description": "Liste de {require: {key_col: val}, fields: {Col: val}}"}},
                     "required": ["table_id", "records"]}},
]

# ── PROMPTS ───────────────────────────────────────────────────────────────────

PROMPTS = [
    {"name": "explore-doc",
     "description": "Explorer le schema et les donnees du document Grist actif.",
     "arguments": []},
    {"name": "build-app",
     "description": "Construire une app complete (schema + artefacts).",
     "arguments": [{"name": "description", "description": "Description de l app a construire", "required": True}]},
    {"name": "write-artefact",
     "description": "Creer un artefact HTML/JS pour une table donnee.",
     "arguments": [
         {"name": "nom",      "description": "Nom de l artefact",                     "required": True},
         {"name": "type",     "description": "Type: grist|html|react|markdown",        "required": True},
         {"name": "objectif", "description": "Ce que l artefact doit faire",            "required": True}]},
    {"name": "patch-canvas",
     "description": "Modifier le canvas existant de facon ciblee.",
     "arguments": [{"name": "modification", "description": "Ce qui doit changer", "required": True}]},
    {"name": "debug-canvas",
     "description": "Deboguer le canvas (exec -> analyse -> patch jusqu a returncode 0).",
     "arguments": []},
    {"name": "design-schema",
     "description": "Concevoir le schema relationnel Grist pour un domaine metier.",
     "arguments": [{"name": "domaine", "description": "Domaine metier (ex: patrimoine, stock, RH)", "required": True}]},
]

def _prompt_messages(name, args):
    if name == "explore-doc":
        return [{"role": "user", "content": {"type": "text", "text": (
            "Explore le document Grist actif.\n"
            "1. sessions_list() -> identifier la session\n"
            "2. grist_schema() -> toutes les tables, colonnes, types, refs\n"
            "3. grist_records(table, limit=5) pour chaque table principale\n"
            "4. grist_sql() pour explorer les relations\n"
            "5. session_info() -> artefacts existants\n"
            "Resumer : modele de donnees, relations, artefacts, suggestions."
        )}}]
    if name == "build-app":
        desc = args.get("description", "une application")
        return [{"role": "user", "content": {"type": "text", "text": (
            f"Construire : {desc}\n\n"
            "1. artefact_init() -> table Artefacts\n"
            "2. grist_schema() -> tables existantes\n"
            "3. resources/read grist-coder://docs/app-patterns -> architecture + schema\n"
            "4. resources/read grist-coder://docs/widget-patterns -> patterns code\n"
            "5. Concevoir le schema relationnel (tables, Ref:, Choice, formules)\n"
            "6. Creer les tables via POST /tables REST :\n"
            "   -> Tables sans refs d abord, puis Ref:TableId, puis configurer visibleCol\n"
            "7. Pour chaque artefact :\n"
            "   a. canvas_write(htmlCode) -> ecrire\n"
            "   b. grist_upsert('Artefacts', [{require:{Nom:'...'}, fields:{...}}])\n"
            "   c. canvas_screenshot() -> valider le rendu\n"
            "8. session_info() -> verifier l etat final"
        )}}]
    if name == "write-artefact":
        nom = args.get("nom", "MonArtefact")
        typ = args.get("type", "grist")
        obj = args.get("objectif", "afficher des donnees")
        return [{"role": "user", "content": {"type": "text", "text": (
            f"Creer artefact Nom='{nom}' Type='{typ}' : {obj}\n\n"
            "1. grist_schema() -> tables disponibles\n"
            "2. resources/read grist-coder://docs/widget-patterns -> pattern pour ce type\n"
            f"3. canvas_write(code_{typ}) -> coder l artefact complet\n"
            "   -> Inclure appContext, safeLoad, etats UI (loading/empty/error/success)\n"
            f"4. grist_upsert('Artefacts', [{{require:{{Nom:'{nom}'}}, fields:{{Type:'{typ}',Code:canvas,...}}}}])\n"
            "5. canvas_screenshot() -> valider"
        )}}]
    if name == "patch-canvas":
        mod = args.get("modification", "ameliorer")
        return [{"role": "user", "content": {"type": "text", "text": (
            f"Modification : {mod}\n\n"
            "1. canvas_read() -> lire le code actuel\n"
            "2. canvas_patch(old_str, new_str) pour chaque modification\n"
            "3. canvas_screenshot() -> valider le rendu"
        )}}]
    if name == "debug-canvas":
        return [{"role": "user", "content": {"type": "text", "text": (
            "1. canvas_read()\n"
            "2. canvas_exec() -> voir l erreur\n"
            "3. canvas_patch() -> corriger\n"
            "4. canvas_exec() -> repeter jusqu a returncode 0"
        )}}]
    if name == "design-schema":
        dom = args.get("domaine", "metier")
        return [{"role": "user", "content": {"type": "text", "text": (
            f"Concevoir le schema relationnel Grist pour : {dom}\n\n"
            "1. resources/read grist-coder://docs/grist-api -> types colonnes, Ref:, visibleCol\n"
            "2. resources/read grist-coder://docs/app-patterns -> patterns schema\n"
            "3. grist_schema() -> tables existantes\n"
            "4. Proposer le schema complet (tables, colonnes, types, refs, formules)\n"
            "5. Creer les tables dans le bon ordre via grist_records_add sur /tables"
        )}}]
    return [{"role": "user", "content": {"type": "text", "text": f"Prompt '{name}' inconnu."}}]

# ── RESOURCES ─────────────────────────────────────────────────────────────────

STATIC_RESOURCES = [
    {"uri": "grist-coder://docs/guide",
     "name": "Guide Grist Coder v5",
     "description": "Workflow complet, tools, concept document=codebase.",
     "mimeType": "text/plain"},
    {"uri": "grist-coder://docs/grist-api",
     "name": "Grist API Reference",
     "description": "REST schema (tables, colonnes, Ref:, visibleCol) + Widget API + SQL cross-tables.",
     "mimeType": "text/plain"},
    {"uri": "grist-coder://docs/widget-patterns",
     "name": "Widget Patterns",
     "description": "Coder un artefact : appContext, safeLoad, types (grist/html/react/markdown), toast, UI states.",
     "mimeType": "text/plain"},
    {"uri": "grist-coder://docs/app-patterns",
     "name": "App Patterns",
     "description": "Schema relationnel, manifeste app, routing par Nom, inter-artefacts, recette patrimoine.",
     "mimeType": "text/plain"},
]

RESOURCE_TEMPLATES = [
    {"uriTemplate": "grist-coder://canvas/{token}",
     "name": "Canvas de la session",
     "description": "Code actuel du canvas (subscribable).",
     "mimeType": "text/x-python"},
    {"uriTemplate": "grist-coder://schema/{token}",
     "name": "Schema Grist de la session",
     "mimeType": "application/json"},
    {"uriTemplate": "grist-coder://artefacts/{token}",
     "name": "Artefacts IsDoc du projet",
     "description": "Artefacts IsDoc=true du document actif avec leur code (contexte vivant).",
     "mimeType": "text/markdown"},
]

# ── RESOURCE CONTENT ──────────────────────────────────────────────────────────

GUIDE = """GRIST CODER MCP v5 - Guide
===========================
CONCEPT : Le document Grist = ta codebase.
  Tables donnees (Batiments...)  = modele metier
  Table Artefacts (HTML/JS)      = fichiers source des widgets
  Schema relationnel (Ref:, formules) = architecture

ANALOGIE
  canvas_read()          = read_file()
  canvas_patch(old, new) = str_replace()   TOUJOURS PREFERER
  canvas_write(code)     = write_file()
  canvas_exec()          = bash("python ...")
  grist_schema()         = ls schema
  grist_records(table)   = cat data
  grist_sql(query)       = SELECT cross-tables
  grist_upsert(t, rs)    = INSERT OR UPDATE sur cle metier
  artefact_init()        = initialiser le projet (cree table Artefacts)
  canvas_screenshot()    = valider le rendu visuellement

WORKFLOW COMPLET
  1. sessions_list()          -> quels docs sont ouverts ?
  2. artefact_init()          -> creer/verifier table Artefacts
  3. grist_schema()           -> tables existantes
  4. resources/read widget-patterns + app-patterns
  5. Concevoir schema relationnel -> creer tables dans le bon ordre
  6. canvas_write/patch() -> coder chaque artefact
  7. grist_upsert('Artefacts', ...) -> persister
  8. canvas_screenshot() -> valider visuellement
  9. session_info() -> etat du projet

REGLES
  - canvas_read() AVANT canvas_patch() : old_str exact
  - Creer tables sans Ref: d abord, puis Ref:TableId, puis visibleCol
  - Chaque patch SSE -> widget mis a jour en temps reel"""

GRIST_API = """GRIST API REFERENCE v5
=======================

== CREER DES TABLES (REST) ==
POST {siteUrl}/api/docs/{docId}/tables
{
  "tables": [{
    "id": "Batiments",
    "columns": [
      {"id": "Nom",        "fields": {"type": "Text",    "label": "Nom"}},
      {"id": "Adresse",    "fields": {"type": "Text",    "label": "Adresse"}},
      {"id": "Surface_m2", "fields": {"type": "Numeric", "label": "Surface m2"}},
      {"id": "Statut",     "fields": {"type": "Choice",  "label": "Statut",
        "widgetOptions": "{\"choices\":[\"Actif\",\"Inactif\",\"En travaux\"],\"choiceOptions\":{\"Actif\":{\"fillColor\":\"#dcfce7\"},\"Inactif\":{\"fillColor\":\"#f1f5f9\"}}}"
      }},
      {"id": "NbLocaux",   "fields": {"type": "Int", "label": "Nb Locaux",
        "formula": "len($locaux_Batiment)", "isFormula": true
      }}
    ]
  }]
}

TYPES DE COLONNES GRIST
  Text, Int, Numeric, Bool, Date, DateTime  -> types de base
  Choice       -> type + widgetOptions.choices (array) + widgetOptions.choiceOptions
  ChoiceList   -> choix multiples
  Ref:TableId  -> reference a une ligne d une autre table (stocke un entier = rowId)
  RefList:TableId -> references multiples
  Attachments  -> pieces jointes

COLONNES REFERENCE (Ref:) - ORDRE CRITIQUE
Phase 1 : creer les tables sans Ref: (tables independantes)
  POST /tables -> {id: "Batiments", columns: [...sans refs...]}
  POST /tables -> {id: "Prestataires", columns: [...sans refs...]}

Phase 2 : creer les tables avec Ref: (tables dependantes)
  POST /tables -> {id: "Interventions", columns: [
    {"id": "Batiment",    "fields": {"type": "Ref:Batiments",    "label": "Batiment"}},
    {"id": "Prestataire", "fields": {"type": "Ref:Prestataires", "label": "Prestataire"}}
  ]}

Phase 3 : configurer visibleCol (quelle colonne afficher dans les listes)
  PATCH /tables/Interventions/columns
  {"columns": [
    {"id": "Batiment",    "fields": {"visibleCol": "Nom"}},
    {"id": "Prestataire", "fields": {"visibleCol": "Nom"}}
  ]}

COLONNES FORMULE
  {"id": "NomComplet", "fields": {
    "type": "Text",
    "formula": "$Prenom + ' ' + $Nom",
    "isFormula": true
  }}
  Formules Grist = Python avec acces $ aux colonnes
  References : $Batiment.Nom, $Batiment.Surface_m2
  Lookups : $locaux_Batiment (auto cree pour Ref:Batiments dans Locaux)
  Agregations : SUM($locaux_Batiment.Surface), len($locaux_Batiment)

AJOUTER UNE COLONNE APRES CREATION DE TABLE
  POST /tables/{t}/columns
  {"columns": [{"id": "Urgence", "fields": {"type": "Choice", "label": "Urgence",
    "widgetOptions": "{\"choices\":[\"Critique\",\"Haute\",\"Moyenne\",\"Basse\"]}"
  }}]}

== ACTIONS BATCH (applyUserActions dans widget) ==
await grist.docApi.applyUserActions([
  ['AddTable', 'NomTable', [
    {id: 'Colonne', fields: {type: 'Text', label: 'Label'}}
  ]],
  ['AddRecord',    'Table', null, {Col: 'val'}],
  ['UpdateRecord', 'Table', rowId, {Col: 'new'}],
  ['RemoveRecord', 'Table', rowId],
  ['BulkUpdateRecord', 'Table', [id1,id2], {Col: ['v1','v2']}]
])

== WIDGET API (grist-plugin-api.js) ==
grist.ready({ requiredAccess: 'full' | 'read table' | 'none' })
grist.onRecords((records, mappedCols) => {})
grist.onRecord((record, mappedCols) => {})
grist.docApi.fetchTable('TableId')  -> {id:[...], Col1:[...], Col2:[...]}
grist.docApi.applyUserActions([...])
grist.setCursorPos({rowId, tableId})
grist.setSelectedRows([1, 2, 3])

const tok = await grist.docApi.getAccessToken({readOnly: false})
fetch(tok.baseUrl + '/tables/T/records', {
  method: 'POST',
  headers: {Authorization: 'Bearer ' + tok.token},
  body: JSON.stringify({records: [{fields: {Col: val}}]})
})

== REST API EXTERNE ==
GET  /tables                            -> liste des tables
GET  /tables/{t}/records?limit=100&filter={"Col":["val"]}&sort=Col
GET  /tables/{t}/columns                -> colonnes + types + formules
POST /sql {"sql": "SELECT ...", "args": ["val"]}
POST  /tables/{t}/records               -> INSERT {"records":[{"fields":{...}}]}
PATCH /tables/{t}/records               -> UPDATE {"records":[{"id":5,"fields":{...}}]}
PUT   /tables/{t}/records               -> UPSERT {"records":[{"require":{key:val},"fields":{...}}]}

== SQL CROSS-TABLES ==
La colonne Ref: stocke l id entier de la ligne referencee.
SELECT B.Nom, COUNT(I.id) as nb_interventions
FROM Interventions I JOIN Batiments B ON I.Batiment = B.id
GROUP BY B.Nom ORDER BY nb_interventions DESC

SELECT B.Nom, B.Surface_m2, SUM(L.Surface) as surface_locaux
FROM Locaux L JOIN Batiments B ON L.Batiment = B.id
GROUP BY B.id

SELECT * FROM Interventions
WHERE Urgence IN ('Critique','Haute') AND Statut != 'Terminee'
ORDER BY UpdatedAt DESC"""

WIDGET_PATTERNS = """WIDGET PATTERNS - Developpement d artefacts v5
===============================================

1. TABLE ARTEFACTS (schema 9 colonnes)
   Nom (Text)          Identifiant unique stable (cle metier pour upsert)
   Type (Choice)       app | grist | html | react | markdown | svg | mermaid | python | sql | component
   Code (Text)         Code source complet de l artefact
   Description (Text)  Description fonctionnelle (pour IsDoc)
   Dependencies (Text) JSON array des Nom d artefacts requis
   IsDoc (Bool)        true -> inclus dans session_info + grist-coder://artefacts/{token}
   Icon (Text)         Emoji affiche dans le widget
   Output (Text)       Sortie generee (types python/sql)
   UpdatedAt (DateTime) Timestamp Unix de derniere modification

2. PATTERN DE BASE - Tous les types d artefacts
const appContext = {
    isGristCoder: typeof window.app !== 'undefined' && typeof window.app.navigate === 'function',
    app: window.app || {
        navigate: (path) => showToast('Ouvrez "' + path + '" dans Grist', 'info'),
        emit: () => {}, on: () => {}, setState: () => {}, state: {}
    }
};
// window.grist = API native OU proxy GristBridge transparent selon le contexte

3. safeLoad(tableName) - Lecture resiliente (toujours utiliser ce pattern)
async function safeLoad(tableName) {
    try {
        const data = await grist.docApi.fetchTable(tableName);
        const n = data.id?.length || 0;
        const records = [];
        for (let i = 0; i < n; i++) {
            const r = { id: data.id[i] };
            Object.keys(data).forEach(k => { if (k !== 'id') r[k] = data[k][i]; });
            records.push(r);
        }
        return records;
    } catch (e) { console.warn('Table ' + tableName + ' non trouvee'); return []; }
}

4. PATTERN TYPE: grist - Widget reactif avec donnees Grist
<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8">
<script src="https://docs.getgrist.com/grist-plugin-api.js"></script>
<script src="https://cdn.tailwindcss.com"></script></head><body>
<div id="app" class="p-4"></div>
<script>
const appContext = {
    isGristCoder: typeof window.app !== 'undefined' && typeof window.app.navigate === 'function',
    app: window.app || { navigate: p => showToast('Ouvrir: '+p,'info'), emit:()=>{}, on:()=>{}, setState:()=>{}, state:{} }
};
async function safeLoad(t) {
    try {
        const d = await grist.docApi.fetchTable(t);
        const n = d.id?.length||0; const r=[];
        for(let i=0;i<n;i++){const o={id:d.id[i]};Object.keys(d).forEach(k=>{if(k!=='id')o[k]=d[k][i];});r.push(o);}
        return r;
    } catch(e){return[];}
}
let state = { loading: true, error: null, data: [] };
async function loadData() { state.data = await safeLoad('MaTable'); state.loading = false; render(); }
function render() {
    const app = document.getElementById('app');
    if (state.loading) { app.innerHTML = '<div class="text-gray-500">Chargement...</div>'; return; }
    if (state.error)   { app.innerHTML = '<div class="text-red-500">'+state.error+'</div>'; return; }
    if (!state.data.length) { app.innerHTML = '<div class="text-gray-400 text-center py-8">Aucune donnee</div>'; return; }
    app.innerHTML = state.data.map(r => '<div class="p-3 border-b hover:bg-gray-50">'+(r.Nom||r.id)+'</div>').join('');
}
function showToast(msg, type='info') {
    const colors={info:'#3b82f6',success:'#10b981',error:'#ef4444',warning:'#f59e0b'};
    const t=document.createElement('div');
    t.style.cssText='position:fixed;bottom:20px;right:20px;padding:12px 20px;background:'+colors[type]+';color:#fff;border-radius:8px;font-size:13px;z-index:9999;box-shadow:0 4px 12px rgba(0,0,0,.2)';
    t.textContent=msg; document.body.appendChild(t); setTimeout(()=>t.remove(),3500);
}
grist.ready({ requiredAccess: 'read table' });
loadData();
</script></body></html>

5. PATTERN TYPE: html - UI independante
// Tailwind CDN, Chart.js, etc. selon besoin
// window.app disponible si isGristCoder (navigation, evenements)
// Pas de grist.ready() requis si aucune donnee Grist

6. PATTERN TYPE: react - React + Babel CDN
// Dans head :
// <script src="https://unpkg.com/react@18/umd/react.production.min.js"></script>
// <script src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js"></script>
// <script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>
// <script type="text/babel">
// const App = () => { const [data, setData] = React.useState([]); ... };
// ReactDOM.createRoot(document.getElementById('root')).render(<App />);
// </script>

7. NAVIGATION inter-artefacts
function navigateTo(path) {
    if (appContext.isGristCoder) appContext.app.navigate(path);
    else showToast('Ouvrez "' + path + '" dans un panneau Grist', 'info');
}
// Emettre un evenement :
appContext.app.emit('batiment-select', { id: 42, nom: 'Mairie' });
// Ecouter un evenement :
appContext.app.on('batiment-select', ({ id, nom }) => { filtrerParBatiment(id); });
// Etat partage :
appContext.app.setState({ currentBatiment: 42 });
const current = appContext.app.state.currentBatiment;
// Si pas en mode grist-coder : ignorer les filtres inter-widgets
if (!appContext.isGristCoder) { _filteredId = null; }

8. BUILDCOL - Creer colonnes via applyUserActions
function buildColDef(col) {
    const def = { id: col.id, fields: { type: col.type } };
    if (col.label) def.fields.label = col.label;
    if (col.formula) { def.fields.formula = col.formula; def.fields.isFormula = true; }
    if (col.widgetOptions) def.fields.widgetOptions = JSON.stringify(col.widgetOptions);
    return def;
}
buildColDef({ id: 'Urgence', type: 'Choice', label: 'Urgence', widgetOptions: {
    choices: ['Critique','Haute','Moyenne','Basse'],
    choiceOptions: { Critique:{fillColor:'#fee2e2'}, Haute:{fillColor:'#fef3c7'} }
}})

9. ETATS UI OBLIGATOIRES (loading -> empty -> error -> success)
function render() {
    const app = document.getElementById('app');
    if (state.loading) { app.innerHTML = renderLoading(); return; }
    if (state.error)   { app.innerHTML = renderError(state.error); return; }
    if (!state.data.length) { app.innerHTML = renderEmpty(); return; }
    app.innerHTML = renderData(state.data);
}"""

APP_PATTERNS = """APP PATTERNS - Architecture d apps completes v5
================================================

1. LE DOCUMENT GRIST = LA CODEBASE
Chaque document Grist est un projet complet :
  Tables de donnees    -> modele metier (Batiments, Interventions, Locaux...)
  Table Artefacts      -> code source des widgets (HTML/JS)
  Schema relationnel   -> architecture (Ref:, formules, visibleCol, lookups)
L IA construit le projet en 3 couches :
  Couche 1 : Schema (tables, colonnes, types, relations)
  Couche 2 : Artefacts (widgets HTML/JS qui lisent ce schema)
  Couche 3 : Manifeste (JSON qui orchestre les artefacts en app)

2. SCHEMA RELATIONNEL PATRIMOINE - Ordre de creation critique

Phase 1 - Tables sans refs (independantes) :
  Batiments     : Nom, Adresse, Surface_m2(Numeric), Statut(Choice), Annee(Int), DPE(Choice)
  Prestataires  : Nom, Specialite, Contact, Email
  CTR_Types     : Nom, Periodicite_mois(Int), Reglementaire(Bool)

Phase 2 - Tables avec refs (dependantes) :
  Locaux        : Nom, Surface(Numeric), Etage(Int), Batiment(Ref:Batiments)
  Interventions : Titre, Statut(Choice), Urgence(Choice),
                  Batiment(Ref:Batiments), Prestataire(Ref:Prestataires),
                  Date_debut(Date), Date_fin(Date), Cout(Numeric)
  CTR_Controles : Batiment(Ref:Batiments), Type(Ref:CTR_Types),
                  Date_dernier(Date), Date_prochain(Date), Resultat(Choice)

Phase 3 - Configurer visibleCol :
  PATCH /tables/Locaux/columns        -> Batiment.visibleCol = "Nom"
  PATCH /tables/Interventions/columns -> Batiment.visibleCol = "Nom", Prestataire.visibleCol = "Nom"
  PATCH /tables/CTR_Controles/columns -> Batiment.visibleCol = "Nom", Type.visibleCol = "Nom"

Formules Grist utiles :
  Batiments.NbLocaux       = "len($locaux_Batiment)"
  Batiments.Surface_totale = "SUM($locaux_Batiment.Surface)"
  Interventions.Duree_jours = "($Date_fin - $Date_debut).days if $Date_fin else None"

3. MANIFESTE APP (Type: app, IsDoc: true)
{
  "name": "Gestion Patrimoine",
  "icon": "🏛️",
  "theme": { "primary": "#10b981", "accent": "#3b82f6" },
  "layout": "sidebar",
  "routes": [
    { "path": "/",             "label": "Accueil",       "icon": "🏠", "artefact": "AccueilPatrimoine" },
    { "path": "/batiments",    "label": "Batiments",     "icon": "🏢", "artefact": "ListeBatiments" },
    { "path": "/interventions","label": "Interventions", "icon": "🔧", "artefact": "Interventions" },
    { "path": "/controles",    "label": "Controles",     "icon": "✅", "artefact": "Controles" }
  ],
  "tables": ["Batiments", "Locaux", "Interventions", "CTR_Controles"],
  "sharedState": { "currentBatiment": null }
}
routes[].artefact reference par Nom (string stable, pas id fragile).

4. window.app API (disponible dans tous les artefacts rendus en iframe)
window.app.navigate('/batiments')             -> Changer de route
window.app.emit('batiment-select', {id,nom})  -> Evenement inter-artefacts
window.app.on('batiment-select', callback)    -> Ecouter evenement
window.app.setState({ currentBatiment: 1 })  -> Etat partage global
window.app.state.currentBatiment             -> Lire etat
window.app.notify('Sauvegarde', 'success')   -> Toast dans widget parent

5. window.grist via GristBridge (transparent dans les iframes)
La balise script grist-plugin-api.js est remplacee automatiquement.
L API est identique - aucun changement a faire dans le code de l artefact :
  await grist.docApi.fetchTable('Batiments')
  await grist.docApi.applyUserActions([['UpdateRecord','Interventions',id,{Statut:'Terminee'}]])
  grist.onRecords(records => { /* reactif */ })

6. ARTEFACTS PATRIMOINE - 5 artefacts principaux

a) PatrimoineApp (Type: app, IsDoc: true)
   -> Manifeste JSON de l app (voir section 3)

b) AccueilPatrimoine (Type: grist)
   -> Dashboard : 4 KPIs (batiments, surface totale, interventions en cours, urgences)
   -> safeLoad(['Batiments','Interventions','CTR_Controles'])
   -> Alertes si interventions urgentes ou controles a venir (< 30 jours)
   -> Modules cards avec navigateTo(path)
   -> appContext.isGristCoder ? navigateTo('/batiments') : showToast(...)

c) ListeBatiments (Type: grist)
   -> Tableau filtrabe : Nom, Adresse, Surface_m2, Statut, DPE, NbLocaux
   -> Recherche texte + filtre Statut
   -> Clic ligne -> app.emit('batiment-select', {id, nom}) + setCursorPos
   -> grist.sql() : JOIN Locaux pour compter et sommer surfaces

d) Interventions (Type: grist)
   -> Kanban : colonnes Demandee | En cours | Terminee | Annulee
   -> Filtre par urgence (Critique en rouge, Haute en orange)
   -> app.on('batiment-select', ({id}) => filtrerParBatiment(id))
   -> Changement statut via applyUserActions UpdateRecord
   -> Formulaire ajout via modale

e) GUIDE_PATRIMOINE (Type: markdown, IsDoc: true)
   -> Documentation : tables, schema, workflow, contacts

7. COMMUNICATION INTER-ARTEFACTS - Exemple concret
// Dans ListeBatiments.html :
function selectBatiment(id, nom) {
    appContext.app.emit('batiment-select', { id, nom });
    appContext.app.setState({ currentBatiment: id });
    grist.setCursorPos({ rowId: id });  // synchronise aussi les widgets Grist natifs
}

// Dans Interventions.html :
let _filteredBatimentId = null;
appContext.app.on('batiment-select', ({ id }) => {
    _filteredBatimentId = id;
    render();
});
// Si pas en mode grist-coder : afficher tout
if (!appContext.isGristCoder) { _filteredBatimentId = null; }

8. WORKFLOW MCP COMPLET
1.  sessions_list()
2.  artefact_init()
3.  grist_schema()
4.  resources/read grist-coder://docs/grist-api
5.  Creer tables Phase 1 (sans refs) via grist_records_add sur POST /tables
6.  Creer tables Phase 2 (avec Ref:) via grist_records_add sur POST /tables
7.  Configurer visibleCol via grist_records_patch sur PATCH /tables/T/columns
8.  Pour chaque artefact :
    canvas_write(html) -> grist_upsert('Artefacts',[{require:{Nom:'...'},fields:{...}}]) -> canvas_screenshot()
9.  grist_upsert('Artefacts',[{require:{Nom:'PatrimoineApp'},fields:{Type:'app',Code:manifest,IsDoc:true}}])
10. session_info() + resources/read grist-coder://artefacts/{token}"""

# ── READ RESOURCE ─────────────────────────────────────────────────────────────

async def _read_resource(uid_key, mcp_sid, uri):
    if uri == "grist-coder://docs/guide":
        return {"uri": uri, "mimeType": "text/plain", "text": GUIDE}
    if uri == "grist-coder://docs/grist-api":
        return {"uri": uri, "mimeType": "text/plain", "text": GRIST_API}
    if uri == "grist-coder://docs/widget-patterns":
        return {"uri": uri, "mimeType": "text/plain", "text": WIDGET_PATTERNS}
    if uri == "grist-coder://docs/app-patterns":
        return {"uri": uri, "mimeType": "text/plain", "text": APP_PATTERNS}

    if uri.startswith("grist-coder://canvas/"):
        token = uri.split("/")[-1]
        ctx = registry.resolve(uid_key, token)
        if not ctx: raise ValueError(f"Session inconnue : {token}")
        ctx.subscribers.add(mcp_sid)
        return {"uri": uri, "mimeType": "text/x-python", "text": ctx.canvas or "# canvas vide\n"}

    if uri.startswith("grist-coder://schema/"):
        token = uri.split("/")[-1]
        ctx = registry.resolve(uid_key, token)
        if not ctx: raise ValueError(f"Session inconnue : {token}")
        try:    schema = await grist_get(ctx, "tables")
        except Exception as e: schema = {"error": str(e)}
        return {"uri": uri, "mimeType": "application/json",
                "text": json.dumps(schema, ensure_ascii=False, indent=2)}

    if uri.startswith("grist-coder://artefacts/"):
        token = uri.split("/")[-1]
        ctx = registry.resolve(uid_key, token)
        if not ctx: raise ValueError(f"Session inconnue : {token}")
        try:
            filt = json.dumps({"IsDoc": [True]})
            data = await grist_get(ctx, f"tables/Artefacts/records?filter={filt}")
            records = data.get("records", [])
            lines = [f"# Artefacts IsDoc — {ctx.doc_title}\n"]
            for r in records:
                f = r.get("fields", {})
                nom  = f.get("Nom", "")
                typ  = f.get("Type", "")
                desc = f.get("Description", "")
                code = f.get("Code", "")
                lines.append(f"## {nom} ({typ})")
                if desc: lines.append(f"_{desc}_\n")
                lines.append(f"```{typ}")
                lines.append(code)
                lines.append("```\n")
            return {"uri": uri, "mimeType": "text/markdown", "text": "\n".join(lines)}
        except Exception as e:
            return {"uri": uri, "mimeType": "text/plain",
                    "text": f"Table Artefacts non trouvee. Appeler artefact_init() d abord.\nErreur: {e}"}

    raise ValueError(f"Resource inconnue : {uri}")


def _resources_list(uid_key):
    res = list(STATIC_RESOURCES)
    for s in registry.list_sessions(uid_key):
        t, title = s["token"], s["doc_title"]
        sha = s.get("canvas_sha") or "vide"
        res += [
            {"uri": f"grist-coder://canvas/{t}", "name": f"Canvas — {title}",
             "description": f"Code actif ({sha}).", "mimeType": "text/x-python"},
            {"uri": f"grist-coder://schema/{t}", "name": f"Schema — {title}",
             "mimeType": "application/json"},
            {"uri": f"grist-coder://artefacts/{t}", "name": f"Artefacts IsDoc — {title}",
             "description": "Contexte vivant du projet.", "mimeType": "text/markdown"},
        ]
    return res

# ── TOOL CALL ─────────────────────────────────────────────────────────────────

_active_tokens: dict[str, str] = {}

async def call_tool(uid_key, mcp_sid, name, args):
    if name == "sessions_list":
        s = registry.list_sessions(uid_key)
        return s if s else {
            "info": "Aucun widget connecte.",
            "hint": "Ouvrez le widget Grist Coder dans Grist. Connexion automatique (pas de cle a saisir)."
        }

    if name == "session_select":
        ctx = registry.resolve(uid_key, args["token"])
        if not ctx: return {"error": f"Token inconnu : {args['token']}"}
        _active_tokens[mcp_sid] = ctx.token
        ctx.touch()
        return {"ok": True, "selected": ctx.meta()}

    token = _active_tokens.get(mcp_sid)
    ctx   = registry.resolve(uid_key, token)
    if not ctx: return {"error": "Aucune session active. Appeler sessions_list() d abord."}
    ctx.touch()

    if name == "session_info":
        info = ctx.meta()
        try:
            schema = await grist_get(ctx, "tables")
            info["tables"] = [t["id"] for t in schema.get("tables", [])]
        except Exception as e:
            info["tables"] = []; info["grist_error"] = str(e)
        if "Artefacts" in info.get("tables", []):
            try:
                filt = json.dumps({"IsDoc": [True]})
                data = await grist_get(ctx, f"tables/Artefacts/records?filter={filt}")
                recs = data.get("records", [])
                info["artefacts_count"] = len(recs)
                info["artefacts_doc"] = [
                    {"nom":  r["fields"].get("Nom", ""),
                     "type": r["fields"].get("Type", ""),
                     "description": r["fields"].get("Description", "")}
                    for r in recs
                ]
                info["hint"] = f"Lire grist-coder://artefacts/{ctx.token} pour le code complet"
            except Exception:
                info["artefacts_count"] = 0
        return info

    # ── Canvas
    if name == "canvas_read":
        return ctx.canvas or "# canvas vide\n"

    if name == "canvas_write":
        ctx.canvas = args["code"]
        sha = hashlib.sha1(ctx.canvas.encode()).hexdigest()[:8]
        ctx.history.appendleft({"ts": time.time(), "sha": sha, "op": "write"})
        _push(uid_key, {"type": "canvas_updated", "token": ctx.token, "sha": sha})
        _notify_resource(uid_key, f"grist-coder://canvas/{ctx.token}")
        return {"ok": True, "sha": sha}

    if name == "canvas_patch":
        old, new = args["old_str"], args["new_str"]
        if old not in ctx.canvas:
            return {"error": f"Fragment introuvable : {old!r:.80}"}
        ctx.canvas = ctx.canvas.replace(old, new, 1)
        sha = hashlib.sha1(ctx.canvas.encode()).hexdigest()[:8]
        ctx.history.appendleft({"ts": time.time(), "sha": sha, "op": "patch"})
        _push(uid_key, {"type": "canvas_patched", "token": ctx.token, "sha": sha})
        _notify_resource(uid_key, f"grist-coder://canvas/{ctx.token}")
        return {"ok": True, "sha": sha}

    if name == "canvas_exec":
        if not ctx.canvas.strip():
            return {"stdout": "", "stderr": "canvas vide", "returncode": 1}
        try:
            r = subprocess.run([sys.executable, "-c", ctx.canvas],
                               capture_output=True, text=True, timeout=10)
            return {"stdout": r.stdout[-3000:], "stderr": r.stderr[-1000:],
                    "returncode": r.returncode}
        except subprocess.TimeoutExpired:
            return {"error": "timeout 10s"}

    if name == "canvas_screenshot":
        loop = asyncio.get_event_loop()
        fut  = loop.create_future()
        _screenshot_waiters[ctx.token] = fut
        _push(uid_key, {"type": "screenshot_request", "token": ctx.token})
        try:
            image_b64 = await asyncio.wait_for(fut, timeout=15.0)
            raw = image_b64.split(",")[1] if "," in image_b64 else image_b64
            return {"ok": True, "_image_b64": raw, "mime": "image/png"}
        except asyncio.TimeoutError:
            return {"error": "Timeout 15s : widget ferme ou html2canvas non disponible."}
        finally:
            _screenshot_waiters.pop(ctx.token, None)

    # ── Artefact
    if name == "artefact_init":
        try:
            tables_data = await grist_get(ctx, "tables")
            table_ids   = [t["id"] for t in tables_data.get("tables", [])]
            if "Artefacts" in table_ids:
                cols = await grist_get(ctx, "tables/Artefacts/columns")
                col_ids = [c["id"] for c in cols.get("columns", [])]
                return {"ok": True, "status": "exists", "columns": col_ids,
                        "hint": "Table Artefacts existante. Utilisez grist_upsert pour ajouter/modifier des artefacts."}
        except Exception:
            pass
        try:
            await grist_post(ctx, "tables", ARTEFACTS_TABLE_DEF)
            return {"ok": True, "status": "created",
                    "columns": ["Nom","Type","Code","Description","Dependencies","IsDoc","Icon","Output","UpdatedAt"],
                    "hint": "Table Artefacts creee. Lisez widget-patterns et app-patterns avant de coder."}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Grist lecture
    if name == "grist_schema":
        return await grist_get(ctx, "tables")

    if name == "grist_records":
        lim  = int(args.get("limit", 50))
        path = f"tables/{args['table_id']}/records?limit={lim}"
        if "filter" in args: path += f"&filter={json.dumps(args['filter'])}"
        if "sort"   in args: path += f"&sort={args['sort']}"
        data    = await grist_get(ctx, path)
        records = data.get("records", [])
        return {"records": records[:lim], "total": len(records), "returned": min(len(records), lim)}

    if name == "grist_sql":
        body = {"sql": args["query"]}
        if "args" in args: body["args"] = args["args"]
        return await grist_post(ctx, "sql", body)

    # ── Grist ecriture
    if name == "grist_records_add":
        result = await grist_post(ctx, f"tables/{args['table_id']}/records",
                                  {"records": args["records"]})
        ids = [r["id"] for r in result.get("records", [])]
        return {"ok": True, "created": len(ids), "ids": ids}

    if name == "grist_records_patch":
        await grist_patch(ctx, f"tables/{args['table_id']}/records",
                          {"records": args["records"]})
        return {"ok": True, "updated": len(args["records"])}

    if name == "grist_upsert":
        await grist_put(ctx, f"tables/{args['table_id']}/records",
                        {"records": args["records"]})
        return {"ok": True, "upserted": len(args["records"])}

    return {"error": f"outil inconnu : {name}"}

# ── DISPATCH ──────────────────────────────────────────────────────────────────

async def dispatch(uid_key, mcp_sid, method, params):
    if method == "initialize":
        return {
            "protocolVersion": MCP_VER,
            "serverInfo": {"name": "grist-coder", "version": "5.0.0",
                           "instructions": SERVER_INSTRUCTIONS},
            "capabilities": {
                "tools":     {"listChanged": False},
                "resources": {"subscribe": True, "listChanged": True},
                "prompts":   {"listChanged": False},
                "logging":   {},
            },
        }
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name   = params["name"]
        result = await call_tool(uid_key, mcp_sid, name, params.get("arguments", {}))
        if name == "canvas_screenshot" and isinstance(result, dict) and result.get("ok") and "_image_b64" in result:
            return {"content": [
                {"type": "image", "data": result["_image_b64"], "mimeType": "image/png"},
                {"type": "text",  "text": "Capture du panneau de rendu."},
            ]}
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}
    if method == "prompts/list":
        return {"prompts": PROMPTS}
    if method == "prompts/get":
        pname = params.get("name", "")
        p = next((p for p in PROMPTS if p["name"] == pname), None)
        if not p: raise ValueError(f"Prompt inconnu : {pname}")
        return {"description": p["description"],
                "messages":    _prompt_messages(pname, params.get("arguments", {}))}
    if method == "resources/list":
        return {"resources": _resources_list(uid_key),
                "resourceTemplates": RESOURCE_TEMPLATES}
    if method == "resources/read":
        content = await _read_resource(uid_key, mcp_sid, params.get("uri", ""))
        return {"contents": [content]}
    if method == "resources/subscribe":
        uri = params.get("uri", "")
        if uri.startswith("grist-coder://canvas/"):
            ctx = registry.resolve(uid_key, uri.split("/")[-1])
            if ctx: ctx.subscribers.add(mcp_sid)
        return {}
    if method == "resources/unsubscribe":
        uri = params.get("uri", "")
        if uri.startswith("grist-coder://canvas/"):
            ctx = registry.resolve(uid_key, uri.split("/")[-1])
            if ctx: ctx.subscribers.discard(mcp_sid)
        return {}
    if method in ("logging/setLevel", "ping"):
        return {}
    raise ValueError(f"methode inconnue : {method}")

# ── FASTAPI ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app):
    print(f"Grist Coder v5.0 · {HOST_URL}")
    print(f"  tools: {len(TOOLS)}  prompts: {len(PROMPTS)}")
    print(f"  resources: {len(STATIC_RESOURCES)} static + {len(RESOURCE_TEMPLATES)} templates")
    yield

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"], expose_headers=["Mcp-Session-Id"])

def _auth(auth):
    if not auth: return None
    parts = auth.split()
    return parts[1] if len(parts) == 2 and parts[0].lower() == "bearer" else None

async def _resolve_uid_key(raw_bearer, x_grist_site):
    if not raw_bearer: return None
    if raw_bearer.startswith("gc-"):
        return _token_to_uid.get(raw_bearer)
    uid_key = registry.get_uid_for_grist_key(raw_bearer)
    if uid_key:
        if x_grist_site:
            registry.provision(uid_key, grist_key=raw_bearer, site=x_grist_site.rstrip("/"))
        return uid_key
    site = (x_grist_site or "").rstrip("/")
    if site:
        profile = await fetch_grist_user_profile(site, raw_bearer)
        if profile and profile.get("id"):
            uid_key = f"uid:{profile['id']}"
            registry.provision(uid_key, grist_key=raw_bearer, site=site)
            return uid_key
    uid_key = f"key:{hashlib.sha1(raw_bearer.encode()).hexdigest()[:12]}"
    registry.provision(uid_key, grist_key=raw_bearer)
    return uid_key


@app.post("/mcp")
async def mcp_post(request: Request,
                   authorization: str | None = Header(default=None),
                   mcp_session_id: str | None = Header(default=None, alias="Mcp-Session-Id"),
                   x_grist_site: str | None = Header(default=None, alias="X-Grist-Site")):
    raw_bearer = _auth(authorization)
    if not raw_bearer:
        return JSONResponse({"error": "Authorization: Bearer requis"}, status_code=401)
    uid_key = await _resolve_uid_key(raw_bearer, x_grist_site)
    if not uid_key:
        return JSONResponse({"error": "Token inconnu ou expire. Rechargez le widget."}, status_code=401)
    mcp_sid  = mcp_session_id or str(uuid.uuid4())
    body     = await request.json()
    is_batch = isinstance(body, list)
    reqs     = body if is_batch else [body]
    responses = []
    for req in reqs:
        req_id = req.get("id")
        if req_id is None: continue
        try:
            result = await dispatch(uid_key, mcp_sid, req.get("method",""), req.get("params",{}))
            responses.append({"jsonrpc":"2.0","id":req_id,"result":result})
        except Exception as e:
            responses.append({"jsonrpc":"2.0","id":req_id,"error":{"code":-32603,"message":str(e)}})
    if not responses: return Response(status_code=202)
    payload = responses if is_batch else responses[0]
    return JSONResponse(payload, headers={"Mcp-Session-Id": mcp_sid})


@app.get("/mcp")
async def mcp_sse(request: Request,
                  authorization: str | None = Header(default=None),
                  mcp_session_id: str | None = Header(default=None, alias="Mcp-Session-Id")):
    raw_bearer = _auth(authorization)
    uid_key = None
    if raw_bearer:
        uid_key = await _resolve_uid_key(raw_bearer, None)
    if not uid_key:
        arto_token = request.query_params.get("token")
        if arto_token:
            uid_key = _token_to_uid.get(arto_token)
    if not uid_key:
        return Response("Authorization requis", status_code=401)
    sid = mcp_session_id or str(uuid.uuid4())
    _queues[sid] = asyncio.Queue(maxsize=64)
    async def stream():
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(_queues[sid].get(), timeout=20)
                    if ev.get("_user") != uid_key: continue
                    if ev.get("type") == "mcp_notification":
                        yield f"data: {json.dumps({'jsonrpc':'2.0','method':ev['method'],'params':ev['params']})}\n\n"
                    else:
                        yield f"data: {json.dumps(ev)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            _queues.pop(sid, None)
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Mcp-Session-Id": sid})


@app.delete("/mcp")
async def mcp_delete(mcp_session_id: str | None = Header(default=None, alias="Mcp-Session-Id")):
    _queues.pop(mcp_session_id, None)
    return Response(status_code=204)


@app.post("/register")
async def register(request: Request):
    data         = await request.json()
    access_token = data.get("accessToken", "").strip()
    grist_key    = data.get("gristKey", "").strip()
    site_url     = data.get("siteUrl", "").rstrip("/")
    doc_id       = data.get("docId", "")
    doc_title    = data.get("docTitle", "") or doc_id
    bearer = access_token or grist_key
    if not bearer:
        return JSONResponse({"error": "accessToken manquant"}, status_code=400)
    grist_user_id = None
    if access_token:
        payload = _jwt_payload(access_token)
        raw = payload.get("userId")
        if raw is not None:
            try: grist_user_id = int(raw)
            except (ValueError, TypeError): pass
    if not grist_user_id:
        raw2 = data.get("userId")
        if raw2 is not None:
            try: grist_user_id = int(raw2)
            except (ValueError, TypeError): pass
    if not grist_user_id and grist_key and site_url:
        profile = await fetch_grist_user_profile(site_url, grist_key)
        if profile: grist_user_id = profile.get("id")
    if not grist_user_id:
        return JSONResponse({"error": "Impossible de verifier l identite Grist."}, status_code=401)
    uid_key = f"uid:{grist_user_id}"
    registry.provision(uid_key, grist_key=grist_key, access_token=access_token, site=site_url)
    ctx = registry.register_session(uid_key, doc_id, doc_title, site_url,
                                    grist_key=grist_key, access_token=access_token)
    _token_to_uid[ctx.token] = uid_key
    _push(uid_key, {"type": "widget_connected", "token": ctx.token,
                    "doc_id": ctx.doc_id, "doc_title": ctx.doc_title})
    _push(uid_key, {"type": "mcp_notification",
                    "method": "notifications/resources/list_changed", "params": {}})
    return {"ok": True, "token": ctx.token, "doc_title": ctx.doc_title,
            "gristUserId": grist_user_id}


@app.post("/screenshot")
async def screenshot(request: Request):
    data  = await request.json()
    token = data.get("token", "")
    image = data.get("image", "")
    fut   = _screenshot_waiters.pop(token, None)
    if fut and not fut.done():
        fut.set_result(image)
        return {"ok": True}
    return {"ok": False, "error": "Pas de waiter actif pour ce token."}


@app.get("/health")
async def health():
    total = sum(len(u["sessions"]) for u in registry._users.values())
    return {"ok": True, "version": "5.0.0", "mcp_protocol": MCP_VER,
            "sessions": total, "users": len(registry._users),
            "tools": len(TOOLS), "prompts": len(PROMPTS),
            "resources": {"static": len(STATIC_RESOURCES), "templates": len(RESOURCE_TEMPLATES)}}


@app.get("/", response_class=HTMLResponse)
async def widget_html(): return WIDGET

# ── WIDGET ────────────────────────────────────────────────────────────────────

WIDGET = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8"><title>Grist Coder v5</title>
<script src="https://docs.getgrist.com/grist-plugin-api.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/ace/1.32.6/ace.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/ace/1.32.6/mode-html.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/ace/1.32.6/mode-javascript.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/ace/1.32.6/mode-markdown.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/ace/1.32.6/mode-json.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/ace/1.32.6/theme-one_dark.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/html2canvas.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#fff;--bg2:#f8fafc;--bg3:#f1f5f9;
  --border:#e2e8f0;--text:#0f172a;--text2:#64748b;
  --accent:#3b82f6;--success:#10b981;--error:#ef4444;--warn:#f59e0b;
  --navy:#1e293b;
}
html,body{height:100%;overflow:hidden;font-family:system-ui,-apple-system,sans-serif;font-size:13px}
body{display:flex;flex-direction:column}

/* TOP BAR */
#topBar{height:40px;background:var(--navy);display:flex;align-items:center;gap:8px;padding:0 12px;flex-shrink:0}
.vdiv{width:1px;height:20px;background:rgba(255,255,255,.12)}
#artSelect{padding:3px 8px;border-radius:4px;border:1px solid rgba(255,255,255,.2);
  background:rgba(255,255,255,.06);color:rgba(255,255,255,.85);font-size:11px;min-width:160px;max-width:260px}
#artSelect option{background:#1e293b}
.type-badge{padding:2px 7px;border-radius:10px;font-size:10px;font-weight:600;text-transform:uppercase;display:none}
.tb-grist{background:#dcfce7;color:#166534}.tb-html{background:#fef3c7;color:#92400e}
.tb-react{background:#dbeafe;color:#1e40af}.tb-app{background:#f3e8ff;color:#6b21a8}
.tb-markdown{background:#fce7f3;color:#9d174d}.tb-default{background:var(--bg3);color:var(--text2)}
#dot{width:8px;height:8px;border-radius:50%;background:var(--error);transition:background .3s}
#dot.on{background:var(--success)}
#tokenBadge{font-size:10px;font-family:monospace;background:rgba(255,255,255,.08);
  border:1px solid rgba(255,255,255,.15);padding:2px 8px;border-radius:3px;
  color:rgba(255,255,255,.7);cursor:pointer;display:none}
#tokenBadge:hover{background:rgba(255,255,255,.15)}

/* LAYOUT */
#appBody{flex:1;display:flex;overflow:hidden}
#editorPane{flex:0 0 50%;display:flex;flex-direction:column;border-right:1px solid var(--border);min-width:180px}
#splitter{width:5px;background:var(--border);cursor:col-resize;flex-shrink:0;transition:background .15s}
#splitter:hover,#splitter.active{background:var(--accent)}
#previewPane{flex:1;display:flex;flex-direction:column;min-width:120px;background:var(--bg2)}

/* EDITOR PANE */
#editorBar{height:32px;background:var(--bg);border-bottom:1px solid var(--border);
  display:flex;align-items:center;gap:6px;padding:0 10px;flex-shrink:0}
#editorBar .btn{padding:3px 9px;border-radius:4px;border:1px solid var(--border);
  background:var(--bg);font-size:11px;cursor:pointer;white-space:nowrap}
#editorBar .btn:hover{background:var(--bg2)}
#editorBar .primary{background:var(--accent);color:#fff;border-color:var(--accent)}
#editorLabel{font-size:11px;color:var(--text2);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#aceEditor{flex:1}

/* PREVIEW PANE */
#previewBar{height:32px;background:var(--bg);border-bottom:1px solid var(--border);
  display:flex;align-items:center;gap:6px;padding:0 10px;flex-shrink:0;font-size:11px;color:var(--text2)}
#pdot{width:7px;height:7px;border-radius:50%;background:var(--success);flex-shrink:0}
#pdot.loading{background:var(--warn);animation:pulse 1s infinite}
#pdot.err{background:var(--error)}
#renderFrame{flex:1;border:none;background:#fff}

/* STATUS BAR */
#statusBar{height:22px;background:var(--navy);border-top:1px solid rgba(255,255,255,.06);
  display:flex;align-items:center;gap:16px;padding:0 12px;font-size:10px;
  color:rgba(255,255,255,.4);flex-shrink:0}

/* VIEW BUTTONS */
.vbtn{padding:3px 10px;border-radius:4px;border:1px solid rgba(255,255,255,.15);
  background:transparent;color:rgba(255,255,255,.6);font-size:11px;cursor:pointer;transition:all .15s}
.vbtn:hover{background:rgba(255,255,255,.08);color:#fff}
.vbtn.active{background:var(--accent);color:#fff;border-color:var(--accent)}

@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.toast{position:fixed;bottom:20px;right:20px;padding:10px 18px;border-radius:6px;color:#fff;
  font-size:12px;z-index:9999;box-shadow:0 4px 12px rgba(0,0,0,.2);
  animation:ti .2s ease;pointer-events:none}
@keyframes ti{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
</style>
</head>
<body>
<div id="topBar">
  <button class="vbtn active" onclick="setView('split',this)">Split</button>
  <button class="vbtn" onclick="setView('code',this)">Code</button>
  <button class="vbtn" onclick="setView('preview',this)">Rendu</button>
  <div class="vdiv"></div>
  <select id="artSelect" onchange="onArtSelect()"><option value="">— aucun —</option></select>
  <span id="typeBadge" class="type-badge"></span>
  <div style="margin-left:auto;display:flex;align-items:center;gap:8px">
    <div id="dot"></div>
    <span id="tokenBadge" onclick="copyToken()" title="Copier token MCP"></span>
  </div>
</div>
<div id="appBody">
  <div id="editorPane">
    <div id="editorBar">
      <span id="editorLabel">Canvas</span>
      <span id="saveStatus" style="font-size:10px;color:var(--text2)"></span>
    </div>
    <div id="aceEditor"></div>
  </div>
  <div id="splitter"></div>
  <div id="previewPane">
    <div id="previewBar">
      <div id="pdot"></div>
      <span id="previewLabel" style="flex:1">Rendu</span>
    </div>
    <iframe id="renderFrame" sandbox="allow-scripts allow-same-origin allow-forms allow-modals allow-popups"></iframe>
  </div>
</div>
<div id="statusBar">
  <span id="sbDoc">—</span>
  <span id="sbArts">0 artefacts</span>
  <span id="sbTables">0 tables</span>
  <span id="sbCanvas">vide</span>
</div>
<script>
// ═══════════════════════════════════════════════════════
// GRIST BRIDGE SCRIPT — injecte dans les iframes srcdoc
// Remplace window.grist par un proxy postMessage transparent
// ═══════════════════════════════════════════════════════
const GRIST_BRIDGE_SCRIPT =
  "(function(){" +
  "var _id=0,_p=new Map(),_cbs=new Map();" +
  "window.addEventListener('message',function(e){" +
    "var m=e.data;if(!m)return;" +
    "if(m.type==='grist-bridge-response'){" +
      "var p=_p.get(m.callbackId);" +
      "if(p){_p.delete(m.callbackId);" +
      "if(m.error)p.reject(new Error(m.error));else p.resolve(m.result);}}" +
    "if(m.type==='grist-bridge-callback'){" +
      "var cb=_cbs.get(m.callbackId);if(cb)cb.apply(null,m.args||[]);}" +
  "});" +
  "function call(a,args){return new Promise(function(res,rej){" +
    "var id=++_id;_p.set(id,{resolve:res,reject:rej});" +
    "var t=window.parent===window?window:window.parent;" +
    "t.postMessage({type:'grist-bridge',action:a,args:args,callbackId:id},'*');" +
    "setTimeout(function(){if(_p.has(id)){_p.delete(id);" +
    "rej(new Error('Bridge timeout'));}},30000);" +
  "})}" +
  "function reg(a,cb){var id=++_id;_cbs.set(id,cb);" +
    "var t=window.parent===window?window:window.parent;" +
    "t.postMessage({type:'grist-bridge',action:a,callbackId:id},'*');}" +
  "window.grist={" +
    "ready:function(){return Promise.resolve();}," +
    "docApi:{" +
      "fetchTable:function(n){return call('fetchTable',[n]);}," +
      "applyUserActions:function(a){return call('applyUserActions',[a]);}," +
      "listTables:function(){return call('listTables',[]);}," +
      "getAccessToken:function(o){return call('getAccessToken',[o||{}]);}" +
    "}," +
    "onRecords:function(cb){reg('onRecords',cb);}," +
    "onRecord:function(cb){reg('onRecord',cb);}," +
    "setCursorPos:function(p){return call('setCursorPos',[p]);}," +
    "setSelectedRows:function(ids){return call('setSelectedRows',[ids]);}" +
  "};" +
  "var t=window.parent===window?window:window.parent;" +
  "t.postMessage({type:'grist-bridge-ready'},'*');" +
  "})();";

// ═══════════════════════════════════════════════════════
// APP RUNTIME SCRIPT — injecte dans les iframes
// Fournit window.app (navigate, emit, on, setState, notify)
// ═══════════════════════════════════════════════════════
const APP_RUNTIME_SCRIPT =
  "(function(){" +
  "var _s=Object.assign({},window.__APP_STATE__||{});" +
  "var _ls={};" +
  "window.app={" +
    "navigate:function(p){window.parent.postMessage({type:'app-navigate',path:p},'*');}," +
    "emit:function(ev,d){window.parent.postMessage({type:'app-event',event:ev,data:d},'*');}," +
    "on:function(ev,cb){if(!_ls[ev])_ls[ev]=[];_ls[ev].push(cb);}," +
    "setState:function(p){Object.assign(_s,p);" +
      "window.parent.postMessage({type:'app-setState',state:p},'*');}," +
    "get state(){return _s;}," +
    "notify:function(m,t){window.parent.postMessage({type:'app-notification',msg:m,notifType:t||'info'},'*');}" +
  "};" +
  "window.addEventListener('message',function(e){" +
    "var m=e.data;if(!m)return;" +
    "if(m.type==='app-event-relay'&&_ls[m.event])" +
      "_ls[m.event].forEach(function(cb){cb(m.data);});" +
    "if(m.type==='app-state-update')Object.assign(_s,m.state);" +
  "});" +
  "if(window.__APP_ROUTE__)" +
    "window.parent.postMessage({type:'app-init',route:window.__APP_ROUTE__},'*');" +
  "})();";

// ═══════════════════════════════════════════════════════
// prepareWidgetHTML — Injecte bridge + runtime dans artefact
// ═══════════════════════════════════════════════════════
function prepareWidgetHTML(html, ctx) {
  ctx = ctx || {};
  var bridgeTag = '<script>' + GRIST_BRIDGE_SCRIPT + '<' + '/script>';
  html = html.replace(/<script[^>]*grist-plugin-api\\.js[^>]*><\\/script>/gi, bridgeTag);
  var init = '<script>window.__APP_STATE__=' + JSON.stringify(ctx.state || {}) +
    ';window.__APP_ROUTE__="' + (ctx.route || '/') + '";' +
    APP_RUNTIME_SCRIPT + '<' + '/script>';
  return html.includes('</head>') ? html.replace('</head>', init + '</head>') : init + html;
}

// ═══════════════════════════════════════════════════════
// GristBridgeParent — Relay postMessage -> Grist API
// ═══════════════════════════════════════════════════════
window.addEventListener('message', async function(e) {
  var m = e.data; if (!m) return;
  // Bridge calls
  if (m.type === 'grist-bridge') {
    var action = m.action, args = m.args||[], cid = m.callbackId, src = e.source;
    try {
      var result = null;
      if (action === 'fetchTable')        result = await grist.docApi.fetchTable(args[0]);
      else if (action === 'applyUserActions') result = await grist.docApi.applyUserActions(args[0]);
      else if (action === 'listTables')   result = await grist.docApi.listTables();
      else if (action === 'getAccessToken') result = await grist.docApi.getAccessToken(args[0]||{});
      else if (action === 'setCursorPos') grist.setCursorPos(args[0]);
      else if (action === 'setSelectedRows') grist.setSelectedRows(args[0]);
      else if (action === 'onRecords') {
        grist.onRecords(function(records, mapped) {
          src.postMessage({type:'grist-bridge-callback',callbackId:cid,args:[records,mapped]},'*');
        });
      } else if (action === 'onRecord') {
        grist.onRecord(function(record, mapped) {
          src.postMessage({type:'grist-bridge-callback',callbackId:cid,args:[record,mapped]},'*');
        });
      }
      src.postMessage({type:'grist-bridge-response',callbackId:cid,result:result},'*');
    } catch(err) {
      src.postMessage({type:'grist-bridge-response',callbackId:cid,error:err.message},'*');
    }
  }
  // App events
  if (m.type === 'app-navigate') showToast('Navigate: ' + m.path, 'info');
  if (m.type === 'app-event') {
    var frames = [document.getElementById('renderFrame')];
    frames.forEach(function(f) {
      if (f && f.contentWindow && f.contentWindow !== e.source)
        f.contentWindow.postMessage({type:'app-event-relay',event:m.event,data:m.data},'*');
    });
  }
  if (m.type === 'app-setState') Object.assign(_sharedState, m.state);
  if (m.type === 'app-notification') showToast(m.msg, m.notifType||'info');
});

// ═══════════════════════════════════════════════════════
// STATE
// ═══════════════════════════════════════════════════════
const BASE = window.location.origin;
var _registered=false, _registering=false, _token=null, _mcpId=1, _es=null, _refreshTimer=null;
var _artefacts=[], _artefactsMap={}, _currentArt=null, _sharedState={};
var _editor;

// ═══════════════════════════════════════════════════════
// ACE EDITOR
// ═══════════════════════════════════════════════════════
window.addEventListener('DOMContentLoaded', function() {
  _editor = ace.edit('aceEditor');
  _editor.setTheme('ace/theme/one_dark');
  _editor.setOptions({fontSize:'13px',tabSize:2,useSoftTabs:true,
    wrap:false,showPrintMargin:false});
  _editor.on('change', debounce(autoPreview, 1800));
  _editor.on('change', debounce(autoSave, 3000));
  initSplitter();
});

function setEditorMode(type) {
  var modes = {html:'html',grist:'html',react:'html',component:'html',
               markdown:'markdown',app:'json',python:'python',sql:'sql'};
  _editor.session.setMode('ace/mode/' + (modes[type] || 'html'));
}

// ═══════════════════════════════════════════════════════
// GRIST INIT + REGISTER
// ═══════════════════════════════════════════════════════
grist.ready({ requiredAccess: 'full' });
grist.on('message', async function(msg) {
  window._gristMsg = msg;
  if (!_registered) await doRegister(msg);
});

async function doRegister(msg) {
  if (_registering) return; _registering = true;
  try {
    var tk = await grist.docApi.getAccessToken({readOnly: false});
    var parts = (tk.baseUrl||'').split('/api/docs/');
    if (parts.length !== 2) { showToast('baseUrl invalide','error'); return; }
    var siteUrl = parts[0], docId = parts[1].split('/')[0];
    var docTitle = (msg && msg.docTitle) || document.title || docId;
    var userId = (msg && msg.userId) || null;
    if (!userId) {
      try {
        var b64 = tk.token.split('.')[1];
        if (b64) {
          var pl = JSON.parse(atob(b64.replace(/-/g,'+').replace(/_/g,'/')));
          var raw = pl.userId != null ? pl.userId : (pl.sub != null ? pl.sub : null);
          userId = raw !== null ? Number(raw)||null : null;
        }
      } catch(_) {}
    }
    var res = await fetch(BASE+'/register', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({accessToken:tk.token,docId,docTitle,siteUrl,userId})});
    var data = await res.json();
    if (data.error) { showToast('Erreur: '+data.error,'error'); return; }
    _registered = true; _token = data.token;
    document.getElementById('dot').className = 'on';
    var badge = document.getElementById('tokenBadge');
    badge.textContent = _token; badge.style.display = 'inline-block';
    document.getElementById('sbDoc').textContent = docTitle;
    listenSSE();
    await loadArtefacts();
    if (_refreshTimer) clearTimeout(_refreshTimer);
    var delay = Math.max((tk.ttlMsecs||3600000)*0.8, 60000);
    _refreshTimer = setTimeout(async function() {
      _registered = false;
      if (window._gristMsg) await doRegister(window._gristMsg);
    }, delay);
  } catch(e) { showToast('Connexion: '+e.message,'error'); }
  finally { _registering = false; }
}

// ═══════════════════════════════════════════════════════
// SSE
// ═══════════════════════════════════════════════════════
function listenSSE() {
  if (!_token) return;
  if (_es) { _es.close(); _es = null; }
  _es = new EventSource(BASE+'/mcp?token='+encodeURIComponent(_token));
  _es.onmessage = function(e) {
    try {
      var ev = JSON.parse(e.data);
      if (ev.token === _token) {
        if (ev.type === 'canvas_updated' || ev.type === 'canvas_patched') {
          loadCanvasIntoEditor().then(renderCurrent);
          document.getElementById('sbCanvas').textContent = ev.sha || 'updated';
        }
        if (ev.type === 'screenshot_request') captureScreenshot();
      }
    } catch(_) {}
  };
  _es.onerror = function() {
    if (_es) { _es.close(); _es = null; }
    _registered = false;
    setTimeout(async function() {
      if (window._gristMsg) await doRegister(window._gristMsg);
    }, 3000);
  };
}

// ═══════════════════════════════════════════════════════
// MCP TOOL CALL
// ═══════════════════════════════════════════════════════
async function tool(name, args) {
  if (!_token) throw new Error('Widget non connecte');
  var r = await fetch(BASE+'/mcp', {method:'POST',
    headers:{'Content-Type':'application/json','Authorization':'Bearer '+_token},
    body: JSON.stringify({jsonrpc:'2.0',id:_mcpId++,method:'tools/call',
                          params:{name:name,arguments:args||{}}})});
  var d = await r.json();
  if (d.error) throw new Error(d.error.message);
  return JSON.parse(d.result.content[0].text);
}

async function loadCanvasIntoEditor() {
  try {
    var code = await tool('canvas_read');
    if (_editor && typeof code === 'string' && code !== _editor.getValue()) {
      var pos = _editor.getCursorPosition();
      _editor.setValue(code, -1);
      _editor.moveCursorToPosition(pos);
    }
  } catch(e) { console.warn('canvas_read:', e.message); }
}

// ═══════════════════════════════════════════════════════
// ARTEFACTS
// ═══════════════════════════════════════════════════════
async function initArtefactsTable() {
  showToast('Initialisation table Artefacts…', 'info');
  await grist.docApi.applyUserActions([['AddTable', 'Artefacts', [
    {id:'Nom',          type:'Text'},
    {id:'Type',         type:'Choice',   widgetOptions:JSON.stringify({choices:['grist','html','react','app','markdown','python','sql']})},
    {id:'Code',         type:'Text'},
    {id:'Description',  type:'Text'},
    {id:'Dependencies', type:'Text'},
    {id:'IsDoc',        type:'Bool'},
    {id:'Icon',         type:'Text'},
    {id:'Output',       type:'Text'},
    {id:'UpdatedAt',    type:'DateTime:Europe/Paris'},
  ]]]);
  showToast('Table Artefacts créée ✓', 'success');
}

async function loadArtefacts() {
  var data;
  try {
    data = await grist.docApi.fetchTable('Artefacts');
  } catch(e) {
    // Table absente → initialisation automatique
    try {
      await initArtefactsTable();
      data = await grist.docApi.fetchTable('Artefacts');
    } catch(e2) {
      document.getElementById('sbArts').textContent = '0 artefacts';
      return;
    }
  }
  var n = (data.id||[]).length;
  _artefacts = []; _artefactsMap = {};
  for (var i=0; i<n; i++) {
    var art = {
      id:   data.id[i],
      Nom:  (data.Nom||[])[i]  || '',
      Type: (data.Type||[])[i] || 'html',
      Code: (data.Code||[])[i] || '',
      Icon: (data.Icon||[])[i] || '📄',
      Description: (data.Description||[])[i] || '',
      IsDoc: !!(data.IsDoc||[])[i],
    };
    _artefacts.push(art);
    if (art.Nom) _artefactsMap[art.Nom] = art;
  }
  renderArtSelect();
  document.getElementById('sbArts').textContent = _artefacts.length + ' artefacts';
  try {
    var s = await tool('grist_schema');
    var tables = (s.tables||[]).map(function(t){return t.id;});
    document.getElementById('sbTables').textContent = tables.length + ' tables';
  } catch(_) {}
  // Auto-sélectionner le premier artefact si aucun sélectionné
  if (_artefacts.length > 0 && !_currentArt) {
    document.getElementById('artSelect').value = _artefacts[0].Nom;
    await onArtSelect();
  }
}

function renderArtSelect() {
  var sel = document.getElementById('artSelect');
  var cur = sel.value;
  sel.innerHTML = '<option value="">— aucun —</option>' +
    _artefacts.map(function(a) {
      return '<option value="'+a.Nom+'">'+(a.Icon||'')+' '+a.Nom+' ('+a.Type+')</option>';
    }).join('');
  if (cur) sel.value = cur;
}

async function onArtSelect() {
  var nom = document.getElementById('artSelect').value;
  if (!nom) { _currentArt = null; setEditorHeader(null); return; }
  var art = _artefactsMap[nom];
  if (!art) return;
  _currentArt = art;
  _editor.setValue(art.Code || '', -1);
  setEditorMode(art.Type);
  setEditorHeader(art);
  try { await tool('canvas_write', {code: art.Code || ''}); } catch(_) {}
  renderArt(art);
}

function setEditorHeader(art) {
  var label = document.getElementById('editorLabel');
  var badge = document.getElementById('typeBadge');
  if (!art) { label.textContent = 'Canvas'; badge.style.display = 'none'; return; }
  label.textContent = art.Nom;
  badge.textContent = art.Type;
  badge.className = 'type-badge tb-' + (art.Type || 'default');
  badge.style.display = 'inline-block';
}

// ═══════════════════════════════════════════════════════
// RENDER
// ═══════════════════════════════════════════════════════
function renderArt(art) {
  if (!art) return;
  var type = art.Type || 'html';
  var code = art.Code || '';
  var frame = document.getElementById('renderFrame');
  var pdot = document.getElementById('pdot');
  pdot.className = 'loading';
  document.getElementById('previewLabel').textContent = art.Nom;
  if (['html','grist','react','component'].includes(type)) {
    frame.srcdoc = prepareWidgetHTML(code, {state: _sharedState, route: '/'});
  } else if (type === 'markdown') {
    frame.srcdoc = '<!DOCTYPE html><html><head><meta charset="UTF-8">' +
      '<style>body{font-family:system-ui,sans-serif;padding:24px;max-width:800px;margin:0 auto;line-height:1.6;color:#1e293b}h1,h2,h3{margin-top:1.5em}code{background:#f1f5f9;padding:2px 6px;border-radius:3px}pre{background:#282c34;color:#abb2bf;padding:16px;border-radius:6px;overflow-x:auto}</style>' +
      '</head><body><div id="c"></div>' +
      '<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"><' + '/script>' +
      '<script>document.getElementById("c").innerHTML=marked.parse(' + JSON.stringify(code) + ');<' + '/script>' +
      '</body></html>';
  } else if (type === 'svg') {
    frame.srcdoc = '<!DOCTYPE html><html><body style="margin:0;background:#f8fafc;display:flex;align-items:center;justify-content:center;min-height:100vh">' + code + '</body></html>';
  } else if (type === 'app') {
    try {
      var m = JSON.parse(code);
      var routes = (m.routes||[]).map(function(r) {
        return '<div style="padding:8px 16px;border-bottom:1px solid #e2e8f0">' +
          (r.icon||'')+'  <strong>'+r.label+'</strong> <span style="color:#94a3b8;font-size:11px">'+r.path+' → '+r.artefact+'</span></div>';
      }).join('');
      frame.srcdoc = '<!DOCTYPE html><html><head><meta charset="UTF-8"><script src="https://cdn.tailwindcss.com"><' + '/script></head>' +
        '<body class="bg-gray-50 min-h-screen p-8"><div class="max-w-lg mx-auto bg-white rounded-xl shadow p-6">' +
        '<div class="text-4xl mb-2">'+(m.icon||'🖥️')+'</div>' +
        '<h1 class="text-xl font-bold mb-1">'+(m.name||'App')+'</h1>' +
        '<p class="text-sm text-gray-500 mb-4">Manifeste · '+(m.routes||[]).length+' routes</p>' +
        '<div class="border rounded-lg overflow-hidden mb-4">'+routes+'</div>' +
        '<p class="text-xs text-gray-400">Tables: '+(m.tables||[]).join(', ')+'</p>' +
        '</div></body></html>';
    } catch(e) {
      frame.srcdoc = '<!DOCTYPE html><html><body style="padding:20px;color:#ef4444;font-family:monospace">JSON invalide: '+e.message+'</body></html>';
    }
  } else {
    var escaped = code.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    frame.srcdoc = '<!DOCTYPE html><html><head><meta charset="UTF-8">' +
      '<style>body{margin:0;background:#282c34;color:#abb2bf;font-family:monospace;font-size:12px}pre{padding:16px;white-space:pre-wrap}</style>' +
      '</head><body><pre>'+escaped+'</pre></body></html>';
  }
  frame.onload = function() { pdot.className = ''; };
  frame.onerror = function() { pdot.className = 'err'; };
}

function renderCurrent() {
  var code = _editor ? _editor.getValue() : '';
  renderArt(Object.assign({}, _currentArt || {Type:'html',Nom:'Canvas'}, {Code:code}));
}

function autoPreview() { renderCurrent(); }

// ═══════════════════════════════════════════════════════
// SAVE
// ═══════════════════════════════════════════════════════
async function saveArt() {
  if (!_currentArt) return;
  var code = _editor ? _editor.getValue() : '';
  if (code === _currentArt.Code) return;  // pas de changement
  var ss = document.getElementById('saveStatus');
  if (ss) ss.textContent = 'saving…';
  try {
    await tool('grist_upsert', {table_id:'Artefacts', records:[{
      require: {Nom: _currentArt.Nom},
      fields:  {Code: code, UpdatedAt: Math.floor(Date.now()/1000)}
    }]});
    _currentArt.Code = code;
    _artefactsMap[_currentArt.Nom].Code = code;
    if (ss) ss.textContent = 'saved ✓';
    setTimeout(function(){ if (ss) ss.textContent = ''; }, 2000);
  } catch(e) { if (ss) ss.textContent = 'err'; }
}

function autoSave() { saveArt(); }

// ═══════════════════════════════════════════════════════
// SCREENSHOT
// ═══════════════════════════════════════════════════════
async function captureScreenshot() {
  var frame = document.getElementById('renderFrame');
  try {
    var fdoc = frame.contentDocument || (frame.contentWindow && frame.contentWindow.document);
    if (!fdoc || !fdoc.body) return;
    // Injecter html2canvas dans l'iframe si absent
    if (!frame.contentWindow.html2canvas) {
      await new Promise(function(res, rej) {
        var s = fdoc.createElement('script');
        s.src = 'https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/html2canvas.min.js';
        s.onload = res; s.onerror = rej;
        fdoc.head.appendChild(s);
      });
    }
    // Attendre que les CDN (Tailwind etc.) aient fini d'appliquer les styles
    await new Promise(function(r){ setTimeout(r, 600); });
    // Capturer depuis l'interieur de l'iframe via postMessage
    var msgId = 'sc-' + Date.now();
    var image = await new Promise(function(res) {
      var timer = setTimeout(function(){ window.removeEventListener('message',h); res(''); }, 10000);
      function h(e) {
        if (e.data && e.data.type === 'screenshot-result' && e.data.id === msgId) {
          clearTimeout(timer); window.removeEventListener('message', h); res(e.data.image);
        }
      }
      window.addEventListener('message', h);
      var s = fdoc.createElement('script');
      s.textContent = '(function(){' +
        'html2canvas(document.body,{useCORS:true,allowTaint:true,logging:false})' +
        '.then(function(c){parent.postMessage({type:"screenshot-result",id:"'+msgId+'",image:c.toDataURL("image/png")},"*")})' +
        '.catch(function(){parent.postMessage({type:"screenshot-result",id:"'+msgId+'",image:""},"*")});' +
        '})();';
      fdoc.body.appendChild(s);
    });
    if (!image) return;
    await fetch(BASE+'/screenshot', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({token:_token, image:image})});
  } catch(e) { console.warn('screenshot:', e.message); }
}

// ═══════════════════════════════════════════════════════
// LAYOUT : VIEW SWITCH + SPLITTER
// ═══════════════════════════════════════════════════════
function setView(mode, btn) {
  document.querySelectorAll('.vbtn').forEach(function(b){b.classList.remove('active');});
  if (btn) btn.classList.add('active');
  var ep = document.getElementById('editorPane');
  var sp = document.getElementById('splitter');
  var pp = document.getElementById('previewPane');
  if (mode === 'code') {
    ep.style.cssText='flex:1;display:flex;flex-direction:column;';
    sp.style.display='none'; pp.style.display='none';
  } else if (mode === 'preview') {
    ep.style.display='none'; sp.style.display='none';
    pp.style.cssText='flex:1;display:flex;flex-direction:column;';
  } else {
    ep.style.cssText='flex:0 0 50%;display:flex;flex-direction:column;min-width:180px';
    sp.style.display='block';
    pp.style.cssText='flex:1;display:flex;flex-direction:column;min-width:120px';
  }
  if (_editor) _editor.resize();
}

function initSplitter() {
  var sp = document.getElementById('splitter');
  var dragging=false, startX=0, startW=0;
  sp.addEventListener('mousedown', function(e) {
    dragging=true; startX=e.clientX;
    startW=document.getElementById('editorPane').offsetWidth;
    sp.classList.add('active');
    document.body.style.cursor='col-resize';
    document.body.style.userSelect='none';
  });
  document.addEventListener('mousemove', function(e) {
    if (!dragging) return;
    var total = document.getElementById('appBody').offsetWidth;
    var newW  = Math.max(150, Math.min(startW + e.clientX - startX, total-150));
    document.getElementById('editorPane').style.flex = '0 0 ' + newW + 'px';
    if (_editor) _editor.resize();
  });
  document.addEventListener('mouseup', function() {
    if (!dragging) return;
    dragging=false; sp.classList.remove('active');
    document.body.style.cursor='';
    document.body.style.userSelect='';
  });
}

// ═══════════════════════════════════════════════════════
// UTILS
// ═══════════════════════════════════════════════════════
function copyToken() {
  if (!_token) return;
  navigator.clipboard.writeText(_token);
  showToast('Token copie','success');
}

function showToast(msg, type) {
  var colors={info:'#3b82f6',success:'#10b981',error:'#ef4444',warn:'#f59e0b',warning:'#f59e0b'};
  var t = document.createElement('div');
  t.className = 'toast';
  t.style.background = colors[type] || colors.info;
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(function(){t.remove();}, 3000);
}

function debounce(fn, delay) {
  var timer;
  return function() { clearTimeout(timer); timer = setTimeout(fn, delay); };
}
</script>
</body>
</html>"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("grist_coder:app", host="0.0.0.0", port=8742, reload=True)
