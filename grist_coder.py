"""
GRIST CODER · MCP Server v4.3 · streamable HTTP spec 2025-03-26
───────────────────────────────────────────────────────────────
AUTH - IDENTITE PAR GRIST USER ID (uid_key = 'uid:{id}')

  Widget     -> grist.docApi.getAccessToken() -> token court-terme
             -> POST /register {accessToken, docId, siteUrl, userId}
             -> serveur verifie le token -> obtient grist_user_id stable
             -> retourne arto-xxx
             -> SSE via GET /mcp?token=arto-xxx
             -> appels MCP via POST /mcp + Authorization: Bearer arto-xxx
             -> PAS DE CLE API a saisir dans le widget

  Claude Desktop -> Authorization: Bearer <grist_key>
                 -> serveur verifie via GET /api/profile/user -> grist_user_id
                 -> uid_key = 'uid:{grist_user_id}' -> acces aux sessions du user

  Isolation : sessions indexees par uid_key (partagees widget + Claude Desktop).
  Securite  : ni la cle Grist ni le token temporaire ne transitent en query param.

TOOLS (13)
  sessions : sessions_list, session_select, session_info
  canvas   : canvas_read, canvas_write, canvas_patch, canvas_exec
  grist R  : grist_schema, grist_records, grist_sql
  grist W  : grist_records_add, grist_records_patch, grist_upsert

FLOW CLAUDE DESKTOP
  1. initialize()           -> recoit SERVER_INSTRUCTIONS
  2. sessions_list()        -> docs Grist ouverts avec cette cle
  3. grist_schema()         -> tables disponibles
  4. grist_records(table)   -> lire les donnees
  5. canvas_write/patch()   -> ecrire/modifier le canvas
  6. canvas_exec()          -> executer
  7. grist_records_add()    -> ecrire les resultats dans Grist

INSTALL   pip install fastapi uvicorn httpx python-dotenv
RUN       uvicorn grist_coder:app --port 8742

CLAUDE DESKTOP  %APPDATA%\\Claude\\claude_desktop_config.json
  {
    "mcpServers": {
      "grist-coder": {
        "type": "http",
        "url": "https://<ngrok>.ngrok-free.app/mcp",
        "headers": { "Authorization": "Bearer <votre_cle_grist>" }
      }
    }
  }
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

HOST_URL   = os.getenv("HOST_URL", "http://localhost:8742")
MCP_VER    = "2025-03-26"

# ── SERVER INSTRUCTIONS ───────────────────────────────────────────────────────

SERVER_INSTRUCTIONS = """
Tu es connecte a Grist Coder - service MCP de canvas de code Grist.

AUTH : Ta cle Bearer = cle Grist de l utilisateur.
Le serveur la verifie aupres de Grist (GET /api/profile/user) et t associe a ton
grist_user_id stable. Tu n as acces qu aux sessions ouvertes par cet utilisateur.

CONCEPT : Le canvas est ton fichier de travail, manipule comme Claude Code :
  canvas_read()              = read_file()
  canvas_patch(old, new)     = str_replace(old, new)   PREFERER
  canvas_write(code)         = write_file()
  canvas_exec()              = bash("python ...")
  grist_schema()             = lister les tables
  grist_records(table)       = lire les donnees
  grist_sql(query)           = SELECT SQL direct
  grist_records_add(t, rows) = INSERT dans Grist
  grist_records_patch(t, rs) = UPDATE dans Grist (par id)
  grist_upsert(t, rs)        = INSERT OR UPDATE sur cle metier

WORKFLOW OPTIMAL :
  1. sessions_list()            -> quels docs sont ouverts ?
  2. grist_schema()             -> tables disponibles
  3. grist_records() ou sql()   -> lire les donnees
  4. canvas_write/patch()       -> coder l analyse
  5. canvas_exec()              -> valider, corriger si returncode != 0
  6. grist_records_add()        -> ecrire les resultats si besoin

REGLES :
  - canvas_read() AVANT canvas_patch() : old_str doit correspondre exactement
  - canvas_patch >> canvas_write : preserves l historique, visible en diff
  - Le widget voit chaque patch en SSE temps reel

RESOURCES disponibles via resources/list :
  grist-coder://canvas/{token}         -> code Python actuel (subscribable)
  grist-coder://schema/{token}         -> tables Grist
  grist-coder://ui/{token}             -> widget interactif (MCP Apps SEP-1865)
  grist-coder://docs/guide             -> guide complet
  grist-coder://docs/grist-api         -> API Grist (widget + REST)
  grist-coder://docs/canvas-patterns   -> recettes Python

PROMPTS disponibles via prompts/list :
  explore-doc, write-canvas, patch-canvas, debug-canvas, grist-formula, analyze-table
""".strip()

# ── Registre ──────────────────────────────────────────────────────────────────

class SessionCtx:
    def __init__(self, doc_id, doc_title, site_url, grist_key="", access_token=""):
        self.doc_id       = doc_id
        self.doc_title    = doc_title or doc_id
        self.site_url     = site_url.rstrip("/")
        self.grist_key    = grist_key    # clé API permanente (Claude Desktop)
        self.access_token = access_token  # token court-terme docApi (widget)
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
    """
    Identite = uid_key = 'uid:{grist_user_id}' (stable, partage widget + Claude Desktop).

    Widget     : envoie accessToken (temp) -> serveur verifie -> uid_key
    Claude Dsk : envoie grist_key (perm)   -> serveur verifie -> uid_key

    _grist_key_to_uid : grist_key (Claude Desktop) -> uid_key  (cache, evite verif a chaque appel)
    """
    def __init__(self):
        self._users: dict[str, dict] = {}            # uid_key -> {sessions, site, grist_key, access_token}
        self._grist_key_to_uid: dict[str, str] = {}  # grist_key -> uid_key

    def get_uid_for_grist_key(self, gk: str) -> str | None:
        return self._grist_key_to_uid.get(gk)

    def provision(self, uid_key: str, *, grist_key: str = "", access_token: str = "", site: str = ""):
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

    def get_site(self, uid_key: str) -> str:
        u = self._users.get(uid_key)
        return u["site"] if u else ""

    def get_api_token(self, uid_key: str) -> str:
        """Credential pour les appels Grist API (grist_key perm ou access_token court-terme)."""
        u = self._users.get(uid_key, {})
        return u.get("grist_key") or u.get("access_token") or ""

    def register_session(self, uid_key: str, doc_id: str, doc_title: str, site_url: str,
                         grist_key: str = "", access_token: str = "") -> "SessionCtx":
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

    def resolve(self, uid_key: str, token: str | None) -> "SessionCtx | None":
        user = self._users.get(uid_key)
        if not user: return None
        sessions = user["sessions"]
        if not sessions: return None
        if token: return sessions.get(token)
        if len(sessions) == 1:
            ctx = next(iter(sessions.values())); ctx.touch(); return ctx
        return max(sessions.values(), key=lambda s: s.last_seen)

    def list_sessions(self, uid_key: str) -> list:
        user = self._users.get(uid_key)
        return [s.meta() for s in user["sessions"].values()] if user else []


registry = UserRegistry()

# ── Index SSE : arto-token -> uid_key ─────────────────────────────────────────
# Peuple lors du POST /register. Vide au redemarrage -> re-registration auto.
_token_to_uid: dict[str, str] = {}

async def fetch_grist_user_profile(site_url: str, bearer_token: str) -> dict | None:
    """GET {site}/api/profile/user avec un token Bearer (grist_key ou accessToken).
    Retourne {id, email, name} ou None."""
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{site_url}/api/profile/user",
                            headers={"Authorization": f"Bearer {bearer_token}"})
            if r.status_code == 200:
                return r.json()
    except Exception:
        pass
    return None

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

def _log(uid_key, level, logger, message, data=None):
    _push(uid_key, {"type": "mcp_notification",
                    "method": "notifications/message",
                    "params": {"level": level, "logger": logger,
                               "data": {"message": message, **(data or {})}}})

# ── Grist HTTP helpers ────────────────────────────────────────────────────────

def _jwt_payload(token: str) -> dict:
    """Decode JWT payload (base64url) sans verifier la signature."""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.b64decode(part.replace("-", "+").replace("_", "/")))
    except Exception:
        return {}

def _gh(ctx):
    """Headers Grist : Authorization Bearer pour cle permanente uniquement."""
    h = {"Content-Type": "application/json"}
    if ctx.grist_key:
        h["Authorization"] = f"Bearer {ctx.grist_key}"
    return h

def _aq(ctx):
    """Query params Grist : ?auth=token pour access_token court-terme."""
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

# ── TOOLS ─────────────────────────────────────────────────────────────────────

TOOLS = [
    # ── Sessions
    {"name": "sessions_list",
     "title": "Lister les documents Grist ouverts",
     "description": "Liste tous les widgets actifs de l utilisateur. Appeler EN PREMIER.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},

    {"name": "session_select",
     "title": "Selectionner une session",
     "description": "Selectionne une session par token. Inutile si une seule session active.",
     "inputSchema": {"type": "object",
                     "properties": {"token": {"type": "string"}},
                     "required": ["token"]},
     "annotations": {"readOnlyHint": False, "destructiveHint": False}},

    {"name": "session_info",
     "title": "Infos session active",
     "description": "Doc Grist, tables disponibles, etat du canvas.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True, "idempotentHint": True}},

    # ── Canvas
    {"name": "canvas_read",
     "title": "Lire le canvas",
     "description": "Lit le code complet du canvas. Appeler AVANT tout canvas_patch.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True, "idempotentHint": True}},

    {"name": "canvas_write",
     "title": "Reecrire le canvas",
     "description": "Reecrit integralement le canvas. Preferer canvas_patch pour modifications ciblees.",
     "inputSchema": {"type": "object",
                     "properties": {"code": {"type": "string"}},
                     "required": ["code"]},
     "annotations": {"readOnlyHint": False, "destructiveHint": True}},

    {"name": "canvas_patch",
     "title": "Patcher le canvas (str_replace)",
     "description": "Remplace precisement un fragment unique. Equivalent str_replace Claude Code.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "old_str": {"type": "string", "description": "Fragment exact a remplacer (unique)"},
                         "new_str": {"type": "string", "description": "Fragment de remplacement"}},
                     "required": ["old_str", "new_str"]},
     "annotations": {"readOnlyHint": False, "destructiveHint": False}},

    {"name": "canvas_exec",
     "title": "Executer le canvas Python",
     "description": "Execute le canvas Python (subprocess isole, timeout 10s).",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": False, "openWorldHint": True}},

    # ── Grist lecture
    {"name": "grist_schema",
     "title": "Schema des tables Grist",
     "description": "Definitions de toutes les tables du document actif.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True, "idempotentHint": True}},

    {"name": "grist_records",
     "title": "Lire des enregistrements Grist",
     "description": "Enregistrements d une table (max 100). Supporte filtre et tri.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "table_id": {"type": "string"},
                         "limit":    {"type": "integer", "default": 50, "minimum": 1, "maximum": 100},
                         "filter":   {"type": "object",  "description": "Ex: {\"Statut\": [\"actif\"]}"},
                         "sort":     {"type": "string",  "description": "Ex: 'Score' ou '-Score' (desc)"}},
                     "required": ["table_id"]},
     "annotations": {"readOnlyHint": True}},

    {"name": "grist_sql",
     "title": "Requete SQL sur le document Grist",
     "description": "Executes une requete SELECT SQLite directement sur le document. Ideal pour agregations complexes.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "query": {"type": "string", "description": "SELECT SQL (SQLite)"},
                         "args":  {"type": "array",  "description": "Parametres ? dans la requete", "items": {}}},
                     "required": ["query"]},
     "annotations": {"readOnlyHint": True}},

    # ── Grist ecriture
    {"name": "grist_records_add",
     "title": "Ajouter des enregistrements dans Grist",
     "description": "Cree de nouvelles lignes dans une table Grist. Retourne les ids crees.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "table_id": {"type": "string"},
                         "records":  {"type": "array",
                                      "description": "Liste de {fields: {Col: val}}",
                                      "items": {"type": "object"}}},
                     "required": ["table_id", "records"]},
     "annotations": {"readOnlyHint": False, "destructiveHint": False}},

    {"name": "grist_records_patch",
     "title": "Modifier des enregistrements Grist (par id)",
     "description": "Met a jour des lignes existantes par leur id. Chaque record doit avoir id + fields.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "table_id": {"type": "string"},
                         "records":  {"type": "array",
                                      "description": "Liste de {id: rowId, fields: {Col: val}}",
                                      "items": {"type": "object"}}},
                     "required": ["table_id", "records"]},
     "annotations": {"readOnlyHint": False, "destructiveHint": False}},

    {"name": "grist_upsert",
     "title": "Upsert dans Grist (add-or-update sur cle metier)",
     "description": "Cree ou met a jour des lignes selon une cle metier (require). Ideal pour sync.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "table_id": {"type": "string"},
                         "records":  {"type": "array",
                                      "description": "Liste de {require: {key_col: val}, fields: {Col: val}}",
                                      "items": {"type": "object"}}},
                     "required": ["table_id", "records"]},
     "annotations": {"readOnlyHint": False, "destructiveHint": False}},
]

# ── PROMPTS ───────────────────────────────────────────────────────────────────

PROMPTS = [
    {"name": "explore-doc",
     "title": "Explorer le document Grist actif",
     "description": "Schema complet + echantillons de chaque table.",
     "arguments": []},
    {"name": "write-canvas",
     "title": "Ecrire un canvas pour un objectif",
     "description": "Cree un canvas Python complet base sur le schema reel.",
     "arguments": [{"name": "objectif", "description": "Ce que le canvas doit accomplir", "required": True}]},
    {"name": "patch-canvas",
     "title": "Modifier le canvas existant",
     "description": "Modifications ciblees sans reecriture totale.",
     "arguments": [{"name": "modification", "description": "Ce qui doit changer", "required": True}]},
    {"name": "debug-canvas",
     "title": "Deboguer le canvas",
     "description": "Execute, analyse erreurs, corrige par patches jusqu a returncode 0.",
     "arguments": []},
    {"name": "grist-formula",
     "title": "Generer une formule Grist",
     "description": "Formule Python Grist pour une colonne donnee.",
     "arguments": [
         {"name": "table",   "description": "Table cible",        "required": True},
         {"name": "colonne", "description": "Colonne a calculer", "required": True},
         {"name": "logique", "description": "Logique souhaitee",  "required": True}]},
    {"name": "analyze-table",
     "title": "Analyse statistique d une table",
     "description": "Canvas d analyse (nulls, min/max/mean, doublons, anomalies).",
     "arguments": [{"name": "table", "description": "Table a analyser", "required": True}]},
]

def _prompt_messages(name, args):
    if name == "explore-doc":
        return [{"role": "user", "content": {"type": "text", "text": (
            "Explore le document Grist actif.\n"
            "1. sessions_list() -- identifier la session\n"
            "2. grist_schema() -- toutes les tables et colonnes\n"
            "3. grist_records(table, limit=5) pour chaque table\n"
            "4. Resume : tables, colonnes-cles, types, volume, suggestions canvas."
        )}}]
    if name == "write-canvas":
        obj = args.get("objectif", "analyser les donnees")
        return [{"role": "user", "content": {"type": "text", "text": (
            f"Objectif : {obj}\n\n"
            "1. grist_schema() -- tables disponibles\n"
            "2. grist_records() sur les tables pertinentes\n"
            "3. canvas_write(code) -- script Python complet, commente, print() des resultats\n"
            "4. canvas_exec() -- valider. Si returncode!=0 : canvas_patch() corriger."
        )}}]
    if name == "patch-canvas":
        mod = args.get("modification", "ameliorer le canvas")
        return [{"role": "user", "content": {"type": "text", "text": (
            f"Modification : {mod}\n\n"
            "1. canvas_read() -- lire le code actuel\n"
            "2. canvas_patch(old_str, new_str) pour chaque modification\n"
            "3. canvas_exec() -- valider."
        )}}]
    if name == "debug-canvas":
        return [{"role": "user", "content": {"type": "text", "text": (
            "Debug le canvas actif.\n"
            "1. canvas_read()\n"
            "2. canvas_exec() -- voir l erreur\n"
            "3. canvas_patch() -- corriger\n"
            "4. canvas_exec() -- repeter jusqu a returncode 0."
        )}}]
    if name == "grist-formula":
        t, c, l = args.get("table","?"), args.get("colonne","?"), args.get("logique","?")
        return [{"role": "user", "content": {"type": "text", "text": (
            f"Formule Grist pour {t}.{c} -- logique : {l}\n\n"
            "1. grist_schema() -- verifier les colonnes existantes\n"
            "2. Ecrire la formule Python Grist ($Col, SUMIF(table.Col, val, table.Val))\n"
            "3. canvas_write() -- documenter avec exemples\n"
            "4. Expliquer la formule et ses variantes."
        )}}]
    if name == "analyze-table":
        t = args.get("table", "MaTable")
        return [{"role": "user", "content": {"type": "text", "text": (
            f"Analyse statistique de {t}.\n\n"
            f"1. grist_records('{t}', limit=100)\n"
            "2. canvas_write() -- stats (count/nulls/min/max/mean), anomalies\n"
            "3. canvas_exec() -- produire le rapport."
        )}}]
    return [{"role": "user", "content": {"type": "text", "text": f"Prompt '{name}' inconnu."}}]

# ── RESOURCES ─────────────────────────────────────────────────────────────────

STATIC_RESOURCES = [
    {"uri": "grist-coder://docs/guide",
     "name": "Guide Grist Coder",
     "description": "Architecture, flow, tools, patterns d usage optimaux.",
     "mimeType": "text/plain"},
    {"uri": "grist-coder://docs/grist-api",
     "name": "Grist API Reference",
     "description": "API Widget (grist-plugin-api.js) + REST API externe. Lecture, ecriture, SQL.",
     "mimeType": "text/plain"},
    {"uri": "grist-coder://docs/canvas-patterns",
     "name": "Patterns canvas Python",
     "description": "Recettes Python : analyse Grist, formules, stats, anomalies.",
     "mimeType": "text/plain"},
]

RESOURCE_TEMPLATES = [
    {"uriTemplate": "grist-coder://canvas/{token}",
     "name": "Canvas d une session",
     "description": "Code Python actuel (subscribable).",
     "mimeType": "text/x-python"},
    {"uriTemplate": "grist-coder://schema/{token}",
     "name": "Schema Grist d une session",
     "mimeType": "application/json"},
    {"uriTemplate": "grist-coder://ui/{token}",
     "name": "Widget Grist Coder (MCP Apps SEP-1865)",
     "mimeType": "text/html"},
]

GUIDE = """GRIST CODER - Guide complet v4.3
==================================
AUTH : Identite = uid_key = 'uid:{grist_user_id}' (partage widget + Claude Desktop).
  Widget     -> grist.docApi.getAccessToken() -> token court-terme (pas de cle a saisir)
             -> POST /register {accessToken, docId, siteUrl, userId}
             -> serveur verifie -> uid_key
             -> retourne arto-xxx
             -> SSE via ?token=arto-xxx
             -> appels MCP : Authorization: Bearer arto-xxx
  Claude Desktop -> Bearer <grist_key>
             -> serveur verifie -> uid_key = 'uid:{grist_user_id}'
             -> meme namespace que le widget : sessions partagees
  Isolation stricte : sessions indexees par uid_key.

ANALOGIE CLAUDE CODE
  canvas_read()              = read_file()
  canvas_patch(old, new)     = str_replace()  <- TOUJOURS PREFERER
  canvas_write(code)         = write_file()
  canvas_exec()              = bash("python ...")
  grist_schema()             = ls schema
  grist_records(table)       = cat data
  grist_sql(query)           = SELECT direct
  grist_records_add(t, rows) = INSERT
  grist_records_patch(t, rs) = UPDATE by id
  grist_upsert(t, rs)        = INSERT OR UPDATE on key

WORKFLOW OPTIMAL
  1. sessions_list()         -> quels docs sont ouverts ?
  2. grist_schema()          -> tables disponibles
  3. grist_records()/sql()   -> lire les donnees
  4. canvas_write/patch()    -> coder l analyse
  5. canvas_exec()           -> valider, corriger si erreur
  6. grist_records_add()     -> ecrire les resultats si besoin

REGLES
  - canvas_read() AVANT canvas_patch() : old_str exact
  - canvas_patch >> canvas_write : historique preserve
  - Chaque patch SSE -> widget mis a jour en temps reel"""

GRIST_API = """GRIST API REFERENCE
===================

== WIDGET API (grist-plugin-api.js) ==
// Dans l iframe, pas de cle API - communication par postMessage

grist.ready({ requiredAccess: 'full' | 'read table' | 'none' })

// Contexte recu automatiquement
grist.on('message', msg => {
  msg.docId       // ID document
  msg.userId      // ID utilisateur Grist
  msg.siteUrl     // URL instance (ex: https://grist.cerema.fr)
  msg.tableId     // table courante
  msg.rowId       // ligne curseur
  msg.type        // 'theme' (light/dark)
})

// Abonnements reactifs
grist.onRecord(record => {})      // ligne curseur change
grist.onRecords(records => {})    // table entiere change
grist.onOptions(opts, info => {}) // config widget change
grist.onNewRecord(() => {})       // nouvelle ligne vide

// Lecture
grist.fetchSelectedTable()        // table en cours (columnar)
grist.fetchSelectedRecord(rowId)
grist.docApi.listTables()         // toutes les tables (besoin 'read table')
grist.docApi.fetchTable(tableId)

// Ecriture sur la table selectionnee
grist.selectedTable.create({fields: {Col: val}})
grist.selectedTable.update(rowId, {Col: val})
grist.selectedTable.upsert([{id, fields}])
grist.selectedTable.destroy(rowId)

// Ecriture sur n importe quelle table
const tbl = grist.getTable('MaTable')
tbl.create / tbl.update / tbl.upsert / tbl.destroy

// Profil utilisateur (userId stable, ne change pas si la cle est regeneree)
const profile = await grist.getUserProfile()
// profile.userId (entier), profile.name, profile.email, profile.locale

// Token court-terme pour appels REST depuis le widget
const tok = await grist.docApi.getAccessToken({readOnly: false})
// tok.token (valide ~1h), tok.baseUrl = "{siteUrl}/api/docs/{docId}"
fetch(`${tok.baseUrl}/tables/T/records`, {
  method: 'POST',
  headers: {Authorization: `Bearer ${tok.token}`},
  body: JSON.stringify({records: [{fields: {Col: val}}]})
})

// Etat widget
grist.setOption('key', value)
grist.getOption('key')
grist.setSelectedRows([1, 2, 3])   // filtrer widgets lies
grist.setCursorPos({rowId, tableId})

== REST API EXTERNE ==
// Authorization: Bearer <grist_api_key>
// Base : {siteUrl}/api/docs/{docId}

// Lecture
GET  /tables                          -> liste des tables
GET  /tables/{tableId}/records        -> enregistrements
     ?limit=100&filter={"Col":["val"]}&sort=Col
GET  /tables/{tableId}/columns        -> colonnes + types
GET  /sql?q=SELECT+*+FROM+Table1      -> SQL direct (GET)

// SQL avec parametres
POST /sql
     {"sql": "SELECT * FROM T WHERE Col=?", "args": ["val"], "timeout": 5000}

// Ecriture
POST  /tables/{tableId}/records
      {"records": [{"fields": {"Col": val}}]}
      -> {"records": [{"id": 5}]}

PATCH /tables/{tableId}/records
      {"records": [{"id": 5, "fields": {"Col": new_val}}]}

PUT   /tables/{tableId}/records   <- UPSERT
      {"records": [{"require": {"key_col": val}, "fields": {"Col": val}}]}

DELETE /tables/{tableId}/records
       via POST /tables/{tableId}/data/delete {"id": [1, 2, 3]}

// Actions batch (bas niveau)
POST /apply
     [["AddRecord", "Table1", null, {"Col": "val"}],
      ["UpdateRecord", "Table1", 5, {"Col": "new"}]]

// Schemas
POST  /tables               -> creer une table
PATCH /tables               -> renommer
POST  /tables/{t}/columns   -> ajouter colonnes
PATCH /tables/{t}/columns   -> modifier colonnes

// Webhooks
GET  /webhooks
POST /webhooks {"webhooks": [{"url": "https://...", "eventTypes": ["add","update"]}]}"""

CANVAS_PATTERNS = """CANVAS PATTERNS - Recettes Python
===================================
# Records depuis grist_records() : [{id:1, fields:{Col:val}}, ...]
rows = [r["fields"] for r in records]

# Stats de base
scores = [r["Score"] for r in rows if r.get("Score") is not None]
mean   = sum(scores) / len(scores) if scores else 0
print(f"N={len(rows)}, mean={mean:.3f}, max={max(scores) if scores else 0:.3f}")

# Detection anomalies (2 sigma)
import statistics
if len(scores) > 1:
    stdev = statistics.stdev(scores)
    anomalies = [r for r in rows if abs(r.get("Score",0) - mean) > 2 * stdev]
    print(f"Anomalies: {len(anomalies)}")

# SUMIF equivalent
def sumif(records, match_col, match_val, sum_col):
    return sum(r["fields"].get(sum_col, 0)
               for r in records if r["fields"].get(match_col) == match_val)

# Score composite
def score_passage(row):
    conf  = row.get("confidence", 0)
    width = row.get("largeur_m", 0)
    return round(conf * 0.6 + min(width / 10, 1) * 0.4, 3)

# Preparer des records pour grist_records_add
results = [{"fields": {"Colonne": val, "Score": score}} for val, score in data]
# Puis appeler : grist_records_add("ResultTable", results)

# Upsert sur cle metier
upsert_records = [
    {"require": {"ExternalId": row["id"]},
     "fields":  {"Valeur": row["val"], "UpdatedAt": int(time.time())}}
    for row in source_data
]
# Puis appeler : grist_upsert("MaTable", upsert_records)"""

async def _read_resource(uid_key, mcp_sid, uri):
    if uri == "grist-coder://docs/guide":
        return {"uri": uri, "mimeType": "text/plain", "text": GUIDE}
    if uri == "grist-coder://docs/grist-api":
        return {"uri": uri, "mimeType": "text/plain", "text": GRIST_API}
    if uri == "grist-coder://docs/canvas-patterns":
        return {"uri": uri, "mimeType": "text/plain", "text": CANVAS_PATTERNS}
    if uri.startswith("grist-coder://canvas/"):
        token = uri.split("/")[-1]
        ctx = registry.resolve(uid_key, token)
        if not ctx: raise ValueError(f"Session inconnue : {token}")
        ctx.subscribers.add(mcp_sid)
        return {"uri": uri, "mimeType": "text/x-python",
                "text": ctx.canvas or "# canvas vide\n"}
    if uri.startswith("grist-coder://schema/"):
        token = uri.split("/")[-1]
        ctx = registry.resolve(uid_key, token)
        if not ctx: raise ValueError(f"Session inconnue : {token}")
        try:    schema = await grist_get(ctx, "tables")
        except Exception as e: schema = {"error": str(e)}
        return {"uri": uri, "mimeType": "application/json",
                "text": json.dumps(schema, ensure_ascii=False, indent=2)}
    if uri.startswith("grist-coder://ui/"):
        token = uri.split("/")[-1]
        ctx = registry.resolve(uid_key, token)
        if not ctx: raise ValueError(f"Session inconnue : {token}")
        html = (f'<!DOCTYPE html><html><head><meta charset="UTF-8">'
                f'<title>Grist Coder - {ctx.doc_title}</title>'
                f'<style>body{{margin:0;height:100vh}}</style></head><body>'
                f'<iframe src="{HOST_URL}/?token={token}" '
                f'style="width:100%;height:100%;border:none" allow="clipboard-write"></iframe>'
                f'</body></html>')
        return {"uri": uri, "mimeType": "text/html", "text": html}
    raise ValueError(f"Resource inconnue : {uri}")

def _resources_list(uid_key):
    res = list(STATIC_RESOURCES)
    for s in registry.list_sessions(uid_key):
        t, title, sha = s["token"], s["doc_title"], s.get("canvas_sha") or "vide"
        res += [
            {"uri": f"grist-coder://canvas/{t}", "name": f"Canvas - {title}",
             "description": f"Code Python actif ({sha}).", "mimeType": "text/x-python"},
            {"uri": f"grist-coder://schema/{t}", "name": f"Schema - {title}",
             "description": f"Tables Grist du document.", "mimeType": "application/json"},
            {"uri": f"grist-coder://ui/{t}", "name": f"Widget - {title} (MCP Apps)",
             "description": f"Interface interactive.", "mimeType": "text/html"},
        ]
    return res

# ── TOOL CALL ─────────────────────────────────────────────────────────────────

_active_tokens: dict[str, str] = {}

async def call_tool(uid_key, mcp_sid, name, args):
    # ── Sessions
    if name == "sessions_list":
        s = registry.list_sessions(uid_key)
        return s if s else {
            "info": "Aucun widget connecte.",
            "hint": "Ouvrez le widget Grist Coder dans Grist. Il se connecte automatiquement (pas de cle a saisir)."
        }

    if name == "session_select":
        ctx = registry.resolve(uid_key, args["token"])
        if not ctx: return {"error": f"Token inconnu : {args['token']}"}
        _active_tokens[mcp_sid] = ctx.token
        ctx.touch()
        return {"ok": True, "selected": ctx.meta()}

    # Resoudre la session active pour tous les autres tools
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

    # ── Grist lecture
    if name == "grist_schema":
        return await grist_get(ctx, "tables")

    if name == "grist_records":
        lim  = int(args.get("limit", 50))
        path = f"tables/{args['table_id']}/records?limit={lim}"
        if "filter" in args:
            path += f"&filter={json.dumps(args['filter'])}"
        if "sort" in args:
            path += f"&sort={args['sort']}"
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

# ── DISPATCH ─────────────────────────────────────────────────────────────────

async def dispatch(uid_key, mcp_sid, method, params):
    if method == "initialize":
        return {
            "protocolVersion": MCP_VER,
            "serverInfo": {"name": "grist-coder", "version": "4.3.0",
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
        result = await call_tool(uid_key, mcp_sid, params["name"], params.get("arguments", {}))
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}
    if method == "prompts/list":
        return {"prompts": PROMPTS}
    if method == "prompts/get":
        name = params.get("name", "")
        p = next((p for p in PROMPTS if p["name"] == name), None)
        if not p: raise ValueError(f"Prompt inconnu : {name}")
        return {"description": p["description"],
                "messages":    _prompt_messages(name, params.get("arguments", {}))}
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
    print(f"grist-coder v4.3 · {HOST_URL}")
    print(f"  auth   : widget=accessToken(docApi) / Claude Desktop=grist_key -> uid:user_id")
    print(f"  tools  : {len(TOOLS)} ({sum(1 for t in TOOLS if 'grist' in t['name'])} grist)")
    print(f"  prompts: {len(PROMPTS)}  resources statiques: {len(STATIC_RESOURCES)}")
    yield

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"], expose_headers=["Mcp-Session-Id"])

def _auth(auth):
    if not auth: return None
    parts = auth.split()
    return parts[1] if len(parts) == 2 and parts[0].lower() == "bearer" else None

async def _resolve_uid_key(raw_bearer: str, x_grist_site: str | None) -> str | None:
    """
    Resout le uid_key a partir du Bearer.

    Cas 1 : Bearer = arto-xxx (widget) -> lookup dans _token_to_uid
    Cas 2 : Bearer = grist_key (Claude Desktop) -> cache _grist_key_to_uid
            sinon verification via GET /api/profile/user + mise en cache
    """
    if not raw_bearer:
        return None
    # Cas 1 : token de session widget
    if raw_bearer.startswith("gc-"):
        return _token_to_uid.get(raw_bearer)
    # Cas 2 : clé Grist permanente (Claude Desktop)
    uid_key = registry.get_uid_for_grist_key(raw_bearer)
    if uid_key:
        # Mettre a jour les credentials si nouveau X-Grist-Site
        if x_grist_site:
            registry.provision(uid_key, grist_key=raw_bearer, site=x_grist_site.rstrip("/"))
        return uid_key
    # Pas encore connu : verifier via Grist API
    site = (x_grist_site or "").rstrip("/")
    if site:
        profile = await fetch_grist_user_profile(site, raw_bearer)
        if profile and profile.get("id"):
            uid_key = f"uid:{profile['id']}"
            registry.provision(uid_key, grist_key=raw_bearer, site=site)
            return uid_key
    # Fallback sans site URL (rare) : uid derive du hash de la cle
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
        return JSONResponse({"error": "Authorization: Bearer requis (grist_key ou arto-token)"}, status_code=401)
    uid_key = await _resolve_uid_key(raw_bearer, x_grist_site)
    if not uid_key:
        return JSONResponse({"error": "Token inconnu ou expiré. Rechargez le widget."}, status_code=401)
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
    # Resolution uid_key (par ordre de securite decroissant) :
    # 1. Authorization: Bearer arto-xxx  (widget POST /mcp) ou Bearer grist_key (Claude Desktop)
    # 2. ?token=arto-xxx                 (EventSource widget - pas de header possible)
    raw_bearer = _auth(authorization)
    uid_key = None
    if raw_bearer:
        uid_key = await _resolve_uid_key(raw_bearer, None)
    if not uid_key:
        arto_token = request.query_params.get("token")
        if arto_token:
            uid_key = _token_to_uid.get(arto_token)
    if not uid_key:
        return Response("Authorization requis : Bearer arto-token ou ?token=arto-xxx", status_code=401)
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
    """
    Appele par le widget au demarrage ou lors du refresh du token.
    Body: { accessToken, docId, docTitle, siteUrl, userId? }
          (gristKey accepte aussi pour compat Claude Desktop direct)

    1. Verifie accessToken (ou gristKey) via GET /api/profile/user -> grist_user_id stable.
    2. uid_key = 'uid:{grist_user_id}' -> namespace partage widget + Claude Desktop.
    3. Enregistre/met a jour la session, peuple _token_to_uid.
    4. Retourne {token: "gc-xxx"} -> widget ouvre SSE via ?token= et appelle POST /mcp
       avec Authorization: Bearer arto-xxx. La cle Grist ne transite jamais.
    """
    data         = await request.json()
    access_token = data.get("accessToken", "").strip()
    grist_key    = data.get("gristKey", "").strip()    # compat Claude Desktop direct
    site_url     = data.get("siteUrl", "").rstrip("/")
    doc_id       = data.get("docId", "")
    doc_title    = data.get("docTitle", "") or doc_id

    bearer = access_token or grist_key
    if not bearer:
        return JSONResponse({"error": "accessToken manquant (fourni par grist.docApi.getAccessToken())"},
                            status_code=400)

    # Resoudre grist_user_id
    # Priorite 1 : decoder le JWT de l accessToken (pas d appel API, pas de CORS)
    grist_user_id = None
    if access_token:
        payload = _jwt_payload(access_token)
        raw = payload.get("userId")
        if raw is not None:
            try: grist_user_id = int(raw)
            except (ValueError, TypeError): pass

    # Priorite 2 : userId envoye explicitement par le widget
    if not grist_user_id:
        raw2 = data.get("userId") or None
        if raw2 is not None:
            try: grist_user_id = int(raw2)
            except (ValueError, TypeError): pass

    # Priorite 3 : API Grist (uniquement pour cle permanente grist_key - Claude Desktop direct)
    if not grist_user_id and grist_key and site_url:
        profile = await fetch_grist_user_profile(site_url, grist_key)
        if profile:
            grist_user_id = profile.get("id")

    if not grist_user_id:
        return JSONResponse({"error": "Impossible de verifier l identite Grist (userId introuvable)."},
                            status_code=401)

    uid_key = f"uid:{grist_user_id}"
    registry.provision(uid_key,
                       grist_key=grist_key,
                       access_token=access_token,
                       site=site_url)

    ctx = registry.register_session(
        uid_key=uid_key,
        doc_id=doc_id,
        doc_title=doc_title,
        site_url=site_url,
        grist_key=grist_key,
        access_token=access_token)

    # Index SSE : arto-token -> uid_key (pas de credential brut dans les logs)
    _token_to_uid[ctx.token] = uid_key

    _push(uid_key, {"type": "widget_connected", "token": ctx.token,
                    "doc_id": ctx.doc_id, "doc_title": ctx.doc_title})
    _push(uid_key, {"type": "mcp_notification",
                    "method": "notifications/resources/list_changed", "params": {}})
    return {"ok": True, "token": ctx.token, "doc_title": ctx.doc_title,
            "gristUserId": grist_user_id}

@app.get("/health")
async def health():
    total = sum(len(u["sessions"]) for u in registry._users.values())
    return {"ok":True,"version":"4.3.0","mcp_protocol":MCP_VER,
            "sessions":total,"users":len(registry._users),
            "sse_tokens":len(_token_to_uid),
            "tools":len(TOOLS),"prompts":len(PROMPTS),
            "auth":"uid_key(uid:user_id), widget=accessToken, Claude=grist_key"}

@app.get("/", response_class=HTMLResponse)
async def widget_html(): return WIDGET

WIDGET = """<!DOCTYPE html>
<html lang="fr"><head>
<meta charset="UTF-8"><title>Grist Coder</title>
<link href="https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500&display=swap" rel="stylesheet">
<script src="https://docs.getgrist.com/grist-plugin-api.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --gc-navy:#42494B;
  --gc-primary:#3E5DE7;
  --gc-primary-dk:#2845C1;
  --gc-bg:#ffffff;
  --gc-editor:#f8f9fc;
  --gc-text:#222631;
  --gc-muted:#626A80;
  --gc-border:#ddd;
  --gc-ok:#2B695A;
  --gc-err:#E32C39;
}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
     background:var(--gc-bg);color:var(--gc-text);
     height:100vh;display:flex;flex-direction:column;overflow:hidden;font-size:13px}
#bar{display:flex;align-items:center;gap:8px;padding:6px 10px;
     background:var(--gc-navy);flex-shrink:0}
#dot{width:7px;height:7px;border-radius:50%;background:var(--gc-err);
     flex-shrink:0;transition:background .3s}
#dot.on{background:#4ade80}
#bar-title{font-size:12px;font-weight:600;color:#fff;letter-spacing:.02em;flex:1}
#token-badge{font-size:10px;font-family:'Fira Code',monospace;
             background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.2);
             padding:2px 8px;border-radius:3px;color:rgba(255,255,255,.8);
             cursor:pointer;display:none;transition:background .15s}
#token-badge:hover{background:rgba(255,255,255,.2)}
#editor{flex:1;width:100%;background:var(--gc-editor);color:var(--gc-text);
        border:none;outline:none;resize:none;
        font-family:'Fira Code',monospace;font-size:12px;
        line-height:1.75;padding:12px 14px;tab-size:2}
#foot{display:flex;align-items:center;gap:8px;padding:7px 10px;
      background:var(--gc-bg);border-top:1px solid var(--gc-border);flex-shrink:0}
#btn-run{font-size:12px;font-weight:500;padding:4px 14px;border-radius:3px;
         border:none;cursor:pointer;background:var(--gc-primary);color:#fff;
         transition:background .15s}
#btn-run:hover{background:var(--gc-primary-dk)}
#btn-run:disabled{opacity:.5;cursor:default}
#out{margin-left:auto;font-size:11px;font-family:'Fira Code',monospace;
     color:var(--gc-muted);max-width:240px;
     overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#out.ok{color:var(--gc-ok)}
#out.err{color:var(--gc-err)}
</style></head><body>
<div id="bar">
  <div id="dot"></div>
  <span id="bar-title">grist coder</span>
  <span id="token-badge" onclick="copyToken()" title="Copier le token MCP"></span>
</div>
<textarea id="editor" spellcheck="false"
  placeholder="# Canvas vide&#10;# Connexion automatique en cours...&#10;# Utilisez Claude Desktop pour modifier ce canvas via MCP"></textarea>
<div id="foot">
  <button id="btn-run" onclick="execCanvas()">&#9654; Run</button>
  <span id="out">—</span>
</div>
<script>
const BASE=window.location.origin;
let _registered=false,_token=null,_id=1,_refreshTimer=null,_es=null;
const $=id=>document.getElementById(id);
const setOut=(t,cls="")=>{const el=$("out");el.textContent=t;el.className=cls};

grist.ready({requiredAccess:"full"});
grist.on("message",async(msg)=>{
  window._gristMsg=msg;
  if(_registered)return;
  await doRegister(msg);
});

let _registering=false;
async function doRegister(msg){
  if(_registering)return;
  _registering=true;
  try{
    // 1. Token court-terme docApi
    const tk=await grist.docApi.getAccessToken({readOnly:false});
    const parts=(tk.baseUrl||"").split("/api/docs/");
    if(parts.length!==2){setOut("✗ baseUrl invalide","err");return;}
    const siteUrl=parts[0];
    const docId=parts[1].split("/")[0];
    const docTitle=msg?.docTitle||document.title||docId;

    // 2. userId : decoder le payload JWT sans verifier la signature
    let userId=msg?.userId||null;
    if(!userId){
      try{
        const b64=tk.token.split('.')[1];
        if(b64){
          const payload=JSON.parse(atob(b64.replace(/-/g,'+').replace(/_/g,'/')));
          const raw=payload?.userId??payload?.sub??payload?.id??null;
          userId=raw!==null?Number(raw)||null:null;
        }
      }catch(_){}
    }

    // 3. Enregistrement
    const res=await fetch(BASE+"/register",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({accessToken:tk.token,docId,docTitle,siteUrl,userId})});
    const data=await res.json();
    if(data.error){setOut("✗ "+data.error,"err");return;}

    _registered=true;_token=data.token;
    $("dot").className="on";
    const badge=$("token-badge");
    badge.textContent=_token;badge.style.display="inline-block";

    listenSSE();
    await loadCanvas();

    // 4. Refresh automatique avant expiration (80% TTL, min 60s)
    if(_refreshTimer)clearTimeout(_refreshTimer);
    const refreshIn=Math.max((tk.ttlMsecs||3600000)*0.8,60000);
    _refreshTimer=setTimeout(async()=>{
      _registered=false;
      if(window._gristMsg)await doRegister(window._gristMsg);
    },refreshIn);

  }catch(e){setOut("✗ "+e.message,"err");}
  finally{_registering=false;}
}

function copyToken(){
  if(!_token)return;
  navigator.clipboard.writeText(_token);
  setOut("✓ copie","ok");setTimeout(()=>setOut("—"),1500);
}

function listenSSE(){
  if(!_token)return;
  if(_es){_es.close();_es=null;}
  _es=new EventSource(BASE+"/mcp?token="+encodeURIComponent(_token));
  _es.onmessage=e=>{
    try{
      const ev=JSON.parse(e.data);
      if(_token&&(ev.token===_token)&&(ev.type==="canvas_patched"||ev.type==="canvas_updated")){
        loadCanvas();setOut("⬡ "+ev.sha);
      }
    }catch(_){}
  };
  _es.onerror=()=>{
    if(_es){_es.close();_es=null;}
    _registered=false;
    setTimeout(async()=>{
      if(window._gristMsg)await doRegister(window._gristMsg);
    },3000);
  };
}

async function tool(name,args={}){
  if(!_token)throw new Error("Widget non connecte");
  const r=await fetch(BASE+"/mcp",{method:"POST",
    headers:{"Content-Type":"application/json","Authorization":"Bearer "+_token},
    body:JSON.stringify({jsonrpc:"2.0",id:_id++,method:"tools/call",
                         params:{name,arguments:args}})});
  const d=await r.json();
  if(d.error)throw new Error(d.error.message);
  return JSON.parse(d.result.content[0].text);
}

async function loadCanvas(){
  try{$("editor").value=await tool("canvas_read");}
  catch(e){setOut("✗ "+e.message,"err");}
}
async function execCanvas(){
  const btn=$("btn-run");
  btn.disabled=true;setOut("…","");
  try{
    await tool("canvas_write",{code:$("editor").value});
    const r=await tool("canvas_exec");
    if(r.returncode===0){
      setOut("✓ "+(r.stdout.split("\\n")[0]||"ok"),"ok");
    }else{
      setOut("✗ "+(r.stderr.split("\\n")[0]||"erreur"),"err");
    }
  }catch(e){setOut("✗ "+e.message,"err");}
  finally{btn.disabled=false;}
}
</script></body></html>"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("grist_coder:app", host="0.0.0.0", port=8742, reload=True)
