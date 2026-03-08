# grist-coder-mcp

> Widget Grist + MCP Server HTTP streamable · spec 2025-03-26 · v5.2

---

## Vision

Un document Grist devient une **app métier complète**, déployée dans le navigateur, sans infrastructure supplémentaire. L'utilisateur final interagit avec des artefacts (widgets HTML/React) liés à ses données. Ses actions peuvent déclencher des effets externes. Tout est construit et maintenu depuis ce MCP.

**Philosophie** : n'implémenter que ce qui est optimal, peu complexe, facile au regard de l'existant.

---

## Architecture — 4 couches d'une app complète

```
1. Données    tables Grist + formules colonnes       ce que l'utilisateur possède
2. UI         artefacts HTML/React + pages Grist     ce que l'utilisateur voit
3. Logique    grist_apply, grist_sql, grist_upsert   ce que le système fait
4. Intégr.    grist_webhooks → CRM / email / ERP     ce que le doc déclenche à l'extérieur
```

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

---

## Tools MCP (21)

### Sessions
| Tool | Description |
|------|-------------|
| `sessions_list()` | Liste les documents Grist ouverts — **appeler en premier** |
| `session_select(token)` | Sélectionne une session |
| `session_info()` | Infos complètes : doc, tables, artefacts, état canvas |

### Canvas
| Tool | Description |
|------|-------------|
| `canvas_read()` | Lit le code complet — **appeler avant canvas_patch** |
| `canvas_write(code)` | Réécrit intégralement le canvas |
| `canvas_patch(old_str, new_str)` | str_replace ciblé — SSE push vers le widget |
| `canvas_exec()` | Exécute le canvas Python (subprocess isolé, timeout 10s) |
| `canvas_screenshot()` | Capture le rendu iframe du widget |

### Artefact
| Tool | Description |
|------|-------------|
| `artefact_init()` | Crée la table Artefacts si absente (idempotent) |

### Grist — Lecture
| Tool | Description |
|------|-------------|
| `grist_schema()` | Schéma des tables du document actif |
| `grist_records(table_id, filter?, limit?)` | Enregistrements d'une table |
| `grist_sql(sql)` | Requête SQL libre sur le document |

### Grist — Écriture
| Tool | Description |
|------|-------------|
| `grist_records_add(table_id, records)` | Ajoute des enregistrements |
| `grist_records_patch(table_id, records)` | Met à jour des enregistrements existants |
| `grist_upsert(table_id, records)` | Upsert sur clé métier (`require` + `fields`) |
| `grist_apply(actions)` | User Actions Grist bas niveau (AddTable, AddColumn…) |

### Document / Pages
| Tool | Description |
|------|-------------|
| `grist_views_list()` | Liste les pages avec leurs sections |
| `grist_view_create(table_id, page_name, artefact?)` | Crée une page grille + widget lié |
| `grist_view_add_widget(view_ref, table_id, artefact?)` | Ajoute un widget à une page existante |
| `grist_section_configure(section_ref, artefact?, link_section_ref?)` | Configure un widget custom |

### Webhooks
| Tool | Description |
|------|-------------|
| `grist_webhooks(action, fields?, webhook_id?)` | CRUD webhooks — `list` OK avec accessToken ; `create/update/delete` nécessitent clé API owner |

---

## Endpoints HTTP

| Méthode | Route | Rôle |
|---------|-------|------|
| `POST` | `/mcp` | JSON-RPC MCP |
| `GET` | `/mcp` | SSE stream — mises à jour canvas vers widget |
| `DELETE` | `/mcp` | Ferme une session SSE |
| `POST` | `/register` | Enregistrement automatique widget |
| `POST` | `/webhook-receive/{docId}` | Receiver webhooks Grist → SSE fan-out (production uniquement, HOST_URL public requis) |
| `GET` | `/` | Widget HTML Grist |
| `GET` | `/health` | Healthcheck JSON |

---

## Webhooks — Patterns d'usage

```
Pattern A — Intégration externe (cas principal)
  Table change → webhook → POST /api/crm | /api/notify | /api/erp
  Setup via MCP : grist_webhooks(action="create", fields={tableId, eventTypes, url, name})

Pattern B — Async processing loop (HOST_URL public requis)
  Artefact écrit record Statut="pending"
  → webhook → HOST_URL/webhook-receive/{docId} → SSE au widget
  → traitement externe → patch Statut="done" → onRecords met à jour l'UI

Pattern C — Inter-artefacts temps réel (localhost OK, pas de webhook)
  app.emit/on | grist.setCursorPos | grist.onRecord
```

> En développement local (localhost) : Grist externe ne peut pas atteindre le receiver. Utiliser ngrok ou Pattern C.

---

## Ressources MCP

| URI | Contenu |
|-----|---------|
| `docs/schema` | Recettes tables/colonnes |
| `docs/artefacts` | Templates HTML/JS + API bridge Grist |
| `docs/playbook` | Séquences pages, layouts, liaisons |
| `docs/app-patterns` | Patterns multi-widgets : nav, sync, Artefactory |
| `docs/formulas` | Formules colonnes Python natives Grist |
| `playbook/{scenario}` | Guide cible : dashboard\|fiche\|table\|full-app\|master-detail |
| `context/{token}` | Snapshot live : tables + artefacts + pages |
| `code/{token}` | Source de tous les artefacts |

---

## Auth

```
Widget                          Serveur
──────                          ───────
grist.docApi.getAccessToken()  → POST /register → decode JWT → uid:{userId}
                                                → gc-xxxxxx session token

Claude Desktop                  _resolve_uid_key()
  Bearer grist_api_key         → GET /api/profile/user → uid:{id}
```

- Les appels Grist API utilisent `?auth=token` (widget) ou `Authorization: Bearer` (Claude Desktop)
- accessToken widget = permission document (pas admin webhooks)
- Clé API owner = CRUD complet dont webhooks

---

## Erreurs communes

| Erreur | Cause | Solution |
|--------|-------|----------|
| `401` widget | Token expiré | Widget se ré-enregistre automatiquement toutes les 12 min |
| `401` Claude Desktop | Clé Grist incorrecte | Vérifier `Authorization: Bearer` dans `claude_desktop_config.json` |
| `403` sur webhook create | accessToken insuffisant | Utiliser `grist_webhooks` via MCP avec clé API owner |
| `Fragment introuvable` | `old_str` inexact | Appeler `canvas_read()` d'abord |
| Pas de sessions | Widget non chargé | Ouvrir le widget dans Grist, attendre la connexion |
| `timeout 10s` | Boucle infinie canvas | Éviter les boucles sans condition de sortie |

---

## Stack

- Python 3.11+ · FastAPI · uvicorn · httpx
- Transport MCP : Streamable HTTP 2025-03-26
- Auth : Grist docApi JWT (widget) / Grist API key (Claude Desktop)
- Widget : Grist Custom Widget API (`grist-plugin-api.js`)
