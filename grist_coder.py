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

import asyncio, base64, hashlib, json, os, subprocess, sys, time, uuid
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
Tu es connecte a Grist Coder MCP v5.1 - service de developpement d apps Grist.

CONCEPT FONDAMENTAL
Le document Grist ouvert dans le widget = ta codebase complete.
  Tables de donnees (Batiments, Interventions...)  = modele de donnees
  Table Artefacts (widgets HTML/JS)               = fichiers source
  Pages Grist (grille + widget par page)          = ecrans de l app
  Schema relationnel (Ref:, formules, visibleCol) = architecture

OUTILS (17)
Sessions  : sessions_list, session_select, session_info
Canvas    : canvas_read, canvas_write, canvas_patch, canvas_exec, canvas_screenshot
Artefact  : artefact_init
Grist R   : grist_schema, grist_records, grist_sql
Grist W   : grist_records_add, grist_records_patch, grist_upsert
Document  : grist_views_list, grist_view_create

RESSOURCES - lire au debut de chaque session
  grist-coder://docs/schema        -> recettes REST exactes : Phase 1/2/3 + formules + pages
  grist-coder://docs/artefacts     -> templates HTML/JS prets a l emploi + navigation inter-artefacts
  grist-coder://context/{token}    -> snapshot complet : tables+colonnes+artefacts+pages (1 lecture)
  grist-coder://code/{token}       -> code source de tous les artefacts existants

WORKFLOW OPTIMAL - APP COMPLETE ET STRUCTUREE
  1. sessions_list()
  2. resources/read grist-coder://context/{token}   -> etat complet en 1 lecture
  3. resources/read grist-coder://docs/schema       -> recettes schema
  4. resources/read grist-coder://docs/artefacts    -> patterns code
  5. Creer tables : Phase 1 (sans refs) -> Phase 2 (Ref:TableId) -> Phase 3 (visibleCol)
  6. Pour chaque ecran de l app :
     a. canvas_write(code_html)          -> live dans le widget
     b. canvas_screenshot()             -> valider le rendu
     c. canvas_patch() si ajustements
     d. grist_upsert('Artefacts', ...)  -> persister dans Grist
     e. grist_view_create(table, page)  -> page Grist avec grille + widget lies
  7. resources/read grist-coder://context/{token}   -> verifier l etat final

REGLES CANVAS
  canvas_read() AVANT canvas_patch() - old_str doit etre exact et unique
  canvas_patch >> canvas_write - preserve l historique, evite regressions
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
    {"name": "grist_views_list",
     "description": "Liste les pages (vues) du document avec leurs sections.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True}},

    {"name": "grist_view_create",
     "description": "Cree une page Grist avec grille de donnees + widget custom lies. Structure un ecran de l app.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "table_id":   {"type": "string", "description": "Table source (ex: 'Batiments')"},
                         "page_name":  {"type": "string", "description": "Nom de la page dans Grist"},
                         "widget_url": {"type": "string", "description": "URL du widget custom (defaut: HOST_URL/)"}},
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
            "3. resources/read grist-coder://docs/schema -> recettes schema\n"
            "4. resources/read grist-coder://docs/artefacts -> patterns code\n"
            "5. Creer les tables (Phase 1 sans refs, Phase 2 Ref:, Phase 3 visibleCol)\n"
            "6. Pour chaque ecran :\n"
            "   a. canvas_write(code) -> live preview\n"
            "   b. canvas_screenshot() -> valider\n"
            "   c. grist_upsert('Artefacts', ...) -> persister\n"
            "   d. grist_view_create(table, page) -> structurer le document\n"
            "7. resources/read grist-coder://context/{token} -> verifier"
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
grist_view_create(
  table_id="Batiments",
  page_name="Batiments",
  widget_url="http://localhost:8742/"  <- HOST_URL
)
-> Cree page Grist : grille Batiments (gauche) + widget custom (droite) lies
-> Repeter pour chaque table/ecran de l app

ORDRE DE CREATION RECOMMANDE
1. Tables sans Ref: (Batiments, Prestataires, Categories...)
2. Tables avec Ref: (Locaux->Batiments, Interventions->Batiments+Prestataires...)
3. Configurer visibleCol sur toutes les colonnes Ref:
4. Ajouter formules de comptage/somme
5. Pour chaque table : grist_view_create() -> page complete dans le document
""".strip()

DOCS_ARTEFACTS = """GRIST CODER - Recettes artefacts HTML/JS
=========================================

TEMPLATE BASE - TYPE: grist (widget reactif aux donnees)
<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8">
<script src="https://docs.getgrist.com/grist-plugin-api.js"></script>
<script src="https://cdn.tailwindcss.com"></script>
</head><body><div id="app" class="p-4"></div>
<script>
const appContext = {
  isGristCoder: typeof window.app !== 'undefined' && typeof window.app.navigate === 'function',
  app: window.app || { navigate: p => showToast(p,'info'), emit:()=>{}, on:()=>{}, setState:()=>{}, state:{} }
};
async function safeLoad(tableName) {
  try {
    const d = await grist.docApi.fetchTable(tableName);
    const n = d.id?.length||0; const rows=[];
    for(let i=0;i<n;i++){const r={id:d.id[i]};Object.keys(d).forEach(k=>{if(k!=='id')r[k]=d[k][i];});rows.push(r);}
    return rows;
  } catch(e){console.warn('Table '+tableName+' non trouvee');return[];}
}
function showToast(msg,type='info'){
  const c={info:'#3b82f6',success:'#10b981',error:'#ef4444',warning:'#f59e0b'};
  const t=document.createElement('div');
  t.style.cssText='position:fixed;bottom:20px;right:20px;padding:12px 20px;background:'+c[type]+
    ';color:#fff;border-radius:8px;font-size:13px;z-index:9999;box-shadow:0 4px 12px rgba(0,0,0,.2)';
  t.textContent=msg;document.body.appendChild(t);setTimeout(()=>t.remove(),3500);
}
let _state = { loading:true, error:null, data:[] };
async function loadData() {
  _state.data = await safeLoad('MaTable');
  _state.loading = false;
  render();
}
function render() {
  const el = document.getElementById('app');
  if(_state.loading){el.innerHTML='<div class="text-gray-400 p-8 text-center">Chargement...</div>';return;}
  if(!_state.data.length){el.innerHTML='<div class="text-gray-400 text-center py-8">Aucune donnee</div>';return;}
  el.innerHTML = _state.data.map(r=>`
    <div class="flex items-center p-3 border-b hover:bg-gray-50 cursor-pointer" onclick="select(${r.id})">
      <span class="flex-1 font-medium">${r.Nom||r.id}</span>
    </div>`).join('');
}
grist.ready({requiredAccess:'read table'});
loadData();
</script></body></html>

---
TEMPLATE BASE - TYPE: html (UI independante, pas de donnees Grist)
<!-- Pas de grist.ready() si aucune donnee Grist necessaire -->
<!-- window.app disponible si isGristCoder -->
<!DOCTYPE html><html><head><meta charset="UTF-8">
<script src="https://cdn.tailwindcss.com"></script>
</head><body>...</body></html>

---
NAVIGATION INTER-ARTEFACTS
function navigateTo(path) {
  if (appContext.isGristCoder) appContext.app.navigate(path);
  else showToast('Ouvrir "'+path+'" dans Grist','info');
}

EVENEMENTS INTER-ARTEFACTS
// Emettre
appContext.app.emit('item-select', {id: 42, nom: 'Mairie'});
// Ecouter
appContext.app.on('item-select', ({id, nom}) => { filtrer(id); render(); });
// Etat partage
appContext.app.setState({currentItem: 42});
const current = appContext.app.state.currentItem;
// Pas en mode widget : ignorer les filtres
if (!appContext.isGristCoder) { _filteredId = null; }

---
MANIFESTE APP - TYPE: app (JSON)
{
  "name": "MonApp",
  "icon": "🏢",
  "tables": ["Batiments", "Interventions", "Prestataires"],
  "routes": [
    {"path": "/",            "label": "Dashboard",   "icon": "📊", "artefact": "Dashboard"},
    {"path": "/batiments",   "label": "Batiments",   "icon": "🏢", "artefact": "ListeBatiments"},
    {"path": "/interventions","label": "Interventions","icon": "🔧","artefact": "Interventions"}
  ]
}

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
Artefacts      : Dashboard(html), ListeBatiments(grist), KanbanInterventions(grist), FicheDetail(grist)
Pages          : grist_view_create x4 -> document structure complet
""".strip()

# ── STATIC RESOURCES ──────────────────────────────────────────────────────────

STATIC_RESOURCES = [
    {"uri":         "grist-coder://docs/schema",
     "name":        "Schema Recipes",
     "description": "Recettes REST exactes : Phase 1/2/3, types de colonnes, formules, pages.",
     "mimeType":    "text/plain"},

    {"uri":         "grist-coder://docs/artefacts",
     "name":        "Artefact Recipes",
     "description": "Templates HTML/JS prets a l emploi : grist, html, react, app + navigation inter-artefacts.",
     "mimeType":    "text/plain"},
]

RESOURCE_TEMPLATES = [
    {"uriTemplate":  "grist-coder://context/{token}",
     "name":         "Project Context",
     "description":  "Snapshot complet du projet : tables+colonnes+artefacts+pages. Lire une fois au demarrage.",
     "mimeType":     "application/json"},

    {"uriTemplate":  "grist-coder://code/{token}",
     "name":         "Artefacts Code",
     "description":  "Code source complet de tous les artefacts. Pour iterer sur une app existante.",
     "mimeType":     "text/plain"},
]

# ── RESOURCE READ ─────────────────────────────────────────────────────────────

async def _read_resource(uid_key, mcp_sid, uri):
    if uri == "grist-coder://docs/schema":
        return {"uri": uri, "mimeType": "text/plain", "text": DOCS_SCHEMA}

    if uri == "grist-coder://docs/artefacts":
        return {"uri": uri, "mimeType": "text/plain", "text": DOCS_ARTEFACTS}

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
        # Pages
        try:
            pages_resp  = await grist_get(ctx, "tables/_grist_Pages/records")
            views_resp  = await grist_get(ctx, "tables/_grist_Views/records")
            views_map   = {r["id"]: r["fields"].get("name","") for r in views_resp.get("records",[])}
            snapshot["pages"] = [
                {"page_id":  r["id"],
                 "view_ref": r["fields"].get("viewRef",0),
                 "name":     views_map.get(r["fields"].get("viewRef",0),""),
                 "indent":   r["fields"].get("indentation",0)}
                for r in pages_resp.get("records",[])
            ]
        except Exception:
            snapshot["pages"] = []
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

    if name == "grist_view_create":
        table_id   = args.get("table_id")
        page_name  = args.get("page_name") or table_id
        widget_url = args.get("widget_url", HOST_URL + "/")
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
            custom_view_str = json.dumps({
                "mode": "url", "url": widget_url, "access": "full",
                "widgetDef": None, "pluginId": "", "sectionId": "",
                "renderAfterReady": False, "widgetId": None,
                "widgetOptions": None, "columnsMapping": None,
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
            "serverInfo": {"name": "grist-coder", "version": "5.1.0",
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
    return {"ok": True, "version": "5.1.0", "mcp_protocol": MCP_VER,
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
