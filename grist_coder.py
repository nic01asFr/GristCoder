"""
GRIST CODER · MCP Server v5.1 · streamable HTTP spec 2025-03-26
────────────────────────────────────────────────────────────────
Document Grist = codebase du projet.
Widget = split vertical Ace editor | iframe preview.
LLM via MCP : schema relationnel + artefacts + pages structurées.

AUTH  : widget -> grist.docApi.getAccessToken() -> POST /register -> gc-xxx
        Claude Desktop -> Bearer <grist_key> -> uid:user_id stable
TOOLS : sessions(3) canvas(5) artefact(1) grist-r(3) grist-w(3) doc(2) = 17
"""

import asyncio, base64, hashlib, json, os, re, subprocess, sys, time, uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

load_dotenv()
HOST_URL = os.getenv("HOST_URL", "http://localhost:8742")
MCP_VER  = "2025-03-26"
WIDGET_PATH = Path(__file__).parent / "widget.html"

# ── SERVER INSTRUCTIONS ───────────────────────────────────────────────────────

SERVER_INSTRUCTIONS = """
Tu es connecte a Grist Coder MCP v5.2 — service de dev d apps Grist.

CONCEPT
  Tables de donnees (Clients, Commandes...)  = modele de donnees
  Table Artefacts (widgets HTML/JS)          = fichiers source
  Pages Grist (grille + widget custom)       = ecrans de l app

OUTILS (20)
  Sessions : sessions_list, session_select, session_info
  Canvas   : canvas_read, canvas_write, canvas_patch, canvas_exec, canvas_screenshot
  Artefact : artefact_init
  Grist R  : grist_schema, grist_records, grist_sql
  Grist W  : grist_records_add, grist_records_patch, grist_upsert
  Document : grist_apply, grist_views_list, grist_view_create,
             grist_section_configure, grist_view_add_widget

RESSOURCES
  docs/schema           -> recettes tables/colonnes (lire avant schema)
  docs/artefacts        -> templates HTML/JS + API bridge (lire avant coder)
  docs/playbook         -> sequences pages, layouts, liaisons (lire avant creer pages)
  docs/app-patterns     -> patterns multi-widgets : nav, sync, auto-provisioning, Artefactory
  docs/formulas         -> formules colonnes Python natives Grist
  playbook/{scenario}   -> guide cible : dashboard|fiche|table|full-app|master-detail
  context/{token}       -> snapshot live : tables+artefacts+pages (lire en debut session)
  code/{token}          -> source de tous les artefacts (lire avant iterer sur app existante)

WORKFLOW
  1. sessions_list() -> token
  2. context/{token} -> etat doc
  3. docs/playbook (ou playbook/{scenario}) -> sequence exacte
  4. Construire : tables -> artefacts (canvas_write+screenshot+upsert) -> pages
  5. context/{token} -> verifier

REGLES CRITIQUES
  JAMAIS REST PATCH sur _grist_Views / _grist_Views_section -> crash frontend
  TOUJOURS grist_apply(["UpdateRecord", ...]) pour toutes les tables meta
  canvas_read() AVANT canvas_patch() — old_str doit etre exact et unique
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

async def grist_apply(ctx, actions: list) -> dict:
    """Applique des user actions Grist via POST /apply."""
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"{_base(ctx)}/apply", headers=_gh(ctx), params=_aq(ctx),
                         content=json.dumps(actions))
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

# ── ARTEFACTS TABLE DEF ───────────────────────────────────────────────────────

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
    # Sessions
    {"name": "sessions_list",
     "description": "Liste tous les widgets actifs. Appeler EN PREMIER pour obtenir le token.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True}},

    {"name": "session_select",
     "description": "Selectionne une session par token. Inutile si une seule session.",
     "inputSchema": {"type": "object",
                     "properties": {"token": {"type": "string"}},
                     "required": ["token"]}},

    {"name": "session_info",
     "description": "Resume de la session : tables, artefacts IsDoc, canvas. Preferer context/{token} pour snapshot complet.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True}},

    # Canvas
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
     "description": "Remplace precisement un fragment unique (str_replace). canvas_read() d abord.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "old_str": {"type": "string", "description": "Fragment exact a remplacer (unique dans le canvas)"},
                         "new_str": {"type": "string", "description": "Fragment de remplacement"}},
                     "required": ["old_str", "new_str"]}},

    {"name": "canvas_exec",
     "description": "Execute le canvas Python (subprocess isole, timeout 10s).",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"openWorldHint": True}},

    {"name": "canvas_screenshot",
     "description": "Capture le rendu iframe du widget (html2canvas injecte). Timeout 15s. Retourne image/png.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True}},

    # Artefact
    {"name": "artefact_init",
     "description": "Cree la table Artefacts (9 colonnes) si absente. Idempotent. (Le widget le fait aussi automatiquement.)",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"idempotentHint": True}},

    # Grist lecture
    {"name": "grist_schema",
     "description": "Schema du document : tables et colonnes avec types, formules, refs.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "table_id": {"type": "string", "description": "Si fourni, colonnes de cette table uniquement"}}},
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

    # Grist ecriture
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
     "description": "Cree ou met a jour selon une cle metier (require). Pattern principal pour sync artefacts.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "table_id": {"type": "string"},
                         "records":  {"type": "array", "items": {"type": "object"},
                                      "description": "Liste de {require: {col: val}, fields: {col: val}}"}},
                     "required": ["table_id", "records"]}},

    # Document structure
    {"name": "grist_apply",
     "description": "Execute des User Actions Grist (AddTable, AddColumn, BulkAddOrReplaceRecord, UpdateRecord...). Permet de creer tables, colonnes, et modifier les metadonnees du document.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "actions": {"type": "array",
                                     "description": "Liste d actions ex: [[\"AddTable\",\"MaTable\",[{\"id\":\"Nom\",\"type\":\"Text\"}]]]",
                                     "items": {}}},
                     "required": ["actions"]}},

    {"name": "grist_views_list",
     "description": "Liste les pages (vues) du document avec leurs sections.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True}},

    {"name": "grist_section_configure",
     "description": "Configure un widget custom existant : URL + artefact display mode + lien optionnel vers une section source. Evite de generer manuellement le JSON options/customView.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "section_ref":      {"type": "integer", "description": "ID de la section custom a configurer (depuis grist_views_list)"},
                         "artefact":         {"type": "string",  "description": "Nom de l artefact display mode (ex: 'FicheClient'). Laisse vide pour widget coding."},
                         "widget_url":       {"type": "string",  "description": "URL custom (defaut: HOST_URL/). Ignore si artefact est fourni."},
                         "link_section_ref": {"type": "integer", "description": "Section source pour lier les selections de ligne (linkSrcSectionRef). 0 = pas de lien."}},
                     "required": ["section_ref"]}},

    {"name": "grist_view_add_widget",
     "description": "Ajoute un widget custom a une page existante (qui n a qu une grille). Cree la section, la configure et met a jour le layout.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "view_ref":         {"type": "integer", "description": "ID de la vue (depuis grist_views_list)"},
                         "table_id":         {"type": "string",  "description": "Table source du widget (meme que la grille en general)"},
                         "artefact":         {"type": "string",  "description": "Nom de l artefact a afficher (mode display)"},
                         "grid_section_ref": {"type": "integer", "description": "ID de la section grille a lier (pour onRecord). 0 = pas de lien."}},
                     "required": ["view_ref", "table_id"]}},

    {"name": "grist_view_create",
     "description": "Cree une page Grist avec grille de donnees + widget custom lies. Structure un ecran de l app.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "table_id":   {"type": "string", "description": "Table source (ex: 'Batiments')"},
                         "page_name":  {"type": "string", "description": "Nom de la page dans Grist"},
                         "widget_url": {"type": "string", "description": "URL du widget custom (defaut: HOST_URL/)"},
                         "artefact":   {"type": "string", "description": "Nom d un artefact a afficher automatiquement dans le widget (mode display). Ex: 'FicheClient'. Ajoute ?a=NomArtefact a l URL."}},
                     "required": ["table_id"]}},
]

# ── PROMPTS ───────────────────────────────────────────────────────────────────

PROMPTS = [
    {"name": "explore-doc",
     "description": "Explorer le schema et les donnees du document Grist actif.",
     "arguments": []},
    {"name": "build-app",
     "description": "Construire une app complete (schema + artefacts + pages structurees).",
     "arguments": [{"name": "description", "description": "Description de l app", "required": True}]},

    {"name": "create-page",
     "description": "Creer une page Grist avec widget(s) en 2-3 rounds. Choisit automatiquement le bon outil.",
     "arguments": [
         {"name": "description", "description": "Ce que la page doit afficher", "required": True},
         {"name": "scenario",    "description": "dashboard | fiche | table | master-detail", "required": False}]},
    {"name": "write-artefact",
     "description": "Creer un artefact HTML/JS pour une table donnee.",
     "arguments": [
         {"name": "nom",      "description": "Nom de l artefact",          "required": True},
         {"name": "type",     "description": "grist|html|react|markdown",  "required": True},
         {"name": "objectif", "description": "Ce que l artefact doit faire","required": True}]},
    {"name": "patch-canvas",
     "description": "Modifier le canvas de facon ciblee.",
     "arguments": [{"name": "modification", "description": "Ce qui doit changer", "required": True}]},
    {"name": "debug-canvas",
     "description": "Deboguer le canvas (exec -> patch -> boucle jusqu a returncode 0).",
     "arguments": []},
    {"name": "design-schema",
     "description": "Concevoir le schema relationnel Grist pour un domaine metier.",
     "arguments": [{"name": "domaine", "description": "Domaine metier (ex: patrimoine, RH, stock)", "required": True}]},
]

def _prompt_messages(name, args):
    if name == "explore-doc":
        return [{"role": "user", "content": {"type": "text", "text": (
            "Explore le document Grist actif.\n"
            "1. sessions_list() -> identifier la session\n"
            "2. resources/read grist-coder://context/{token} -> snapshot complet\n"
            "3. grist_records(table, limit=5) pour chaque table principale\n"
            "4. grist_sql() pour explorer les relations\n"
            "Resumer : modele, relations, artefacts, pages, suggestions."
        )}}]
    if name == "build-app":
        desc = args.get("description", "une application")
        return [{"role": "user", "content": {"type": "text", "text": (
            f"Construire : {desc}\n\n"
            "1. sessions_list() -> token\n"
            "2. resources/read grist-coder://context/{token} -> etat actuel\n"
            "3. resources/read grist-coder://docs/schema -> recettes tables\n"
            "4. resources/read grist-coder://docs/artefacts -> templates code\n"
            "5. resources/read grist-coder://playbook/full-app -> sequence complete\n"
            "6. Construire : schema -> donnees -> artefacts (canvas+screenshot+upsert) -> pages\n"
            "7. resources/read grist-coder://context/{token} -> verifier etat final"
        )}}]

    if name == "create-page":
        desc     = args.get("description", "une page")
        scenario = args.get("scenario", "")
        pb_uri   = f"grist-coder://playbook/{scenario}" if scenario else "grist-coder://docs/playbook"
        return [{"role": "user", "content": {"type": "text", "text": (
            f"Creer une page Grist : {desc}\n\n"
            "1. sessions_list() -> token\n"
            "2. resources/read grist-coder://context/{token} -> tables et pages existantes\n"
            f"3. resources/read {pb_uri} -> sequence et outil adapte\n"
            "4. Si artefact necessaire : canvas_write -> canvas_screenshot -> grist_upsert\n"
            "5. Creer la page :\n"
            "   - Nouvelle page        -> grist_view_create(table_id, page_name[, artefact])\n"
            "   - Widget sur existante -> grist_view_add_widget(view_ref, table_id, artefact, grid_section_ref)\n"
            "   - Reconfigurer section -> grist_section_configure(section_ref, artefact)\n"
            "6. resources/read grist-coder://context/{token} -> verifier"
        )}}]
    if name == "write-artefact":
        nom = args.get("nom", "MonArtefact")
        typ = args.get("type", "grist")
        obj = args.get("objectif", "afficher des donnees")
        return [{"role": "user", "content": {"type": "text", "text": (
            f"Creer artefact Nom='{nom}' Type='{typ}' : {obj}\n\n"
            "1. resources/read grist-coder://docs/artefacts -> template pour ce type\n"
            "2. grist_schema() -> tables disponibles\n"
            f"3. canvas_write(code_{typ}) -> coder l artefact complet\n"
            "   -> Inclure appContext.isGristCoder, safeLoad, etats UI\n"
            "4. canvas_screenshot() -> valider\n"
            f"5. grist_upsert('Artefacts', [{{require:{{Nom:'{nom}'}}, fields:{{Type:'{typ}',Code:...}}}}])\n"
            "6. grist_view_create(table_id, page_name) -> page dans le document"
        )}}]
    if name == "patch-canvas":
        mod = args.get("modification", "ameliorer")
        return [{"role": "user", "content": {"type": "text", "text": (
            f"Modification : {mod}\n\n"
            "1. canvas_read() -> lire le code actuel\n"
            "2. canvas_patch(old_str, new_str) pour chaque modification\n"
            "3. canvas_screenshot() -> valider"
        )}}]
    if name == "debug-canvas":
        return [{"role": "user", "content": {"type": "text", "text": (
            "1. canvas_read()\n2. canvas_exec() -> voir l erreur\n"
            "3. canvas_patch() -> corriger\n4. canvas_exec() -> repeter jusqu a returncode 0"
        )}}]
    if name == "design-schema":
        dom = args.get("domaine", "metier")
        return [{"role": "user", "content": {"type": "text", "text": (
            f"Concevoir le schema relationnel Grist pour : {dom}\n\n"
            "1. resources/read grist-coder://docs/schema -> types, Ref:, visibleCol, formules\n"
            "2. grist_schema() -> tables existantes\n"
            "3. Proposer schema complet (tables, colonnes, refs, formules)\n"
            "4. Creer Phase 1 (sans refs) via grist_records_add\n"
            "5. Creer Phase 2 (Ref:) via grist_records_add sur colonnes\n"
            "6. Configurer Phase 3 (visibleCol) via grist_records_patch"
        )}}]
    return [{"role": "user", "content": {"type": "text", "text": f"Prompt '{name}' inconnu."}}]

# ── RESOURCE DOCS CONTENT ─────────────────────────────────────────────────────

DOCS_SCHEMA = """GRIST SCHEMA - Recettes exactes (REST API)
==========================================

PHASE 1 - Creer tables sans colonnes Ref:
Tool: grist_records_add avec table_id="_grist_Tables" NON
-> Utiliser: grist_records_add sur POST /tables (endpoint special)
Body exact:
{
  "tables": [{
    "id": "Batiments",
    "columns": [
      {"id": "Nom",     "fields": {"type": "Text",    "label": "Nom"}},
      {"id": "Adresse", "fields": {"type": "Text",    "label": "Adresse"}},
      {"id": "Surface", "fields": {"type": "Numeric", "label": "Surface m2"}},
      {"id": "Statut",  "fields": {"type": "Choice",  "label": "Statut",
                                   "widgetOptions": "{\"choices\":[\"Actif\",\"Inactif\",\"Travaux\"]}"}},
      {"id": "DPE",     "fields": {"type": "Choice",  "label": "DPE",
                                   "widgetOptions": "{\"choices\":[\"A\",\"B\",\"C\",\"D\",\"E\",\"F\",\"G\"]}"}},
      {"id": "UpdatedAt","fields": {"type": "DateTime:Europe/Paris", "label": "Mis a jour"}}
    ]
  }]
}
Via MCP: grist_records_add(table_id="_endpoint_tables", records=[...]) NON
-> Utiliser l endpoint REST direct via canvas_exec ou via appel HTTP.
-> OU via grist_apply interne (tool non expose, mais grist_records_add sur /tables fonctionne).

TYPES DE COLONNES
Text | Numeric | Int | Bool | Date | DateTime | DateTime:Europe/Paris
Choice | ChoiceList | Ref:TableId | RefList:TableId | Attachments

PHASE 2 - Ajouter colonnes Ref: (apres creation de la table cible)
POST /api/docs/{docId}/tables/{tableId}/columns
{
  "columns": [{
    "id": "Batiment",
    "fields": {"type": "Ref:Batiments", "label": "Batiment"}
  }]
}
Via MCP: grist_records_add(
  table_id="_grist_Tables_column",
  records=[{"fields": {"parentId": TABLE_REF_INT, "colId": "Batiment",
                       "type": "Ref:Batiments", "label": "Batiment"}}]
)

PHASE 3 - Configurer visibleCol
PATCH /api/docs/{docId}/tables/{tableId}/columns/{colId}
{"fields": {"widgetOptions": "{\"visibleCol\": \"Nom\"}"}}
Via MCP: grist_records_patch(
  table_id="_grist_Tables_column",
  records=[{"id": COL_REF_INT, "fields": {"widgetOptions": "{\"visibleCol\":\"Nom\"}"}}]
)

FORMULES (ajouter isFormula:true)
Compteur  : {"formula": "len($locaux_Batiment)", "isFormula": true}
Somme     : {"formula": "SUM($locaux_Batiment.Surface)", "isFormula": true}
Date auto : {"formula": "NOW()", "isFormula": true}
Lookup    : {"formula": "$Batiment.Nom", "isFormula": true}

SQL CROSS-TABLES (via grist_sql)
SELECT B.Nom, COUNT(I.id) as nb, SUM(I.Cout) as total
FROM Interventions I JOIN Batiments B ON I.Batiment = B.id
GROUP BY B.Nom ORDER BY total DESC

PAGES - Structurer le document (ecrans de l app)
================================================

== OUTIL PRINCIPAL ==
grist_view_create(table_id, page_name, widget_url?, artefact?)
  -> Cree: grille (gauche) + widget custom lie (droite)
  -> Interne: CreateViewSection x2 + UpdateRecord via grist_apply
  -> Retourne: view_ref, grid_section, widget_section

  DISPLAY MODE (artefact=NomArtefact):
    Le widget affiche directement l artefact nomme (editeur masque).
    grist_view_create("Clients", "Fiche Client", artefact="FicheClient")
      -> URL widget = HOST_URL/?a=FicheClient
      -> Widget masque l editeur, affiche FicheClient plein ecran
      -> grist.onRecord() => _sharedState.record = ligne selectionnee
      -> Dans l artefact HTML : window.__APP_STATE__.record = donnees Grist
    Usage: pages de consultation/rendu lies a une grille sur la meme page

== CONFIGURATIONS AVANCEES (via grist_apply) ==

1. PAGE WIDGET SEUL (full-screen, ex: Dashboard IsDoc)
   grist_apply([["CreateViewSection", tableRef, 0, "custom", null, null]])
   -> view_ref = retValues[0]["viewRef"], section_ref = retValues[0]["sectionRef"]
   Puis configurer:
   grist_apply([
     ["UpdateRecord", "_grist_Views_section", section_ref, {"options": OPTIONS_JSON}],
     ["UpdateRecord", "_grist_Views", view_ref, {"name": "Dashboard",
       "layoutSpec": '{"children":[{"leaf":SECTION}],"collapsed":[]}'}]
   ])

2. GRILLE SEULE (sans widget)
   grist_apply([["CreateViewSection", tableRef, 0, "record", null, null]])

3. MASTER-DETAIL (deux tables liees par Ref:)
   a) Creer page avec grille master:
      grist_apply([["CreateViewSection", masterTableRef, 0, "record", null, null]])
      -> view_ref=V, grid_master=S1
   b) Ajouter grille detail dans la meme vue:
      grist_apply([["CreateViewSection", detailTableRef, V, "record", null, null]])
      -> grid_detail=S2
   c) Trouver colRef de la colonne Ref: dans la table detail:
      grist_sql("SELECT id FROM _grist_Tables_column WHERE parentId=DETAIL_TABLE_REF AND colId='NomColRef'")
   d) Lier + layout:
      grist_apply([
        ["UpdateRecord", "_grist_Views_section", S2, {
          "linkSrcSectionRef": S1, "linkSrcColRef": 0, "linkTargetColRef": COL_REF_ID
        }],
        ["UpdateRecord", "_grist_Views", V, {"name": "Master->Detail",
          "layoutSpec": '{"children":[{"children":[{"leaf":S1},{"leaf":S2}]}],"collapsed":[]}'}]
      ])

4. GRILLE + CARTE + WIDGET (3 sections, layout vertical+horizontal)
   a) grist_apply([["CreateViewSection", tableRef, 0, "record", null, null]]) -> V, S_grid
   b) grist_apply([["CreateViewSection", tableRef, V, "single", null, null]]) -> S_card
   c) grist_apply([["CreateViewSection", tableRef, V, "custom", null, null]]) -> S_widget
   d) grist_apply([
        ["UpdateRecord","_grist_Views_section", S_card, {"linkSrcSectionRef": S_grid}],
        ["UpdateRecord","_grist_Views_section", S_widget, {"linkSrcSectionRef": S_grid, "options": OPTIONS_JSON}],
        ["UpdateRecord","_grist_Views", V, {
          "layoutSpec": '{"children":[{"leaf":S_grid},{"children":[{"leaf":S_card},{"leaf":S_widget}]}],"collapsed":[]}'
        }]
      ])

== LAYOUTSPEC - FORMAT EXACT ==
Root = colonne (vertical stack). Nested children = ligne (horizontal split).
  Un seul plein ecran   : {"children":[{"leaf":N}],"collapsed":[]}
  Cote a cote           : {"children":[{"children":[{"leaf":A},{"leaf":B}]}],"collapsed":[]}
  Haut + [bas gauche + bas droite]:
    {"children":[{"leaf":A},{"children":[{"leaf":B},{"leaf":C}]}],"collapsed":[]}
  Avec tailles:
    {"children":[{"leaf":A,"size":40},{"leaf":B,"size":60}],"collapsed":[]}

== OPTIONS WIDGET CUSTOM - FORMAT EXACT ==
CRITIQUE: customView doit etre une JSON STRING dans l outer JSON (pas un objet nested!)
OPTIONS_JSON = json.dumps({
  "verticalGridlines": True, "horizontalGridlines": True,
  "zebraStripes": False, "numFrozen": 0,
  "customView": json.dumps({
    "mode": "url", "url": HOST_URL+"/", "access": "full",
    "widgetDef": None, "pluginId": "", "sectionId": "",
    "renderAfterReady": False, "widgetId": None,
    "widgetOptions": None, "columnsMapping": None
  })
})
!! JAMAIS utiliser grist_patch/REST sur _grist_Views ou _grist_Views_section !!
   -> Le REST PATCH stocke les JSON strings comme objets JS -> crash frontend
   -> TOUJOURS utiliser grist_apply(["UpdateRecord", ...]) pour les tables meta

== LIENS ENTRE SECTIONS ==
linkSrcSectionRef=N  -> reagit a la selection de ligne de la section N
linkSrcColRef=0      -> cle = rowId (liens directs Ref:)
linkTargetColRef=M   -> colonne Ref: dans la table cible (id depuis _grist_Tables_column)
  Ex: section Commandes liee a Clients via colonne "Client" (Ref:Clients, id=21)
  -> linkSrcSectionRef=clients_section, linkSrcColRef=0, linkTargetColRef=21

== TYPES DE SECTIONS ==
"record" = grille (tableau)
"single" = fiche/carte (un enregistrement)
"custom" = widget custom (iframe URL)
"chart"  = graphique (non teste)

== OBTENIR LES REFS NECESSAIRES ==
tableRef   : grist_sql("SELECT id FROM _grist_Tables WHERE tableId='NomTable'")
colRef     : grist_sql("SELECT id FROM _grist_Tables_column WHERE parentId=TABLE_REF AND colId='NomCol'")
viewRef    : grist_sql("SELECT id FROM _grist_Views WHERE name='NomPage'")
sectionRef : grist_sql("SELECT id FROM _grist_Views_section WHERE parentId=VIEW_REF AND parentKey='custom'")

== ORDRE DE CREATION RECOMMANDE ==
1. Tables sans Ref: (Entites principales...)
2. Tables avec Ref: (Tables de jonction, details...)
3. visibleCol sur toutes les colonnes Ref:
4. Formules de comptage/somme
5. grist_view_create() pour chaque table principale -> page grille+widget
6. grist_apply() pour pages avancees (master-detail, dashboard, multi-sections)
""".strip()

DOCS_ARTEFACTS = """GRIST CODER - Recettes artefacts HTML/JS
=========================================

TEMPLATE BASE - TYPE: grist (widget reactif aux donnees)
<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8">
<script src="https://docs.getgrist.com/grist-plugin-api.js"></script>
<script src="https://cdn.tailwindcss.com"></script>
</head><body><div id="app" class="p-4"></div>
<script>
const app = window.app || {navigate:p=>showToast(p,'info'),emit:()=>{},on:()=>{},setState:()=>{},state:{}};
const isGristCoder = typeof window.app?.navigate === 'function';
function showToast(msg,type='info'){
  const c={info:'#3b82f6',success:'#10b981',error:'#ef4444',warning:'#f59e0b'};
  const t=document.createElement('div');
  t.style.cssText='position:fixed;bottom:20px;right:20px;padding:12px 20px;background:'+c[type]+
    ';color:#fff;border-radius:8px;font-size:13px;z-index:9999;box-shadow:0 4px 12px rgba(0,0,0,.2)';
  t.textContent=msg;document.body.appendChild(t);setTimeout(()=>t.remove(),3500);
}
// Donnees globales (IsDoc): toutes les lignes
async function safeLoad(tableName) {
  try {
    const d = await grist.docApi.fetchTable(tableName);
    const n = d.id?.length||0; const rows=[];
    for(let i=0;i<n;i++){const r={id:d.id[i]};Object.keys(d).forEach(k=>{if(k!=='id')r[k]=d[k][i];});rows.push(r);}
    return rows;
  } catch(e){console.warn('Table '+tableName+' non trouvee');return[];}
}
// Donnees liees (IsDoc=false): lignes filtrees par la section liee (respecte linkSrcSectionRef)
async function loadLinked() {
  const d = await grist.fetchSelectedTable();     // retourne seulement les lignes filtrees
  const n = d.id?.length||0; const rows=[];
  for(let i=0;i<n;i++){const r={id:d.id[i]};Object.keys(d).forEach(k=>{if(k!=='id')r[k]=d[k][i];});rows.push(r);}
  return rows;
}
let _state = { loading:true, error:null, data:[], record:{} };
async function loadData() {
  _state.data = await safeLoad('MaTable');
  _state.loading = false; render();
}
function render() {
  const el = document.getElementById('app');
  if(_state.loading){el.innerHTML='<div class="text-gray-400 p-8 text-center">Chargement...</div>';return;}
  if(!_state.data.length){el.innerHTML='<div class="text-gray-400 text-center py-8">Aucune donnee</div>';return;}
  el.innerHTML = _state.data.map(r=>`
    <div class="flex items-center p-3 border-b hover:bg-gray-50 cursor-pointer" onclick="selectRow(${r.id})">
      <span class="flex-1 font-medium">${r.Nom||r.id}</span>
    </div>`).join('');
}
grist.ready({requiredAccess:'full'});
loadData();
</script></body></html>

---
TEMPLATE BASE - TYPE: html (UI independante, pas de donnees Grist)
<!-- Pas de grist.ready() si aucune donnee Grist necessaire -->
<!DOCTYPE html><html><head><meta charset="UTF-8">
<script src="https://cdn.tailwindcss.com"></script>
</head><body>...</body></html>

---
GRIST PLUGIN API - BRIDGE COMPLET (disponible dans tous les artefacts)
=======================================================================
IMPORTANT: grist.ready({requiredAccess:'full'}) doit etre appele avant tout usage de l API.
           requiredAccess: 'read table' (lecture seule) | 'full' (lecture+ecriture)

## LECTURE DONNEES
# Toutes les lignes d une table (IsDoc=true, dashboard global)
const d = await grist.docApi.fetchTable('MaTable');
// d = {id:[1,2,3], Nom:['A','B','C'], CA:[100,200,300]}  -- format colonnes
const rows = d.id.map((id,i) => Object.fromEntries(Object.keys(d).map(k=>[k,d[k][i]])));

# Lignes filtrees par la liaison de section (IsDoc=false, widget lie a une grille)
const d = await grist.fetchSelectedTable();
// Respecte linkSrcSectionRef -> retourne seulement les lignes liees a la selection courante
// CRITIQUE: utiliser fetchSelectedTable() et NON docApi.fetchTable() pour les widgets lies

# Enregistrement selectionne (record courant)
const r = window.__APP_STATE__?.record || {};  // injecte par le widget parent via onRecord
// OU reactif via event:
app.on('record', r => { _state.record = r; render(); });

## ECRITURE DONNEES (selectedTable = table a laquelle le widget est lie)
await grist.selectedTable.create({fields: {Nom:'Nouveau', CA:0}});      // cree un enregistrement
await grist.selectedTable.update({id:42, fields: {CA: 999}});            // met a jour
await grist.selectedTable.destroy([42, 43]);                             // supprime par ids
await grist.selectedTable.upsert(                                        // cree ou met a jour
  {require:{Nom:'Unique'}, fields:{CA:100}},
  {onMany:'all', allowEmptyRequire:false}
);
await grist.selectedTable.fetch({filters:{Actif:[true]}});               // lecture avec filtre

## ECOUTE EVENEMENTS
grist.onRecord(cb)          // record selectionne change (cb: function(record, mappings))
grist.onRecords(cb)         // liste de records change (cb: function(records, mappings))
grist.onNewRecord(cb)       // nouvelle ligne vide cree -> cb({}) avec id=0
grist.onOptions(cb)         // options du widget changent (widgetOptions JSON)

## NAVIGATION / CURSEUR
await grist.setCursorPos({rowId: 42});               // positionne le curseur sur une ligne
await grist.setSelectedRows([42, 43]);               // selectionne plusieurs lignes

## OPTIONS PERSISTANTES (stockees dans widgetOptions du customView)
await grist.setOption('maCle', valeur);             // persist une valeur
const val = await grist.getOption('maCle');          // lire
await grist.setOptions({cle1:v1, cle2:v2});         // batch set
const all = await grist.getOptions();               // lire tout
await grist.clearOptions();                          // reset

## ACTIONS GRIST DIRECTES
await grist.docApi.applyUserActions([               // actions Grist bas niveau
  ['AddRecord', 'MaTable', null, {Nom:'X'}],
  ['UpdateRecord', 'MaTable', 42, {CA:500}],
  ['RemoveRecord', 'MaTable', 42],
]);

## ACTIONS GRIST AVANCEES
// BulkAddRecord : insere N lignes en une seule action (format colonnaire)
await grist.docApi.applyUserActions([
  ['BulkAddRecord', 'MaTable', records.map(()=>null), {
    Nom: records.map(r=>r.nom),
    CA:  records.map(r=>r.ca),
  }]
]);
// BulkUpdateRecord : met a jour N lignes en une seule action
await grist.docApi.applyUserActions([
  ['BulkUpdateRecord', 'MaTable', [1,2,3], {Statut:['ok','ok','ko']}]
]);

// AddTable : cree une table avec son schema complet
await grist.docApi.applyUserActions([
  ['AddTable', 'Config', [
    {id:'cle',    type:'Text'},
    {id:'valeur', type:'Text'},
    {id:'ref',    type:'Ref:Clients'},
    {id:'tags',   type:'ChoiceList'},
    {id:'statut', type:'Choice',
      widgetOptions: JSON.stringify({choices:['actif','inactif']})},
    {id:'date',   type:'DateTime'},
  ]]
]);
// AddColumn : ajoute une colonne a une table existante
await grist.docApi.applyUserActions([
  ['AddColumn', 'MaTable', 'NouvelleCol', {type:'Text'}]
]);

// ensureTable : cree seulement si la table n existe pas
async function ensureTable(name, schema) {
  const tables = await grist.docApi.listTables();
  if (!tables.includes(name))
    await grist.docApi.applyUserActions([['AddTable', name, schema]]);
}

## FORMATS SPECIAUX GRIST
// ChoiceList et ReferenceList : toujours prefixer de 'L'
['L', 'urgent', 'important']   // ChoiceList (ecriture)
['L', 1, 5, 12]                // ReferenceList (ecriture)
gristList.slice(1)             // -> JS array (lecture)
['L', ...array]                // -> Grist list (ecriture)

// Dates : Grist stocke en SECONDES Unix (pas ms !)
Math.floor(Date.now() / 1000)  // JS -> Grist DateTime
new Date(timestamp * 1000)     // Grist -> JS Date

## TABLES SYSTEME (requiredAccess:'full')
const tables = await grist.docApi.fetchTable('_grist_Tables');
// tables.tableId : ['Clients','Contrats', ...] — toutes les tables
const cols = await grist.docApi.fetchTable('_grist_Tables_column');
// cols.colId, cols.type, cols.label, cols.parentId (= tables.id)
// Lister les colonnes d une table:
const tIdx = tables.tableId.indexOf('MaTable');
const tId  = tables.id[tIdx];
const maCols = cols.colId.filter((_,i) => cols.parentId[i] === tId);

---
RECORD COURANT - DEUX MODES
# Mode __APP_STATE__ (statique au chargement)
const r = window.__APP_STATE__?.record || {};
document.getElementById('nom').textContent = r.Nom || '(aucun)';

# Mode reactif (re-render a chaque changement de selection)
app.on('record', r => { _state.record = r; render(); });
// app.on() est disponible car window.app est injecte par le widget parent

---
NAVIGATION INTER-ARTEFACTS
function navigateTo(path) {
  if (isGristCoder) app.navigate(path);
  else showToast('Ouvrir: '+path,'info');
}
// Exemples: app.navigate('/'), app.navigate('/clients'), app.navigate('/fiche')

EVENEMENTS INTER-ARTEFACTS (bus d evenements partage entre artefacts de la meme page)
app.emit('client-select', {id: 42, nom: 'Mairie de Paris'});
app.on('client-select', ({id, nom}) => { _filteredId = id; render(); });
app.setState({currentClientId: 42});
const cid = app.state.currentClientId;

---
MANIFESTE APP - TYPE: app (JSON)
{
  "name": "MonApp",
  "icon": "🏢",
  "tables": ["Clients", "Contrats", "Prestataires"],
  "routes": [
    {"path": "/",         "label": "Dashboard",  "icon": "📊", "artefact": "Dashboard"},
    {"path": "/clients",  "label": "Clients",    "icon": "👥", "artefact": "ListeClients"},
    {"path": "/contrats", "label": "Contrats",   "icon": "📄", "artefact": "Contrats"}
  ]
}

---
PATTERNS WIDGET LIE (IsDoc=false, linkSrcSectionRef defini)
# Pattern 1: Fiche detail simple (onRecord via __APP_STATE__)
function render() {
  const r = window.__APP_STATE__?.record || {};
  if (!r.id) { el.innerHTML = '<p class="text-gray-400">Selectionnez un enregistrement</p>'; return; }
  el.innerHTML = `<h2 class="text-xl font-bold">${r.Nom}</h2><p>${r.Email||''}</p>`;
}
grist.ready({requiredAccess:'full'}); render();
app.on('record', r => { render(); });  // re-render sur changement de selection

# Pattern 2: Liste filtree par selection (fetchSelectedTable)
async function reload() {
  const d = await grist.fetchSelectedTable();
  const rows = d.id.map((id,i)=>({id, Nom:d.Nom[i], Statut:d.Statut[i]}));
  _state.rows = rows; render();
}
grist.ready({requiredAccess:'full'});
grist.onRecords(() => reload());   // recharge quand la liste filtree change

# Pattern 3: Widget avec ecriture (CRUD via selectedTable)
async function addItem(nom) {
  await grist.selectedTable.create({fields:{Nom:nom, Statut:'Actif'}});
  await reload();
}
async function deleteItem(id) {
  await grist.selectedTable.destroy([id]);
  await reload();
}

# Pattern 4: Options persistantes (etat du widget entre sessions)
let _view = 'list';
grist.onOptions(opts => { _view = opts?.view || 'list'; render(); });
async function toggleView() {
  _view = _view === 'list' ? 'grid' : 'list';
  await grist.setOption('view', _view); render();
}
grist.ready({requiredAccess:'full', allowSelectBy:true});

---
WORKFLOW COMPLET PAR ARTEFACT
1. canvas_write(code_html_complet)               -> live dans le widget
2. canvas_screenshot()                           -> valider le rendu visuel
3. canvas_patch(old_str, new_str)                -> affiner si besoin
4. grist_upsert('Artefacts', [{
     require: {Nom: 'MonArtefact'},
     fields:  {Type: 'grist', Code: '<code>', Description: '...', IsDoc: false, Icon: '📋'}
   }])                                           -> persister dans Grist
5. grist_view_create('MaTable', 'Nom Page')      -> page Grist complete

---
EXEMPLE APP PATRIMOINE - Schema + Artefacts + Pages
Tables Phase 1 : Batiments, Prestataires, CTR_Types
Tables Phase 2 : Locaux(Ref:Batiments), Interventions(Ref:Batiments+Ref:Prestataires)
Tables Phase 3 : visibleCol='Nom' sur toutes les Ref:
Formules       : NbLocaux="len($locaux_Batiment)", Surface_totale="SUM($locaux_Batiment.Surface)"
Artefacts      : Dashboard(html,IsDoc:true), ListeBatiments(grist), FicheBatiment(grist,IsDoc:false)
                 KanbanInterventions(grist), FicheDetail(grist,IsDoc:false)
Pages          : grist_view_create x4 -> document structure complet
""".strip()

DOCS_PLAYBOOK = """GRIST CODER - Playbook pages & widgets
=======================================

DECISION TREE - Quel outil ?
  1. Nouvelle page (table connue)             -> grist_view_create(table_id, page_name[, artefact])
  2. Ajouter widget a page existante          -> grist_view_add_widget(view_ref, table_id, artefact, grid_section_ref)
  3. Reconfigurer section custom existante    -> grist_section_configure(section_ref, artefact)
  4. Config avancee (multi-sections, liaison) -> grist_apply(["CreateViewSection",...]) direct

SEQUENCES MINIMALES
-------------------
A. Dashboard (IsDoc=true, pas de liaison)
   Round 1 (parallele):
     grist_records_add("Artefacts",[{Nom,Type:"html",IsDoc:true,Code:"..."}])
   Round 2:
     grist_view_create("Clients","Dashboard",artefact="Dashboard")
   -> 2 appels, page operationnelle

B. Page grille + fiche liee (display mode)
   Round 1 (parallele si table nouvelle):
     grist_apply([["AddTable","MaTable",[...]]]])        -> tableRef
     grist_apply([["BulkAddRecord","MaTable",[...]]])    -> donnees
     grist_records_add("Artefacts",[{Nom:"Fiche...",IsDoc:false,Code:"..."}])
   Round 2:
     grist_view_create("MaTable","NomPage")              -> {view_ref, section_ref}
   Round 3:
     grist_view_add_widget(view_ref,"MaTable",artefact="FicheMaTable",grid_section_ref=<section_ref>)
   -> 3 rounds, grille + widget lies en display mode

C. Reconfigurer section existante
   Round unique:
     grist_section_configure(section_ref, artefact="NomArtefact"[, link_section_ref=N])

DISPLAY MODE
  URL widget : HOST_URL/?a=NomArtefact  (pose par grist_view_create/grist_view_add_widget)
  Record actif : const r = window.__APP_STATE__?.record || {}; r.NomColonne
  IsDoc=true  -> pas de liaison (dashboard global, fetchTable via grist.docApi)
  IsDoc=false -> liaison via grid_section_ref, record change sur selection de ligne

LAYOUTS (layoutSpec = JSON STRING dans _grist_Views.layoutSpec)
  Cote a cote    : {"children":[{"children":[{"leaf":A},{"leaf":B}]}],"collapsed":[]}
  Vertical       : {"children":[{"leaf":A},{"leaf":B}],"collapsed":[]}
  Grille+[B+C]   : {"children":[{"leaf":A},{"children":[{"leaf":B},{"leaf":C}]}],"collapsed":[]}
  Widget seul    : {"children":[{"leaf":A}],"collapsed":[]}

LIAISONS (_grist_Views_section via grist_apply UpdateRecord)
  Selection ligne   : linkSrcSectionRef = sectionRef_source
  Filtre Ref:col    : linkSrcSectionRef = N, linkTargetColRef = colRef (_grist_Tables_column.id)

DEPENDANCES - ordre d execution obligatoire
  AddTable            -> BulkAddRecord (parallele OK)
  grist_view_create   -> retourne view_ref + section_ref (necessaires pour Round suivant)
  grist_view_add_widget <- depend de view_ref + section_ref retournes au Round precedent
""".strip()

# Playbooks par scenario (ressource template grist-coder://playbook/{scenario})
PLAYBOOKS = {
    "dashboard": """PLAYBOOK : Dashboard (artefact IsDoc, donnees globales)
=======================================================
Quand : page de synthese/KPIs sans liaison de ligne
Rounds: 2

Round 1 (parallele):
  grist_records_add("Artefacts",[{
    Nom: "Dashboard", Type: "html", IsDoc: true, Icon: "📊",
    Description: "Vue d ensemble",
    Code: "<html>...fetchTable via grist.docApi.fetchTable(table)..."
  }])

Round 2:
  grist_view_create("Clients","Dashboard",artefact="Dashboard")
  # table_id = n importe quelle table (non liee car IsDoc)

Pattern code artefact IsDoc:
  async function loadAll() {
    const d = await grist.docApi.fetchTable('MaTable');
    // d.id, d.Nom, d.CA sont des arrays
    const rows = d.id.map((id,i)=>({id, Nom:d.Nom[i], CA:d.CA[i]}));
    render(rows);
  }
  grist.ready(); loadAll();
""",

    "fiche": """PLAYBOOK : Fiche liee (grille + detail en display mode)
=======================================================
Quand : vue detail d un enregistrement selectionne dans une grille
Rounds: 2 (si table existe) ou 3 (si table a creer)

Round 1 — si table existante (parallele):
  grist_records_add("Artefacts",[{
    Nom: "FicheClient", Type: "html", IsDoc: false, Icon: "👤",
    Code: "...window.__APP_STATE__?.record || {}..."
  }])

Round 2:
  grist_view_create("Clients","Clients",artefact="FicheClient")
  # retourne: {view_ref: N, section_ref: M}

Round 3 (optionnel si widget seul ne suffit pas):
  grist_view_add_widget(view_ref=N, table_id="Clients",
    artefact="FicheClient", grid_section_ref=M)

Pattern code artefact fiche:
  function render() {
    const r = window.__APP_STATE__?.record || {};
    if (!r.id) { el.innerHTML = '<p>Selectionnez un enregistrement</p>'; return; }
    el.innerHTML = '<h2>'+r.Nom+'</h2><p>'+r.Email+'</p>';
  }
  // Reagit automatiquement aux changements de selection (grist.onRecord injecte dans __APP_STATE__)
""",

    "table": """PLAYBOOK : Nouvelle table avec donnees et page
===============================================
Quand : creer une table de zéro avec schema + donnees + page

Round 1 (tout en parallele):
  grist_apply([["AddTable","MaTable",[
    {"id":"Nom","type":"Text"},
    {"id":"CA","type":"Numeric"},
    {"id":"Statut","type":"Choice","widgetOptions":"{\\"choices\\":[\\"Actif\\",\\"Inactif\\"]}"}
  ]]])
  # Note: Ref: en Phase 2 apres que les tables cibles existent

Round 2:
  grist_apply([["BulkAddRecord","MaTable",[null,null,null],{
    "Nom":["A","B","C"],"CA":[1000,2000,3000],"Statut":["Actif","Actif","Inactif"]
  }]])

Round 3:
  grist_view_create("MaTable","Nom Page")   # grille simple
  # OU grist_view_create("MaTable","Nom Page",artefact="MonArtefact")  # avec display mode

PHASES SCHEMA (si relations):
  Phase 1 : tables sans Ref: (AddTable en parallele)
  Phase 2 : colonnes Ref:AutreTable (grist_records_add sur _grist_Tables_column)
  Phase 3 : visibleCol (grist_records_patch pour chaque Ref:)
""",

    "full-app": """PLAYBOOK : Application complete (schema + artefacts + pages)
=============================================================
Quand : construire une app de zero

Etape 0 — Lire le contexte:
  context/{token}      -> etat actuel du doc
  docs/schema          -> recettes tables
  docs/artefacts       -> templates code

Etape 1 — Schema (phases):
  Phase 1 : grist_apply([["AddTable",...]]) pour toutes les tables sans Ref: (parallele)
  Phase 2 : grist_records_add sur _grist_Tables_column pour les Ref:
  Phase 3 : grist_records_patch pour visibleCol sur chaque Ref:

Etape 2 — Donnees initiales (parallele par table):
  grist_apply([["BulkAddRecord",...]]) pour chaque table

Etape 3 — Artefacts (parallele si independants):
  Pour chaque ecran :
    a. canvas_write(code_html_complet)    -> live dans le widget
    b. canvas_screenshot()               -> valider le rendu
    c. canvas_patch() si besoin
    d. grist_upsert("Artefacts",[...])   -> persister

Etape 4 — Pages (sequentiel car depend des view_ref):
  Pour chaque page :
    grist_view_create(table_id, page_name, artefact="NomArt")
    # si grille + widget: Round N+1 -> grist_view_add_widget(view_ref, ...)

Etape 5 — Verifier:
  context/{token}  -> snapshot final
""",

    "master-detail": """PLAYBOOK : Master-detail (deux tables liees, filtre Ref:)
=========================================================
Quand : selectionner un client -> voir ses commandes filtrees

Round 1:
  grist_view_create("Clients","Clients -> Commandes")
  # retourne: {view_ref: V, section_ref: S_clients}

Round 2:
  grist_apply([
    ["CreateViewSection", tableRef_Commandes, V, "record", null, null]
  ])
  # retourne sectionRef detail: S_commandes

Round 3:
  grist_apply([
    ["UpdateRecord","_grist_Views_section", S_commandes, {
      "linkSrcSectionRef": S_clients,
      "linkTargetColRef": colRef_Client_in_Commandes
    }]
  ])
  # colRef = id dans _grist_Tables_column pour la colonne Ref:Clients de Commandes

TROUVER colRef:
  grist_sql("SELECT id FROM _grist_Tables_column WHERE tableRef=<tableRef_Commandes> AND colId='Client'")
""",
}

DOCS_APP_PATTERNS = """GRIST CODER - App Patterns multi-widgets
==========================================

## PATTERN 1 — AUTO-PROVISIONING
# Un widget qui cree ses propres tables au premier lancement.
# A appeler dans grist.ready() avant tout autre code.

async function ensureTable(name, schema) {
  const tables = await grist.docApi.listTables();
  if (!tables.includes(name))
    await grist.docApi.applyUserActions([['AddTable', name, schema]]);
}

async function initApp() {
  grist.ready({ requiredAccess: 'full' });
  await ensureTable('Config', [
    { id: 'cle',    type: 'Text' },
    { id: 'valeur', type: 'Text' },
  ]);
  await ensureTable('Items', [
    { id: 'Nom',    type: 'Text' },
    { id: 'Statut', type: 'Choice',
      widgetOptions: JSON.stringify({ choices: ['actif', 'inactif'] }) },
  ]);
  render(await safeLoad('Items'));
}

---
## PATTERN 2 — NAVIGATION MULTI-WIDGETS (setCursorPos / onRecord)
# Widget A (navigation) lie a la table Pages.
# Widget B (contenu) lie a la table Contenu, "Select By" = Widget A.
# Grist route automatiquement le curseur de A vers B via onRecord.

// Widget A — Navigation
async function navigateTo(pageId) {
  await grist.setCursorPos({ rowId: pageId });
}
grist.onRecords(pages => renderNav(pages));

// Widget B — Contenu
let allItems = [], currentPageId = null;
grist.onRecord(page => {
  currentPageId = page?.id || null;
  renderFiltered();
});
grist.onRecords(items => { allItems = items; renderFiltered(); });
function renderFiltered() {
  render(allItems.filter(i => i.page === currentPageId));
}

---
## PATTERN 3 — SYNC INTER-WIDGETS (setSelectedRows / onRecord)
# Meme table, plusieurs widgets independants (ex : Kanban + Gantt + Calendar).
# Chaque widget emet ET recoit la selection — anti-boucle par garde.

let selectedId = null;

function selectItem(id) {
  if (id === selectedId) return;   // anti-boucle
  selectedId = id;
  grist.setSelectedRows([id]);
  highlight(id);
}

grist.onRecord(record => {
  if (record?.id && record.id !== selectedId) {
    selectedId = record.id;
    highlight(record.id);
  }
});

---
## PATTERN 4 — COMMUNICATION VIA OPTIONS (setOption / onOptions)
# Persiste dans widgetOptions du customView. Utile pour filtres, preferences.

// Emetteur
await grist.setOption('filter', { category: 'urgent', userId: 42 });

// Recepteur
grist.onOptions(opts => {
  if (!opts) return;
  applyFilter(opts.filter);
});

---
## PATTERN 5 — CONTEXTE ARTEFACTORY
# Dans Artefactory, les artefacts tournent dans des sous-iframes.
# GristBridge est injecte automatiquement -> grist.docApi.* fonctionne.
# window.app est injecte automatiquement -> navigation et evenements.

const isArtefactory = typeof window.app?.navigate === 'function';

// Navigation entre artefacts
app.navigate('/clients');
app.navigate('/fiche?id=42');

// Bus d evenements inter-artefacts
app.emit('client-selected', { id: 42 });
app.on('client-selected', ({ id }) => loadDetail(id));

// Etat partage
app.setState({ currentId: 42 });
const id = app.state.currentId;

// Record courant (injecte par le widget parent)
const r = window.__APP_STATE__?.record || {};
app.on('record', r => { _state.record = r; render(); });
""".strip()


DOCS_FORMULAS = """GRIST CODER - Formules Grist natives (colonnes Python)
=======================================================
# Toutes les formules de colonnes Grist sont du Python.
# $col = valeur de la colonne 'col' du record courant.
# Les sections marquees [DETAIL] peuvent etre approfondies en session.

## REFERENCES
$Client                       # ID entier si colonne Reference
$Client.Nom                   # valeur dans la table liee
$Tags.label                   # sur ReferenceList: iterable de valeurs

## LOOKUPS [DETAIL]
Clients.lookupOne(Email=$Email)
Clients.lookupRecords(Statut='actif')
Clients.lookupOne(Nom=$Nom, sort_by='-CA')

## AGREGATION SUR REFERENCES INVERSES [DETAIL]
len(Commandes.lookupRecords(Client=$id))
sum(Commandes.lookupRecords(Client=$id).Montant)
sum(r.Montant for r in Commandes.lookupRecords(Client=$id)
    if r.Statut == 'paye')

## AGREGATION SUR REFERENCELIST
SUM($Lignes.Montant)
MAX($Items.Score)
', '.join($Tags.label)

## DATES [DETAIL]
TODAY()
NOW()
DATEADD($Date, months=1)
$DateFin - $DateDebut         # duree en jours (entier)
$Date.year / $Date.month / $Date.day

## LOGIQUE
IF($CA > 1000, 'Grand', 'Petit')
IFERROR($Montant / $Qte, 0)
$Statut if $Actif else 'Inactif'

## TEXTE
$Nom.upper() / $Nom.lower() / $Nom.strip()
LEN($Nom)
LEFT($Texte, 3) / RIGHT($Texte, 3)
SUBSTITUTE($Texte, 'old', 'new')
f"{$Prenom} {$Nom}"

## MATH
ROUND($CA, 2)
ABS($Delta)
$CA * $Taux / 100

## COLONNES DECLENCHEES (trigger columns) [DETAIL]
# isFormula=False + formula non vide -> execute a la creation/modif
# Timestamp creation : rec.CreatedAt = NOW()
# Timestamp modif    : rec.UpdatedAt = NOW()  (recalcOnChanges=True)
# Slug auto          : rec.Slug = $Nom.lower().replace(' ', '-')
""".strip()


# ── STATIC RESOURCES ──────────────────────────────────────────────────────────

STATIC_RESOURCES = [
    {"uri":         "grist-coder://docs/schema",
     "name":        "Schema Recipes",
     "description": "Lire avant de creer tables/colonnes. Types, Ref:, formules, phases d import.",
     "mimeType":    "text/plain"},

    {"uri":         "grist-coder://docs/artefacts",
     "name":        "Artefact Recipes",
     "description": "Lire avant de coder un artefact. Templates HTML/JS : grist, html, app + navigation.",
     "mimeType":    "text/plain"},

    {"uri":         "grist-coder://docs/playbook",
     "name":        "Page Playbook",
     "description": "Lire avant de creer pages/widgets. Sequences exactes, decision tree, layouts, liaisons.",
     "mimeType":    "text/plain"},

    {"uri":         "grist-coder://docs/app-patterns",
     "name":        "App Patterns",
     "description": "Patterns multi-widgets : auto-provisioning, navigation, sync, setOption, contexte Artefactory.",
     "mimeType":    "text/plain"},

    {"uri":         "grist-coder://docs/formulas",
     "name":        "Grist Formulas",
     "description": "Formules colonnes Python natives Grist : references, lookups, agregation, dates, trigger columns.",
     "mimeType":    "text/plain"},
]

RESOURCE_TEMPLATES = [
    {"uriTemplate":  "grist-coder://context/{token}",
     "name":         "Project Context",
     "description":  "Snapshot live : tables+colonnes+artefacts+pages. Lire en debut de session.",
     "mimeType":     "application/json"},

    {"uriTemplate":  "grist-coder://code/{token}",
     "name":         "Artefacts Code",
     "description":  "Source de tous les artefacts. Lire avant d iterer sur une app existante.",
     "mimeType":     "text/plain"},

    {"uriTemplate":  "grist-coder://playbook/{scenario}",
     "name":         "Scenario Playbook",
     "description":  "Guide cible par scenario. Valeurs: dashboard | fiche | table | full-app | master-detail",
     "mimeType":     "text/plain"},
]

# ── RESOURCE READ ─────────────────────────────────────────────────────────────

async def _read_resource(uid_key, mcp_sid, uri):
    if uri == "grist-coder://docs/schema":
        return {"uri": uri, "mimeType": "text/plain", "text": DOCS_SCHEMA}

    if uri == "grist-coder://docs/artefacts":
        return {"uri": uri, "mimeType": "text/plain", "text": DOCS_ARTEFACTS}

    if uri == "grist-coder://docs/playbook":
        return {"uri": uri, "mimeType": "text/plain", "text": DOCS_PLAYBOOK}

    if uri == "grist-coder://docs/app-patterns":
        return {"uri": uri, "mimeType": "text/plain", "text": DOCS_APP_PATTERNS}

    if uri == "grist-coder://docs/formulas":
        return {"uri": uri, "mimeType": "text/plain", "text": DOCS_FORMULAS}

    if uri.startswith("grist-coder://playbook/"):
        scenario = uri.split("/")[-1]
        content = PLAYBOOKS.get(scenario)
        if not content:
            available = " | ".join(PLAYBOOKS.keys())
            content = f"Scenario inconnu : '{scenario}'\nDisponibles : {available}"
        return {"uri": uri, "mimeType": "text/plain", "text": content}

    if uri.startswith("grist-coder://context/"):
        token = uri.split("/")[-1]
        ctx = registry.resolve(uid_key, token)
        if not ctx: raise ValueError(f"Session inconnue : {token}")
        ctx.subscribers.add(mcp_sid)
        snapshot = {
            "doc":    {"title": ctx.doc_title, "doc_id": ctx.doc_id, "site_url": ctx.site_url},
            "canvas": {"sha": hashlib.sha1(ctx.canvas.encode()).hexdigest()[:8] if ctx.canvas else None,
                       "lines": len(ctx.canvas.splitlines()) if ctx.canvas else 0},
            "tables": [], "artefacts": [], "pages": [],
        }
        # Tables + colonnes
        try:
            tables_resp = await grist_get(ctx, "tables")
            table_ids = [t["id"] for t in tables_resp.get("tables", [])
                         if not t["id"].startswith("_grist_")]
            snapshot["tables"] = table_ids
            # Colonnes en parallele
            async def fetch_cols(tid):
                try:
                    cols_resp = await grist_get(ctx, f"tables/{tid}/columns")
                    return tid, [
                        {k: v for k, v in {
                            "id":      c["id"],
                            "type":    c["fields"].get("type",""),
                            "formula": c["fields"].get("formula","") or None,
                            "label":   c["fields"].get("label","") or None,
                        }.items() if v}
                        for c in cols_resp.get("columns", [])
                        if not c["id"].startswith("gristHelper_")
                    ]
                except Exception:
                    return tid, []
            results = await asyncio.gather(*[fetch_cols(tid) for tid in table_ids])
            snapshot["schema"] = dict(results)
        except Exception as e:
            snapshot["tables_error"] = str(e)
        # Artefacts
        try:
            arts_resp = await grist_get(ctx, "tables/Artefacts/records?limit=200")
            snapshot["artefacts"] = [
                {"nom":         r["fields"].get("Nom",""),
                 "type":        r["fields"].get("Type",""),
                 "description": r["fields"].get("Description",""),
                 "isDoc":       bool(r["fields"].get("IsDoc",False)),
                 "icon":        r["fields"].get("Icon",""),
                 "updatedAt":   r["fields"].get("UpdatedAt","")}
                for r in arts_resp.get("records",[])
            ]
        except Exception:
            snapshot["artefacts"] = []
        # Pages + app graph (sections par vue)
        try:
            pages_resp    = await grist_get(ctx, "tables/_grist_Pages/records")
            views_resp    = await grist_get(ctx, "tables/_grist_Views/records")
            sections_resp = await grist_get(ctx, "tables/_grist_Views_section/records")
            tables_meta   = await grist_get(ctx, "tables/_grist_Tables/records")
            views_map     = {r["id"]: r["fields"].get("name","") for r in views_resp.get("records",[])}
            table_ref_map = {r["id"]: r["fields"].get("tableId","") for r in tables_meta.get("records",[])}
            type_map      = {"record": "grid", "single": "card", "custom": "custom", "chart": "chart"}

            def _extract_artefact(options_str):
                if not options_str: return None
                try:
                    cv = json.loads(json.loads(options_str).get("customView") or "null")
                    if not cv: return None
                    m = re.search(r'[?&]a=([^&]+)', cv.get("url",""))
                    if m: return m.group(1)
                    wo = cv.get("widgetOptions")
                    if wo: return (json.loads(wo) if isinstance(wo,str) else wo).get("artefact")
                except Exception: pass
                return None

            sections_by_view: dict = {}
            for r in sections_resp.get("records", []):
                f = r["fields"]
                pid = f.get("parentId", 0)
                if not pid: continue
                sections_by_view.setdefault(pid, []).append({
                    "id":         r["id"],
                    "type":       type_map.get(f.get("parentKey",""), f.get("parentKey","")),
                    "table":      table_ref_map.get(f.get("tableRef",0), ""),
                    "artefact":   _extract_artefact(f.get("options","")),
                    "linked_to":  f.get("linkSrcSectionRef") or None,
                    "linked_col": f.get("linkTargetColRef") or None,
                })

            snapshot["pages"] = [
                {"page_id":  r["id"],
                 "view_ref": r["fields"].get("viewRef",0),
                 "name":     views_map.get(r["fields"].get("viewRef",0),""),
                 "indent":   r["fields"].get("indentation",0),
                 "sections": sections_by_view.get(r["fields"].get("viewRef",0), [])}
                for r in pages_resp.get("records",[])
            ]
        except Exception as e:
            snapshot["pages"] = []
            snapshot["pages_error"] = str(e)
        return {"uri": uri, "mimeType": "application/json",
                "text": json.dumps(snapshot, ensure_ascii=False, indent=2)}

    if uri.startswith("grist-coder://code/"):
        token = uri.split("/")[-1]
        ctx = registry.resolve(uid_key, token)
        if not ctx: raise ValueError(f"Session inconnue : {token}")
        try:
            arts_resp = await grist_get(ctx, "tables/Artefacts/records?limit=200")
            lines = [f"# Code source artefacts — {ctx.doc_title}\n"]
            for r in arts_resp.get("records",[]):
                f = r.get("fields",{})
                nom  = f.get("Nom","")
                typ  = f.get("Type","")
                code = f.get("Code","")
                lines.append(f"=== {nom} ({typ}) ===")
                lines.append(code)
                lines.append("")
            return {"uri": uri, "mimeType": "text/plain", "text": "\n".join(lines)}
        except Exception as e:
            return {"uri": uri, "mimeType": "text/plain",
                    "text": f"Table Artefacts non trouvee. Appeler artefact_init() d abord.\nErreur: {e}"}

    raise ValueError(f"Resource inconnue : {uri}")


def _resources_list(uid_key):
    res = list(STATIC_RESOURCES)
    for s in registry.list_sessions(uid_key):
        t, title = s["token"], s["doc_title"]
        res += [
            {"uri": f"grist-coder://context/{t}",
             "name": f"Context — {title}",
             "description": "Snapshot complet : tables, colonnes, artefacts, pages.",
             "mimeType": "application/json"},
            {"uri": f"grist-coder://code/{t}",
             "name": f"Code — {title}",
             "description": "Code source de tous les artefacts.",
             "mimeType": "text/plain"},
        ]
    return res

# ── TOOL CALL ─────────────────────────────────────────────────────────────────

_active_tokens: dict[str, str] = {}

async def call_tool(uid_key, mcp_sid, name, args):
    if name == "sessions_list":
        s = registry.list_sessions(uid_key)
        if s: return s
        # Debug: lister tous les users connus pour diagnostiquer le mismatch d uid
        all_uids = {u: len(d["sessions"]) for u, d in registry._users.items()}
        return {
            "info": "Aucun widget connecte.",
            "hint": "Ouvrez le widget Grist Coder dans Grist. Connexion automatique.",
            "_debug_caller_uid": uid_key,
            "_debug_all_uids": all_uids,
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

    # ── Session info
    if name == "session_info":
        info = ctx.meta()
        try:
            schema = await grist_get(ctx, "tables")
            info["tables"] = [t["id"] for t in schema.get("tables", [])]
        except Exception as e:
            info["tables"] = []; info["grist_error"] = str(e)
        if "Artefacts" in info.get("tables", []):
            try:
                data = await grist_get(ctx, "tables/Artefacts/records?limit=200")
                recs = data.get("records", [])
                info["artefacts_count"] = len(recs)
                info["artefacts"] = [
                    {"nom": r["fields"].get("Nom",""), "type": r["fields"].get("Type",""),
                     "isDoc": bool(r["fields"].get("IsDoc",False))}
                    for r in recs
                ]
                info["hint"] = f"Lire grist-coder://context/{ctx.token} pour snapshot complet"
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
        _notify_resource(uid_key, f"grist-coder://context/{ctx.token}")
        return {"ok": True, "sha": sha}

    if name == "canvas_patch":
        old, new = args["old_str"], args["new_str"]
        if old not in ctx.canvas:
            return {"error": f"Fragment introuvable : {old!r:.80}"}
        ctx.canvas = ctx.canvas.replace(old, new, 1)
        sha = hashlib.sha1(ctx.canvas.encode()).hexdigest()[:8]
        ctx.history.appendleft({"ts": time.time(), "sha": sha, "op": "patch"})
        _push(uid_key, {"type": "canvas_patched", "token": ctx.token, "sha": sha})
        _notify_resource(uid_key, f"grist-coder://context/{ctx.token}")
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
            return {"error": "Timeout 15s : widget ferme ou iframe vide."}
        finally:
            _screenshot_waiters.pop(ctx.token, None)

    # ── Artefact
    if name == "artefact_init":
        try:
            tables_data = await grist_get(ctx, "tables")
            if "Artefacts" in [t["id"] for t in tables_data.get("tables", [])]:
                cols = await grist_get(ctx, "tables/Artefacts/columns")
                return {"ok": True, "status": "exists",
                        "columns": [c["id"] for c in cols.get("columns", [])]}
        except Exception:
            pass
        try:
            await grist_post(ctx, "tables", ARTEFACTS_TABLE_DEF)
            return {"ok": True, "status": "created",
                    "columns": ["Nom","Type","Code","Description","Dependencies",
                                "IsDoc","Icon","Output","UpdatedAt"]}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Grist lecture
    if name == "grist_schema":
        table_id = args.get("table_id")
        if table_id:
            cols = await grist_get(ctx, f"tables/{table_id}/columns")
            return {"table_id": table_id, "columns": cols.get("columns", [])}
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

    # ── Document structure
    if name == "grist_apply":
        try:
            result = await grist_apply(ctx, args["actions"])
            return {"ok": True, "result": result}
        except Exception as e:
            return {"error": str(e)}

    if name == "grist_views_list":
        try:
            pages_resp   = await grist_get(ctx, "tables/_grist_Pages/records")
            views_resp   = await grist_get(ctx, "tables/_grist_Views/records")
            sections_resp= await grist_get(ctx, "tables/_grist_Views_section/records")
            views_map = {r["id"]: r["fields"].get("name","") for r in views_resp.get("records",[])}
            sects_by_view: dict[int, list] = {}
            for r in sections_resp.get("records",[]):
                vid = r["fields"].get("parentId", 0)
                sects_by_view.setdefault(vid, []).append({
                    "id":   r["id"],
                    "type": r["fields"].get("parentKey",""),
                })
            pages = []
            for r in pages_resp.get("records",[]):
                vref = r["fields"].get("viewRef", 0)
                pages.append({
                    "page_id":  r["id"],
                    "view_ref": vref,
                    "name":     views_map.get(vref,""),
                    "sections": sects_by_view.get(vref,[]),
                })
            return {"pages": pages, "total": len(pages)}
        except Exception as e:
            return {"error": str(e)}

    if name in ("grist_section_configure", "grist_view_add_widget"):
        def _make_widget_options(artefact, widget_url):
            """Genere le JSON options/customView correctement echappe."""
            url = widget_url or (HOST_URL + "/")
            if artefact:
                sep = "&" if "?" in url else "?"
                url = url + sep + "a=" + artefact
            widget_options_val = json.dumps({"artefact": artefact}) if artefact else None
            custom_view_str = json.dumps({
                "mode": "url", "url": url, "access": "full",
                "widgetDef": None, "pluginId": "", "sectionId": "",
                "renderAfterReady": False, "widgetId": None,
                "widgetOptions": widget_options_val, "columnsMapping": None,
            })
            return json.dumps({
                "verticalGridlines": True, "horizontalGridlines": True,
                "zebraStripes": False, "numFrozen": 0,
                "customView": custom_view_str,
            })

        if name == "grist_section_configure":
            section_ref      = int(args["section_ref"])
            artefact         = args.get("artefact", "")
            widget_url       = args.get("widget_url", "")
            link_section_ref = int(args.get("link_section_ref") or 0)
            try:
                fields: dict = {"options": _make_widget_options(artefact, widget_url)}
                if link_section_ref:
                    fields["linkSrcSectionRef"] = link_section_ref
                    fields["linkSrcColRef"]     = 0
                    fields["linkTargetColRef"]  = 0
                await grist_apply(ctx, [["UpdateRecord", "_grist_Views_section", section_ref, fields]])
                return {"ok": True, "section_ref": section_ref,
                        "artefact": artefact or "(coding mode)",
                        "hint": f"Section {section_ref} configuree."}
            except Exception as e:
                return {"error": str(e)}

        if name == "grist_view_add_widget":
            view_ref         = int(args["view_ref"])
            table_id         = args["table_id"]
            artefact         = args.get("artefact", "")
            grid_section_ref = int(args.get("grid_section_ref") or 0)
            try:
                # Trouver tableRef
                tables_resp = await grist_get(ctx, "tables/_grist_Tables/records")
                table_ref = next((r["id"] for r in tables_resp.get("records", [])
                                  if r["fields"].get("tableId") == table_id), None)
                if not table_ref:
                    return {"error": f"Table '{table_id}' introuvable"}
                # Creer la section custom
                r = await grist_apply(ctx, [["CreateViewSection", table_ref, view_ref, "custom", None, None]])
                section_ref = r["result"]["retValues"][0]["sectionRef"]
                # Recup sections existantes pour le layout
                sects_resp = await grist_get(ctx, "tables/_grist_Views_section/records")
                view_sects = [r["id"] for r in sects_resp.get("records", [])
                              if r["fields"].get("parentId") == view_ref
                              and r["id"] != section_ref]
                # Configurer widget + lien + layout
                section_fields: dict = {"options": _make_widget_options(artefact, "")}
                if grid_section_ref:
                    section_fields["linkSrcSectionRef"] = grid_section_ref
                    section_fields["linkSrcColRef"]     = 0
                    section_fields["linkTargetColRef"]  = 0
                # Layout : sections existantes a gauche + nouveau widget a droite
                if view_sects:
                    left_children = [{"leaf": s} for s in view_sects]
                    if len(left_children) == 1:
                        layout = {"children": [{"children": [left_children[0], {"leaf": section_ref}]}], "collapsed": []}
                    else:
                        layout = {"children": left_children + [{"leaf": section_ref}], "collapsed": []}
                else:
                    layout = {"children": [{"leaf": section_ref}], "collapsed": []}
                await grist_apply(ctx, [
                    ["UpdateRecord", "_grist_Views_section", section_ref, section_fields],
                    ["UpdateRecord", "_grist_Views", view_ref, {"layoutSpec": json.dumps(layout)}],
                ])
                return {"ok": True, "section_ref": section_ref, "view_ref": view_ref,
                        "artefact": artefact or "(coding mode)",
                        "hint": f"Widget ajoute a la vue {view_ref}."}
            except Exception as e:
                return {"error": str(e)}

    if name == "grist_view_create":
        table_id   = args.get("table_id")
        page_name  = args.get("page_name") or table_id
        widget_url = args.get("widget_url", HOST_URL + "/")
        artefact   = args.get("artefact")
        if artefact:
            sep = "&" if "?" in widget_url else "?"
            widget_url = widget_url + sep + "a=" + artefact
        try:
            # Trouver le tableRef entier depuis _grist_Tables
            tables_meta = await grist_get(ctx, "tables/_grist_Tables/records")
            table_ref = next(
                (r["id"] for r in tables_meta.get("records",[])
                 if r["fields"].get("tableId") == table_id),
                None
            )
            if not table_ref:
                return {"error": f"Table '{table_id}' non trouvee dans le document"}

            # Etape 1 : creer page avec section grille
            await grist_apply(ctx, [["CreateViewSection", table_ref, 0, "record", None, None]])

            # Trouver la nouvelle vue (max viewRef)
            views_resp = await grist_get(ctx, "tables/_grist_Views/records")
            view_ref = max((r["id"] for r in views_resp.get("records",[])), default=0)

            # Trouver la section grille creee
            sects_resp = await grist_get(ctx, "tables/_grist_Views_section/records")
            grid_sections = [r for r in sects_resp.get("records",[])
                             if r["fields"].get("parentId") == view_ref
                             and r["fields"].get("parentKey") == "record"]
            grid_ref = max((r["id"] for r in grid_sections), default=0)

            # Etape 2 : ajouter section custom widget sur la meme vue
            await grist_apply(ctx, [["CreateViewSection", table_ref, view_ref, "custom", None, None]])

            # Trouver la nouvelle section custom
            sects_resp2 = await grist_get(ctx, "tables/_grist_Views_section/records")
            custom_sections = [r for r in sects_resp2.get("records",[])
                               if r["fields"].get("parentId") == view_ref
                               and r["fields"].get("parentKey") == "custom"]
            custom_ref = max((r["id"] for r in custom_sections), default=0)
            if not custom_ref:
                return {"error": "Section custom non trouvee apres creation"}

            # Etape 3 : configurer URL + lier a la grille via User Action
            # IMPORTANT: grist_patch sur _grist_Views_section stocke options comme objet
            # ce qui fait crasher CustomView.ts:129 (JSON.parse([object Object])).
            # UpdateRecord via /apply respecte le type Text et stocke bien une string.
            # Format exact copie depuis section Grist UI existante :
            # customView est une JSON string encodee dans options (pas un objet nested)
            # widgetOptions: JSON string passee au widget via grist.onOptions()
            widget_options_val = json.dumps({"artefact": artefact}) if artefact else None
            custom_view_str = json.dumps({
                "mode": "url", "url": widget_url, "access": "full",
                "widgetDef": None, "pluginId": "", "sectionId": "",
                "renderAfterReady": False, "widgetId": None,
                "widgetOptions": widget_options_val, "columnsMapping": None,
            })
            section_fields: dict = {
                "options": json.dumps({
                    "verticalGridlines": True, "horizontalGridlines": True,
                    "zebraStripes": False, "numFrozen": 0,
                    "customView": custom_view_str,
                }),
            }
            if grid_ref:
                section_fields["linkSrcSectionRef"] = grid_ref
            await grist_apply(ctx, [["UpdateRecord", "_grist_Views_section", custom_ref, section_fields]])

            # Etape 4 : definir le layout et le nom via User Action UpdateRecord
            # IMPORTANT: grist_patch sur _grist_Views stocke les JSON strings comme objets
            # ce qui casse le parsing cote Grist frontend. UpdateRecord via /apply est correct.
            layout_spec = json.dumps({
                "children": [{"children": [{"leaf": grid_ref}, {"leaf": custom_ref}]}],
                "collapsed": []
            })
            update_fields: dict = {"layoutSpec": layout_spec}
            if page_name:
                update_fields["name"] = page_name
            await grist_apply(ctx, [["UpdateRecord", "_grist_Views", view_ref, update_fields]])

            return {
                "ok": True, "view_ref": view_ref, "page_name": page_name,
                "grid_section": grid_ref, "widget_section": custom_ref,
                "widget_url": widget_url,
                "hint": f"Page '{page_name}' creee : grille {table_id} + widget custom lies."
            }
        except Exception as e:
            return {"error": str(e)}

    return {"error": f"outil inconnu : {name}"}

# ── DISPATCH ──────────────────────────────────────────────────────────────────

async def dispatch(uid_key, mcp_sid, method, params):
    if method == "initialize":
        return {
            "protocolVersion": MCP_VER,
            "serverInfo": {"name": "grist-coder", "version": "5.2.0",
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
        if uri.startswith("grist-coder://context/"):
            ctx = registry.resolve(uid_key, uri.split("/")[-1])
            if ctx: ctx.subscribers.add(mcp_sid)
        return {}
    if method == "resources/unsubscribe":
        uri = params.get("uri", "")
        if uri.startswith("grist-coder://context/"):
            ctx = registry.resolve(uid_key, uri.split("/")[-1])
            if ctx: ctx.subscribers.discard(mcp_sid)
        return {}
    if method in ("logging/setLevel", "ping"):
        return {}
    raise ValueError(f"methode inconnue : {method}")

# ── FASTAPI ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app):
    print(f"Grist Coder v5.1 · {HOST_URL}")
    print(f"  tools: {len(TOOLS)}  prompts: {len(PROMPTS)}")
    print(f"  resources: {len(STATIC_RESOURCES)} static + {len(RESOURCE_TEMPLATES)} templates")
    print(f"  widget: {'widget.html' if WIDGET_PATH.exists() else 'MANQUANT'}")
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
    # Chercher parmi les users existants qui ont ce grist_key (ex: enregistre via widget)
    for uid, udata in registry._users.items():
        if udata.get("grist_key") == raw_bearer and udata["sessions"]:
            registry._grist_key_to_uid[raw_bearer] = uid
            return uid
    # Fallback : uid temporaire non cache pour permettre retry ulterieur
    uid_key = f"key:{hashlib.sha1(raw_bearer.encode()).hexdigest()[:12]}"
    registry.provision(uid_key)
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
    return {"ok": True, "version": "5.2.0", "mcp_protocol": MCP_VER,
            "sessions": total, "users": len(registry._users),
            "tools": len(TOOLS), "prompts": len(PROMPTS),
            "resources": {"static": len(STATIC_RESOURCES), "templates": len(RESOURCE_TEMPLATES)},
            "widget": WIDGET_PATH.exists()}


@app.get("/", response_class=HTMLResponse)
async def widget_html():
    if WIDGET_PATH.exists():
        return HTMLResponse(WIDGET_PATH.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>widget.html manquant</h1>", status_code=503)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("grist_coder:app", host="0.0.0.0", port=8742, reload=True)
