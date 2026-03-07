# grist-coder-mcp

> Widget Grist + MCP Server HTTP streamable · spec 2025-03-26

**v4.3** — authentification sans saisie de clé : le widget se connecte automatiquement via `grist.docApi.getAccessToken()`.

---

## Concept

Le **canevas est le fichier de travail**. Claude Desktop manipule le code Python du widget Grist exactement comme Claude Code manipule un fichier local.

```
Widget Grist                   Grist Coder MCP              Claude Desktop
────────────                   ───────────────              ──────────────
getAccessToken()  →  /register → uid:user_id  ←  Bearer <grist_key>
← arto-xxxxxx                  sessions{}         sessions_list()
SSE /mcp?token=…               canvas             canvas_read()
POST /mcp Bearer arto-xxx   →  canvas_patch()  ←  canvas_patch(old, new)
                               grist API          grist_records("Table")
```

**Identité** : `uid:{grist_user_id}` — partagé entre widget (via JWT docApi) et Claude Desktop (via clé API). Chaque utilisateur ne voit que ses propres sessions.

---

## Démarrage rapide

```bash
python -m venv .venv && .venv/Scripts/activate   # Windows
pip install -r requirements.txt
cp .env.example .env                              # remplir HOST_URL si besoin
uvicorn grist_coder:app --port 8742 --reload
curl http://localhost:8742/health                 # -> {"ok":true,...}
```

Ou avec Docker :

```bash
cp .env.example .env
docker compose up
```

---

## Configuration widget Grist

Dans Grist → **Ajouter un widget personnalisé** → URL : `http://localhost:8742/`

Le widget se connecte **automatiquement** via `grist.docApi.getAccessToken()`. Aucune clé à saisir.

---

## Configuration Claude Desktop

Fichier : `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "grist-coder": {
      "type": "http",
      "url": "http://localhost:8742/mcp",
      "headers": { "Authorization": "Bearer <grist_api_key>" }
    }
  }
}
```

La clé API Grist est le seul credential — Claude Desktop l'utilise comme Bearer. Le service la vérifie auprès de Grist (`GET /api/profile/user`) pour établir l'identité `uid:{user_id}`.

Redémarrer Claude Desktop après toute modification.

---

## Tools MCP (13)

### Sessions
| Tool | Description |
|------|-------------|
| `sessions_list()` | Liste les documents Grist ouverts — **appeler en premier** |
| `session_select(token)` | Sélectionne une session (auto si une seule active) |
| `session_info()` | Infos complètes : doc, tables, état canvas |

### Canvas
| Tool | Description |
|------|-------------|
| `canvas_read()` | Lit le code complet — **appeler avant canvas_patch** |
| `canvas_write(code)` | Réécrit intégralement le canvas |
| `canvas_patch(old_str, new_str)` | str_replace ciblé — SSE push vers le widget |
| `canvas_exec()` | Exécute le canvas Python (subprocess isolé, timeout 10s) |

### Grist (lecture)
| Tool | Description |
|------|-------------|
| `grist_schema()` | Définitions des tables du document actif |
| `grist_records(table_id, limit?)` | Enregistrements d'une table |
| `grist_sql(sql)` | Requête SQL libre sur le document |

### Grist (écriture)
| Tool | Description |
|------|-------------|
| `grist_records_add(table_id, records)` | Ajoute des enregistrements |
| `grist_records_patch(table_id, records)` | Met à jour des enregistrements existants |
| `grist_upsert(table_id, records)` | Upsert sur clé métier (`require` + `fields`) |

---

## Endpoints HTTP

| Méthode | Route | Rôle |
|---------|-------|------|
| `POST` | `/mcp` | JSON-RPC MCP (tools, initialize, prompts, resources) |
| `GET` | `/mcp` | SSE stream — pousse les mises à jour canvas au widget |
| `DELETE` | `/mcp` | Ferme une session SSE |
| `POST` | `/register` | Enregistrement automatique du widget (`{accessToken, docId, siteUrl, userId?}`) |
| `GET` | `/` | Sert le widget HTML Grist |
| `GET` | `/health` | Healthcheck JSON |

---

## Analogie Claude Code

```
Claude Code           →  Grist Coder
─────────────────────────────────────
str_replace(old, new) →  canvas_patch(old_str, new_str)
read_file(path)       →  canvas_read()
bash('python ...')    →  canvas_exec()
list_files()          →  grist_schema()
read_file('data.csv') →  grist_records(table_id)
```

---

## Auth v4.3 — détail technique

```
Widget                          Serveur
──────                          ───────
grist.docApi                    POST /register
  .getAccessToken()  ────────►  decode JWT payload → userId
  .token (JWT)                  uid_key = "uid:{userId}"
  .baseUrl → siteUrl, docId     → arto-xxxxxx session token

grist API calls     ◄────────►  GET  /api/docs/{id}/tables?auth={token}
(document-scoped)               POST /api/docs/{id}/tables/{t}/records?auth={token}

Claude Desktop                  _resolve_uid_key()
  Bearer grist_key  ────────►   GET /api/profile/user → id
                                uid_key = "uid:{id}"
```

Points clés :
- L'access token Grist est un **JWT signé par document** (HS256, TTL 15 min)
- Le payload contient `userId` (int) et `docId` — décodé sans vérification côté serveur
- Les appels Grist API utilisent `?auth=token` (pas `Authorization: Bearer`) pour les sessions widget
- Le token `arto-xxx` ne contient aucune credential Grist

---

## Erreurs communes

| Erreur | Cause | Solution |
|--------|-------|----------|
| `401` widget | Token expiré | Le widget se re-enregistre automatiquement toutes les 12 min |
| `401` Claude Desktop | Clé Grist incorrecte | Vérifier la clé dans `claude_desktop_config.json` |
| `Fragment introuvable` | `old_str` inexact | Appeler `canvas_read()` d'abord pour copier le fragment exact |
| Pas de sessions | Widget non chargé | Ouvrir le widget dans Grist, attendre la connexion automatique |
| `timeout 10s` | Boucle infinie | Éviter les boucles sans condition de sortie dans le canvas |

---

## Stack

- Python 3.11+ · FastAPI · uvicorn · httpx
- Transport MCP : Streamable HTTP 2025-03-26
- Authentification : Grist docApi JWT (widget) / Grist API key (Claude Desktop)
- Widget : Grist Custom Widget API (`grist-plugin-api.js`)
