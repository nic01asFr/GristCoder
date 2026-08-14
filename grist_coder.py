"""
GRIST CODER · MCP Server v5.14 · streamable HTTP spec 2025-03-26
────────────────────────────────────────────────────────────────
Document Grist = codebase du projet.
Widget = split vertical Ace editor | iframe preview.
LLM via MCP : schema relationnel + artefacts + pages structurées.

AUTH  : widget -> grist.docApi.getAccessToken() -> POST /register -> gc-xxx
        Claude Desktop -> Bearer <grist_key> + X-App-Token -> uid:user_id stable
        Connecteur OAuth (en ligne, si PUBLIC_URL) -> /oauth/* (cle Grist en consent)
                        -> access token gco-xxx revocable -> meme uid:user_id
TOOLS : sessions(3) plan(1) canvas(7) wizard(2) context(1) chat(2) subagent(1)
        artefact(2) grist-r(3) grist-w(3) validate(1) doc(5) webhooks(1) = 32
MCP   : sampling/createMessage (client capability) -> subagent_call + chat auto-reply
"""

import asyncio, base64, difflib, hashlib, hmac, io, json, os, re, secrets, shutil, subprocess, sys, tarfile, tempfile, time, uuid, urllib.parse, urllib.request, urllib.error
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

load_dotenv()
# URL publique de CE serveur. Le chart Onyxia n'injecte que PUBLIC_URL (voir
# charts/grist-coder/templates/deployment.yaml) : sans ce repli, un pod en ligne
# retombait sur le defaut localhost et posait des URLs inutilisables dans les
# documents (sections custom de grist_view_create/grist_view_add_widget, URLs de
# webhook). Le rstrip evite les doubles slash des concatenations HOST_URL + "/".
HOST_URL        = (os.getenv("HOST_URL", "").strip().rstrip("/")
                   or os.getenv("PUBLIC_URL", "").strip().rstrip("/")
                   or "http://localhost:8742")
WEBHOOK_SECRET  = os.getenv("WEBHOOK_SECRET", "")
MCP_VER  = "2025-03-26"
WIDGET_PATH = Path(__file__).parent / "widget.html"
# Sessions in-memory : purge des sessions inactives au-dela de ce TTL (defaut 24h).
SESSION_TTL     = int(os.getenv("SESSION_TTL", str(24 * 3600)))
# /llm-proxy : liste blanche d hotes autorises (CSV). Vide = endpoint desactive (defaut).
LLM_PROXY_ALLOWED_HOSTS = {
    h.strip().lower() for h in os.getenv("LLM_PROXY_ALLOWED_HOSTS", "").split(",") if h.strip()
}

# ── Deploiement Onyxia par-utilisateur (pod souverain) ─────────────────────────
# Ces variables sont injectees par le chart charts/grist-coder. Toutes vides en dev
# local -> aucun changement de comportement (garde et owner-lock desactives).
#
# Garde d'acces du pod : token Bearer exige sur la surface protegee (/mcp, /register,
# /llm-proxy). Vide = pas de garde (dev local, ou pod volontairement ouvert).
APP_AUTH_TOKEN = os.getenv("APP_AUTH_TOKEN", "").strip()
# Owner-lock TOFU : le 1er compte Grist (uid) qui s'enregistre devient proprietaire ;
# tout autre uid est rejete (403). Empeche un tiers d'utiliser ce pod avec sa cle.
OWNER_LOCK = os.getenv("OWNER_LOCK", "").strip().lower() in ("1", "on", "true", "yes")
_owner_uid: str | None = None  # epingle au 1er enregistrement quand OWNER_LOCK actif
# Defauts LLM injectes par le chart (repli quand le widget n'envoie pas X-LLM-*).
LLM_BASE_URL_DEFAULT  = os.getenv("LLM_BASE_URL", "").strip().rstrip("/")
LLM_MODEL_DEFAULT     = os.getenv("LLM_MODEL", "").strip()
LLM_API_KEY_ENV       = os.getenv("LLM_API_KEY", "").strip()
# Recuperation auto de la cle LLM depuis la config AI Assistant du datalab SSPCloud
# (Secret *-secretassistant), quand le pod tourne avec kubernetes.role: edit.
LLM_AUTO_FROM_DATALAB = os.getenv("LLM_AUTO_FROM_DATALAB", "").strip().lower() in ("1", "on", "true", "yes")


def _check_app_token(request) -> bool:
    """Vrai si la garde du pod est satisfaite (ou desactivee). En deploiement Onyxia,
    APP_AUTH_TOKEN est injecte ; la surface protegee doit le presenter via l'en-tete
    X-App-Token, la query app_token, ou le body (POST /register). Vide -> toujours vrai."""
    if not APP_AUTH_TOKEN:
        return True
    tok = (request.headers.get("X-App-Token")
           or request.query_params.get("app_token") or "")
    return bool(tok) and hmac.compare_digest(tok, APP_AUTH_TOKEN)


def _owner_gate(uid_key) -> bool:
    """Owner-lock TOFU : epingle le 1er uid:{id} reel, rejette tout autre. True = autorise.
    Les uid de repli key:{sha1} (profil non resolu, compte vide) ne sont jamais epingles."""
    global _owner_uid
    if not OWNER_LOCK or not uid_key or not uid_key.startswith("uid:"):
        return True
    if _owner_uid is None:
        _owner_uid = uid_key
        return True
    return uid_key == _owner_uid


# Cle LLM resolue une fois (env explicite, sinon lecture datalab). Cache best-effort.
_llm_key_cache: dict = {"key": None, "loaded": False}


async def _datalab_llm_key():
    """Lit la cle LLM depuis le Secret AI Assistant du datalab SSPCloud (pattern qgis-sspcloud).
    Le pod doit tourner avec kubernetes.role: edit (lecture des Secrets du namespace).
    Retourne la cle ou None. Resultat mis en cache (une seule lecture par process)."""
    if _llm_key_cache["loaded"]:
        return _llm_key_cache["key"]
    _llm_key_cache["loaded"] = True
    sa_dir = "/var/run/secrets/kubernetes.io/serviceaccount"
    try:
        with open(f"{sa_dir}/token", encoding="utf-8") as f:
            sa_token = f.read().strip()
        ns = os.getenv("SSPCLOUD_NAMESPACE", "").strip()
        if not ns:
            with open(f"{sa_dir}/namespace", encoding="utf-8") as f:
                ns = f.read().strip()
        async with httpx.AsyncClient(verify=f"{sa_dir}/ca.crt", timeout=10.0) as c:
            r = await c.get(
                f"https://kubernetes.default.svc/api/v1/namespaces/{ns}/secrets",
                headers={"Authorization": f"Bearer {sa_token}"})
            r.raise_for_status()
            for item in r.json().get("items", []):
                name = (item.get("metadata", {}) or {}).get("name", "")
                if name.endswith("secretassistant"):
                    raw = (item.get("data", {}) or {}).get("config.json")
                    if not raw:
                        continue
                    cfg = json.loads(base64.b64decode(raw).decode())
                    key = (cfg.get("api_keys", {}) or {}).get("OPENAI_API_KEY", "")
                    if key:
                        _llm_key_cache["key"] = key
                        print(f"[llm] cle recuperee du datalab (Secret {name})", file=sys.stderr)
                        return key
    except Exception as e:
        print(f"[llm] lecture datalab echouee : {type(e).__name__}: {e}", file=sys.stderr)
    return None


async def _resolve_llm_key():
    """Cle LLM server-side : env explicite, sinon config datalab (si autoFromDatalab)."""
    if LLM_API_KEY_ENV:
        return LLM_API_KEY_ENV
    if LLM_AUTO_FROM_DATALAB:
        return await _datalab_llm_key()
    return None

# Security warning: webhook receiver is unauthenticated unless WEBHOOK_SECRET is set.
# If HOST_URL is publicly accessible (not localhost), this allows anyone to inject
# fake webhook events into widgets connected to the server.
if not WEBHOOK_SECRET and "localhost" not in HOST_URL and "127.0.0.1" not in HOST_URL:
    print(
        "[SECURITY WARNING] WEBHOOK_SECRET is not set but HOST_URL appears public "
        f"({HOST_URL}). The /webhook-receive endpoint is unauthenticated — anyone "
        "can POST to it. Set WEBHOOK_SECRET in .env to prevent fake event injection.",
        file=sys.stderr,
    )

# ── SERVER INSTRUCTIONS ───────────────────────────────────────────────────────

SERVER_INSTRUCTIONS = """
Tu es Grist Coder MCP v5.14 — service de construction d apps Grist completes et guidees.

MISSION
  Transformer le besoin utilisateur en une application Grist complete (donnees + UI + logique +
  integrations) en 4 phases guidees. plan/{token} est la source de verite du projet en cours.
  Le widget est ton interface directe avec l utilisateur — utilise-le activement.

SURFACE UTILISATEUR — WIZARD MULTI-CARD
  L overlay wizard est une surface unifiee : plusieurs cards coexistent, chacune independante.
  - plan_update() -> card ctx-plan-progress (plan courant + phases + metadata)
  - canvas_wizard(id=...) -> card nommee (interaction, info, progres)
  - canvas_context_update(card_id=...) -> card nommee libre (feedback, resume subagent, etat)
  - canvas_wizard_close(card_id=...) -> ferme une card specifique ; sans arg = ferme tout
  Principe : ne jamais fermer tout l overlay sauf livraison finale. Chaque card a sa duree de vie.

COUCHES APP (toutes les 4 doivent etre traitees selon le besoin)
  1. Donnees  : tables Grist + formules (ce que possede le doc)
  2. UI       : artefacts HTML/React + pages Grist (ce que voit l utilisateur)
  3. Logique  : grist_apply, grist_sql, grist_upsert (ce que fait le systeme)
  4. Integr.  : grist_webhooks -> CRM/email/ERP/IA (effets exterieurs invisibles)

CYCLE GUIDE — 4 PHASES

  PHASE 1 — QUALIFIER (doc vide ou besoin flou)
    1. sessions_list() -> token + _next hint
    2. plan/{token} -> lire si plan existant (reprise) ; _next_step indique l action recommandee
    3. Si reference externe dans la requete (URL, standard, spec, nom de domaine metier) :
       WebFetch/WebSearch en premier -> extraire entites, relations, contraintes metier
    4. canvas_wizard(type="input", id="collect-need") -> collecter le besoin brut
    5. docs/qualification -> identifier categorie + architecture type adaptee
    6. Si doc existant : plan_update(status="assessing") -> schema-diagram/{token} + context/{token}
       pour comprendre l etat reel avant de concevoir
    7. Si besoin hors categorie standard ou domaine specialise : subagent_call(role="data-architect")
       est le chemin par defaut (pas l exception) -> canvas_context_update(card_id="arch-analysis")
    8. canvas_wizard(type="choice", id="confirm-category") -> confirmer categorie
    9. canvas_wizard(type="form", id="project-details") -> details : utilisateurs, donnees, integrations
    10. plan_update(need, tables, artefacts, pages, status="designing")
        -> _next_resources indique les ressources a lire avant conception

  NOTE CONTEXTES : TOUS les outils sont toujours disponibles — le status du plan ne verrouille rien.
    qualifying -> assessing -> designing -> building -> verifying -> done
    Le status est un GUIDE DE SEQUENCEMENT (quel outil est optimal a quel moment), pas une barriere.
    plan_update(status=...) met a jour ce guide et le bandeau de progression cote widget.

  PHASE 2 — CONCEVOIR (valider le plan avec l utilisateur)
    1. canvas_wizard(source="doc-overview") si tables existantes -> vue complete 3 onglets
       (etat donnees + pages & vues + flux) pour valider la comprehension structurelle
    2. canvas_wizard(type="confirm", id="validate-plan", content=plan_markdown) -> valider ou amender
    3. plan_update(status="building") si valide ; sinon retour phase 1 avec corrections
    4. artefact_init() si table Artefacts absente

  PHASE 3 — CONSTRUIRE (sequentiel, progression visible)
    Debut obligatoire :
      context/{token} -> snapshot complet : relations (graphe FK) + _quality (widgets vides,
                         tables sans page, Ref sans visibleCol) + _delta plan vs realite
      Nettoyage : si _quality ou _delta.extra -> supprimer orphelins (RemoveTable, RemoveRecord)
                  avant toute construction ; ne jamais construire par-dessus un etat incoherent
    canvas_wizard(type="progress", id="build-progress", steps=[nettoyage, tables, artefacts, pages, verification])
    Pour chaque table :
      docs/schema -> LIRE AVANT tout AddTable (types, formules, visibleCol, linked sections)
      Regle formule : toute valeur derivable d autres colonnes = colonne isFormula:true
                      (jours restants, totaux, statuts calcules, slugs...) jamais saisie manuelle
      grist_apply([AddTable, ...]) + grist_apply([BulkAddRecord, ...]) (min 3-5 lignes exemple)
      -> update progress card
    STYLE : pour utiliser le DSFR (Systeme de Design de l Etat), ajouter dans le <head> de l artefact :
      <link href="https://cdn.jsdelivr.net/npm/@gouvfr/dsfr@1.14/dist/dsfr.min.css" rel="stylesheet">
      <link href="https://cdn.jsdelivr.net/npm/@gouvfr/dsfr@1.14/dist/utility/utility.min.css" rel="stylesheet">
      Puis utiliser les classes fr-* (fr-btn, fr-card, fr-table, fr-alert, fr-input-group, fr-grid-row...)
      NE PAS injecter DSFR dans les artefacts utilisant des libs graphiques (MapLibre, Leaflet, Chart.js, D3)
      car le CSS global DSFR casse leurs rendus. Voir docs/artefacts pour le catalogue.

    LIBRAIRIES ET PUBLICATION — regles mesurees, pas theoriques :
      npm : un artefact peut faire `import x from "pkg"`. artefact_publish le bundle
        (esbuild cote serveur, paquets tires du registre a la demande). Le JSX passe,
        dans un artefact de type html — le controle de type porte sur Artefacts.Type,
        pas sur le contenu. La PREVISUALISATION restera vide : le navigateur ne resout
        pas les imports, seul le publie est bundle.
      Ecriture d'une source JSX : IMPOSSIBLE depuis le serveur sur une instance derriere
        un WAF (403 sur les balises hors chaine ; echapper < et > n'y change rien, le WAF
        normalise les echappements JSON). Passer par canvas_write avec un widget ouvert —
        le repli est automatique — ou ecrire en h(...) / React.createElement.
      CDN : un <script src=...> FONCTIONNE dans un widget publie (mesure). Il est signale,
        pas bloque : le widget depend alors de ce CDN a l'execution.
      Poids : preact 5 Ko gzip, preact/compat 10, leaflet 42, chart.js 59, react+dom 59,
        recharts ~105 hors React. Preact divise une app complete par ~1,5 face a React.
        Imports nommes, jamais `import *` : ECharts -50%, Recharts -29%.
      INTERDIT dans un artefact publie : toute reference au pod (/ai-proxy, /llm-proxy,
        /webhook-receive, son URL). artefact_publish refuse — le widget mourrait avec le
        serveur. Pour un artefact qui doit rester servi par le pod : grist_view_create.

    APPLICATION MULTI-ECRANS — artefact_publish(mode="app") :
      Les lignes nommees app/Xxx deviennent les ecrans ; un shell les monte a la demande.
      Gain mesure sur 3 ecrans : 6,5 Ko de metadonnees de section contre 755 Ko pour un
      artefact monolithique — or Grist retelecharge TOUTES les tables _grist_* a chaque
      ouverture du document. Un ecran jamais visite n'est jamais telecharge (237 o d'index
      au demarrage). Modifier un ecran = modifier la ligne + recharger, sans republier.
      LIMITE : les ecrans ne sont PAS bundles (c'est ce qui permet l'edition a chaud).
      Un ecran qui importe un paquet npm rendra VIDE. L'ecrire sans import.
      Depuis un ecran : app.navigate(nom), app.setState(o), app.emit(ev,d), app.on(ev,cb).

    Pour chaque artefact (dashboard -> fiches -> composants) :
      docs/artefacts -> LIRE AVANT canvas_write (templates, API Grist, patterns lies, composants DSFR)
      context/{token}/page/{page_id} -> contexte page : schema table source, liaisons entrantes/
                                        sortantes, artefact attendu, colonnes disponibles
      canvas_write(code) -> canvas_screenshot -> grist_upsert (ou Save widget si WAF)
      -> update progress card
    Pour chaque page Grist :
      playbook/{scenario} -> LIRE le scenario adapte : master-detail | dashboard | fiche | full-app
      docs/playbook -> sequences et decision tree
      grist_view_create / grist_view_add_widget + grist_section_configure
      Regle completude : toute section custom = artefact configure ; toute table metier = une page

  PHASE 4 — VERIFIER ET LIVRER
    1. context/{token} -> verifier _quality : plus de widgets vides, Ref avec visibleCol
    2. canvas_wizard(source="doc-overview") -> validation visuelle app complete (3 onglets)
    3. canvas_wizard(type="confirm", id="delivery") -> resume construit + actions utilisateur
    4. Si l utilisateur veut une app autonome (sans serveur MCP au runtime) :
       artefact_publish(artefact, section_ref?) pour chaque artefact html finalise
       -> le code est fige DANS le doc (options de section, widget builder galerie)
    5. plan_update(status="done")
    6. canvas_wizard_close() -> ferme tout l overlay

  REPRISE DE SESSION (plan existant)
    1. plan/{token} -> lire plan + status + _next_step + _history (timeline transitions)
       Le 'need' du plan est PERSISTE dans Grist (table Artefacts, record _project_meta)
       et automatiquement restaure au demarrage — pas besoin de re-qualifier.
    2. context/{token} -> etat reel actuel + _inferred_plan (status deduit de l etat doc :
       tables, artefacts, pages, qualite). Si memory_status_diverges est present,
       l etat memoire est obsolete -> faire confiance a _inferred_plan.
    3. plan_update() non-bloquant -> restaure la card ctx-plan-progress
    4. canvas_wizard(type="confirm", id="resume") -> "Reprendre ?" ou "Modifier le plan ?"
    5. Continuer depuis le status precedent (memoire ou inferred)

PRINCIPES D ORCHESTRATION
  - Chaque outil a un moment optimal : lire _next des reponses pour savoir quoi appeler apres
  - Plusieurs cards simultanees = richesse : ctx-plan-progress (plan) + progress (build) + feedback subagent
  - Ne jamais bloquer sans feedback : toujours une card progress visible pendant les ops longues
  - subagent_call : si sampling dispo -> reponse directe {role, response, structured?}
                    si sampling absent -> {fallback_mode:True, system_prompt, task, instruction}
                    fallback : suspendre role orchestrateur -> executer en role specialise -> reprendre orchestrateur
                    Dans les deux cas : traiter le resultat puis canvas_context_update(card_id="X")
  - plan_update() en debut + fin de chaque phase : maintient la coherence de la source de verite

BOUCLE AUTONOME — CHAT WIDGET (v5.12)
  Le widget dispose d un composant chat en bas de l overlay wizard.
  L utilisateur peut envoyer des messages libres PENDANT qu une operation se deroule.
  PATTERN STANDARD : chat_reply(message, wait=True) — un seul appel envoie + attend reponse.
  Retourne {text, ts} quand l utilisateur repond.
  Pour attendre sans envoyer de message : wait_for_chat(blocking=True).
  Le composant chat n apparait dans le widget que quand chat_reply est appele (masque par defaut).

BOUCLE POST-WIZARD (apres chaque reponse interactive bloquante)
  La reponse wizard contient toujours : _ux_context, _ux_task, _next
  1. Traiter la reponse (valider, patcher, noter)
  2a. Si transition de phase evidente -> plan_update(status=...) -> progress card auto-mise a jour
  2b. Sinon -> subagent_call(role='ux-navigator', task=_ux_task)
      -> retourne {next_step, resources_a_lire, reasoning}
      -> lire resources_a_lire si pertinent
      -> canvas_wizard(step=next_step) pour enchainer naturellement
  3. Le wizard ne se ferme que sur plan_update(status='done') ou choix explicite utilisateur
  Objectif : le LLM ne laisse jamais l overlay vide apres une reponse — il enchaine toujours.

OUTILS (34)
  Plan     : plan_update              <- META-CONTROLE : persiste + _next_resources par phase
  Sessions : sessions_list            <- _next hint integre (plan? ou docs/qualification)
             session_select, session_info
             session_open(doc_id)     <- ouvre un doc SANS navigateur (cle de l appelant).
             Aucun widget requis, artefact_publish compris. Seul canvas_screenshot
             en demande un, par nature.
             ROUTAGE : si plusieurs documents sont ouverts, passer token=<token> A CHAQUE
             outil (le token vient de sessions_list). C est portable, ca survit aux clients
             qui ne gerent pas Mcp-Session-Id, et ca permet a plusieurs agents/onglets de
             travailler en parallele sur des docs differents. session_select ne fait
             qu epingler un defaut pour la connexion courante.
  Canvas   : canvas_select (switch artefact, lecture seule), canvas_read, canvas_write, canvas_patch, canvas_exec, canvas_screenshot, canvas_type
  Wizard   : canvas_wizard (id requis, choice|form|confirm|progress|info|input)
             canvas_wizard_close (card_id? -> ferme une card ; absent -> ferme tout)
  Chat     : chat_reply(message, wait=False) -> bulle assistant (wait=True = envoie + attend reponse)
             wait_for_chat(blocking?, timeout?) -> attend message user sans envoyer (cas async)
  Context  : canvas_context_update (card_id? -> card nommee libre dans l overlay)
  Subagent : subagent_call(role=...)  <- roles: data-architect|ui-designer|page-architect|
                                          data-analyst|integrator|assistant
             subagent_call(role='ux-navigator', task=_ux_task)
                                       <- POST-WIZARD : compose le step wizard suivant.
                                          Auto-injecte etat doc + ressources disponibles.
                                          Retourne {next_step, resources_a_lire, reasoning}.
  Artefact : artefact_init
             artefact_publish       <- LIVRAISON : fige un artefact html en widget 100% autonome
                                       (code copie dans les options de section, builder galerie —
                                       zero dependance au serveur MCP au runtime)
  Grist R  : grist_schema, grist_records, grist_sql
  Validate : grist_validate        <- PRE-VOL avant grist_apply (Ref forward, formats) — evite les 500 sandbox
  Grist W  : grist_records_add, grist_records_patch, grist_upsert
  Document : grist_apply, grist_views_list, grist_view_create,
             grist_section_configure, grist_view_add_widget
  Webhooks : grist_webhooks (list OK accessToken | CRUD = cle API owner)

RESSOURCES — niveaux de contexte

  DOC (session entiere)
    plan/{token}           -> PREMIER : plan persistant + _next_step (reprise ou debut)
    context/{token}        -> snapshot doc complet : schema + relations (graphe FK) + artefacts
                              + pages/sections + _quality (anomalies) + _delta plan vs realite
    schema-diagram/{token} -> erDiagram mermaid auto-genere : tables + FK Ref: + visibleCol
    code/{token}           -> source de tous les artefacts (avant iteration sur app existante)

  CONSTRUCTION (avant operation)
    docs/qualification     -> LIRE avant phase 1 : categories, architectures, criteres completude
    docs/schema            -> types colonnes, formules, visibleCol AVANT tout grist_apply
    docs/artefacts         -> templates, API Grist, patterns AVANT canvas_write
    docs/formulas          -> colonnes Python Grist (isFormula, getattr, lookupOne...)
    docs/app-patterns      -> patterns avances (nav, sync, Artefactory)

  PAGE (avant coder un artefact ou creer une page)
    context/{token}/page/{page_id} -> schema table source + liaisons entrantes/sortantes
                                       + artefact configure + colonnes disponibles
    playbook/{scenario}    -> guide scenario avant grist_view_create
                              valeurs : dashboard | fiche | table | full-app | master-detail
    docs/playbook          -> decision tree pages + sequences linked sections
    docs/wizard            -> schema wizard avant canvas_wizard

  SERVICES (avant integration externe)
    docs/services-geo      -> geocodage, cartographie (BAN, OSM, IGN, Leaflet)
    docs/services-data     -> donnees ouvertes (SIRENE, DVF, data.gouv, API Geo)
    docs/services-ai       -> patterns IA (sync canvas_exec, async webhook, bridge, subagent)
    examples/{domain}      -> schema + donnees exemple pour un domaine metier

  LIVRAISON (avant artefact_publish)
    docs/publication       -> figer un artefact en widget autonome dans le doc (html/svg, split script)

STANDARDS QUALITE (non-negotiables)
  Donnees  : types corrects (Text/Numeric/Date/Bool/Choice/Ref:Table), FK via Ref:, formules
             Toute valeur derivable d autres colonnes -> colonne isFormula:true (jamais saisie manuelle)
             Toute colonne Ref: -> visibleCol defini (sinon champ vide dans UI)
  UI       : design responsive (Inter, CSS var, mobile-first), palette widget (#3e5de7/#10b981)
  UX       : donnees exemple (BulkAddRecord min 3-5 lignes), pages nommees, widgets lies
  Pages    : toute table metier -> au moins une page Grist avec widget
             toute section custom -> artefact configure (url ou widgetOptions.artefact)
  Completude : dashboard (vue globale) + fiche detail (vue unitaire) + navigation si >2 pages

REGLES CRITIQUES
  JAMAIS REST PATCH sur _grist_Views / _grist_Views_section -> crash frontend
  TOUJOURS grist_apply(["UpdateRecord",...]) pour toutes les tables meta
  canvas_read() AVANT canvas_patch() — old_str doit etre exact et unique
  WAF grist.numerique.gouv.fr : artefacts lourds -> canvas_write/patch -> Save widget
  canvas_write cree automatiquement l artefact si inexistant (lookup Grist interne).
    Le type est auto-detecte depuis le contenu si art_type absent.
    Si art_nom absent : nouvel artefact Draft_{sha6} cree.
    Apres canvas_write : widget refresh + switch sur le bon artefact + saveArt() browser.
""".strip()

# ── SESSION CTX ───────────────────────────────────────────────────────────────

class SessionCtx:
    def __init__(self, doc_id, doc_title, site_url, grist_key="", access_token=""):
        self.doc_id       = doc_id
        self.doc_title    = doc_title or doc_id
        self.site_url     = site_url.rstrip("/")
        self.grist_key    = grist_key
        self.access_token = access_token
        self.canvas          = ""
        self.history         = deque(maxlen=50)
        self.created_at      = time.time()
        self.last_seen       = time.time()
        self.token           = "gc-" + uuid.uuid4().hex[:6]
        self.subscribers: set[str] = set()
        self.current_art_id  = None   # id Grist de l'artefact courant
        self.current_art_nom = None
        self.current_art_type= None
        self.wizard_responses: deque = deque(maxlen=20)
        self._wizard_events: dict[str, asyncio.Event] = {}  # keyed by step/card id
        self._async_wizard_responses: dict[str, dict] = {}  # card_id -> dernière réponse (mode async)
        self.project_plan: dict = {}   # plan de projet persistant : need, tables, artefacts, pages, status
        self.plan_history: deque = deque(maxlen=50)  # [{ts, status, summary}] — timeline des transitions
        self.current_context: str = "qualifying"   # qualifying|assessing|designing|building|verifying|done
        self._active_wizard_cards: dict[str, dict] = {}  # card_id -> step dict (persist on SSE reconnect)
        self._chat_history: deque = deque(maxlen=200)  # {role, content, ts} — replay sur reconnexion SSE

    def touch(self): self.last_seen = time.time()

    def meta(self):
        # Auto-cleanup: never expose _project_meta as the current artefact
        if self.current_art_nom == "_project_meta":
            self.current_art_id = None
            self.current_art_nom = None
            self.current_art_type = None
            self.canvas = ""
        age = int(time.time() - self.last_seen)
        return {
            "token":        self.token,
            "doc_id":       self.doc_id,
            "doc_title":    self.doc_title,
            "site_url":     self.site_url,
            "canvas_sha":        hashlib.sha1(self.canvas.encode()).hexdigest()[:8] if self.canvas else None,
            "canvas_lines":      len(self.canvas.splitlines()) if self.canvas else 0,
            "last_seen":         f"{age}s ago" if age < 3600 else f"{age//3600}h ago",
            "current_artefact":  {"id": self.current_art_id, "nom": self.current_art_nom,
                                  "type": self.current_art_type} if self.current_art_id else None,
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
        if token:
            ctx = sessions.get(token)
        elif len(sessions) == 1:
            ctx = next(iter(sessions.values())); ctx.touch()
        else:
            ctx = max(sessions.values(), key=lambda s: s.last_seen)
        if ctx and not ctx.grist_key and user.get("grist_key"):
            ctx.grist_key = user["grist_key"]
        return ctx

    def list_sessions(self, uid_key):
        user = self._users.get(uid_key)
        return [s.meta() for s in user["sessions"].values()] if user else []

    def purge_expired(self, ttl_seconds):
        """Supprime les sessions inactives depuis > ttl. Retourne (tokens_supprimes, uids_vides)."""
        now = time.time()
        removed_tokens, empty_uids = [], []
        for uid, u in list(self._users.items()):
            for tok, s in list(u["sessions"].items()):
                if now - s.last_seen > ttl_seconds:
                    del u["sessions"][tok]
                    removed_tokens.append(tok)
            if not u["sessions"]:
                empty_uids.append(uid)
        for uid in empty_uids:
            u = self._users.pop(uid, None)
            if u and u.get("grist_key"):
                self._grist_key_to_uid.pop(u["grist_key"], None)
        return removed_tokens, empty_uids


registry  = UserRegistry()
_token_to_uid: dict[str, str] = {}
_screenshot_waiters: dict[str, asyncio.Future] = {}
# Round-trip d'ecriture navigateur : le serveur pousse des UserActions au widget,
# qui les execute via grist.docApi.applyUserActions (bypass WAF + privileges owner)
# puis acquitte sur POST /apply-result/{token}.
_apply_waiters: dict[str, asyncio.Future] = {}
# Diagnostic de rendu : l'artefact remonte ses erreurs et l'etat de son rendu
# (POST /art-diag/{token}). canvas_write l'attend brievement pour que l'agent
# apprenne dans la MEME reponse si le code qu'il vient d'ecrire s'execute.
_diag_waiters: dict[str, asyncio.Future] = {}
_dernier_diag: dict[str, dict] = {}
_client_capabilities: dict[str, dict] = {}   # uid_key -> capabilities déclarées par le client MCP
_mcp_client_sids: dict[str, str] = {}        # uid_key -> sid SSE du client MCP (Claude Desktop)
_display_sids: set[str] = set()              # sids des widgets en display mode (?a=...) — pas de replay wizard
_sid_token: dict[str, str] = {}             # sid SSE -> token de session widget (quel DOCUMENT il couvre)
_sampling_waiters: dict[str, asyncio.Future] = {}  # smp_id -> Future pour sampling/createMessage
_chat_waiters: dict[str, asyncio.Future] = {}      # uid_key -> Future pour wait_for_chat

# ── SECURITY HELPERS ──────────────────────────────────────────────────────────

_SECRET_RE = re.compile(r'(auth=)[^&\s"\'\\]+')
_BEARER_RE = re.compile(r'(Bearer\s+)[A-Za-z0-9._\-]+')

def _scrub_secrets(text):
    """Masque les tokens (auth=..., Bearer ...) avant de renvoyer une erreur au client."""
    if not text:
        return text
    s = _SECRET_RE.sub(r'\1[redacted]', str(text))
    return _BEARER_RE.sub(r'\1[redacted]', s)

async def _read_json(request, *, default=None):
    """Parse le body JSON en tolerant l'absence/malformation. Retourne (data, err) ; err=JSONResponse ou None."""
    try:
        return await request.json(), None
    except Exception:
        if default is not None:
            return default, None
        return None, JSONResponse({"error": "Corps de requete JSON invalide"}, status_code=400)

# ── SSE ───────────────────────────────────────────────────────────────────────

_queues: dict[str, asyncio.Queue] = {}
_sid_uid: dict[str, str] = {}   # sid SSE -> uid_key proprietaire (routage du fan-out par user)

_WIZARD_EVENT_TYPES = {"wizard_step", "wizard_close", "context_update"}

def _push(uid_key, event):
    event["_user"] = uid_key
    is_wizard_event = event.get("type") in _WIZARD_EVENT_TYPES
    for sid, q in _queues.items():
        if _sid_uid.get(sid) != uid_key:
            continue  # Router uniquement vers les SSE de ce user (pas de pollution cross-user)
        if is_wizard_event and sid in _display_sids:
            continue  # Ne pas envoyer les events wizard aux widgets en display mode
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            # Drop oldest event to make room — keeps the queue responsive instead
            # of silently losing the newest events
            try:
                q.get_nowait()
                q.put_nowait(event)
                print(f"[SSE] queue full for sid={sid[:8]} — dropped oldest event", file=sys.stderr)
            except Exception:
                print(f"[SSE] queue full for sid={sid[:8]} — dropped event {event.get('type')}", file=sys.stderr)

def _notify_resource(uid_key, uri):
    _push(uid_key, {"type": "mcp_notification",
                    "method": "notifications/resources/updated",
                    "params": {"uri": uri}})


def _has_live_widget(uid_key, token: str = "") -> bool:
    """Vrai si un widget navigateur (SSE) est connecte pour ce user. Exclut la connexion
    SSE du client MCP lui-meme (Claude Desktop ouvre aussi un GET /mcp) : elle n'execute
    pas applyUserActions et ne doit pas compter comme un widget capable d'ecrire.

    token : restreint au widget couvrant CE document. Sans ce filtre, un widget ouvert
    sur un autre doc faisait croire qu'un relais existait — on tentait l'ecriture
    navigateur, elle expirait au bout de 20 s, et l'utilisateur recevait un timeout
    au lieu du message actionnable."""
    mcp_sids = set(_mcp_client_sids.values())
    for sid in _queues:
        if _sid_uid.get(sid) != uid_key or sid in mcp_sids:
            continue
        if token and _sid_token.get(sid) != token:
            continue
        return True
    return False


def _contient_balises_nues(records) -> bool:
    """Detecte du JSX / des balises hors chaine dans des valeurs a ecrire — la forme
    que le WAF refuse depuis le serveur (voir _waf_json)."""
    txt = json.dumps(records, ensure_ascii=False)
    # une balise suivie d'un retour ou d'un accolade JSX, non precedee d'un guillemet
    return bool(re.search(r"[^\"'\\](<)\s*[A-Za-z][\w.-]*[^>]{0,120}>\s*\{", txt)) or \
           bool(re.search(r"return\s*\(\s*<[A-Za-z]", txt))


async def _ecrire_donnees(uid_key, ctx, table_id, records, *, mode="upsert"):
    """Ecrit des enregistrements, avec repli navigateur sur refus du WAF.

    Une source JSX est rejetee en 403 par le WAF quand elle part du serveur (le
    detail est dans _waf_json). Le navigateur, lui, ecrit en same-origin : on lui
    passe la main plutot que de rendre un 403 opaque."""
    try:
        if mode == "upsert":
            await grist_put(ctx, f"tables/{table_id}/records", {"records": records})
        else:
            return await grist_post(ctx, f"tables/{table_id}/records", {"records": records})
        return {"ok": True}
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 403:
            raise
        actions = [["AddRecord", table_id, None, r.get("fields", r)] for r in records]
        if mode == "upsert":
            actions = [["AddRecord", table_id, None,
                        dict(r.get("require", {}), **r.get("fields", {}))] for r in records]
        if _has_live_widget(uid_key, ctx.token):
            ok, err = await _browser_apply(uid_key, ctx, actions)
            if ok:
                return {"ok": True, "_via": "navigateur",
                        "_note": ("Ecriture serveur refusee par le WAF (source contenant des "
                                  "balises hors chaine, typiquement du JSX). Le widget a pris "
                                  "le relais.")}
            raise RuntimeError(f"serveur refuse (WAF) et navigateur en echec : {err}")
        indice = (" Le contenu ressemble a du JSX : c'est la forme que le WAF refuse."
                  if _contient_balises_nues(records) else "")
        raise RuntimeError(
            "Ecriture refusee par le WAF de l'instance Grist (403)." + indice
            + " Deux issues : ouvrir le widget Coder sur ce document et reecrire par"
              " canvas_write (la sauvegarde transite alors par le navigateur), ou ecrire"
              " la source en h(...) / React.createElement plutot qu'en JSX — elle se"
              " bundle aussi bien a la publication.")


async def _attendre_diag(uid_key, ctx, *, timeout=3.0) -> dict | None:
    """Attend le diagnostic de rendu de l'artefact qu'on vient d'ecrire.

    Court par construction : on ne bloque pas l'agent pour un confort. Sans widget
    ouvert, on ne tente rien — le rendu n'a simplement pas lieu."""
    if not _has_live_widget(uid_key, ctx.token):
        return None
    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    ancien = _diag_waiters.pop(ctx.token, None)
    if ancien and not ancien.done():
        ancien.cancel()
    _diag_waiters[ctx.token] = fut
    try:
        return await asyncio.wait_for(fut, timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        return None
    finally:
        if _diag_waiters.get(ctx.token) is fut:
            _diag_waiters.pop(ctx.token, None)


def _lire_diag(diag: dict) -> dict | None:
    """Traduit le diagnostic brut en verdict actionnable, ou None si tout va bien."""
    if not diag:
        return None
    erreurs = diag.get("erreurs") or []
    rejets = diag.get("rejets") or []
    cerr = diag.get("console_error") or []
    ress = diag.get("ressources") or []
    rendu = diag.get("rendu") or {}
    out, quoi = {}, []
    if erreurs:
        out["erreurs"] = erreurs[:5]
        quoi.append(f"{len(erreurs)} exception(s)")
    if rejets:
        out["rejets_de_promesse"] = rejets[:5]; quoi.append(f"{len(rejets)} rejet(s)")
    if cerr:
        out["console_error"] = cerr[:5]; quoi.append(f"{len(cerr)} console.error")
    if ress:
        out["ressources_non_chargees"] = ress[:5]
        quoi.append(f"{len(ress)} ressource(s) non chargee(s)")
    # Le cas sans exception : le script echoue en amont, le markup s'affiche, rien d'autre.
    if rendu.get("vide"):
        out["rendu_vide"] = True
        quoi.append("rendu vide")
    if not quoi:
        return None
    out["resume"] = "Artefact en echec au rendu : " + ", ".join(quoi) + "."
    out["_next"] = ("Corriger puis reecrire — le diagnostic revient a chaque canvas_write. "
                    "Rendu vide sans exception : verifier les imports npm (non resolus en "
                    "previsualisation, ils le seront a la publication) et les identifiants "
                    "vises par getElementById.")
    return out


async def _apply_meta(uid_key, ctx, actions, *, timeout=20.0):
    """Ecriture de tables meta (_grist_Views*), par le chemin le plus court disponible.

    Serveur d'abord quand la session porte une VRAIE cle API : les deux raisons qui
    imposaient le detour par le navigateur tombent alors — la portee insuffisante
    d'un accessToken sur les tables meta (une cle a les droits owner) et le WAF sur
    les payloads contenant du script (_waf_json echappe deja < et >). Verifie en
    production : UpdateRecord sur _grist_Views_section.options accepte cote serveur.

    Repli sur le navigateur sinon, ou si l'ecriture serveur echoue. C'est ce qui
    permet de publier SANS widget ouvert (session_open)."""
    if ctx.grist_key:
        try:
            await grist_apply(ctx, actions)
            return True, None
        except Exception as e:
            err_srv = _scrub_secrets(str(e))
        if not _has_live_widget(uid_key, ctx.token):
            return False, (f"Ecriture serveur refusee ({err_srv}) et aucun widget Coder "
                           "ouvert pour prendre le relais.")
    else:
        err_srv = "session sans cle API (accessToken widget)"
    ok, err_nav = await _browser_apply(uid_key, ctx, actions, timeout=timeout)
    return ok, (None if ok else f"serveur : {err_srv} | navigateur : {err_nav}")


async def _browser_apply(uid_key, ctx, actions, *, timeout=20.0):
    """Execute des UserActions DANS le navigateur (widget) via grist.docApi.applyUserActions.
    Pourquoi : les ecritures serveur vers grist.numerique.gouv.fr passent par le WAF Incapsula
    (403 sur gros payloads / <script> / droits meta insuffisants de l'accessToken). Le navigateur,
    lui, ecrit en same-origin sur la session authentifiee de l'utilisateur -> bypass WAF ET
    privileges pleins du proprietaire (ecriture des tables meta _grist_Views_section OK).
    Round-trip SSE (apply_request) + ack (POST /apply-result/{token}). Retourne (ok, error)."""
    if not _has_live_widget(uid_key):
        return False, ("Aucun widget Coder connecte pour ce compte. Ouvre le widget Coder "
                       "dans ce doc Grist pour publier : l'ecriture passe par le navigateur "
                       "afin de contourner le pare-feu de l'instance.")
    prior = _apply_waiters.pop(ctx.token, None)
    if prior and not prior.done():
        prior.cancel()
    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    _apply_waiters[ctx.token] = fut
    _push(uid_key, {"type": "apply_request", "token": ctx.token, "actions": actions})
    try:
        res = await asyncio.wait_for(fut, timeout=timeout)
        if isinstance(res, dict) and res.get("error"):
            return False, str(res["error"])
        return True, None
    except asyncio.TimeoutError:
        return False, ("Timeout : le widget connecte n'a pas execute l'action dans le delai. "
                       "Verifie que le widget Coder est ouvert et actif sur ce doc.")
    finally:
        if _apply_waiters.get(ctx.token) is fut:
            _apply_waiters.pop(ctx.token, None)

# ── PLAN PROGRESS CARD ────────────────────────────────────────────────────────
# Construit la card "ctx-plan-progress" (bandeau Zone 1) depuis ctx.project_plan.
# Factorise pour etre reutilise par plan_update ET par le replay SSE (restauration
# du bandeau apres reconnexion / reload).

_PLAN_STATUS_META = {
    "qualifying":  ("Qualification du besoin",   "info",    10),
    "assessing":   ("Exploration du document",   "info",    20),
    "designing":   ("Conception de l app",       "info",    35),
    "building":    ("Construction en cours",     "warn",    65),
    "verifying":   ("Verification finale",       "success", 85),
    "done":        ("App livree",                "success", 100),
}
_PLAN_PHASES = [
    ("qualifying", "Qualification du besoin"),
    ("assessing",  "Exploration du document"),
    ("designing",  "Conception de l app"),
    ("building",   "Construction"),
    ("verifying",  "Verification finale"),
    ("done",       "Livre"),
]

def _plan_progress_step(ctx):
    """Card progress du plan courant (sections + steps + %), ou None si pas de plan."""
    plan = ctx.project_plan
    if not plan or not plan.get("status"):
        return None
    status = plan.get("status", "qualifying")
    label, style, progress = _PLAN_STATUS_META.get(status, ("En cours", "default", 20))
    meta_sections = [{"label": "Phase", "content": label, "style": style}]
    if plan.get("need"):
        meta_sections.append({"label": "Besoin", "content": str(plan["need"])[:90], "style": "default"})
    tables = plan.get("tables", [])
    if tables:
        names = ", ".join(t.get("name", t) if isinstance(t, dict) else t for t in tables[:6])
        meta_sections.append({"label": "Tables", "content": names, "style": "code"})
    arts = plan.get("artefacts", [])
    if arts:
        names = ", ".join(a.get("name", a) if isinstance(a, dict) else a for a in arts[:5])
        meta_sections.append({"label": "Artefacts", "content": names, "style": "code"})
    if plan.get("notes"):
        meta_sections.append({"label": "Note", "content": str(plan["notes"])[:80], "style": "default"})
    phase_order = [p[0] for p in _PLAN_PHASES]
    cur_idx = phase_order.index(status) if status in phase_order else 0
    prog_steps = [
        {"id": ph, "label": lbl,
         "status": "done" if i < cur_idx else ("active" if i == cur_idx else "pending")}
        for i, (ph, lbl) in enumerate(_PLAN_PHASES)
    ]
    return {"id": "ctx-plan-progress", "type": "progress",
            "title": f"Plan · {ctx.doc_title}", "steps": prog_steps,
            "sections": meta_sections, "progress": progress}

# ── GRIST HTTP HELPERS ────────────────────────────────────────────────────────


def _gh(ctx):
    h = {"Content-Type": "application/json"}
    if ctx.grist_key:
        h["Authorization"] = f"Bearer {ctx.grist_key}"
    return h

def _aq(ctx):
    return {"auth": ctx.access_token} if ctx.access_token else {}

def _base(ctx): return f"{ctx.site_url}/api/docs/{ctx.doc_id}"

def _detect_type(code: str) -> str:
    """Auto-detect artefact type from code content."""
    import re as _re
    s = (code or "").strip()
    if _re.match(r'<svg[\s>]', s, _re.I):                                        return "svg"
    if _re.match(r'<!DOCTYPE html|<html', s, _re.I):                             return "html"
    if _re.match(r'(graph |sequenceDiagram|classDiagram|flowchart |erDiagram|gantt\n|pie |journey)', s, _re.I): return "mermaid"
    if "React.createElement" in s or "useState" in s or "useEffect" in s:        return "react"
    if _re.match(r'SELECT\s', s, _re.I):                                          return "sql"
    # Python before markdown: check first 15 lines for Python-specific constructs
    first = "\n".join(s.split("\n")[:15])
    if _re.search(r'(^|\n)(import |from \w+ import|def |class |print\(|[a-z_]+ = )', "\n" + first): return "python"
    if s.startswith("#!"):                                                         return "python"
    if s.startswith("```") or _re.match(r"#{1,6} \w", s) or (s.startswith("---") and "\n" in s): return "markdown"
    if "<" in s and ">" in s:                                                     return "html"
    return "html"

# Le WAF (Imperva/Incapsula) devant grist.numerique.gouv.fr challenge par
# intermittence avec un 302 + Set-Cookie vers la meme URL. Un jar partage +
# follow_redirects permet de resoudre le challenge une fois pour tout le process.
_GRIST_COOKIES = httpx.Cookies()

def _grist_client():
    return httpx.AsyncClient(timeout=15, follow_redirects=True, cookies=_GRIST_COOKIES)

def _waf_json(obj) -> str:
    """Serialise en JSON en echappant < et > en \\u003c / \\u003e. Grist les redecode
    a l'identique (JSON \\u003c == '<'). A utiliser pour tout body d'ecriture serveur.

    ATTENTION — protection PARTIELLE, contrairement a ce qui etait ecrit ici.
    Le WAF normalise les echappements JSON avant inspection : il voit les balises
    malgre \\u003c. Mesure sur grist.numerique.gouv.fr, meme session, meme endpoint :

        pas de HTML .................................. passe
        h('div', ...) / React.createElement .......... passe
        innerHTML = '<table class="t">...' ........... passe   (balises ENTRE guillemets)
        JSX : return (<div><h2>T</h2></div>) ......... 403
        idem sans gestionnaire d'evenement ........... 403

    Le declencheur est donc la presence de balises HORS chaine de caracteres — la
    signature du JSX. Ni la taille, ni l'endpoint, ni le mode d'authentification
    n'entrent en jeu. Une source JSX ne peut pas etre ecrite depuis le serveur :
    passer par le navigateur (canvas_write, qui fait sauvegarder le widget) ou
    ecrire en h(...) / createElement, qui se bundle aussi bien."""
    return json.dumps(obj).replace("<", "\\u003c").replace(">", "\\u003e")

async def grist_get(ctx, path):
    async with _grist_client() as c:
        r = await c.get(f"{_base(ctx)}/{path}", headers=_gh(ctx), params=_aq(ctx))
        r.raise_for_status(); return r.json()

async def grist_post(ctx, path, body, *, no_token=False):
    async with _grist_client() as c:
        r = await c.post(f"{_base(ctx)}/{path}", headers=_gh(ctx),
                         params={} if no_token else _aq(ctx),
                         content=_waf_json(body))
        r.raise_for_status(); return r.json()

async def grist_patch(ctx, path, body, *, no_token=False):
    async with _grist_client() as c:
        r = await c.patch(f"{_base(ctx)}/{path}", headers=_gh(ctx),
                          params={} if no_token else _aq(ctx),
                          content=_waf_json(body))
        r.raise_for_status(); return r.json()

async def grist_put(ctx, path, body):
    async with _grist_client() as c:
        r = await c.put(f"{_base(ctx)}/{path}", headers=_gh(ctx), params=_aq(ctx),
                        content=_waf_json(body))
        r.raise_for_status(); return r.json()

async def grist_delete(ctx, path, *, no_token=False):
    async with _grist_client() as c:
        r = await c.delete(f"{_base(ctx)}/{path}", headers=_gh(ctx),
                           params={} if no_token else _aq(ctx))
        r.raise_for_status()
        return r.json() if r.content else {"ok": True}

def _rewrite_browser_actions(actions):
    """Sessions widget (access token, sans cle API) : certaines UserActions sont
    restreintes cote navigateur ('not controlled'). BulkAddOrReplaceRecord y est
    bloque -> le reecrire en BulkAddRecord (add-only, rowIds auto) pour que
    l'insertion de donnees exemple fonctionne sans privilege API."""
    if not isinstance(actions, list):
        return actions
    out = []
    for a in actions:
        if isinstance(a, list) and a and a[0] == "BulkAddOrReplaceRecord" and len(a) >= 4:
            n = len(a[2]) if isinstance(a[2], list) else 0
            a = ["BulkAddRecord", a[1], [None] * n, a[3]]
        elif isinstance(a, list) and a and a[0] == "AddOrReplaceRecord" and len(a) >= 3:
            a = ["AddRecord", a[1], None, a[2]] if len(a) == 3 else ["AddRecord", a[1], None, a[3]]
        out.append(a)
    return out

# Taille max de lignes par lot pour les Bulk*Record (imports massifs). Au-dela,
# grist_apply decoupe en sous-lots sequentiels pour rester sous les limites de
# taille du WAF Incapsula. Configurable via env.
_BULK_MAX_ROWS = int(os.getenv("GRIST_BULK_MAX_ROWS", "200"))

def _split_bulk_actions(actions, max_rows=None):
    """Decoupe les Bulk*Record de plus de max_rows lignes en sous-actions, en
    PRESERVANT l'ordre (les Ref: forward et AddTable-avant-insert restent valides).
    Les autres actions sont inchangees. Retourne (flat, split_bool)."""
    max_rows = max_rows or _BULK_MAX_ROWS
    if not isinstance(actions, list) or max_rows < 1:
        return actions, False
    flat, split = [], False
    for a in actions:
        if (isinstance(a, list) and len(a) >= 4 and isinstance(a[0], str)
                and a[0].startswith("Bulk") and isinstance(a[2], list)
                and isinstance(a[3], dict) and len(a[2]) > max_rows):
            verb, tbl, rowids, cols = a[0], a[1], a[2], a[3]
            for i in range(0, len(rowids), max_rows):
                sl = slice(i, i + max_rows)
                flat.append([verb, tbl, rowids[sl],
                             {k: (v[sl] if isinstance(v, list) else v) for k, v in cols.items()}])
            split = True
        else:
            flat.append(a)
    return flat, split

async def _grist_apply_post(ctx, actions) -> dict:
    """POST /apply d'UN lot d'actions, avec retry-5xx + backoff + echappement WAF."""
    body = _waf_json(actions)  # echappe <> -> ne declenche plus le 403 WAF sur <script>
    last = None
    for attempt in range(3):
        try:
            async with _grist_client() as c:
                r = await c.post(f"{_base(ctx)}/apply", headers=_gh(ctx), params=_aq(ctx),
                                 content=body)
                r.raise_for_status(); return r.json()
        except httpx.HTTPStatusError as e:
            if e.response is not None and e.response.status_code >= 500 and attempt < 2:
                last = e
                await asyncio.sleep(0.8 * (attempt + 1))  # backoff : 0.8s puis 1.6s
                continue
            raise
    if last:
        raise last

async def grist_apply(ctx, actions: list) -> dict:
    """Applique des user actions Grist via POST /apply.

    Resilience : les 5xx (WAF Incapsula qui bloque parfois les /apply en rafale)
    sont rejoues avec backoff. Le WAF bloque AVANT que Grist n'applique l'action,
    donc rejouer est sur. Les gros Bulk*Record (> _BULK_MAX_ROWS lignes) sont
    decoupes en lots SEQUENTIELS (ordre preserve) pour rester sous les limites WAF ;
    chaque lot beneficie du retry. Les timeouts ne sont PAS rejoues (evite le double
    insert add-only). NB : le decoupage sacrifie l'atomicite (import partiel possible)
    au profit du passage sous le WAF -- c'est le bon compromis pour les imports massifs."""
    # Session navigateur (pas de cle API) : reecrire les UserActions restreintes.
    if not ctx.grist_key:
        actions = _rewrite_browser_actions(actions)
    flat, split = _split_bulk_actions(actions)
    if not split:
        return await _grist_apply_post(ctx, actions)
    # Import massif : appliquer chaque sous-lot sequentiellement (ordre Ref preserve).
    last = None
    for idx, sub in enumerate(flat):
        try:
            last = await _grist_apply_post(ctx, [sub])
        except Exception as e:
            raise RuntimeError(_scrub_secrets(
                f"Import partiel : {idx} lot(s) applique(s) sur {len(flat)} avant echec. {e}")) from e
    return last if last is not None else {"ok": True, "batched": len(flat)}

# ── PUBLICATION AUTONOME (custom-widget-builder) ─────────────────────────────
# Un artefact "publie" est fige dans les options de sa section Grist via le
# widget galerie @berhalak/custom-widget-builder : le code vit dans le doc,
# aucune dependance au serveur MCP au runtime. L API grist n est disponible
# que dans le champ _js du builder — les <script> inline sont extraits de
# _html et deplaces dans _js.

_BUILDER_WIDGET_ID = "@berhalak/custom-widget-builder"
_BUILDER_DEF_FALLBACK = {
    "name": "Custom widget builder",
    "url": "https://gristgouv.github.io/gristlabs-widgets/custom-widget-builder/index.html",
    "widgetId": _BUILDER_WIDGET_ID,
    "published": True,
    "accessLevel": "none",
    "renderAfterReady": True,
    "description": "Build custom widgets with HTML and JavaScript, right inside Grist.",
    "isGristLabsMaintained": False,
    "authors": [{"name": "berhalak", "url": "https://github.com/berhalak"}],
}

async def _fetch_builder_def(ctx) -> dict:
    """widgetDef du builder depuis la galerie de l instance (fallback statique)."""
    try:
        root = urllib.parse.urlsplit(ctx.site_url or "")
        base = f"{root.scheme}://{root.netloc}"
        async with _grist_client() as c:
            r = await c.get(f"{base}/api/widgets")
            r.raise_for_status()
            for w in r.json():
                if w.get("widgetId") == _BUILDER_WIDGET_ID:
                    return w
    except Exception:
        pass
    return dict(_BUILDER_DEF_FALLBACK)

_SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                        re.IGNORECASE | re.DOTALL)

# ── BUNDLING — esbuild ────────────────────────────────────────────────────────
# Un artefact qui fait `import x from "preact"` ne peut PAS tourner tel quel dans
# un navigateur : il rend blanc. Le bundler resout les imports et inline tout, ce
# qui produit un artefact autonome (zero requete reseau a l'execution).
#
# Paquets recuperes A LA DEMANDE depuis le registre npm, avec cache disque dans le
# pod : rien n'est embarque dans l'image (un paquet coute ~0,2 s a telecharger).
# Extraire un tarball n'execute jamais le code du paquet — pas de postinstall.
#
# Resolution des dependances transitives : on ne reimplemente pas npm. esbuild dit
# exactement ce qui lui manque ("Could not resolve X") ; on telecharge X et on
# relance. Ca converge, chaque tour resolvant au moins un paquet.

NPM_REGISTRY = os.getenv("NPM_REGISTRY", "https://registry.npmjs.org").rstrip("/")
_BUNDLE_DIR = Path(os.getenv("BUNDLE_CACHE_DIR", tempfile.gettempdir())) / "grist-coder-bundle"
_BUNDLE_MAX_TOURS = 24          # garde-fou : boucle de resolution bornee
_BUNDLE_ALERTE_O = 2_000_000    # au-dela, on publie mais on signale le poids

# Catalogue de reference : ce vers quoi on oriente la generation. Ce n'est PAS une
# restriction — tout paquet du registre est bundlable — mais ces librairies-la sont
# celles dont le rapport poids/service est connu et mesure (gzip) :
#   preact 5 Ko · preact/compat 10 Ko · leaflet 42 Ko · chart.js 59 Ko
#   @tanstack/table-core 15 Ko · react+react-dom 59 Ko · d3 modules 8-29 Ko
CATALOGUE_BUNDLE = ["preact", "preact/compat", "@tanstack/table-core",
                    "chart.js", "leaflet", "d3-scale", "d3-shape", "react", "react-dom"]

# Pas d'ancrage en debut de ligne : dans un artefact, le code suit souvent la
# balise sur la MEME ligne (`<script>import {h} from "preact"`). On exige juste que
# le mot ne soit pas colle a un identifiant (evite `noimport`, `obj.import`).
_IMPORT_RE = re.compile(
    r"""(?<![\w.$])import\s+(?:[\w*{}\s,$]+?\s+from\s+)?["']([^"'./][^"']*)["']"""   # import ... from "pkg"
    r"""|(?<![\w.$])import\s*\(\s*["']([^"'./][^"']*)["']\s*\)"""                    # import("pkg") dynamique
    r"""|(?<![\w.$])require\s*\(\s*["']([^"'./][^"']*)["']\s*\)""")                  # require("pkg")
_NON_RESOLU_RE = re.compile(r'Could not resolve ["\']([^"\']+)["\']')


def _paquet_racine(spec: str) -> str:
    """'preact/hooks' -> 'preact' ; '@scope/pkg/sub' -> '@scope/pkg'."""
    parts = spec.split("/")
    return "/".join(parts[:2]) if spec.startswith("@") and len(parts) >= 2 else parts[0]


def imports_npm(code: str) -> list:
    """Paquets npm importes par ce code (hors chemins relatifs). Vide -> rien a bundler."""
    vus = []
    for m in _IMPORT_RE.finditer(code):
        spec = m.group(1) or m.group(2) or m.group(3)
        if not spec:
            continue
        # Un artefact peut AFFICHER du code : `var s = 'import y from "z"'`. Si le
        # dernier caractere significatif avant le match est un guillemet, on est
        # dans une chaine, pas devant une vraie instruction.
        avant = code[:m.start()].rstrip()
        if avant and avant[-1] in "'\"`":
            continue
        r = _paquet_racine(spec)
        if r not in vus:
            vus.append(r)
    return vus


def _esbuild_bin() -> str | None:
    """Chemin du binaire esbuild, ou None s'il n'est pas disponible."""
    p = os.getenv("ESBUILD_PATH", "").strip()
    if p and Path(p).exists():
        return p
    return shutil.which("esbuild")


def _npm_recupere(paquet: str, version: str = "") -> tuple[str, str]:
    """Telecharge et extrait un paquet npm dans le cache. Retourne (version, erreur)."""
    racine = _BUNDLE_DIR / "node_modules" / paquet
    if racine.exists() and (racine / "package.json").exists():
        try:
            return json.loads((racine / "package.json").read_text(encoding="utf-8")).get("version", "?"), ""
        except Exception:
            return "?", ""
    try:
        with urllib.request.urlopen(f"{NPM_REGISTRY}/{urllib.parse.quote(paquet, safe='@/')}",
                                    timeout=30) as r:
            meta = json.loads(r.read())
        ver = version or (meta.get("dist-tags", {}) or {}).get("latest", "")
        infos = (meta.get("versions", {}) or {}).get(ver)
        if not infos:
            return "", f"version introuvable pour {paquet}"
        with urllib.request.urlopen(infos["dist"]["tarball"], timeout=60) as r:
            data = r.read()
        racine.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            for m in tf.getmembers():
                if not m.isfile() or not m.name.startswith("package/"):
                    continue
                rel = m.name[len("package/"):]
                if not rel or ".." in rel:          # anti path-traversal
                    continue
                cible = racine / rel
                cible.parent.mkdir(parents=True, exist_ok=True)
                cible.write_bytes(tf.extractfile(m).read())
        return ver, ""
    except urllib.error.HTTPError as e:
        return "", (f"paquet '{paquet}' introuvable sur le registre npm"
                    if e.code == 404 else f"registre npm : HTTP {e.code}")
    except Exception as e:
        return "", f"registre npm injoignable : {type(e).__name__}"


def bundler_artefact(code: str) -> dict:
    """Bundle un artefact HTML dont le <script> importe des paquets npm.

    Retourne {ok, code?, paquets?, taille?, erreur?, _hint?}. Ne modifie rien si le
    code n'a aucun import : dans ce cas ok=True et bundle=False."""
    paquets = imports_npm(code)
    if not paquets:
        return {"ok": True, "bundle": False}
    exe = _esbuild_bin()
    if not exe:
        return {"ok": False, "bundle": False, "erreur": (
            "Cet artefact importe des paquets npm mais esbuild n'est pas disponible "
            "sur ce serveur — il rendrait une page blanche."),
            "paquets": paquets,
            "_hint": "Reecrire sans import (librairie en <script src=...> CDN), ou installer esbuild."}

    # Separation SANS passer par _split_html_js : celui-ci prefixe grist.ready(),
    # ce qui rendrait le bundle dependant de Grist meme pour un artefact qui n'en
    # a pas besoin. Le bundling doit etre neutre ; c'est la publication qui ajoute
    # le prefixe, plus tard, sur le code deja bundle.
    js_part = "\n\n".join(m.group(1).strip() for m in _SCRIPT_RE.finditer(code)
                          if m.group(1).strip())
    html_part = _SCRIPT_RE.sub("", code).strip()
    _BUNDLE_DIR.mkdir(parents=True, exist_ok=True)
    src = _BUNDLE_DIR / f"src-{hashlib.sha1(js_part.encode()).hexdigest()[:10]}.jsx"
    src.write_text(js_part, encoding="utf-8")
    out = src.with_suffix(".out.js")

    # JSX : runtime automatique plutot qu'une fabrique figee. Avec --jsx-factory=h on
    # imposait la convention Preact, et le meme JSX rendait blanc avec React (h non
    # defini). En automatique, esbuild injecte lui-meme l'import du bon runtime — il
    # suffit de lui dire lequel, deduit des paquets importes.
    source_jsx = "react" if "react" in paquets else "preact"
    versions, manquants_vus = {}, set()
    for tour in range(_BUNDLE_MAX_TOURS):
        r = subprocess.run(
            [exe, str(src), "--bundle", "--minify", "--format=iife", "--target=es2020",
             "--loader:.jsx=jsx", "--jsx=automatic", f"--jsx-import-source={source_jsx}",
             "--define:process.env.NODE_ENV=\"production\"",
             "--legal-comments=none", f"--outfile={out}"],
            capture_output=True, text=True, cwd=str(_BUNDLE_DIR), timeout=120)
        if r.returncode == 0:
            break
        manquants = [_paquet_racine(x) for x in _NON_RESOLU_RE.findall(r.stderr)]
        manquants = [m for m in dict.fromkeys(manquants) if m not in manquants_vus]
        if not manquants:
            return {"ok": False, "bundle": False, "paquets": paquets,
                    "erreur": "Bundling echoue.",
                    "detail": (r.stderr or "")[-600:],
                    "_hint": "Verifier les imports de l'artefact (chemins, noms de paquets)."}
        for m in manquants:
            manquants_vus.add(m)
            ver, err = _npm_recupere(m)
            if err:
                return {"ok": False, "bundle": False, "paquets": paquets,
                        "erreur": f"Dependance non resolue : {err}",
                        "catalogue": CATALOGUE_BUNDLE,
                        "_hint": ("Utiliser une librairie du catalogue, ou la charger en "
                                  "<script src=...> CDN si le registre npm est injoignable.")}
            versions[m] = ver
    else:
        return {"ok": False, "bundle": False, "paquets": paquets,
                "erreur": f"Resolution des dependances non convergente apres {_BUNDLE_MAX_TOURS} tours.",
                "_hint": "Reduire le nombre de librairies importees."}

    js = out.read_text(encoding="utf-8")
    ferm = "<" + "/script>"
    js = js.replace(ferm, "<\\/script>")          # ceinture : esbuild le fait deja
    tag = "<" + "script>" + js + ferm
    fin = "<" + "/body>"
    code_final = (html_part.replace(fin, tag + "\n" + fin) if fin in html_part
                  else html_part + "\n" + tag)
    res = {"ok": True, "bundle": True, "code": code_final, "paquets": paquets,
           "versions": versions, "taille": len(code_final)}
    if len(code_final) > _BUNDLE_ALERTE_O:
        res["avertissement_poids"] = (
            f"Artefact volumineux ({len(code_final)//1024} Ko). Au-dela de ~1 Mo le rendu "
            "se ressent sur mobile : preferer preact a react, et des imports nommes.")
    return res


_SCRIPT_SRC_RE = re.compile(r"<script[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)

def _audit_autonomie(code: str) -> tuple[list, list]:
    """Un widget publie doit vivre sans le serveur MCP : le code voyage avec le
    document et s'execute apres l'arret du pod. Retourne (bloquants, avertissements).

    Bloquant : toute reference au pod. Le widget mourrait avec lui.
    Avertissement : <script src=...> externe. Ces balises restent dans _html et
    s'executent bien — verifie en production : un artefact chargeant leaflet depuis
    jsDelivr voit la requete partir (200) et la librairie disponible. Mais le widget
    depend alors de ce CDN a l'execution : il n'est plus autonome au sens strict et
    casse si le CDN devient injoignable (reseau filtre, hebergeur en panne)."""
    bloquants, avertissements = [], []
    for motif, quoi in (("/ai-proxy",        "appel a /ai-proxy"),
                        ("/llm-proxy",       "appel a /llm-proxy"),
                        ("/webhook-receive", "webhook pointant vers le pod"),
                        ("HOST_URL",         "reference a HOST_URL")):
        if motif in code:
            bloquants.append(f"{quoi} ({motif})")
    for env in ("GRIST_CODER_WIDGET_URL", "PUBLIC_URL", "HOST_URL"):
        hote = os.getenv(env, "").strip().rstrip("/")
        if hote and hote not in ("http://localhost:8742",) and hote in code:
            bloquants.append(f"URL du pod en dur ({hote})")
            break
    for src in _SCRIPT_SRC_RE.findall(code):
        if src.startswith(("http://", "https://", "//")):
            avertissements.append(src[:90])
    return bloquants, avertissements


def _split_html_js(code: str) -> tuple[str, str]:
    """Separe un artefact HTML en (_html, _js) pour le custom-widget-builder.

    Les <script> inline (sans src) sont retires du markup et concatenes dans
    _js, seul contexte ou l API grist est definie. Les <script src=...> CDN
    restent dans _html. grist.ready() est prefixe si absent."""
    js_parts = [m.group(1) for m in _SCRIPT_RE.finditer(code)]
    html = _SCRIPT_RE.sub("", code)
    js = "\n\n".join(p.strip() for p in js_parts if p.strip())
    if "grist.ready" not in js:
        js = "grist.ready({ requiredAccess: 'full' });\n" + js
    return html.strip(), js.strip()

# ── PROVISIONING : creation d'un nouveau document + widget Coder ──────────────
# Cree un document vierge dans un workspace via une cle API de provisioning
# (owner du workspace) stockee cote serveur (.env). Personnel : URL widget =
# celle de CE serveur (localhost par defaut). L'injection du widget est
# best-effort et entierement gardee : un echec ne compromet pas la creation.

def _provision_key(uid_key):
    """Cle API Grist utilisable pour creer un document, avec sa provenance.

    Ordre : override explicite (.env GRIST_PROVISION_KEY) > cle de l'APPELANT
    (Bearer Claude Desktop / connecteur OAuth / widget) > cle du pod (GRIST_API_KEY
    injectee par le chart). Exiger une cle de provisioning dediee etait redondant :
    sur un pod perso, l'appelant est deja owner de ses workspaces, et le doc doit
    lui appartenir a lui, pas a une identite de service."""
    k = os.getenv("GRIST_PROVISION_KEY", "").strip()
    if k:
        return k, "GRIST_PROVISION_KEY"
    user = registry._users.get(uid_key) or {}
    k = (user.get("grist_key") or "").strip()
    if k:
        return k, "cle Grist de l'appelant"
    for s in (user.get("sessions") or {}).values():
        k = (getattr(s, "grist_key", "") or "").strip()
        if k:
            return k, "cle Grist de la session widget"
    k = os.getenv("GRIST_API_KEY", "").strip()
    if k:
        return k, "GRIST_API_KEY (cle du pod)"
    return "", ""


def _erreur_cle_absente(uid_key) -> dict:
    """Message actionnable quand aucune cle Grist n'est disponible.

    Cas devenu frequent depuis les tokens OAuth signes : le token survit au
    redemarrage du pod, mais la cle Grist — recueillie au consentement et gardee
    en memoire — non. L'appelant se retrouve authentifie mais sans cle, etat
    incoherent qu'un message generique rend enigmatique."""
    user = registry._users.get(uid_key) or {}
    connecte = bool(user.get("sessions"))
    return {
        "error": ("Aucune cle Grist disponible pour ce compte."
                  + (" Tu es pourtant connecte : ta cle a ete recueillie au consentement "
                     "et vit en memoire — elle n'a pas survecu au dernier redemarrage du pod."
                     if connecte else "")),
        "_next": ("Reconnecter le connecteur UNE fois : le consentement redonne la cle. "
                  "Ou utiliser Authorization: Bearer <cle_grist>."),
        "_durable": ("Ce reconsentement est le dernier necessaire : depuis cette version la cle "
                     "voyage scellee (chiffree) dans le token OAuth, donc elle survit aux "
                     "redemarrages du pod sans rien a configurer. Les tokens emis AVANT n'en "
                     "portent pas — d'ou ce message."),
    }


def _provision_site_url(uid_key):
    """Base du site Grist : session active de l'appelant > site memorise pour l'uid >
    env GRIST_SITE_URL > n'importe quelle session connue du serveur."""
    user = registry._users.get(uid_key) or {}
    for s in (user.get("sessions") or {}).values():
        if getattr(s, "site_url", ""):
            return s.site_url
    if (user.get("site") or "").strip():
        return user["site"].strip()
    env = os.getenv("GRIST_SITE_URL", "").strip()
    if env:
        return env
    for u in registry._users.values():
        for s in (u.get("sessions") or {}).values():
            if getattr(s, "site_url", ""):
                return s.site_url
    return ""


def _coder_widget_url(avec_token=False):
    """URL publique de CE serveur, a poser dans la section custom du nouveau doc.
    Le chart Onyxia n'injecte que PUBLIC_URL (pas HOST_URL) : sans ce repli, un pod
    en ligne posait une URL localhost inutilisable dans le doc cree.

    avec_token : ajoute ?app_token=... quand la garde du pod est active. Le widget
    lit ce parametre dans son URL et le presente a POST /register — sans lui, la
    garde repond 401 et le widget s'affiche sans jamais se connecter."""
    base = ""
    for env in ("GRIST_CODER_WIDGET_URL", "PUBLIC_URL"):
        v = os.getenv(env, "").strip().rstrip("/")
        if v:
            base = v
            break
    base = base or HOST_URL.rstrip("/")
    if avec_token and APP_AUTH_TOKEN:
        return f"{base}/?app_token={urllib.parse.quote(APP_AUTH_TOKEN, safe='')}"
    return base


async def _provision_workspaces(c, base, phdr, org_hint=""):
    """Enumere les workspaces visibles par la cle, toutes orgs confondues.
    Retourne [{id, name, org, org_name, docs}]. Ne leve pas."""
    orgs = []
    try:
        r = await c.get(f"{base}/api/orgs", headers=phdr)
        r.raise_for_status()
        orgs = [o for o in (r.json() or []) if isinstance(o, dict)]
    except Exception:
        orgs = []
    if not orgs and org_hint:
        orgs = [{"id": org_hint, "domain": org_hint, "name": org_hint}]
    out = []
    for o in orgs:
        oid = o.get("domain") or o.get("id")
        if oid is None:
            continue
        try:
            rw = await c.get(f"{base}/api/orgs/{oid}/workspaces", headers=phdr)
            rw.raise_for_status()
            wss = rw.json() or []
        except Exception:
            continue
        for w in wss:
            if not isinstance(w, dict) or w.get("id") is None:
                continue
            out.append({"id": w.get("id"), "name": w.get("name"),
                        "org": str(o.get("domain") or o.get("id")),
                        "org_name": o.get("name"),
                        "docs": len(w.get("docs") or [])})
    return out


async def _provision_coder_widget(base, doc_id, key, widget_url):
    """Prepare un document neuf pour Grist Coder. Etat vise, et rien d'autre :

        table Artefacts  +  une page "🟢Coder" en section custom sur cette table

    La Table1 du document vierge est supprimee. La table est creee par REST, qui
    n'engendre PAS de page (primaryViewId = 0) — contrairement a l'action AddTable
    lancee depuis le navigateur, d'ou la page "Artefacts" parasite qu'on observait
    quand le widget creait la table lui-meme au premier chargement.

    Retourne True si l'API confirme, False sinon. Ne leve pas : l'appelant garde
    deja, mais on reste defensif."""
    phdr = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    async with _grist_client() as c:
        # 1. Table Artefacts. Idempotent : si elle existe deja on continue.
        r = await c.get(f"{base}/api/docs/{doc_id}/tables", headers=phdr)
        r.raise_for_status()
        existantes = [t.get("id") for t in (r.json() or {}).get("tables", [])]
        table_ref, vues_auto = None, []
        if "Artefacts" not in existantes:
            # AddTable plutot que POST /tables : la creation d'une table engendre
            # TOUJOURS une page (celle qu'on voyait en gris a cote de "🟢Coder"), et
            # seul retValues nous en donne l'id de facon deterministe — on la
            # supprime en fin de provisioning.
            cols = [dict({"id": col["id"]}, **(col.get("fields") or {}))
                    for col in ARTEFACTS_TABLE_DEF["tables"][0]["columns"]]
            rc = await c.post(f"{base}/api/docs/{doc_id}/apply", headers=phdr,
                              content=json.dumps([["AddTable", "Artefacts", cols]]))
            rc.raise_for_status()
            rv = ((rc.json() or {}).get("retValues") or [{}])[0] or {}
            table_ref = rv.get("id")
            vues_auto = [v.get("id") for v in (rv.get("views") or []) if v.get("id")]
        if not table_ref:
            rs = await c.post(f"{base}/api/docs/{doc_id}/sql", headers=phdr,
                              content=json.dumps({"sql": "SELECT id FROM _grist_Tables WHERE tableId = ? LIMIT 1",
                                                  "args": ["Artefacts"]}))
            rs.raise_for_status()
            recs = (rs.json() or {}).get("records", [])
            table_ref = (recs[0].get("fields", {}) or {}).get("id") if recs else None
        if not table_ref:
            return False
        # 2. Page + section custom sur Artefacts.
        ra = await c.post(f"{base}/api/docs/{doc_id}/apply", headers=phdr,
                          content=json.dumps([["CreateViewSection", table_ref, 0, "custom", None, None]]))
        ra.raise_for_status()
        ret = ra.json()
        # retValues[0] = {viewRef, sectionRef} selon la version de Grist.
        rv = ret.get("retValues", [ret]) if isinstance(ret, dict) else [ret]
        info = rv[0] if rv and isinstance(rv[0], dict) else {}
        section_ref = info.get("sectionRef") or info.get("sectionId")
        view_ref = info.get("viewRef") or info.get("viewId")
        if not section_ref:
            return False
        # 3. Bascule la section en custom widget + URL (options JSON string).
        # customView doit etre une CHAINE JSON dans options, pas un objet : le frontend
        # Grist fait JSON.parse dessus. Un objet y arrive serialise en "[object Object]"
        # -> « Erreur lors de l'acces au document » et le doc devient inouvrable.
        # Meme forme que artefact_publish (voir options["customView"] = custom_view).
        # "mode" est obligatoire, son absence donne « Cannot read properties of
        # undefined (reading 'mode') ».
        custom_view = json.dumps({
            "mode": "url", "url": widget_url, "access": "full",
            "renderAfterReady": True, "pluginId": "", "sectionId": "",
            "widgetId": "", "widgetDef": None, "widgetOptions": None,
            "columnsMapping": None,
        })
        opts = json.dumps({"customView": custom_view})
        actions = [["UpdateRecord", "_grist_Views_section", section_ref,
                    {"parentKey": "custom", "options": opts}]]
        # 4. Nomme la page hote "🟢Coder" si on a le viewRef.
        if view_ref:
            actions.append(["UpdateRecord", "_grist_Views", view_ref, {"name": "\U0001F7E2Coder"}])
        ru = await c.post(f"{base}/api/docs/{doc_id}/apply", headers=phdr,
                          content=json.dumps(actions))
        ru.raise_for_status()
        # 5. Menage. Best effort — un echec ici ne compromet pas un document deja
        #    utilisable. On ne laisse que la page "🟢Coder" :
        #      - la page grise creee avec la table Artefacts (RemoveView, verifie)
        #      - la Table1 du document vierge, et sa page avec elle
        menage = [["RemoveView", v] for v in vues_auto if v != view_ref]
        if "Table1" in existantes:
            menage.append(["RemoveTable", "Table1"])
        for action in menage:
            try:
                await c.post(f"{base}/api/docs/{doc_id}/apply", headers=phdr,
                             content=json.dumps([action]))
            except Exception:
                pass
        return True

# ── PROJECT META PERSISTENCE ──────────────────────────────────────────────────
# The "need" (user's project intention) is the only piece of plan state that
# can't be inferred from the live Grist document. We persist it as a single
# special record in the existing Artefacts table — no parallel tables, no
# schema migration, naturally hidden by the IsDoc=True convention.

PROJECT_META_NAME = "_project_meta"

async def _read_project_meta(ctx):
    """Read the persisted project meta (need) from Artefacts table.
       Returns dict {need, created_at, updated_at} or {} if not found."""
    if not ctx.doc_id or not ctx.site_url:
        return {}
    try:
        resp = await grist_post(ctx, "sql",
            {"sql": "SELECT Code, Description, UpdatedAt FROM Artefacts WHERE Nom = ? LIMIT 1",
             "args": [PROJECT_META_NAME]})
        recs = resp.get("records", [])
        if not recs:
            return {}
        f = recs[0]["fields"]
        return {
            "need": f.get("Code", "") or "",
            "description": f.get("Description", "") or "",
            "updated_at": f.get("UpdatedAt", 0),
        }
    except Exception:
        return {}

async def _write_project_meta(ctx, need: str):
    """Persist the project need into the Artefacts table as a special record.
       Idempotent upsert. Fire-and-forget — never raises."""
    if not ctx.doc_id or not ctx.site_url or not need:
        return
    try:
        # Do NOT auto-create the Artefacts table here — if it doesn't exist,
        # the upsert below will fail silently (fire-and-forget). Only the
        # explicit artefact_init() tool should create the table, to avoid
        # creating duplicates (Artefacts2) when the SQL probe fails due to
        # WAF/network errors rather than a genuinely missing table.
        # Upsert by Nom
        await grist_put(ctx, "tables/Artefacts/records",
            {"records": [{
                "require": {"Nom": PROJECT_META_NAME},
                "fields": {
                    "Nom":         PROJECT_META_NAME,
                    "Type":        "markdown",
                    "Code":        need,
                    "Description": "Project need (auto-managed by GristCoderMCP — do not edit manually)",
                    "IsDoc":       True,
                    "UpdatedAt":   int(time.time()),
                }
            }]})
    except Exception as e:
        print(f"[project_meta] write failed for {ctx.doc_id}: {e}", file=sys.stderr)

def _infer_status_from_snapshot(snapshot: dict) -> tuple[str, float, list[str]]:
    """Infer project status, completeness ratio, and next actions from a context snapshot.
       Pure function — no I/O. Used to enrich plan/{token} and context/{token}.
       Returns: (status, completeness_0_to_1, next_actions_list)"""
    tables = [t for t in snapshot.get("tables", []) if t.lower() != "artefacts"]
    artefacts = [a for a in snapshot.get("artefacts", []) if a.get("nom","").lower() != PROJECT_META_NAME]
    pages = snapshot.get("pages", [])
    quality = snapshot.get("_quality", [])
    has_need = bool(snapshot.get("_meta", {}).get("need"))

    next_actions = []
    score = 0.0

    # No tables yet → still qualifying or assessing
    if not tables:
        if has_need:
            next_actions.append("Lire docs/qualification puis canvas_wizard(type='choice', id='confirm-category')")
            return ("qualifying", 0.10, next_actions)
        next_actions.append("canvas_wizard(type='input', id='collect-need') pour collecter le besoin")
        return ("qualifying", 0.0, next_actions)

    score += 0.25  # has tables

    # Has tables but no artefacts → designing
    if not artefacts:
        next_actions.append("artefact_init() puis canvas_write() pour creer les premiers artefacts")
        return ("designing", score, next_actions)

    score += 0.20  # has artefacts

    # Has artefacts but no pages → still building
    if not pages:
        next_actions.append("grist_view_create() pour creer les pages Grist liees aux tables")
        return ("building", score + 0.10, next_actions)

    score += 0.20  # has pages

    # Compute coverage: tables with at least one page section
    tables_with_page = set()
    for p in pages:
        for s in p.get("sections", []):
            if s.get("table"):
                tables_with_page.add(s["table"])
    coverage = len(tables_with_page) / max(len(tables), 1)
    score += 0.20 * coverage

    missing_pages = [t for t in tables if t not in tables_with_page]
    if missing_pages:
        next_actions.append(f"Tables sans page : {missing_pages[:3]} — grist_view_create() pour chacune")

    # Check for quality issues
    if quality:
        custom_no_art = sum(1 for q in quality if q.get("issue") == "custom_section_no_artefact")
        ref_no_visible = sum(1 for q in quality if q.get("issue") == "ref_no_visiblecol")
        if custom_no_art:
            next_actions.append(f"{custom_no_art} section(s) custom sans artefact configure — grist_section_configure()")
        if ref_no_visible:
            next_actions.append(f"{ref_no_visible} colonne(s) Ref: sans visibleCol — grist_apply([UpdateRecord, _grist_Tables_column,...])")

    # Has dashboard?
    art_names_lc = {a.get("nom","").lower() for a in artefacts}
    page_names_lc = {p.get("name","").lower() for p in pages if p.get("name")}
    has_dashboard = any("dashboard" in n for n in art_names_lc | page_names_lc)
    if has_dashboard:
        score += 0.10

    # Verifying state: things look complete
    if score >= 0.85 and not next_actions:
        next_actions.append("plan_update(status='done') pour livrer l app")
        return ("verifying", min(score, 0.99), next_actions)
    if score >= 0.75:
        return ("verifying", score, next_actions)

    return ("building", score, next_actions)

async def fetch_grist_user_profile(site_url, bearer_token):
    """Resout le profil Grist d'une cle Bearer (mapping cle -> uid:{grist_user_id}).

    IMPORTANT : utilise _grist_client() (follow_redirects + cookie jar Incapsula
    partages). Un client nu echoue par intermittence sur le challenge 302 du WAF
    -> profil None -> le client MCP retombe sur un uid key:{sha1} SANS sessions
    (compte parallele vide) alors que les sessions widget vivent sous uid:{id}."""
    try:
        async with _grist_client() as c:
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
                                                  "html","react","grist","markdown","mermaid",
                                                  "python","sql","svg","app"]})}},
            {"id": "Code",         "fields": {"type": "Text",     "label": "Code"}},
            {"id": "Description",  "fields": {"type": "Text",     "label": "Description"}},
            {"id": "Dependencies", "fields": {"type": "Text",     "label": "Dependencies"}},
            {"id": "IsDoc",        "fields": {"type": "Bool",     "label": "IsDoc"}},
            {"id": "Output",       "fields": {"type": "Text",     "label": "Output"}},
            {"id": "UpdatedAt",    "fields": {"type": "DateTime", "label": "Mis a jour"}},
        ]
    }]
}

# ── APP SHELL — modele « magasin d'app » ──────────────────────────────────────
# Un seul widget publie porte ce shell ; les ecrans de l'application sont des
# lignes de la table Artefacts, prefixees APP_MODULE_PREFIXE. Le shell les monte
# a la demande dans des iframes, leur rend l'API grist par postMessage, et leur
# fournit routeur, bus d'evenements et etat partage.
#
# AUCUNE dependance au serveur MCP : le shell n'appelle que grist.docApi et l'API
# REST de Grist (via getAccessToken). Il survit a l'arret du pod.
#
# Le chargement est paresseux SUR LE TRANSFERT : l'index ne rapatrie que les noms
# (l'endpoint /sql projette les colonnes, ce que fetchTable ne sait pas faire), et
# le code d'un ecran n'est telecharge qu'a sa premiere visite. Mesure en conditions
# reelles sur 3 ecrans : index 237 o, un ecran non visite jamais telecharge.

APP_MODULE_PREFIXE = "app/"

_APP_MODULE_RUNTIME = (
    "(function(){var _i=0,_p={},_l={},_s={};"
    "window.addEventListener('message',function(e){var m=e.data;if(!m)return;"
    "if(m.__r){var r=_p[m.id];if(r){delete _p[m.id];m.err?r[1](new Error(m.err)):r[0](m.val);}return;}"
    "if(m.__ev&&_l[m.ev])_l[m.ev].forEach(function(c){try{c(m.data);}catch(x){}});"
    "if(m.__st){for(var k in m.state)_s[k]=m.state[k];"
    "if(_l['state'])_l['state'].forEach(function(c){try{c(_s);}catch(x){}});}});"
    "function q(k,a){return new Promise(function(res,rej){var id=++_i;_p[id]=[res,rej];"
    "parent.postMessage({__q:1,id:id,k:k,a:a},'*');});}"
    "window.grist={ready:function(){},docApi:{"
    "fetchTable:function(t){return q('fetchTable',[t]);},"
    "applyUserActions:function(a){return q('applyUserActions',[a]);},"
    "getAccessToken:function(o){return q('getAccessToken',[o||{}]);}}};"
    "window.app={navigate:function(n){parent.postMessage({__nav:1,nom:n},'*');},"
    "emit:function(ev,d){parent.postMessage({__emit:1,ev:ev,data:d},'*');},"
    "on:function(ev,cb){(_l[ev]=_l[ev]||[]).push(cb);},"
    "setState:function(o){parent.postMessage({__set:1,state:o},'*');},"
    "get state(){return _s;},"
    "notify:function(m,t){parent.postMessage({__notif:1,msg:m,t:t||'info'},'*');},"
    "modules:function(){return q('__modules',[]);}};})();"
)


def _app_shell_html(entree: str = "", prefixe: str = APP_MODULE_PREFIXE) -> str:
    """Artefact HTML complet portant le shell. Passe tel quel dans _split_html_js :
    le conteneur part dans _html, tout le reste dans _js."""
    cfg = json.dumps({"entree": entree, "prefixe": prefixe})
    rt = json.dumps(_APP_MODULE_RUNTIME)
    return (
        "<!DOCTYPE html>\n<html lang=\"fr\"><head><meta charset=\"utf-8\">\n"
        "<style>html,body{margin:0;height:100%;overflow:hidden}#app{height:100%}\n"
        ".msg{font:14px/1.6 system-ui;padding:24px;color:#3a3a3a}\n"
        ".msg b{display:block;margin-bottom:6px}.msg i{color:#777;font-style:normal;font-size:13px}\n"
        "</style></head><body>\n<div id=\"app\"></div>\n"
        "<" + "script>\n"
        "grist.ready({ requiredAccess: 'full' });\n"
        "var RUNTIME = " + rt + ";\n"
        "(function () {\n"
        "  var CFG = " + cfg + ";\n"
        "  var PREFIXE = CFG.prefixe, ENTREE = CFG.entree;\n"
        "  var hote = document.getElementById('app');\n"
        "  var B = null, TOK = null, exp = 0, index = [], cache = {}, vues = {}, etat = {}, courant = null;\n"
        "  function msg(t, d) { hote.innerHTML = '<div class=msg><b>' + t + '</b>'\n"
        "    + (d ? '<i>' + d + '</i>' : '') + '</div>'; }\n"
        "  async function jeton() {\n"
        "    if (TOK && Date.now() < exp) return;\n"
        "    var tk = await grist.docApi.getAccessToken({ readOnly: false });\n"
        "    B = tk.baseUrl; TOK = tk.token;\n"
        "    exp = Date.now() + Math.max(30000, (tk.ttlMsecs || 900000) * 0.8);\n"
        "  }\n"
        "  async function sql(q, args) {\n"
        "    await jeton();\n"
        "    var r = await fetch(B + '/sql?auth=' + encodeURIComponent(TOK), { method: 'POST',\n"
        "      headers: { 'Content-Type': 'application/json' },\n"
        "      body: JSON.stringify({ sql: q, args: args || [] }) });\n"
        "    if (!r.ok) throw new Error('SQL HTTP ' + r.status);\n"
        "    return (await r.json()).records.map(function (x) { return x.fields; });\n"
        "  }\n"
        "  function injecter(src) {\n"
        "    var tag = '<' + 'script>' + RUNTIME + '<' + '/script>';\n"
        "    var fin = '<' + '/head>';\n"
        "    return src.indexOf(fin) >= 0 ? src.replace(fin, tag + fin) : tag + src;\n"
        "  }\n"
        "  async function monter(nom) {\n"
        "    if (vues[nom]) return vues[nom];\n"
        "    if (!cache[nom]) {\n"
        "      var r = await sql('select Code from Artefacts where Nom = ?', [nom]);\n"
        "      if (!r.length || !r[0].Code) return null;\n"
        "      cache[nom] = r[0].Code;\n"
        "    }\n"
        "    var f = document.createElement('iframe');\n"
        "    f.setAttribute('data-module', nom);\n"
        "    f.style.cssText = 'border:0;width:100%;height:100%;display:none';\n"
        "    f.addEventListener('load', function () { try {\n"
        "      f.contentWindow.postMessage({ __st: 1, state: etat }, '*');\n"
        "      f.contentWindow.postMessage({ __ev: 1, ev: 'monte', data: { nom: nom } }, '*');\n"
        "    } catch (e) {} });\n"
        "    f.srcdoc = injecter(cache[nom]);\n"
        "    hote.appendChild(f); vues[nom] = f; return f;\n"
        "  }\n"
        "  async function naviguer(nom) {\n"
        "    var f = await monter(nom);\n"
        "    if (!f) return msg('Ecran introuvable : ' + nom,\n"
        "      'Ajouter une ligne nommee ainsi dans la table Artefacts.');\n"
        "    for (var k in vues) vues[k].style.display = (k === nom ? 'block' : 'none');\n"
        "    courant = nom; diffuser('navigate', { nom: nom });\n"
        "  }\n"
        "  function chaque(fn) { for (var k in vues) { try { fn(vues[k].contentWindow); } catch (e) {} } }\n"
        "  function diffuser(ev, d) { chaque(function (w) { w.postMessage({ __ev: 1, ev: ev, data: d }, '*'); }); }\n"
        "  window.addEventListener('message', function (e) {\n"
        "    var m = e.data; if (!m) return;\n"
        "    if (m.__q) {\n"
        "      var rep = function (v, err) { e.source.postMessage({ __r: 1, id: m.id, val: v, err: err }, '*'); };\n"
        "      if (m.k === '__modules') return rep(index.slice());\n"
        "      var api = grist.docApi[m.k];\n"
        "      if (!api) return rep(null, 'methode indisponible : ' + m.k);\n"
        "      Promise.resolve(api.apply(grist.docApi, m.a || [])).then(function (v) { rep(v); })\n"
        "        .catch(function (err) { rep(null, String(err && err.message || err)); });\n"
        "      return;\n"
        "    }\n"
        "    if (m.__nav) naviguer(m.nom);\n"
        "    if (m.__emit) diffuser(m.ev, m.data);\n"
        "    if (m.__set) { for (var k in m.state) etat[k] = m.state[k];\n"
        "      chaque(function (w) { w.postMessage({ __st: 1, state: etat }, '*'); }); }\n"
        "  });\n"
        "  (async function () {\n"
        "    try {\n"
        "      var idx = await sql('select Nom from Artefacts where Nom like ? order by Nom', [PREFIXE + '%']);\n"
        "      index = idx.map(function (r) { return r.Nom; });\n"
        "      if (!index.length) return msg('Aucun ecran dans la table Artefacts.',\n"
        "        'Chaque ligne nommee ' + PREFIXE + '... est un ecran de l application.');\n"
        "      await naviguer(index.indexOf(ENTREE) >= 0 ? ENTREE : index[0]);\n"
        "    } catch (e) { msg('Chargement impossible.', String(e && e.message || e)); }\n"
        "  })();\n"
        "})();\n"
        "<" + "/script>\n</body></html>"
    )


# ── SAMPLING HELPERS ──────────────────────────────────────────────────────────

async def _do_sample(uid_key: str, messages: list, system_prompt: str = "",
                     max_tokens: int = 2048) -> str:
    """Envoie une sampling/createMessage au client MCP et attend la réponse."""
    smp_id = "smp-" + uuid.uuid4().hex[:8]
    loop   = asyncio.get_event_loop()
    fut    = loop.create_future()
    _sampling_waiters[smp_id] = fut
    sid = _mcp_client_sids.get(uid_key)
    if not sid or sid not in _queues:
        _sampling_waiters.pop(smp_id, None)
        raise ValueError("Pas de client MCP connecte (sampling indisponible)")
    req = {
        "jsonrpc": "2.0", "id": smp_id,
        "method": "sampling/createMessage",
        "params": {
            "messages":      messages,
            "systemPrompt":  system_prompt,
            "includeContext": "thisServer",
            "maxTokens":     max_tokens,
        }
    }
    try:
        _queues[sid].put_nowait({"_raw_rpc": req, "_user": uid_key})
    except asyncio.QueueFull:
        _sampling_waiters.pop(smp_id, None)
        raise ValueError("Queue MCP client saturee")
    try:
        result = await asyncio.wait_for(fut, timeout=120)
        content = result.get("content", {})
        if isinstance(content, dict):
            return content.get("text", "")
        if isinstance(content, list):
            return "".join(c.get("text", "") for c in content if c.get("type") == "text")
        return str(content)
    except asyncio.TimeoutError:
        raise ValueError("Sampling timeout (120s)")
    finally:
        _sampling_waiters.pop(smp_id, None)


async def _handle_chat_sample(uid_key: str, ctx, message: str):
    """Lance un sampling pour répondre automatiquement à un message chat."""
    system = (
        f'Tu es l\'assistant du document Grist "{ctx.doc_title}". '
        "Tu reponds en francais, de maniere concise et claire. "
        "Si la demande necessite des actions sur le document, decris brievement ce que tu vas faire."
    )
    try:
        text = await _do_sample(uid_key,
                                [{"role": "user", "content": {"type": "text", "text": message}}],
                                system, 1024)
    except Exception as e:
        text = f"Erreur: {e}"
    _push(uid_key, {"type": "chat_message", "token": ctx.token,
                    "role": "assistant", "content": text, "ts": time.time()})


# ── TOOLS ─────────────────────────────────────────────────────────────────────

TOOLS = [
    # Sessions
    {"name": "sessions_list",
     "description": (
         "PREMIERE ETAPE obligatoire — liste les widgets actifs et retourne token + doc_title. "
         "La reponse inclut _next : action recommandee immediate (plan existant -> plan/{token}, "
         "nouveau doc -> docs/qualification). Si plusieurs sessions : reutiliser le token de "
         "la session voulue en le passant a chaque outil (token=...) — routage par appel, "
         "compatible multi-agents. session_select n epingle qu un defaut de connexion."
     ),
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True}},

    {"name": "session_open",
     "description": (
         "Ouvre une session sur un document SANS avoir a l ouvrir dans un navigateur. "
         "Utilise la cle Grist de l appelant : le widget Coder n a pas besoin d etre charge. "
         "Retourne un token utilisable par tous les outils, artefact_publish compris "
         "(les ecritures meta passent par le serveur quand la session porte une cle API). "
         "Indispensable pour piloter un document depuis Claude Desktop sans passer par Grist. "
         "Seule limite : canvas_screenshot, qui demande un widget ouvert par nature."
     ),
     "inputSchema": {"type": "object",
                     "properties": {
                         "doc_id": {"type": "string", "description": "Id du document Grist (visible dans l URL)."},
                         "site_url": {"type": "string", "description": "Base du site (defaut : celui des sessions connues ou GRIST_SITE_URL)."}},
                     "required": ["doc_id"]}},

    {"name": "session_select",
     "description": "Selectionne une session parmi plusieurs. Inutile si sessions_list retourne une seule session.",
     "inputSchema": {"type": "object",
                     "properties": {"token": {"type": "string"}},
                     "required": ["token"]}},

    {"name": "grist_doc_create",
     "description": (
         "Cree un NOUVEAU document Grist vierge dans un workspace, le nomme, y ajoute "
         "(best-effort) une page avec le widget Coder, et retourne le lien cliquable. "
         "Utilise la cle Grist de l'appelant : aucune configuration serveur requise. "
         "Si workspace_id est absent et qu'il existe plusieurs workspaces, retourne la "
         "liste pour choisir. Utiliser pour demarrer un projet sur un document propre "
         "plutot que d'encombrer un document existant."
     ),
     "inputSchema": {"type": "object",
                     "properties": {
                         "name": {"type": "string", "description": "Nom du nouveau document."},
                         "workspace_id": {"type": "integer", "description": "Id numerique du workspace cible (sinon GRIST_PROVISION_WORKSPACE_ID)."},
                         "add_coder_widget": {"type": "boolean", "description": "Ajouter une page widget Coder (defaut true, best-effort)."}
                     },
                     "required": ["name"]}},

    {"name": "session_info",
     "description": (
         "Resume leger de la session courante (tables, artefacts IsDoc, canvas actif). "
         "Retourne wizard_responses si des cards async ont recu une reponse utilisateur — "
         "verifier apres avoir lance des canvas_wizard(async=true). "
         "Utiliser context/{token} pour le snapshot complet avant construction ou verification."
     ),
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True}},

    # Canvas
    {"name": "canvas_select",
     "description": (
         "Selectionne un artefact existant par nom — charge son code dans le canvas et switch le widget. "
         "LECTURE SEULE : n ecrit rien, ne cree rien. Retourne le code complet. "
         "Utiliser AVANT canvas_read/canvas_patch pour travailler sur un artefact specifique."
     ),
     "inputSchema": {"type": "object",
                     "properties": {
                         "art_nom": {"type": "string", "description": "Nom exact de l artefact a selectionner"}},
                     "required": ["art_nom"]},
     "annotations": {"readOnlyHint": True}},

    {"name": "canvas_read",
     "description": "Lit le code complet du canvas (artefact actuellement selectionne). Appeler AVANT tout canvas_patch.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True}},

    {"name": "canvas_write",
     "description": (
         "Reecrit integralement le canvas. "
         "Cree automatiquement l artefact dans Grist si art_nom est inconnu (lookup interne). "
         "Type auto-detecte depuis le contenu si art_type absent — pas besoin de canvas_type apres. "
         "Si art_nom absent : cree un artefact Draft_{sha6} par defaut. "
         "Apres : canvas_screenshot pour valider. "
         "WAF grist.numerique.gouv.fr : le Code est sauvegarde par le widget via browser (bypass WAF auto)."
     ),
     "inputSchema": {"type": "object",
                     "properties": {
                         "code":     {"type": "string"},
                         "art_nom":  {"type": "string",  "description": "Nom de l artefact cible (cree si inexistant)"},
                         "art_type": {"type": "string",  "description": "Type explicite (html|react|grist|markdown|mermaid|python|sql|svg|app). Auto-detecte si absent."},
                         "art_id":   {"type": "integer", "description": "Id Grist (optionnel, renseigne par widget)"}},
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

    {"name": "canvas_type",
     "description": "Change le type de l artefact courant (html|react|grist|markdown|mermaid|python|sql|svg|app). "
                    "Met a jour Grist + notifie le widget pour re-render et changer le mode editeur. "
                    "Appeler apres canvas_write quand le type change (ex: on commence en html puis on passe a react).",
     "inputSchema": {"type": "object",
                     "properties": {"type": {"type": "string",
                                             "enum": ["html","react","grist","markdown","mermaid","python","sql","svg","app"],
                                             "description": "Nouveau type de l artefact"}},
                     "required": ["type"]}},

    # Wizard
    {"name": "canvas_wizard",
     "description": (
         "Affiche une etape contextuelle dans le render pane. "
         "input : DEBUT DE SESSION — collecte le besoin brut (suggestions=chips rapides). "
         "choice : QUALIFICATION — choix de categorie d app ou d architecture type. "
         "form : QUALIFICATION — details techniques (nb users, donnees, integrations). "
         "confirm : VALIDATION — soumet le plan markdown a l utilisateur avant construction. "
         "progress : CONSTRUCTION — montre l avancement (tables->artefacts->pages). "
         "info : INFORMATION — message contextuel avec actions optionnelles. "
         "preview : VALIDATION COMPOSANT — iframe live du code + interaction contextuelle en dessous. "
         "  code (str), code_type (html|react|svg|mermaid, defaut html), bridge (bool, defaut true), height (px). "
         "  source='schema'|'doc-map'|'doc-overview' : auto-genere diagramme(s) depuis le schema/structure Grist (transparent). "
         "  Interaction : actions=[{id,label,style}] | choices=[...] | fields=[...] | input=placeholder. "
         "  Non-bloquant si pas d interaction ; bloquant sinon. "
         "Types bloquants (input|choice|form|confirm|preview+interaction) : attendent la reponse. "
         "Types non-bloquants (progress|info sans actions|preview sans interaction) : retournent immediatement. "
         "MODE ASYNC : step.async=true -> retourne immediatement {status:'async'}. "
         "  RESERVER a : travail LLM genuinement parallele pendant que l utilisateur remplit un long formulaire. "
         "  NE PAS utiliser pour collect->traiter->step suivant : preferer le mode BLOQUANT qui enchaîne "
         "  naturellement. Quand async : session_info() -> wizard_responses[card_id], puis canvas_wizard_close(card_id). "
         "TIMEOUT+DEFAULT : step.default={...} -> si timeout, retourne les valeurs par defaut et continue. "
         "Apres chaque etape interactive : plan_update() pour persister les decisions. "
         "Lire docs/wizard avant premiere utilisation."
     ),
     "inputSchema": {"type": "object",
                     "properties": {
                         "step": {
                             "type": "object",
                             "description": (
                                 "Definition de l etape. Champs communs : id (str, unique), "
                                 "type (choice|form|confirm|progress|info|input), title (str), subtitle (str, opt). "
                                 "choice: choices=[{id,icon?,label,desc?}], multi=false. "
                                 "form: fields=[{id,type,label,placeholder?,required?,hint?,options?}]. "
                                 "confirm: content (markdown), actions=[{id,label,style?}]. "
                                 "progress: steps=[{id,label,status (done|active|pending|error)}]. "
                                 "info: content (texte), actions=[{id,label,style?}] optionnel. "
                                 "input: textarea libre. placeholder?, submit_label?, context?, "
                                 "suggestions=[str]. Retourne {values:{text}}. "
                                 "data-import: fetch API externe + selection table + insert Grist DIRECT (sans LLM). "
                                 "Bloquant — retourne {imported:N,table:str} apres que l utilisateur a clique Importer. "
                                 "source={api:str, params:[{id,label,placeholder?}], static_params:{}}. "
                                 "columns=[{key,label}] — colonnes affichees dans la preview. "
                                 "target={table:str, mapping:{GristCol:'json.path[0]'}}. "
                                 "Mapping supporte chemins imbriques ex: 'centre.coordinates[1]'. "
                                 "timeout: int (defaut 300s, types interactifs seulement)."
                             ),
                             "required": ["id", "type", "title"]
                         }
                     },
                     "required": ["step"]}},

    {"name": "canvas_wizard_close",
     "description": (
         "Ferme un card specifique du panneau multi-card (par card_id) ou tous les cards (sans argument). "
         "Sans args : vide le panneau et revient au render pane. "
         "Avec card_id : retire uniquement ce card (les autres restent visibles). "
         "Exemple : fermer la card progress apres construction, garder le plan visible."
     ),
     "inputSchema": {"type": "object", "properties": {
         "card_id": {"type": "string", "description": "ID du card a fermer. Absent = fermer tous."}
     }}},

    # Plan meta-control
    {"name": "plan_update",
     "description": (
         "META-CONTROLE — persiste le plan projet et met a jour l overlay contexte. "
         "Appeler apres chaque decision structurante pour maintenir la source de verite. "
         "Transitions status : qualifying -> assessing (doc existant a explorer) -> designing -> building -> verifying -> done. "
         "Chaque transition push notifications/tools/list_changed (outils progressivement debloquees). "
         "Sans actions/input : non bloquant, met a jour le panneau et continue. "
         "Avec actions=[{label,value,style}] : bloquant — l utilisateur choisit (ex: Valider/Modifier). "
         "Avec input='placeholder' : bloquant — l utilisateur saisit une correction ou direction. "
         "Retourne {value} de l action choisie ou {value} du texte saisi."
     ),
     "inputSchema": {"type": "object", "properties": {
         "need":         {"type": "string",  "description": "Besoin utilisateur qualifie (resumé)"},
         "tables":       {"type": "array",   "description": "Tables prevues [{name, columns:[str]}]"},
         "artefacts":    {"type": "array",   "description": "Artefacts UI prevus [{name, type}]"},
         "pages":        {"type": "array",   "description": "Pages Grist prevues [{name, table}]"},
         "integrations": {"type": "array",   "description": "Webhooks/integrations prevus"},
         "decisions":    {"type": "array",   "description": "Decisions architecturales cles prises"},
         "notes":        {"type": "string",  "description": "Notes libres sur le projet"},
         "status":       {"type": "string",
                          "enum": ["qualifying","assessing","designing","building","verifying","done"],
                          "description": "Phase courante. assessing=exploration doc existant (debloque schema/canvas read). Chaque transition notifie tools/list_changed."},
         "actions":      {"type": "array",   "description": "Boutons [{label,value,style?}] — rend bloquant, retourne la valeur choisie"},
         "input":        {"type": "string",  "description": "Placeholder textarea — rend bloquant, retourne le texte saisi"},
         "timeout":      {"type": "number",  "description": "Timeout en secondes (defaut 300)"},
     }, "required": ["status"]}},

    # Context panel
    {"name": "canvas_context_update",
     "description": (
         "Affiche une card contextuelle libre dans l overlay wizard — complementaire a plan_update. "
         "card_id fourni -> card independante et nommee (peut coexister avec d autres cards). "
         "Absent -> met a jour la card ctx-plan via context_update (distinct de plan_update). "
         "Cas d usage : feedback d etape, resume subagent, etat processus externe, note temporaire. "
         "Sans actions ni input : non bloquant, reste visible. "
         "Avec actions=[{label,value,style?}] ou input='placeholder' : bloquant — attend reponse. "
         "Fermer avec canvas_wizard_close(card_id=...) quand l info n est plus pertinente. "
         "sections[].style : default|success|info|warn|code"
     ),
     "inputSchema": {"type": "object", "properties": {
         "card_id":  {"type": "string", "description": "ID unique de la card (ex: 'arch-analysis', 'build-feedback'). Absent = context_update sans card nommee."},
         "title":    {"type": "string", "description": "Titre de la card"},
         "sections": {"type": "array", "items": {"type": "object", "properties": {
             "label":   {"type": "string"},
             "content": {"type": "string"},
             "style":   {"type": "string", "enum": ["default","success","info","warn","code"]}
         }}, "description": "Sections de contenu."},
         "progress": {"type": "number", "description": "0-100, barre de progression optionnelle"},
         "actions":  {"type": "array",  "description": "Boutons [{label,value,style?}] — rend bloquant"},
         "input":    {"type": "string", "description": "Placeholder textarea — rend bloquant"},
         "timeout":  {"type": "number", "description": "Timeout secondes (defaut 300)"},
     }, "required": ["title"]}},

    # Chat
    {"name": "chat_reply",
     "description": (
         "Envoie un message dans le chat widget et optionnellement attend la reponse. "
         "wait=False (defaut) : envoie seulement, retourne {ok:True}. "
         "wait=True : envoie + bloque jusqu a la reponse user, retourne {text, ts}. "
         "Pattern standard : chat_reply(message, wait=True) remplace chat_reply + wait_for_chat. "
         "Inutile si le client supporte sampling (reponse automatique via _handle_chat_sample)."
     ),
     "inputSchema": {"type": "object",
                     "properties": {
                         "message": {"type": "string"},
                         "wait":    {"type": "boolean", "default": False,
                                     "description": "True = envoie + attend reponse user"},
                         "timeout": {"type": "number", "default": 600},
                     },
                     "required": ["message"]}},

    {"name": "wait_for_chat",
     "description": (
         "Bloque jusqu a ce que l utilisateur envoie un message dans le chat widget. "
         "Retourne {text, ts} quand l utilisateur envoie. "
         "Permet une boucle autonome : LLM agit -> wait_for_chat() -> user repond -> LLM continue. "
         "blocking=False : retourne immediatement avec le dernier message en historique (non-bloquant). "
         "timeout : secondes avant abandon (defaut 600)."
     ),
     "inputSchema": {"type": "object", "properties": {
         "blocking": {"type": "boolean", "default": True,
                      "description": "True = attend le prochain message ; False = retourne last_message immediatement"},
         "timeout":  {"type": "number", "default": 600, "description": "Timeout en secondes"},
     }}},

    # Subagent
    {"name": "subagent_call",
     "description": (
         "Delegue a un agent specialise. Si sampling disponible (Claude Desktop) : via sampling/createMessage. "
         "Sinon (Claude Code, etc.) : retourne fallback_mode=True avec system_prompt + task pour auto-execution. "
         "Dans les deux cas le LLM principal produit ou reçoit une reponse structuree du role specialise. "
         "data-architect : QUALIFICATION — analyse le besoin, propose tables + relations optimales. "
         "ui-designer    : CONCEPTION — genere le plan artefacts UI adapte a la categorie d app. "
         "page-architect : CONSTRUCTION — propose layout pages Grist optimal (grilles, widgets, liens, scenarios). "
         "data-analyst   : CONSTRUCTION — analyse donnees existantes, genere donnees exemple. "
         "integrator     : INTEGRATION — propose schema webhooks + services externes. "
         "ux-navigator   : APRES WIZARD — analyse la reponse user + etat doc, propose le step wizard suivant "
         "le plus pertinent + les ressources a lire. Injecte automatiquement l etat complet du doc (tables, artefacts, pages, wizard_responses). "
         "Retourne {next_step, resources_a_lire, reasoning}. "
         "assistant      : tout contexte — reponse libre ou explications a l utilisateur. "
         "APRES : toujours canvas_context_update(card_id='subagent-X') pour afficher la reponse a l utilisateur."
     ),
     "inputSchema": {"type": "object", "properties": {
         "role":       {"type": "string",
                        "enum": ["data-architect","ui-designer","page-architect","data-analyst","integrator","ux-navigator","assistant"],
                        "description": "Profil specialise de l agent"},
         "task":       {"type": "string", "description": "Instruction precise pour l agent"},
         "context":    {"type": "string", "description": "Contexte supplementaire (schema, code, etc.)"},
         "max_tokens": {"type": "integer", "default": 2048},
     }, "required": ["role", "task"]}},

    # Artefact
    {"name": "artefact_init",
     "description": (
         "Cree la table Artefacts (9 colonnes) si absente. Idempotent. "
         "Appeler en debut de phase CONSTRUCTION si grist_schema ne montre pas la table Artefacts. "
         "(Le widget l appelle automatiquement au chargement.) "
         "APRES : canvas_write(code, art_nom, art_type) pour creer le premier artefact."
     ),
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"idempotentHint": True}},

    {"name": "artefact_publish",
     "description": (
         "LIVRAISON — fige un artefact HTML en widget 100% autonome, stocke DANS le doc Grist "
         "(widget galerie 'Custom widget builder'). Apres publication : aucune dependance au "
         "serveur MCP au runtime — le widget survit a l arret du serveur et voyage avec le doc. "
         "Sans section_ref : cree une page dediee (table_id requis). "
         "Avec section_ref : reconfigure une section custom existante (id depuis grist_views_list). "
         "Les <script> inline sont extraits automatiquement vers le champ _js du builder "
         "(seul contexte ou l API grist est disponible). Types supportes : html, svg (react/app refuses). "
         "La source reste la table Artefacts : modifier puis republier pour mettre a jour. "
         "LIRE docs/publication avant premiere utilisation."
     ),
     "inputSchema": {"type": "object",
                     "properties": {
                         "mode":        {"type": "string", "enum": ["artefact", "app"],
                                         "description": ("artefact (defaut) = un artefact, un widget. "
                                                         "app = publie un SHELL qui monte a la demande les ecrans "
                                                         "nommes 'app/...' de la table Artefacts : une application "
                                                         "multi-ecrans dans un seul widget, chargement paresseux.")},
                         "entree":      {"type": "string",  "description": "mode app : ecran d ouverture (defaut : premier par ordre alphabetique)."},
                         "prefixe":     {"type": "string",  "description": "mode app : prefixe des lignes traitees comme ecrans (defaut 'app/')."},
                         "artefact":    {"type": "string",  "description": "Nom de l artefact a publier (table Artefacts). Inutile en mode app."},
                         "section_ref": {"type": "integer", "description": "Section custom existante a reconfigurer. Absent = nouvelle page."},
                         "table_id":    {"type": "string",  "description": "Table source de la nouvelle page (requis si pas de section_ref)"},
                         "page_name":   {"type": "string",  "description": "Nom de la nouvelle page (defaut : nom de l artefact)"}}}},

    # Grist lecture
    {"name": "grist_schema",
     "description": "Schema du document : tables et colonnes avec types, formules, refs. APRES : lire docs/artefacts avant canvas_write, ou examples/{domain} si domaine identifie.",
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
                         "sql":   {"type": "string", "description": "Requete SELECT SQLite"},
                         "args":  {"type": "array", "items": {}, "description": "Parametres positionnels pour les ?"}},
                     "required": ["sql"]},
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
     "description": (
         "Execute des User Actions Grist (AddTable, AddColumn, BulkAddOrReplaceRecord, UpdateRecord...). "
         "Creer tables et colonnes : AddTable + colonnes [{id, type, widgetOptions?}]. "
         "Donnees exemple : BulkAddOrReplaceRecord (min 3-5 lignes pour rendre l app vivante). "
         "OBLIGATOIRE pour toutes les tables meta _grist_Views* : UpdateRecord via grist_apply. "
         "JAMAIS REST PATCH sur _grist_Views/_grist_Views_section -> crash frontend [object Object]."
     ),
     "inputSchema": {"type": "object",
                     "properties": {
                         "actions": {"type": "array",
                                     "description": "Liste d actions ex: [[\"AddTable\",\"MaTable\",[{\"id\":\"Nom\",\"type\":\"Text\"}]]]",
                                     "items": {}}},
                     "required": ["actions"]}},

    {"name": "grist_validate",
     "description": (
         "PRE-VOL — valide une liste de UserActions AVANT grist_apply, sans rien ecrire. "
         "Detecte les erreurs frequentes : colonne Ref: vers une table creee PLUS TARD dans le meme batch "
         "(cause classique de sandbox error a l insert), formats d action invalides. Rappelle les visibleCol "
         "a definir. Retourne {ok, errors, warnings}. A appeler avant tout grist_apply comportant plusieurs "
         "AddTable avec des colonnes Ref entre elles."
     ),
     "inputSchema": {"type": "object",
                     "properties": {
                         "actions": {"type": "array", "items": {},
                                     "description": "Les UserActions a valider (meme format que grist_apply)"}},
                     "required": ["actions"]},
     "annotations": {"readOnlyHint": True}},

    {"name": "grist_views_list",
     "description": "Liste les pages (vues) du document avec leurs sections.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True}},

    {"name": "grist_section_configure",
     "description": "Reconfigure un widget custom EXISTANT : change l artefact affiche ou le lien de section. INUTILE apres grist_view_create(artefact=...) — celui-ci configure deja tout. Cas d usage : changer l artefact sur une page deja creee, ou reconfigurer une section issue de grist_view_add_widget.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "section_ref":      {"type": "integer", "description": "ID de la section custom a configurer (depuis grist_views_list)"},
                         "artefact":         {"type": "string",  "description": "Nom de l artefact display mode (ex: 'FicheClient'). Laisse vide pour widget coding."},
                         "widget_url":       {"type": "string",  "description": "URL custom (defaut: HOST_URL/). Ignore si artefact est fourni."},
                         "link_section_ref": {"type": "integer", "description": "Section source pour lier les selections de ligne (linkSrcSectionRef). 0 = pas de lien."},
                         "link_target_col_ref": {"type": "integer", "description": "Optionnel : colonne Ref: de la table cible pour un master-detail PAR COLONNE (ex Clients->Commandes). id depuis _grist_Tables_column. 0 = lien par curseur/rowId."},
                         "link_src_col_ref": {"type": "integer", "description": "Optionnel : colonne source du lien (defaut 0 = rowId)."}},
                     "required": ["section_ref"]}},

    {"name": "grist_view_add_widget",
     "description": "Ajoute un widget custom a une page existante (qui n a qu une grille). Cree la section, la configure et met a jour le layout.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "view_ref":         {"type": "integer", "description": "ID de la vue (depuis grist_views_list)"},
                         "table_id":         {"type": "string",  "description": "Table source du widget (meme que la grille en general)"},
                         "artefact":         {"type": "string",  "description": "Nom de l artefact a afficher (mode display)"},
                         "grid_section_ref": {"type": "integer", "description": "ID de la section grille a lier (pour onRecord). 0 = pas de lien."},
                         "link_target_col_ref": {"type": "integer", "description": "Optionnel : colonne Ref: de la table cible pour un master-detail PAR COLONNE (Clients->Commandes). 0 = lien par curseur/rowId."},
                         "link_src_col_ref": {"type": "integer", "description": "Optionnel : colonne source du lien (defaut 0 = rowId)."}},
                     "required": ["view_ref", "table_id"]}},

    {"name": "grist_view_create",
     "description": (
         "CONSTRUCTION PHASE 3 — cree une page Grist complete (grille + widget custom lies). "
         "Avec artefact= : URL, display mode, liaison grille<->widget et layout sont configures automatiquement. "
         "NE PAS appeler grist_section_configure apres — tout est deja fait. "
         "Apres : grist_view_add_widget si besoin d un 2e widget sur la meme page."
     ),
     "inputSchema": {"type": "object",
                     "properties": {
                         "table_id":   {"type": "string", "description": "Table source (ex: 'Batiments')"},
                         "page_name":  {"type": "string", "description": "Nom de la page dans Grist"},
                         "widget_url": {"type": "string", "description": "URL du widget custom (defaut: HOST_URL/)"},
                         "artefact":   {"type": "string", "description": "Nom d un artefact a afficher automatiquement dans le widget (mode display). Ex: 'FicheClient'. Ajoute ?a=NomArtefact a l URL."},
                         "widget_only":{"type": "boolean", "description": "Si true : page = widget SEUL, sans la grille native Grist a cote. Recommande pour un dashboard/artefact autonome (ne pas melanger natif et non-natif). Defaut false (grille + widget lies)."}},
                     "required": ["table_id"]}},

    {"name": "grist_webhooks",
     "description": "CRUD webhooks du document Grist. list=GET toujours dispo. create/update/delete necessitent une cle API owner (pas accessToken widget seul). Champs create: tableId, eventTypes (['add','update']), url, name, memo.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "action":     {"type": "string",
                                        "enum": ["list", "create", "update", "delete"],
                                        "description": "list=GET /webhooks | create=POST | update=PATCH | delete=DELETE /webhooks/{id}"},
                         "webhook_id": {"type": "string",
                                        "description": "ID du webhook (requis pour update/delete)"},
                         "fields":     {"type": "object",
                                        "description": "Pour create: {tableId, eventTypes, url, name, memo}. Pour update: champs a modifier."}},
                     "required": ["action"]}},
]

# ── ROUTAGE DE SESSION PORTE PAR L'APPEL ──────────────────────────────────────
# Chaque outil agissant sur un document accepte un `token` optionnel. Raison : un
# client passerelle (connecteur claude.ai) ne renvoie pas toujours l'en-tete
# Mcp-Session-Id -> l'epinglage serveur pose par session_select est perdu d'un appel
# a l'autre, et deux onglets / deux agents partageant le meme compte ne peuvent de
# toute facon pas se partager un etat global. Avec ce parametre, chaque appel est
# autoportant et le travail en parallele redevient possible.
_NO_SESSION_TOOLS = {"sessions_list", "session_select", "session_open", "grist_doc_create"}
_SESSION_TOKEN_PROP = {
    "type": "string",
    "description": ("Token de la session ciblee (voir sessions_list). Optionnel si un seul "
                    "document est ouvert. A FOURNIR des que plusieurs le sont : le routage "
                    "est porte par l'appel, pas par un etat serveur partage."),
}
for _t in TOOLS:
    if _t["name"] in _NO_SESSION_TOOLS:
        continue
    _t.setdefault("inputSchema", {}).setdefault("properties", {}) \
      .setdefault("token", dict(_SESSION_TOKEN_PROP))

# ── CONTEXT-BASED TOOL FILTERING ──────────────────────────────────────────────
# Maps plan status → set of visible tool names (None = all tools visible)
_QUALIFYING_TOOLS = {
    "sessions_list", "session_select", "session_info",
    "canvas_wizard", "canvas_wizard_close", "canvas_context_update",
    "plan_update", "subagent_call", "artefact_init", "chat_reply", "wait_for_chat",
    "grist_schema", "grist_records", "grist_sql", "grist_validate",
}
_ASSESSING_TOOLS = _QUALIFYING_TOOLS | {
    "canvas_select", "canvas_read", "canvas_screenshot", "grist_views_list",
}
_DESIGNING_TOOLS = _ASSESSING_TOOLS | {
    "canvas_write", "canvas_patch", "canvas_type",
}
CONTEXT_TOOLS: dict[str, set | None] = {
    "qualifying":  _QUALIFYING_TOOLS,
    "assessing":   _ASSESSING_TOOLS,
    "designing":   _DESIGNING_TOOLS,
    "building":    None,   # all tools
    "verifying":   None,   # all tools
    "done":        _QUALIFYING_TOOLS | {"artefact_publish", "grist_views_list"},
}

def _tools_for_context(context: str) -> list:
    allowed = CONTEXT_TOOLS.get(context)
    if allowed is None:
        return TOOLS
    return [t for t in TOOLS if t["name"] in allowed]

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
    {"name": "evolve-app",
     "description": "Faire evoluer une app existante : audit, identification manques, enrichissement (nouvelles tables/artefacts/pages/integrations).",
     "arguments": [{"name": "direction", "description": "Direction de l evolution (ex: ajouter kanban, integrer geocodage, ajouter webhooks)", "required": False}]},
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
            "4. Creer les tables : grist_apply([['AddTable','Nom',[{id,type,widgetOptions?}]], ...])\n"
            "   Colonnes derivables -> isFormula:true. Colonnes Ref: -> type 'Ref:AutreTable'.\n"
            "5. Definir visibleCol sur chaque Ref: via grist_apply(['UpdateRecord','_grist_Tables_column',...])\n"
            "6. Donnees exemple : grist_apply([['BulkAddRecord','Nom',[...],{...}]]) — min 3-5 lignes\n"
            "7. Verifier context/{token} (_quality) : Ref avec visibleCol, chaque table -> une page"
        )}}]
    if name == "evolve-app":
        direction = args.get("direction", "")
        dir_str = f"Direction souhaitee : {direction}\n\n" if direction else ""
        return [{"role": "user", "content": {"type": "text", "text": (
            f"{dir_str}"
            "1. sessions_list() -> token\n"
            "2. plan_update(status='assessing') -> context assessing\n"
            "3. resources/read grist-coder://context/{token} -> etat reel (tables, artefacts, pages)\n"
            "4. resources/read grist-coder://code/{token} -> source des artefacts existants\n"
            "5. Audit : manques par couche (Donnees/UI/Logique/Integrations) ?\n"
            "6. canvas_context_update(card_id='audit') -> afficher le bilan\n"
            "7. canvas_wizard(type='choice', id='evol-direction') -> proposer 3-5 axes d evolution\n"
            "8. canvas_wizard(type='input', id='evol-commentaires', title='Commentaires & contraintes',\n"
            "   placeholder='Ex: ne pas toucher a la table Clients, privilege le mobile, budget reduit...',\n"
            "   context='Optionnel — ajoute des contraintes ou suggestions libres avant construction.',\n"
            "   submit_label='Continuer') -> collecter remarques libres (reponse peut etre vide)\n"
            "9. plan_update(status='designing') -> plan enrichissement valide (integrer les commentaires)\n"
            "10. Enrichir : nouvelles tables (grist_apply), artefacts (canvas_write+upsert), pages, webhooks\n"
            "11. plan_update(status='done') + canvas_wizard_close()"
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
DSFR N EST PAS auto-injecte : ajouter les 2 <link> ci-dessous si tu veux les classes fr-*.
Pas de Tailwind. NE PAS mettre DSFR dans un artefact a librairie graphique (voir plus bas).
<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8">
<link href="https://cdn.jsdelivr.net/npm/@gouvfr/dsfr@1.14/dist/dsfr.min.css" rel="stylesheet">
<link href="https://cdn.jsdelivr.net/npm/@gouvfr/dsfr@1.14/dist/utility/utility.min.css" rel="stylesheet">
<script src="https://docs.getgrist.com/grist-plugin-api.js"></script>
</head><body>
<div class="fr-container fr-py-4w" id="app"></div>
<script>
const app = window.app || {navigate:p=>showToast(p,'info'),emit:()=>{},on:()=>{},'setState':(k,v)=>{},state:{}};
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
  if(_state.loading){el.innerHTML='<div class="fr-py-4w fr-text--center" style="color:var(--text-mention-grey)">Chargement...</div>';return;}
  if(!_state.data.length){el.innerHTML='<div class="fr-py-4w fr-text--center" style="color:var(--text-mention-grey)">Aucune donnee</div>';return;}
  el.innerHTML = '<div class="fr-table"><table><thead><tr><th>Nom</th></tr></thead><tbody>' +
    _state.data.map(r=>`<tr style="cursor:pointer" onclick="selectRow(${r.id})"><td>${r.Nom||r.id}</td></tr>`).join('') +
    '</tbody></table></div>';
}
grist.ready({requiredAccess:'full'});
loadData();
</script></body></html>

---
TEMPLATE BASE - TYPE: html (UI independante, pas de donnees Grist)
DSFR N EST PAS auto-injecte : ajouter les 2 <link> ci-dessous. Pas de Tailwind.
<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8">
<link href="https://cdn.jsdelivr.net/npm/@gouvfr/dsfr@1.14/dist/dsfr.min.css" rel="stylesheet">
<link href="https://cdn.jsdelivr.net/npm/@gouvfr/dsfr@1.14/dist/utility/utility.min.css" rel="stylesheet">
</head><body>
<div class="fr-container fr-py-4w">...</div>
</body></html>

---
DSFR — COMPOSANTS DE REFERENCE (classes fr-*, une fois les 2 <link> ajoutes)
=====================================================================================
Le DSFR (Systeme de Design de l Etat) est le framework CSS officiel de l administration francaise.
Il n est PAS charge automatiquement : chaque artefact qui en a besoin declare les 2 <link>
jsdelivr montres dans les templates ci-dessus. NE PAS le charger dans un artefact utilisant
MapLibre, Leaflet, Chart.js ou D3 — le CSS global DSFR casse leurs rendus.

## LAYOUT
<div class="fr-container">                           <!-- max-width 78rem, centre -->
<div class="fr-grid-row fr-grid-row--gutters">        <!-- grille flexbox -->
  <div class="fr-col-12 fr-col-md-6 fr-col-lg-4">    <!-- 12 colonnes, responsive -->

## BOUTONS
<button class="fr-btn">Principal</button>
<button class="fr-btn fr-btn--secondary">Secondaire</button>
<button class="fr-btn fr-btn--tertiary">Tertiaire</button>
<button class="fr-btn" disabled>Desactive</button>
<ul class="fr-btns-group fr-btns-group--inline">      <!-- groupe horizontal -->
  <li><button class="fr-btn">Action 1</button></li>
  <li><button class="fr-btn fr-btn--secondary">Action 2</button></li>
</ul>

## CARTE
<div class="fr-card fr-card--shadow">
  <div class="fr-card__body">
    <div class="fr-card__content">
      <h3 class="fr-card__title">Titre</h3>
      <p class="fr-card__desc">Description</p>
      <div class="fr-card__start"><p class="fr-badge fr-badge--info">Statut</p></div>
    </div>
  </div>
</div>
<!-- Grille de cartes -->
<div class="fr-grid-row fr-grid-row--gutters">
  <div class="fr-col-12 fr-col-md-6 fr-col-lg-4"><div class="fr-card fr-card--shadow">...</div></div>
</div>

## TABLEAU
<div class="fr-table">
  <table>
    <caption>Titre du tableau</caption>
    <thead><tr><th>Col 1</th><th>Col 2</th><th>Col 3</th></tr></thead>
    <tbody>
      <tr><td>Valeur</td><td>Valeur</td><td>Valeur</td></tr>
    </tbody>
  </table>
</div>

## ALERTE
<div class="fr-alert fr-alert--info"><h3 class="fr-alert__title">Info</h3><p>Message</p></div>
<div class="fr-alert fr-alert--success"><h3 class="fr-alert__title">Succes</h3><p>Message</p></div>
<div class="fr-alert fr-alert--error"><h3 class="fr-alert__title">Erreur</h3><p>Message</p></div>
<div class="fr-alert fr-alert--warning"><h3 class="fr-alert__title">Attention</h3><p>Message</p></div>

## BADGE / TAG
<p class="fr-badge fr-badge--info">Information</p>
<p class="fr-badge fr-badge--success">Succes</p>
<p class="fr-badge fr-badge--error">Erreur</p>
<p class="fr-badge fr-badge--warning">Attention</p>
<p class="fr-badge fr-badge--new">Nouveau</p>
<ul class="fr-tags-group"><li><button class="fr-tag">Tag filtre</button></li></ul>

## FORMULAIRE
<div class="fr-input-group">
  <label class="fr-label" for="input-1">Label<span class="fr-hint-text">Aide</span></label>
  <input class="fr-input" type="text" id="input-1">
</div>
<div class="fr-select-group">
  <label class="fr-label" for="select-1">Label</label>
  <select class="fr-select" id="select-1"><option>Option 1</option></select>
</div>
<div class="fr-checkbox-group">
  <input type="checkbox" id="cb-1"><label class="fr-label" for="cb-1">Option</label>
</div>
<fieldset class="fr-fieldset"><legend class="fr-fieldset__legend">Choix</legend>
  <div class="fr-fieldset__element"><div class="fr-radio-group">
    <input type="radio" id="r-1" name="choix"><label class="fr-label" for="r-1">Option A</label>
  </div></div>
</fieldset>
<!-- Etats validation -->
<div class="fr-input-group fr-input-group--error">
  <label class="fr-label" for="err-1">Champ en erreur</label>
  <input class="fr-input fr-input--error" id="err-1"><p class="fr-error-text">Message erreur</p>
</div>
<div class="fr-input-group fr-input-group--valid">
  <label class="fr-label" for="ok-1">Champ valide</label>
  <input class="fr-input fr-input--valid" id="ok-1"><p class="fr-valid-text">Valide</p>
</div>

## MISE EN AVANT / CALLOUT
<div class="fr-callout">
  <h3 class="fr-callout__title">Titre</h3>
  <p class="fr-callout__text">Texte mis en avant</p>
  <button class="fr-btn">Action</button>
</div>

## TUILE
<div class="fr-tile fr-tile--horizontal">
  <div class="fr-tile__body"><div class="fr-tile__content">
    <h3 class="fr-tile__title"><a href="#">Titre tuile</a></h3>
    <p class="fr-tile__desc">Description</p>
  </div></div>
</div>

## TYPOGRAPHIE UTILITAIRE
fr-h1..fr-h6 fr-text--bold fr-text--lead fr-text--sm fr-text--center
fr-mb-2w fr-mt-4w fr-py-2w fr-px-3w  (espacements: 1w=0.25rem, 2w=0.5rem, 4w=1rem, 8w=2rem)
fr-background-alt--grey  fr-background-contrast--grey

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
// ATTENTION: selectedTable.fetch() N EXISTE PAS — utiliser a la place :
const d = await grist.fetchSelectedTable();  // lignes filtrees (pas d arg filtre)
// ou filtrer cote client apres safeLoad() / docApi.fetchTable()

## selectedTable.getTableId()
const tableName = await grist.selectedTable.getTableId(); // nom de la table liee au widget

## ACCESS TOKEN (appels REST API depuis le widget)
// CORRECT: grist.docApi.getAccessToken (pas grist.getAccessToken)
const ti = await grist.docApi.getAccessToken({readOnly: false});
// ti = { token: '...', baseUrl: 'https://grist.../api/docs/DOCID', ttlMsecs: 300000 }
// Pattern REST recommande : ?auth= en query param (pas Authorization header)
async function gristREST(endpoint, opts={}) {
  const ti = await grist.docApi.getAccessToken({readOnly: opts.method==='GET'});
  const url = new URL(ti.baseUrl + endpoint);
  url.searchParams.set('auth', ti.token);
  const r = await fetch(url, { method: opts.method||'GET',
    headers:{'Content-Type':'application/json'}, body: opts.body ? JSON.stringify(opts.body) : undefined });
  return r.json();
}
// ex: gristREST('/tables'), gristREST('/sql', {method:'POST', body:{sql:'SELECT ...'}})

## IDENTIFIER L UTILISATEUR COURANT
const principals = await grist.docApi.getAclPrincipals();
const me = principals.users.find(u => u.id === principals.currentUserId);
// me = { id: 37212, email: 'user@example.com', name: 'User Name' }

## ECOUTE EVENEMENTS
grist.onRecord(cb)          // record selectionne change (cb: function(record, mappings))
grist.onRecords(cb)         // liste de records change (cb: function(records, mappings))
grist.onNewRecord(cb)       // nouvelle ligne vide cree -> cb({}) avec id=0
grist.onOptions(cb)         // options du widget changent (widgetOptions JSON)

## FORMATS DE DONNEES — DIFFERENCES CRITIQUES
// onRecord : colonnes Reference = OBJETS EXPANDED {id, Nom, ...} (plugin API v2, grist.numerique.gouv.fr)
//            colonnes formule (isFormula:true) ABSENTES si la colonne est cachee dans la vue
// fetchTable : colonnes Reference = ID ENTIER (row ID brut) — fiable, toujours disponible
// REGLE : pour fiches detail avec formules ou references, TOUJOURS utiliser docApi.fetchTable
//         et ne PAS se fier au record de onRecord pour les colonnes formule

// Exemple acces Reference depuis fetchTable (id entier):
const d = await grist.docApi.fetchTable('Commandes');
const clientId = d.Client[i];  // entier, ex: 3
// Exemple acces Reference depuis onRecord (objet expanded):
// record.Client = {id: 3, Nom: 'FinPlus', CA: 780, ...}  <- NE PAS UTILISER POUR JOINTURE

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

// DateTime : secondes Unix (pas ms !)
Math.floor(Date.now() / 1000)          // JS -> Grist DateTime
new Date(timestamp * 1000)             // Grist DateTime -> JS Date
// Date : jours depuis epoch (1970-01-01 = 0)
Math.floor(Date.now() / 86400000)      // JS -> Grist Date
new Date(days * 86400 * 1000)          // Grist Date -> JS Date

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
// app.emit/on fonctionne en self-relay via le parent (app_runtime.html) :
//   emit → window.parent (app-event) → parent broadcast → même iframe (app-event-broadcast) → on callbacks
app.setState('currentClientId', 42);   // signature: (key, value) — PAS ({key:value})
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
WEBHOOKS GRIST

## PERMISSIONS
// list (GET)  : OK avec accessToken widget
// create/update/delete : necessite cle API owner — via MCP (grist_webhooks tool)
// Pas CORS : la 403 sur POST est une question de permission, pas de browser policy

## VIA MCP (recommande pour setup)
// grist_webhooks(action="list")
// grist_webhooks(action="create", fields={tableId:"Clients", eventTypes:["add","update"],
//   url:"https://HOST_URL/webhook-receive/DOC_ID", name:"MonWebhook"})
// grist_webhooks(action="delete", webhook_id="abc123")

## RECEIVER INTEGRE (production uniquement)
// URL : HOST_URL/webhook-receive/{docId}  (HOST_URL doit etre public, pas localhost)
// Grist POSTe le payload -> grist-coder fan-out SSE -> widget recoit l evenement
// Ecoute dans le widget:
grist.ready({requiredAccess:'full'});
window.addEventListener('message', e => {
  if (e.data?.type === 'webhook_event') {
    const rows = e.data.payload;  // [{id, Nom, ...}, ...]
    console.log('webhook recu:', rows);
  }
});

## PATTERNS D USAGE

# Pattern A — Integration externe (CRM, email, ERP) : cas principal
// Table Commandes change -> webhook -> POST /api/crm/sync ou /api/notify
// Setup via MCP: grist_webhooks(action="create", ...)
// Pas de code widget necessaire

# Pattern B — Async processing loop (prod, HOST_URL public)
// 1. Artefact ecrit record avec Statut="pending"
// 2. Webhook -> HOST_URL/webhook-receive/{docId} -> SSE au widget
// 3. Widget recoit evenement -> lance traitement ou affiche resultat
// 4. grist-coder ou service externe patch le record Statut="done"
// Cas: "Soumettre pour analyse IA" -> resultat apparait sans polling

# Pattern C — Inter-artefacts temps reel (pas de webhook necessite)
// Utiliser app.emit/on ou grist.setCursorPos/onRecord a la place
// Plus simple, fonctionne sur localhost, zero latence

## EN DEV LOCAL
// Webhook receiver inaccessible depuis Grist externe
// Alternatives: ngrok tunnel | polling setInterval | Pattern C natif Grist

---
PATTERNS WIDGET LIE (IsDoc=false, linkSrcSectionRef defini)
# Pattern 1: Fiche detail simple — PATTERN CANONIQUE
// __APP_STATE__.record = record injecte par le widget parent (id + colonnes de base)
// app.on('record', cb) = reactive sur changement de selection (IDE mode)
// IMPORTANT: colonnes formule et references completes via docApi.fetchTable (pas dans record)
async function render(rowId) {
  if (!rowId) { el.innerHTML = '<div class="fr-callout"><p class="fr-callout__text">Selectionnez un enregistrement</p></div>'; return; }
  const d = await grist.docApi.fetchTable('MaTable');  // toutes colonnes incl. formules
  const i = d.id.indexOf(rowId);
  const r = { id: rowId, Nom: d.Nom[i], CA: d.CA[i], NbCmd: d.NbCmd[i] };  // formules OK
  el.innerHTML = `<h2 class="fr-h4">${r.Nom}</h2><p class="fr-text--lead">CA: ${r.CA}</p>`;
}
grist.ready({requiredAccess:'read table'});
const _r0 = window.__APP_STATE__?.record || {};
if (_r0.id) render(_r0.id);              // rendu initial
app.on('record', r => { if (r?.id) render(r.id); });  // reactive (IDE + display mode)

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
     fields:  {Type: 'grist', Code: '<code>', Description: '...', IsDoc: false}
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
    Nom: "Dashboard", Type: "html", IsDoc: true,
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
    Nom: "FicheClient", Type: "html", IsDoc: false,
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

    "kanban": """PLAYBOOK : Kanban (colonnes Statut + drag-drop)
================================================
Quand : suivi de taches/tickets avec colonnes Todo / En cours / Fait
Rounds: 3

Round 1 — Schema:
  grist_apply([["AddTable","Taches",[
    {"id":"Titre","type":"Text"},
    {"id":"Statut","type":"Choice","widgetOptions":"{\"choices\":[\"Todo\",\"En cours\",\"Fait\"],\"choiceOptions\":{\"Todo\":{\"fillColor\":\"#e2e8f0\"},\"En cours\":{\"fillColor\":\"#fef3c7\"},\"Fait\":{\"fillColor\":\"#d1fae5\"}}}"},
    {"id":"Priorite","type":"Choice","widgetOptions":"{\"choices\":[\"Haute\",\"Normale\",\"Basse\"]}"},
    {"id":"Assigne","type":"Text"},
    {"id":"Description","type":"Text"},
    {"id":"DateLimite","type":"Date"}
  ]]])
  grist_apply([["BulkAddOrReplaceRecord","Taches",[null,null,null],[
    {"Titre":["Configurer le projet","Concevoir le schema","Tester l app"],
     "Statut":["Todo","En cours","Fait"],
     "Priorite":["Haute","Haute","Normale"]}
  ]]])

Round 2 — Artefact Kanban:
  canvas_write(code_kanban_html)  # voir pattern ci-dessous
  grist_upsert("Artefacts",[{require:{Nom:"Kanban"},fields:{Type:"html",IsDoc:true,Code:"<voir pattern>"}}])

Round 3:
  grist_view_create("Taches","Kanban",artefact="Kanban")

Pattern code artefact Kanban (IsDoc=true):
  const COLS = ["Todo","En cours","Fait"];
  const COLORS = {"Todo":"#e2e8f0","En cours":"#fef3c7","Fait":"#d1fae5"};
  async function load() {
    const d = await grist.docApi.fetchTable("Taches");
    const tasks = d.id.map((id,i)=>({id,Titre:d.Titre[i],Statut:d.Statut[i],Assigne:d.Assigne[i]}));
    document.getElementById("board").innerHTML = COLS.map(col=>
      '<div class="col" ondragover="ev.preventDefault()" ondrop="drop(event,\''+col+'\')">' +
      '<h3>'+col+'</h3>' +
      tasks.filter(t=>t.Statut===col).map(t=>
        '<div class="card" draggable="true" ondragstart="drag(event,'+t.id+')" data-id="'+t.id+'">'+t.Titre+'</div>'
      ).join('')+'</div>'
    ).join('');
  }
  async function drop(ev, newStatut) {
    const id = parseInt(ev.dataTransfer.getData("id"));
    await grist.docApi.applyUserActions([["UpdateRecord","Taches",id,{"Statut":newStatut}]]);
    load();
  }
  function drag(ev, id) { ev.dataTransfer.setData("id", id); }
  grist.ready(); load();
""",

    "calendar": """PLAYBOOK : Calendrier (vue mensuelle avec evenements)
=====================================================
Quand : planning, agenda, suivi de dates (RDV, livraisons, echeances)
Rounds: 3

Round 1 — Schema:
  grist_apply([["AddTable","Evenements",[
    {"id":"Titre","type":"Text"},
    {"id":"DateDebut","type":"DateTime","widgetOptions":"{\"timeFormat\":\"HH:mm\"}"},
    {"id":"DateFin","type":"DateTime","widgetOptions":"{\"timeFormat\":\"HH:mm\"}"},
    {"id":"Categorie","type":"Choice","widgetOptions":"{\"choices\":[\"RDV\",\"Livraison\",\"Interne\",\"Urgence\"]}"},
    {"id":"Description","type":"Text"},
    {"id":"Lieu","type":"Text"}
  ]]])

Round 2 — Artefact Calendrier (IsDoc=true):
  Utiliser une lib CDN legere : FullCalendar ou calendrier custom HTML/CSS
  Pattern minimal avec HTML natif (sans lib):
    - grille 7 colonnes (Lun-Dim)
    - fetchTable("Evenements") -> positionner les evenements par date
    - prev/next month nav
    - click event -> affiche details en sidebar

  Pattern avec FullCalendar (recommande):
    <link href='https://cdn.jsdelivr.net/npm/fullcalendar@6.1.11/index.global.min.css' rel='stylesheet'/>
    <script src='https://cdn.jsdelivr.net/npm/fullcalendar@6.1.11/index.global.min.js'></script>
    async function init() {
      const d = await grist.docApi.fetchTable("Evenements");
      const events = d.id.map((id,i)=>({
        id, title:d.Titre[i],
        start: new Date(d.DateDebut[i]*1000).toISOString(),
        end:   d.DateFin[i] ? new Date(d.DateFin[i]*1000).toISOString() : null,
        color: {RDV:"#3e5de7",Livraison:"#10b981",Urgence:"#ef4444"}[d.Categorie[i]]||"#94a3b8"
      }));
      const cal = new FullCalendar.Calendar(document.getElementById("cal"),{
        initialView:"dayGridMonth", locale:"fr",
        events: events,
        eventClick: info => showDetail(info.event)
      });
      cal.render();
    }
    grist.ready(); init();

Round 3:
  grist_view_create("Evenements","Calendrier",artefact="Calendrier")
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

// Bus d evenements inter-artefacts (app_runtime.html)
// Flux : emit → window.parent (app-event) → parent broadcast → mainFrame (app-event-broadcast) → on callbacks
// Self-relay : OUI — l artefact emetteur recoit ses propres emissions (parent relay vers mainFrame)
app.emit('client-selected', { id: 42 });
app.on('client-selected', ({ id }) => loadDetail(id));

// Etat partage — signature setState(key, value) PAS setState({key:value})
app.setState('currentId', 42);         // correct
app.setState({ currentId: 42 });       // FAUX — cle sera "[object Object]"
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


DOCS_WIZARD = '''GRIST CODER - Wizard multi-card v5.6
=====================================
L overlay wizard est une SURFACE UNIFIEE de cards independantes superposees.
Chaque card a un id unique, une duree de vie propre, et peut coexister avec d autres.
Le LLM pousse des cards -> le widget les affiche -> l utilisateur repond -> le LLM continue.

SURFACES DISPONIBLES
--------------------
canvas_wizard(step)              -> card interactive ou informative (id requis dans step)
plan_update(...)                 -> card ctx-plan-progress (plan + phases + metadata, source unique)
canvas_context_update(card_id=X) -> card libre nommee (sections/progress, non-bloquante par defaut)
canvas_wizard_close(card_id=X)   -> ferme UNE card specifique (les autres restent)
canvas_wizard_close()            -> ferme TOUT l overlay (fin de session uniquement)

TYPES DE STEPS (canvas_wizard)
-------------------------------
id : REQUIS — identifiant unique de la card (ex: "collect-need", "build-progress", "validate-plan")

1. input — textarea libre (collecte besoin brut)
canvas_wizard({"id":"collect-need","type":"input","title":"Ton projet",
  "context":"Decris l app que tu veux construire.",
  "placeholder":"Ex: un CRM pour suivre mes clients et relances...",
  "submit_label":"Analyser",
  "suggestions":["CRM clients","Gestion de stock","Suivi de projet","Tableau de bord"]
})
Reponse: {"step_id":"collect-need","type":"input","values":{"text":"..."}}

2. choice — selection parmi des options (cartes cliquables)
canvas_wizard({"id":"confirm-category","type":"choice","title":"Type d app","subtitle":"Confirme la categorie",
  "choices":[
    {"id":"crm",    "icon":"👥","label":"CRM",    "desc":"Clients, contacts, contrats"},
    {"id":"stock",  "icon":"📦","label":"Stock",  "desc":"Inventaire et mouvements"},
    {"id":"projet", "icon":"📋","label":"Projet", "desc":"Tasks, milestones, equipe"},
    {"id":"custom", "icon":"⚙️","label":"Custom", "desc":"Architecture libre"}
  ]
})
# multi=false (defaut) : clic = reponse immediate
# multi=true : selection multiple + bouton Valider
Reponse: {"step_id":"confirm-category","type":"choice","values":{"selected":"crm"}}

3. form — formulaire structure
canvas_wizard({"id":"project-details","type":"form","title":"Details du projet",
  "fields":[
    {"id":"entities","type":"text",     "label":"Entites principales","placeholder":"Clients, Contrats","required":true},
    {"id":"volume",  "type":"number",   "label":"Volume estime (lignes)","placeholder":"500"},
    {"id":"import",  "type":"toggle",   "label":"Importer des donnees existantes"},
    {"id":"format",  "type":"select",   "label":"Format","options":["CSV","Excel","Manuel"]}
  ]
})
Types champs : text | textarea | number | select | toggle
Reponse: {"step_id":"project-details","type":"form","values":{...}}

4. confirm — soumettre une proposition
canvas_wizard({"id":"validate-plan","type":"confirm","title":"Architecture proposee",
  "content":"## Tables\\n- Clients\\n- Contrats (Ref:Clients)\\n\\n## Artefacts\\n- Dashboard (react)\\n- Fiche Client (html)",
  "actions":[
    {"id":"ok",     "label":"Valider ✓", "style":"primary"},
    {"id":"modify", "label":"Modifier",  "style":"secondary"}
  ]
})
Reponse: {"step_id":"validate-plan","type":"confirm","values":{"action":"ok"}}

5. progress — progression de build (non-bloquant, retourne immediatement)
canvas_wizard({"id":"build-progress","type":"progress","title":"Construction en cours",
  "steps":[
    {"id":"tables", "label":"Schema + donnees", "status":"done"},
    {"id":"arts",   "label":"Artefacts UI",     "status":"active"},
    {"id":"pages",  "label":"Pages Grist",      "status":"pending"}
  ]
})
# Mettre a jour : repousser meme id avec nouveaux statuts -> card mise a jour in-place
# Statuts : done | active (anime) | pending | error
# NE PAS fermer avec canvas_wizard_close() : laisser visible pendant toute la phase 3
# Fermer uniquement : canvas_wizard_close(card_id="build-progress") quand tout est done

6. info — message avec actions optionnelles
canvas_wizard({"id":"delivery","type":"info","title":"App prete",
  "sections":[
    {"label":"Tables","content":"Clients, Contrats, Interactions","style":"code"},
    {"label":"Artefacts","content":"Dashboard, Fiche Client","style":"code"}
  ],
  "actions":[{"id":"ok","label":"Parfait !","style":"primary"}]
})
# Sans actions : non-bloquant, dismissable avec x
# Avec actions : bloquant

7. data-import — fetch API externe + selection + insert Grist DIRECT (sans LLM dans la boucle)
Principe : le widget fetche l API, affiche un tableau selectionnable, et insere les lignes choisies
directement via grist.docApi.applyUserActions (BulkAddRecord). Le LLM recoit juste {imported:N}.
IDEAL pour : donnees geo (communes, departements), SIRENE/entreprises, DVF, tout dataset externe volumineux.

canvas_wizard({"id":"import-geo","type":"data-import","title":"Importer des communes",
  "source": {
    "api": "https://geo.api.gouv.fr/communes",
    "params": [{"id":"codeDepartement","label":"Département"},{"id":"nom","label":"Nom"}],
    "static_params": {"fields":"nom,code,codeDepartement,population,centre","limit":200}
  },
  "columns": [{"key":"nom","label":"Nom"},{"key":"code","label":"Code"},
              {"key":"codeDepartement","label":"Dép."},{"key":"population","label":"Population"}],
  "target": {
    "table": "Communes",
    "mapping": {"Nom":"nom","Code":"code","CodeDept":"codeDepartement",
                "Population":"population","Lat":"centre.coordinates[1]","Lon":"centre.coordinates[0]"}
  }
})
Reponse: {"step_id":"import-geo","type":"data-import","values":{"action":"import","imported":87,"table":"Communes"}}
Note: target.table doit exister (creer avec grist_apply avant si besoin). Mapping supporte chemins imbriques (a.b[0]).

7b. preview — interface live dans le canvas + interaction contextuelle
Principe : l iframe EST l interface complete. Le composant gere tout en interne.
L agent fournit le code HTML/React du composant -> il s execute dans l iframe -> l utilisateur
interagit directement -> wizard.submit(values) retourne les donnees structurees a l agent.

canvas_wizard({"id":"geo-picker","type":"preview","title":"Localisation",
  "subtitle":"Recherche et confirme l adresse",
  "code": "...",   # HTML/React/SVG avec logique interne complete
  "code_type": "html",   # html | react | svg (defaut: html)
  "bridge": true,        # injecter bridge Grist (acces tables/records, defaut: true)
  "height": 320          # hauteur iframe px (defaut: 260)
})
# Optionnel : interaction statique en dessous (complement simple)
# "actions": [...] | "fields": [...] | "choices": [...] | "input": "placeholder"

API window.wizard (disponible dans le code de l iframe) :
  wizard.submit(values)           -> retourne values a l agent (ferme la card)
  wizard.fill({field_id: value})  -> pre-remplit les champs form en dessous
  wizard.notify(msg, level)       -> toast dans le widget (info|success|warn|error)
  wizard.setLoading(bool)         -> desactive/active bouton submit
  wizard.cardId                   -> id de la card courante

Reponse (via wizard.submit ou form): {"step_id":"geo-picker","type":"preview","values":{...}}

EXEMPLES D USAGE PREVIEW
--------------------------

A. GEOCODAGE ADRESSE (API BAN data.gouv.fr)
canvas_wizard({"id":"geocode","type":"preview","title":"Localisation du site","height":340,"code":"""
<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>body{margin:0;padding:16px;font-family:Inter,sans-serif;background:#f8fafc}
input{width:100%;padding:10px;border:1px solid #e2e8f0;border-radius:8px;font-size:.9rem;outline:none}
.res{margin-top:10px;display:flex;flex-direction:column;gap:6px}
.item{padding:10px 12px;background:#fff;border:1px solid #e2e8f0;border-radius:6px;cursor:pointer;font-size:.82rem}
.item:hover{border-color:#3e5de7;background:#eef2ff}</style></head><body>
<input id="q" placeholder="Tapez une adresse..." autocomplete="off">
<div id="res"></div>
<script>
var res = document.getElementById('res');
document.getElementById('q').addEventListener('input', function(){ search(this.value); });
res.addEventListener('click', function(e) {
  var el = e.target.closest('.item'); if (!el) return;
  var f = JSON.parse(decodeURIComponent(el.dataset.f));
  wizard.submit({label: f.properties.label, lat: f.geometry.coordinates[1],
                 lon: f.geometry.coordinates[0], code_postal: f.properties.postcode,
                 ville: f.properties.city});
});
async function search(v) {
  if (v.length < 3) { res.innerHTML = ''; return; }
  const d = await (await fetch('https://api-adresse.data.gouv.fr/search/?q='+encodeURIComponent(v)+'&limit=5')).json();
  res.innerHTML = d.features.map(f =>
    '<div class="item" data-f="'+encodeURIComponent(JSON.stringify(f))+'">'+f.properties.label+'</div>'
  ).join('');
}
</script></body></html>
"""})
# Agent recoit : {"label":"12 rue de la Paix, 75002 Paris","lat":48.869,"lon":2.330,...}
# Agent ecrit dans Grist : grist_records_patch(table_id="Sites", records=[{id:X, fields:{Adresse, Lat, Lon}}])

B. CARTE AVEC FILTRES (Leaflet + OSM)
canvas_wizard({"id":"map-filter","type":"preview","title":"Zone d intervention","height":380,"code":"""
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9/dist/leaflet.js"><\/script>
<div id="map" style="height:300px;border-radius:8px"></div>
<div style="padding:10px;display:flex;gap:8px">
  <button onclick="confirmZone()" style="flex:1;padding:9px;background:#3e5de7;color:#fff;border:none;border-radius:6px;cursor:pointer">Confirmer cette zone</button>
</div>
<script>
var map = L.map('map').setView([46.6, 2.3], 6);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png').addTo(map);
var marker = null;
map.on('click', function(e) {
  if (marker) map.removeLayer(marker);
  marker = L.marker(e.latlng).addTo(map);
  wizard.notify('Position selectionnee', 'info');
});
function confirmZone() {
  if (!marker) { wizard.notify('Selectionne un point sur la carte', 'warn'); return; }
  wizard.submit({lat: marker.getLatLng().lat, lon: marker.getLatLng().lng,
                 zoom: map.getZoom(), bbox: map.getBounds().toBBoxString()});
}
</script>
"""})

C. COMPOSANT AVEC DONNEES GRIST (bridge=true)
canvas_wizard({"id":"client-selector","type":"preview","title":"Selectionner un client","bridge":true,"height":300,"code":"""
<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>body{margin:0;padding:16px;font-family:Inter,sans-serif;background:#f8fafc}
.item{padding:10px;background:#fff;border:1px solid #e2e8f0;border-radius:6px;cursor:pointer;margin-bottom:6px}
.item:hover{border-color:#3e5de7}</style></head><body>
<div id="list">Chargement...</div>
<script>
grist.ready();
grist.docApi.fetchTable('Clients').then(function(data) {
  var html = data.Nom.map(function(n, i) {
    return '<div class="item" onclick=\'wizard.submit({id:'+data.id[i]+', nom:'+JSON.stringify(n)+'})\'>'+n+'</div>';
  }).join('');
  document.getElementById('list').innerHTML = html || 'Aucun client';
});
</script></body></html>
"""})
# Charge les vrais clients Grist, l utilisateur clique -> wizard.submit({id, nom})

PATTERNS MULTI-CARD RECOMMANDES
--------------------------------

A. BUILD (phase 3) — 3 cards simultanees
   plan_update(status="building")               -> ctx-plan-progress visible en permanence
   canvas_wizard(id="build-progress", progress) -> progression active
   canvas_context_update(card_id="arch-note")   -> note architecturale dismissable
   -> Pendant la construction : update "build-progress" a chaque etape
   -> En fin : canvas_wizard_close(card_id="build-progress") puis plan_update(status="verifying")

B. QUALIFICATION (phase 1)
   canvas_wizard(id="collect-need", input)     -> bloquant, collecte le besoin
   subagent_call(role="data-architect")         -> analyse
   canvas_context_update(card_id="arch-result") -> affiche le JSON structure
   canvas_wizard(id="confirm-category", choice) -> bloquant, confirme categorie
   plan_update(need=..., status="designing")    -> ctx-plan-progress mis a jour

C. ENRICHISSEMENT DONNEES (preview + Grist)
   canvas_wizard(id="geo-picker", preview, bridge=true)  -> composant geocodage
   # Agent recoit {label, lat, lon} -> grist_records_patch -> donnees enrichies

D. LIVRAISON (phase 4)
   canvas_wizard(id="delivery", confirm)  -> resume + validation user
   plan_update(status="done")             -> ctx-plan-progress -> "App livree 100%"
   canvas_wizard_close()                  -> ferme tout (SEULEMENT en fin de session)

REGLES
------
- id est REQUIS pour toute card canvas_wizard
- Ne jamais canvas_wizard_close() sans card_id sauf en fin de session complete
- Mettre a jour une card progress en repoussant le meme id (pas de close/reopen)
- canvas_context_update(card_id=X) pour les infos non-interactives (subagent, notes)
- plan_update() toujours non-bloquant sauf si actions/input explicitement utiles
- preview sans interaction statique = iframe pleine hauteur, wizard.submit() obligatoire
- preview avec bridge=true = acces complet aux tables Grist depuis le composant
'''.strip()

DOCS_SERVICES_GEO = """GRIST CODER - Services Géographiques (APIs publiques France)
=============================================================
Toutes ces APIs sont gratuites, sans auth, utilisables en fetch() depuis un artefact HTML.

## 1. BAN — Base Adresse Nationale (géocodage)
URL: https://api-adresse.data.gouv.fr/search/?q={adresse}&limit=5

Pattern artefact (champ adresse + liste résultats):
  async function searchAddress(q) {
    const r = await fetch('https://api-adresse.data.gouv.fr/search/?q='+encodeURIComponent(q)+'&limit=5');
    const d = await r.json();
    return d.features; // [{type,geometry:{coordinates:[lon,lat]},properties:{label,postcode,city,score}}]
  }
  // Résultat: f.properties.label, f.properties.postcode, f.properties.city
  // Coords: f.geometry.coordinates[0]=lon, [1]=lat

Reverse (coords -> adresse):
  fetch('https://api-adresse.data.gouv.fr/reverse/?lon=2.347&lat=48.859')

Batch CSV (POST, max 50 lignes):
  POST https://api-adresse.data.gouv.fr/search/csv/
  body: FormData avec fichier CSV (colonnes: adresse, code_postal, ville)

Stocker dans Grist (pattern wizard→artefact):
  wizard.submit({label, lat, lon, code_postal, ville})
  -> LLM reçoit le résultat -> grist_records_patch(table, [{id, fields:{Lat:lat, Lon:lon, Adresse:label}}])

## 2. OSM Nominatim (géocodage mondial)
URL: https://nominatim.openstreetmap.org/search?q={query}&format=json&limit=5
Header requis: User-Agent: MonApp/1.0
  fetch(url, {headers:{"User-Agent":"GristCoder/1.0"}})
Résultat: [{display_name, lat, lon, type, importance}]
Limite: 1 requête/s, pas de bulk.

## 3. IGN Géoportail (fonds de carte, isochrones)
Fonds de carte Leaflet:
  L.tileLayer('https://wxs.ign.fr/essentiels/geoportail/wmts?...',{...})
API isochrones: https://data.geopf.fr/navigation/isochrone
  ?resource=bdtopo-pgr&profile=pedestrian&costType=time&costValue=15&point=lon,lat

## 4. Affichage carte Leaflet (CDN, no auth)
  <link rel='stylesheet' href='https://unpkg.com/leaflet@1.9/dist/leaflet.css'/>
  <script src='https://unpkg.com/leaflet@1.9/dist/leaflet.js'></script>
  const map = L.map('map').setView([48.85, 2.35], 12);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png').addTo(map);
  L.marker([lat, lon]).addTo(map).bindPopup(label);

## Pattern Kanban géo (artefact IsDoc avec carte + liste):
  grist.ready();
  const d = await grist.docApi.fetchTable("Clients");
  d.id.forEach((id,i) => {
    if (d.Lat[i]) L.marker([d.Lat[i],d.Lon[i]]).addTo(map).bindPopup(d.Nom[i]);
  });
""".strip()

DOCS_SERVICES_DATA = """GRIST CODER - Services de Données Publiques (APIs France)
===========================================================
APIs publiques françaises gratuites, sans auth sauf mention contraire.

## 1. SIRENE — Recherche d'entreprises (INSEE)
API publique: https://recherche-entreprises.api.gouv.fr/search?q={query}&per_page=5
  const r = await fetch('https://recherche-entreprises.api.gouv.fr/search?q='+encodeURIComponent(q)+'&per_page=5');
  const d = await r.json();
  // d.results: [{siren, nom_complet, siege:{adresse_ligne_1, code_postal, libelle_commune}, activite_principale}]
  // Identifiants: siren (9 chiffres), siret (14 chiffres = siren + nic)

Fiche par SIREN:
  fetch('https://recherche-entreprises.api.gouv.fr/search?q='+siren+'&per_page=1')

## 2. data.gouv.fr — Catalogue open data
Recherche datasets:
  fetch('https://www.data.gouv.fr/api/1/datasets/?q={query}&page_size=5')
  // d.data: [{id, title, description, resources:[{url, format, title}]}]

Télécharger un dataset CSV (direct):
  const csv = await fetch(resource.url).then(r=>r.text());
  // Parser avec papaparse CDN ou split('\n').map(l=>l.split(','))

## 3. DVF — Demandes de Valeurs Foncières (prix immobilier)
API: https://apidf-preprod.cerema.fr/dvf_opendata/geomutations/?code_insee={code}&ordering=-date_mutation&page_size=10
  // {count, results:[{date_mutation, valeur_fonciere, code_postal, libelle_commune, surface_reelle_bati, nombre_pieces_principales, nature_mutation}]}

Ou par commune:
  fetch('https://apidf-preprod.cerema.fr/dvf_opendata/geomutations/?code_insee=75056&page_size=20')

## 4. API Geo — Communes, départements, régions
Communes par code postal:
  fetch('https://geo.api.gouv.fr/communes?codePostal=75001&fields=nom,code,centre')
  // [{nom, code, centre:{coordinates:[lon,lat]}}]

Communes dans un rayon (lat/lon/distance):
  fetch('https://geo.api.gouv.fr/communes?lat=48.85&lon=2.35&distance=10000&fields=nom,code')

Départements:
  fetch('https://geo.api.gouv.fr/departements')
  // [{code, nom}]

## 5. Pattern Wizard → Grist (import données publiques)
  // Dans un preview artefact:
  const d = await fetch('https://recherche-entreprises.api.gouv.fr/search?q='+query+'&per_page=10').then(r=>r.json());
  const items = d.results.map(e => encodeURIComponent(JSON.stringify(e)));
  // Afficher liste avec data-f, clic -> wizard.submit(JSON.parse(decodeURIComponent(el.dataset.f)))
  // LLM reçoit la sélection -> grist_records_add("Entreprises",[{fields:{Nom,SIREN,Adresse}}])

## 6. Prix énergie — API ENEDIS (open data)
  fetch('https://data.enedis.fr/api/explore/v2.1/catalog/datasets/bilan-electrique-by-town/records?where=code_commune_insee="{code}"&limit=5')
""".strip()

DOCS_QUALIFICATION = """PROTOCOLE DE QUALIFICATION DES BESOINS
========================================
Lire EN PHASE 1 avant canvas_wizard. Permet de deriver l architecture complete depuis le besoin brut.

ETAPE 1 — CATEGORISER
  Depuis la reponse canvas_wizard(type="input"), identifier la categorie principale :
  CRM         : client, prospect, affaire, opportunite, contact, pipeline, vente, devis
  Projets     : projet, tache, sprint, milestone, equipe, livrable, budget, planning
  Stock       : produit, inventaire, stock, categorie, fournisseur, commande, mouvement
  Analytics   : rapport, tableau de bord, KPI, metrique, analyse, statistiques
  RH/Planning : employe, conge, planning, poste, evaluation, contrat, temps
  Custom      : tout autre besoin -> questions qualifiantes complementaires

ARCHITECTURES TYPES

  CRM / Relation client
    Tables    : Clients(Nom,Secteur,CA_Annuel,Statut,Ville,ResponsableId:Ref:Membres)
                Contacts(Nom,Email,Telephone,Poste,ClientId:Ref:Clients)
                Affaires(Titre,Valeur,Etape,DateCloture,ClientId:Ref:Clients,ResponsableId)
                Activites(Type,Date,Note,AffaireId:Ref:Affaires) — si pertinent
    Artefacts : Dashboard (KPIs: CA pipeline, nb affaires/etape, chart barres Chart.js)
                FicheClient (detail client + contacts + affaires liees via grist.onRecord)
                KanbanAffaires (colonnes Prospect|Qualification|Proposition|Gagne|Perdu)
    Pages     : Clients (grille Clients | FicheClient linked par ClientId)
                Affaires (grille ou Kanban | FicheClient linked)
                Dashboard (table aggregation | widget Dashboard)
    Formules  : NbAffaires = len(Affaires.lookupRecords(ClientId=id))
                CA_Pipeline = SUM([a.Valeur for a in Affaires.lookupRecords(ClientId=id)])

  Gestion de projets
    Tables    : Projets(Nom,Statut,DateDebut,DateFin,Budget,ChefId:Ref:Membres)
                Taches(Titre,Statut,Priorite,ProjetId:Ref:Projets,AssigneId:Ref:Membres,DateEcheance)
                Membres(Nom,Role,Email)
    Artefacts : Dashboard (nb projets/statut, taches en retard, charge equipe)
                KanbanTaches (A faire|En cours|Revue|Termine)
                GanttProjet (mermaid gantt ou SVG, basé grist.onRecord)
    Pages     : Projets (grille | KanbanTaches linked par ProjetId)
                Dashboard

  Catalogue / Stock
    Tables    : Produits(Ref,Nom,Description,PrixHT,CategorieId:Ref:Categories,StockActuel,SeuilAlerte)
                Categories(Nom,Description,Couleur)
                Mouvements(ProduitId:Ref:Produits,Type:Choice:Entree|Sortie,Qte,Date,Note)
    Artefacts : Catalogue (cards produits avec filtres categorie, badge alerte stock)
                Dashboard (valeur stock total, alertes rupture, mouvements recents)
    Pages     : Produits (grille | Catalogue)
                Stocks (Dashboard)

  Analytics / Reporting
    Tables    : (selon domaine existant) — se baser sur les tables deja presentes
    Artefacts : Dashboard principal (Chart.js: barres empilees, lignes tendance, camembert)
                Rapport (artefact markdown avec grist.docApi.fetchTable -> sections dynamiques)
    Pages     : Dashboard — widget seul ou avec grille de donnees source

  RH / Planning
    Tables    : Employes(Nom,Prenom,Email,Poste,DateEntree,DepartementId:Ref:Departements)
                Departements(Nom,ResponsableId:Ref:Employes)
                Conges(EmployeId:Ref:Employes,Debut,Fin,Type,Statut)
                Plannings(EmployeId:Ref:Employes,Date,HeureDebut,HeureFin,Activite) — si pertinent
    Artefacts : PlanningCalendrier (HTML calendrier semaine/mois, grist.onRecords)
                Dashboard (effectifs/departement, conges en cours, taux presence)
    Pages     : Employes (grille | fiche linked)
                Planning (PlanningCalendrier)
                Dashboard

QUESTIONS QUALIFIANTES (si besoin flou ou Custom)
  - Quelles sont les 2-3 entites principales de votre metier ?
  - Quelles relations entre ces entites ? (ex: 1 client -> N affaires)
  - Quel est le flux principal ? (creation -> suivi -> cloture / commande -> expedition...)
  - Combien d utilisateurs et quel niveau d acces ?
  - Donnees existantes a importer ? (csv, autre systeme)
  - Notifications, exports ou integrations necessaires ?

DERIVE AUTOMATIQUE TABLES -> ARTEFACTS
  1 table principale -> 1 artefact fiche detail + 1 vue grille
  Ensemble de tables -> 1 dashboard global (KPIs + charts)
  Si champs Statut/Etape -> 1 kanban
  Si champs Date/Echeance -> envisager calendrier ou gantt
  Si champs Valeur/Montant -> charts dans dashboard (Chart.js inclus via CDN)

CRITERES DE COMPLETUDE (verifier en phase 4)
  ✓ Toutes les tables avec types corrects et relations Ref:Table
  ✓ Donnees d exemple realistes (BulkAddRecord, min 3-5 lignes par table principale)
  ✓ Dashboard : vue globale avec au moins 2 KPIs ou 1 chart
  ✓ Fiche detail : vue unitaire reactive a la selection (grist.onRecord)
  ✓ Pages Grist : nommees, liees (grille + widget custom linked par FK)
  ✓ Navigation inter-pages si app > 2 pages (artefact nav ou grist pages natives)
  ✓ plan_update(status="done") + canvas_wizard_close() en fin de session
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

    {"uri":         "grist-coder://docs/wizard",
     "name":        "Wizard Guide",
     "description": "Lire avant canvas_wizard. Schema des 5 types d etapes + workflows de qualification besoin, import, iteration.",
     "mimeType":    "text/plain"},

    {"uri":         "grist-coder://docs/qualification",
     "name":        "Qualification Protocol",
     "description": "LIRE EN PHASE 1. Categories de besoins, architectures types (CRM/Projets/Stock/RH/Analytics), questions qualifiantes, criteres de completude.",
     "mimeType":    "text/plain"},

    {"uri":         "grist-coder://docs/services-geo",
     "name":        "Services Géo",
     "description": "APIs géographiques publiques France (BAN geocoding, OSM, IGN, Leaflet). Patterns fetch() utilisables dans artefacts HTML. Workflow wizard→Grist.",
     "mimeType":    "text/plain"},

    {"uri":         "grist-coder://docs/services-data",
     "name":        "Services Data Publiques",
     "description": "APIs open data France (SIRENE/INSEE, DVF immobilier, data.gouv, API Geo). Patterns fetch() + import wizard→Grist.",
     "mimeType":    "text/plain"},

    {"uri":         "grist-coder://docs/services-ai",
     "name":        "Services IA",
     "description": "Integrer l IA dans une app Grist : 4 patterns (canvas_exec sync, webhook async, bridge artefact, subagent MCP). Anthropic/OpenAI/Ollama.",
     "mimeType":    "text/plain"},

    {"uri":         "grist-coder://docs/publication",
     "name":        "Publication autonome",
     "description": "LIRE AVANT artefact_publish. Fige un artefact en widget autonome DANS le doc (zero dependance serveur). Contraintes html/svg, split script, page vs section.",
     "mimeType":    "text/plain"},
]

DOCS_PUBLICATION = """GRIST CODER - Publication autonome (artefact_publish)
=====================================================
Fige un artefact HTML termine en widget custom stocke DANS le document Grist.
Apres publication : AUCUNE dependance au serveur MCP au runtime — le widget
survit a l arret du pod et voyage avec le doc (copies, exports).

Ce que « autonome » veut dire exactement, et ce qu il ne veut pas dire :
  - independant du POD : oui, verifie. artefact_publish REFUSE un artefact dont
    le code reference /ai-proxy, /llm-proxy, /webhook-receive, HOST_URL ou l URL
    du pod. Un tel widget mourrait avec le serveur.
  - independant de TOUT serveur : non. Le widget est rendu par le widget de
    galerie 'custom-widget-builder', servi depuis gristgouv.github.io. Si cet
    hebergement tombe, les widgets publies deviennent blancs. C est vrai de tous
    les widgets publies, avant comme apres cette note.
  - les <script src=...> vers un CDN ne s executent PAS : ils restent dans _html,
    insere comme markup. Toute librairie doit etre INLINE dans le code.

## Quand publier
En PHASE 4 (livraison), pour chaque artefact html finalise que l utilisateur
veut garder comme application autonome. La source reste dans la table Artefacts
(re-editable via l atelier) ; la publication est un SNAPSHOT fige.

## Comment ca marche
Le code est copie dans les options de la section Grist via le widget de la
galerie "Custom widget builder" (@berhalak/custom-widget-builder), publie sur
l instance. Deux champs :
  _html : le markup + styles. L API `grist` n y est PAS disponible.
  _js   : la logique — SEUL endroit ou l API `grist` est disponible
artefact_publish fait le split automatiquement : il extrait les <script> inline
vers _js, garde les <script src=...> CDN dans _html, et prefixe grist.ready().

Les <script src=...> restes dans _html S EXECUTENT (verifie en production : un
artefact chargeant leaflet depuis jsDelivr voit la requete partir en 200 et la
librairie disponible). Une librairie CDN fonctionne donc dans un widget publie.
Le prix est une dependance reseau a l execution : le widget casse si le CDN est
injoignable. Pour une autonomie totale, inliner la librairie dans le code.
artefact_publish signale les CDN detectes sans bloquer — seule une dependance au
POD est bloquante.

MODE APPLICATION — artefact_publish(mode="app")
Les lignes nommees app/Xxx deviennent les ecrans d'une application ; un shell de
~6,5 Ko est publie dans la section et les monte a la demande.

Pourquoi : Grist retelecharge TOUTES les tables _grist_* a chaque ouverture du
document. Un artefact monolithique y pese son poids entier, pour tout le monde,
a chaque fois. Mesure sur un cas reel : 755 Ko de metadonnees pour un artefact
seul, contre 6,5 Ko pour une application de trois ecrans. Les ecrans arrivent
ensuite une requete SQL a la fois — 237 octets d'index au demarrage, et un ecran
que personne ne visite n'est jamais telecharge.

Editer un ecran : modifier la ligne et recharger la page. PAS de republication.
Republier seulement pour changer l'ecran d'entree.

LIMITE — les ecrans ne sont PAS bundles. C'est le prix de l'edition a chaud : le
shell lit le code tel quel dans la table. Un ecran qui fait `import x from "pkg"`
monte mais rend VIDE, son script echouant sur l'import non resolu. Ecrire les
ecrans sans import : h(...) / React.createElement, ou une librairie en
<script src=...> CDN, qui fonctionne. artefact_publish le signale a la publication.

API disponible dans un ecran :
  app.navigate(nom)     changer d'ecran
  app.setState(objet)   etat partage entre ecrans (rejoue au montage)
  app.emit(ev, donnees) / app.on(ev, callback)   bus d'evenements
  grist.docApi.fetchTable / applyUserActions     relayes jusqu'au shell

## Contraintes (IMPORTANT)
- Types publiables : html, svg UNIQUEMENT. Un artefact `react`/`app` est REFUSE
  (Babel ne survit pas au split) -> le convertir en html (React.createElement ou
  HTML+JS vanilla) AVANT de publier.
- L artefact doit avoir un Code non vide sauvegarde dans Grist (canvas_write + Save).
- Le code doit utiliser grist.docApi.fetchTable (pas onRecord seul) pour etre robuste.

## Usage
- Nouvelle page dediee :   artefact_publish(artefact="Dashboard", table_id="Chantiers")
- Section existante :       artefact_publish(artefact="Dashboard", section_ref=8)
  (section_ref depuis grist_views_list)

## Mettre a jour un widget publie
Modifier l artefact source (atelier) PUIS re-appeler artefact_publish : le
snapshot dans la section est remplace. Recharger la page Grist pour voir le rendu.

## A savoir
- A l ajout manuel d une URL custom inconnue, l instance affiche un dialogue de
  confiance (one-shot). Les widgets de la galerie n y sont pas soumis.
- Republier sur une section AFFICHEE : le rendu peut necessiter un reload complet
  de la page Grist (le builder hot-reload mais son pont grist reste transitoire).
""".strip()

DOCS_SERVICES_AI = """GRIST CODER - Intégrer l'IA dans une App Grist
================================================
4 patterns selon le contexte (sync/async, user-triggered/auto).

## PATTERN 1 — canvas_exec (sync, test rapide, Python)
Contexte: LLM veut tester/démo un appel IA sans webhook ni artefact.
  # canvas_write puis canvas_exec :
  import httpx, os, json
  client = httpx.Client()
  r = client.post("https://api.anthropic.com/v1/messages",
    headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"],
             "anthropic-version": "2023-06-01", "content-type": "application/json"},
    json={"model": "claude-haiku-4-5-20251001", "max_tokens": 512,
          "messages": [{"role": "user", "content": "Résume : "+texte}]},
    timeout=30)
  print(r.json()["content"][0]["text"])
Limites: pas d'écriture Grist depuis canvas_exec, timeout 10s.

## PATTERN 2 — Webhook Async (auto-triggered, production)
Contexte: action utilisateur dans Grist (ajout/modif) -> traitement IA -> résultat dans Grist.
Architecture: Grist webhook -> POST /webhook-receive/{docId} -> SSE widget + traitement async

Déclarer le webhook:
  grist_webhooks(action="create", fields={
    "tableId": "Clients", "eventTypes": ["add", "update"],
    "url": HOST_URL+"/webhook-receive/"+doc_id,
    "name": "ai-enrichment", "memo": "enrichissement IA auto"
  })

Dans le serveur (nouveau endpoint ou handler /webhook-receive):
  # L'event arrive dans POST /webhook-receive/{docId}
  # Payload: [{"id": rowId, "Nom": "...", ...}]
  # Traitement async: httpx.AsyncClient -> Anthropic -> grist_patch REST

Pattern traitement (à implémenter dans une route FastAPI séparée ou via canvas_exec one-shot):
  async def process_ai(row):
    async with httpx.AsyncClient() as c:
      r = await c.post("https://api.anthropic.com/v1/messages", headers=...,
        json={"model":"claude-haiku-4-5-20251001","max_tokens":256,
              "messages":[{"role":"user","content":f"Classifie ce client: {row}"}]})
      categorie = r.json()["content"][0]["text"].strip()
      await grist_patch(ctx, "Clients", [{"id":row["id"],"fields":{"Categorie":categorie}}])

## PATTERN 3 — Bridge Artefact (user-triggered, interactif)
Contexte: bouton dans un artefact HTML/React -> appel IA -> affiche résultat dans l'artefact.
  // Dans artefact HTML (bridge=true donne accès à grist.docApi)
  async function analyserClient() {
    const rec = window.__APP_STATE__?.record || {};
    const prompt = "Analyse ce client et propose 3 actions : "+JSON.stringify(rec);
    // Appel via proxy serveur (évite CORS + cache clé API côté serveur)
    const r = await fetch(HOST_URL+'/ai-proxy', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({prompt, model:'claude-haiku-4-5-20251001', max_tokens:512})
    });
    const d = await r.json();
    document.getElementById('result').innerHTML = d.text;
    // Optionnel: sauvegarder dans Grist
    await grist.docApi.applyUserActions([["UpdateRecord","Clients",rec.id,{"Analyse":d.text}]]);
  }
Note: /ai-proxy n'existe pas nativement — à implémenter comme route FastAPI ou utiliser pattern webhook.

## PATTERN 4 — subagent_call (MCP sampling, intégré)
Contexte: LLM principal délègue à un sous-agent spécialisé (déjà disponible dans le service).
  subagent_call(role="data-analyst", task="Analyse les ventes Q1 et identifie les top clients",
                context=json.dumps(grist_records("Ventes", limit=100)))
  -> Retourne texte structuré
  -> canvas_context_update(card_id="analyse-ia") pour afficher
  -> grist_records_patch si données à persister

## MODÈLES RECOMMANDÉS (Anthropic)
  Rapide/économique : claude-haiku-4-5-20251001    (subagent, enrichissement auto)
  Equilibré         : claude-sonnet-4-6             (analyse, conception)
  Puissant          : claude-opus-4-6               (architecture complexe)

## SÉCURITÉ
  - Clé API dans variable d'environnement (ANTHROPIC_API_KEY, OPENAI_API_KEY)
  - Jamais dans un artefact HTML (visible côté client)
  - Pattern recommandé: proxy FastAPI côté serveur
  - Rate limiting: ajouter asyncio.sleep(0.5) entre appels batch
""".strip()

# ── EXAMPLES PAR DOMAINE ───────────────────────────────────────────────────────
EXAMPLES: dict[str, dict] = {
    "crm": {
        "domain": "CRM / Gestion clients",
        "tables": [
            {"name": "Clients", "actions": [
                ["AddTable", "Clients", [
                    {"id": "Nom", "type": "Text"}, {"id": "Email", "type": "Text"},
                    {"id": "Telephone", "type": "Text"}, {"id": "Secteur", "type": "Choice",
                     "widgetOptions": '{"choices":["Tech","Commerce","Industrie","Services","Santé"]}'},
                    {"id": "Statut", "type": "Choice",
                     "widgetOptions": '{"choices":["Prospect","Actif","Inactif"],"choiceOptions":{"Actif":{"fillColor":"#d1fae5"},"Inactif":{"fillColor":"#fee2e2"}}}'},
                    {"id": "CA", "type": "Numeric"}, {"id": "Ville", "type": "Text"},
                    {"id": "Notes", "type": "Text"}
                ]],
                ["BulkAddOrReplaceRecord", "Clients", [None]*5, {
                    "Nom": ["Dupont & Fils", "Tech Solutions", "Boulangerie Martin", "Groupe Renard", "Santé Plus"],
                    "Email": ["contact@dupont.fr", "info@techsol.fr", "martin.boulangerie@gmail.com", "contact@renard.fr", "rh@santeplus.fr"],
                    "Secteur": ["Commerce", "Tech", "Commerce", "Industrie", "Santé"],
                    "Statut": ["Actif", "Actif", "Prospect", "Inactif", "Actif"],
                    "CA": [85000, 250000, 12000, 420000, 95000],
                    "Ville": ["Paris", "Lyon", "Bordeaux", "Nantes", "Marseille"]
                }]
            ]},
            {"name": "Contacts", "actions": [
                ["AddTable", "Contacts", [
                    {"id": "Prenom", "type": "Text"}, {"id": "Nom", "type": "Text"},
                    {"id": "ClientId", "type": "Ref:Clients", "visibleCol": "Nom"},
                    {"id": "Role", "type": "Text"}, {"id": "Email", "type": "Text"},
                    {"id": "Telephone", "type": "Text"}
                ]],
                ["BulkAddOrReplaceRecord", "Contacts", [None]*4, {
                    "Prenom": ["Jean", "Marie", "Pierre", "Sophie"],
                    "Nom": ["Dupont", "Lefebvre", "Martin", "Renard"],
                    "ClientId": [1, 2, 3, 4], "Role": ["Directeur", "Acheteuse", "Gérant", "DG"],
                    "Email": ["j.dupont@dupont.fr", "m.lefebvre@techsol.fr", "p.martin@gmail.com", "s.renard@renard.fr"]
                }]
            ]},
            {"name": "Opportunites", "actions": [
                ["AddTable", "Opportunites", [
                    {"id": "Titre", "type": "Text"},
                    {"id": "ClientId", "type": "Ref:Clients", "visibleCol": "Nom"},
                    {"id": "Montant", "type": "Numeric"},
                    {"id": "Phase", "type": "Choice",
                     "widgetOptions": '{"choices":["Decouverte","Proposition","Negociation","Gagnee","Perdue"]}'},
                    {"id": "DateCloture", "type": "Date"}, {"id": "Probabilite", "type": "Numeric"},
                    {"id": "Notes", "type": "Text"}
                ]],
                ["BulkAddOrReplaceRecord", "Opportunites", [None]*4, {
                    "Titre": ["Refonte site web", "Audit SI", "Formation équipe", "Maintenance annuelle"],
                    "ClientId": [1, 2, 3, 2], "Montant": [15000, 35000, 8000, 12000],
                    "Phase": ["Proposition", "Negociation", "Decouverte", "Gagnee"],
                    "Probabilite": [60, 80, 20, 100]
                }]
            ]}
        ],
        "artefacts": [
            {"name": "DashboardCRM", "type": "html", "is_doc": True,
             "description": "KPIs (CA total, nb clients actifs, opportunités en cours) + graphique pipeline"},
            {"name": "FicheClient", "type": "html", "is_doc": False,
             "description": "Fiche client liée (contacts, opportunités, historique, notes)"},
            {"name": "Pipeline", "type": "html", "is_doc": True,
             "description": "Vue kanban des opportunités par phase"}
        ],
        "pages": [
            {"name": "Dashboard", "table": "Clients", "scenario": "dashboard"},
            {"name": "Clients", "table": "Clients", "scenario": "fiche"},
            {"name": "Pipeline", "table": "Opportunites", "scenario": "kanban"}
        ]
    },
    "rh": {
        "domain": "RH / Gestion des ressources humaines",
        "tables": [
            {"name": "Departements", "actions": [
                ["AddTable", "Departements", [
                    {"id": "Nom", "type": "Text"}, {"id": "Budget", "type": "Numeric"},
                    {"id": "Localisation", "type": "Text"}
                ]],
                ["BulkAddOrReplaceRecord", "Departements", [None]*4, {
                    "Nom": ["Direction", "Commercial", "Technique", "RH"],
                    "Budget": [200000, 500000, 350000, 120000],
                    "Localisation": ["Paris", "Paris", "Lyon", "Paris"]
                }]
            ]},
            {"name": "Employes", "actions": [
                ["AddTable", "Employes", [
                    {"id": "Prenom", "type": "Text"}, {"id": "Nom", "type": "Text"},
                    {"id": "Email", "type": "Text"},
                    {"id": "DepartementId", "type": "Ref:Departements", "visibleCol": "Nom"},
                    {"id": "Poste", "type": "Text"}, {"id": "Salaire", "type": "Numeric"},
                    {"id": "DateEntree", "type": "Date"},
                    {"id": "Statut", "type": "Choice",
                     "widgetOptions": '{"choices":["Actif","Conge","Inactif"]}'},
                    {"id": "NomComplet", "type": "Text",
                     "formula": "$Prenom + ' ' + $Nom", "isFormula": True}
                ]],
                ["BulkAddOrReplaceRecord", "Employes", [None]*6, {
                    "Prenom": ["Alice", "Bob", "Claire", "David", "Eva", "François"],
                    "Nom": ["Moreau", "Dupuis", "Lambert", "Simon", "Petit", "Garcia"],
                    "DepartementId": [2, 3, 3, 2, 4, 1],
                    "Poste": ["Commercial", "Dev Senior", "Dev Junior", "Commercial", "RH", "DG"],
                    "Salaire": [38000, 52000, 34000, 40000, 36000, 90000],
                    "Statut": ["Actif", "Actif", "Actif", "Conge", "Actif", "Actif"]
                }]
            ]},
            {"name": "Conges", "actions": [
                ["AddTable", "Conges", [
                    {"id": "EmployeId", "type": "Ref:Employes", "visibleCol": "NomComplet"},
                    {"id": "Debut", "type": "Date"}, {"id": "Fin", "type": "Date"},
                    {"id": "Type", "type": "Choice",
                     "widgetOptions": '{"choices":["Payé","RTT","Maladie","Sans solde"]}'},
                    {"id": "Statut", "type": "Choice",
                     "widgetOptions": '{"choices":["En attente","Validé","Refusé"]}'},
                    {"id": "NbJours", "type": "Numeric",
                     "formula": "($Fin - $Debut).days + 1 if $Debut and $Fin else 0", "isFormula": True}
                ]],
                ["BulkAddOrReplaceRecord", "Conges", [None]*3, {
                    "EmployeId": [1, 3, 4], "Type": ["Payé", "RTT", "Payé"],
                    "Statut": ["Validé", "En attente", "Validé"]
                }]
            ]}
        ],
        "artefacts": [
            {"name": "OrgChart", "type": "html", "is_doc": True,
             "description": "Organigramme interactif par département"},
            {"name": "FicheEmploye", "type": "html", "is_doc": False,
             "description": "Fiche employé : infos, congés, historique"},
            {"name": "DashboardRH", "type": "html", "is_doc": True,
             "description": "Effectifs/département, congés en cours, masse salariale"}
        ],
        "pages": [
            {"name": "Employés", "table": "Employes", "scenario": "fiche"},
            {"name": "Congés", "table": "Conges", "scenario": "table"},
            {"name": "Dashboard RH", "table": "Employes", "scenario": "dashboard"}
        ]
    },
    "stock": {
        "domain": "Stock / Gestion d'inventaire",
        "tables": [
            {"name": "Categories", "actions": [
                ["AddTable", "Categories", [
                    {"id": "Nom", "type": "Text"}, {"id": "Description", "type": "Text"}
                ]],
                ["BulkAddOrReplaceRecord", "Categories", [None]*4, {
                    "Nom": ["Électronique", "Mobilier", "Consommables", "Outillage"],
                    "Description": ["Appareils et composants", "Meubles et équipements", "Articles à usage unique", "Outils et machines"]
                }]
            ]},
            {"name": "Produits", "actions": [
                ["AddTable", "Produits", [
                    {"id": "Reference", "type": "Text"}, {"id": "Nom", "type": "Text"},
                    {"id": "CategorieId", "type": "Ref:Categories", "visibleCol": "Nom"},
                    {"id": "PrixUnitaire", "type": "Numeric"},
                    {"id": "StockActuel", "type": "Numeric"}, {"id": "StockMin", "type": "Numeric"},
                    {"id": "Fournisseur", "type": "Text"},
                    {"id": "Alerte", "type": "Text",
                     "formula": "'⚠️ RUPTURE' if $StockActuel <= $StockMin else ''", "isFormula": True}
                ]],
                ["BulkAddOrReplaceRecord", "Produits", [None]*6, {
                    "Reference": ["EL-001", "EL-002", "MB-001", "CS-001", "CS-002", "OT-001"],
                    "Nom": ["Laptop HP", "Écran 27\"", "Bureau ergonomique", "Ramette papier A4", "Stylos (boîte 50)", "Visseuse"],
                    "CategorieId": [1, 1, 2, 3, 3, 4],
                    "PrixUnitaire": [899, 349, 450, 12, 8, 89],
                    "StockActuel": [15, 8, 3, 45, 12, 6],
                    "StockMin": [5, 3, 2, 20, 10, 3],
                    "Fournisseur": ["Ingram", "Ingram", "OfficeMax", "Bureau Vallée", "Bureau Vallée", "Leroy Merlin"]
                }]
            ]},
            {"name": "Mouvements", "actions": [
                ["AddTable", "Mouvements", [
                    {"id": "ProduitId", "type": "Ref:Produits", "visibleCol": "Nom"},
                    {"id": "Type", "type": "Choice",
                     "widgetOptions": '{"choices":["Entrée","Sortie","Inventaire"]}'},
                    {"id": "Quantite", "type": "Numeric"},
                    {"id": "Date", "type": "Date"}, {"id": "Motif", "type": "Text"}
                ]],
                ["BulkAddOrReplaceRecord", "Mouvements", [None]*4, {
                    "ProduitId": [1, 2, 1, 4], "Type": ["Entrée", "Sortie", "Sortie", "Entrée"],
                    "Quantite": [10, 2, 3, 50], "Motif": ["Commande fournisseur", "Service IT", "Direction", "Réappro"]
                }]
            ]}
        ],
        "artefacts": [
            {"name": "DashboardStock", "type": "html", "is_doc": True,
             "description": "Alertes stock, valeur inventaire, mouvements récents"},
            {"name": "FicheProduit", "type": "html", "is_doc": False,
             "description": "Fiche produit : stock, historique mouvements, seuils"}
        ],
        "pages": [
            {"name": "Produits", "table": "Produits", "scenario": "fiche"},
            {"name": "Mouvements", "table": "Mouvements", "scenario": "table"},
            {"name": "Dashboard Stock", "table": "Produits", "scenario": "dashboard"}
        ]
    },
    "projets": {
        "domain": "Gestion de projets / Suivi tâches",
        "tables": [
            {"name": "Projets", "actions": [
                ["AddTable", "Projets", [
                    {"id": "Nom", "type": "Text"}, {"id": "Description", "type": "Text"},
                    {"id": "Chef", "type": "Text"}, {"id": "DateDebut", "type": "Date"},
                    {"id": "DateFin", "type": "Date"},
                    {"id": "Statut", "type": "Choice",
                     "widgetOptions": '{"choices":["Planifié","En cours","En pause","Terminé","Annulé"]}'},
                    {"id": "Budget", "type": "Numeric"}, {"id": "Priorite", "type": "Choice",
                     "widgetOptions": '{"choices":["Haute","Normale","Basse"]}'}
                ]],
                ["BulkAddOrReplaceRecord", "Projets", [None]*3, {
                    "Nom": ["Refonte site web", "Migration ERP", "Formation équipes"],
                    "Chef": ["Alice Martin", "Bob Dupuis", "Claire Simon"],
                    "Statut": ["En cours", "Planifié", "Terminé"],
                    "Budget": [25000, 80000, 12000], "Priorite": ["Haute", "Haute", "Normale"]
                }]
            ]},
            {"name": "Taches", "actions": [
                ["AddTable", "Taches", [
                    {"id": "Titre", "type": "Text"},
                    {"id": "ProjetId", "type": "Ref:Projets", "visibleCol": "Nom"},
                    {"id": "Assigne", "type": "Text"},
                    {"id": "Statut", "type": "Choice",
                     "widgetOptions": '{"choices":["Todo","En cours","Review","Fait"],"choiceOptions":{"Fait":{"fillColor":"#d1fae5"},"En cours":{"fillColor":"#fef3c7"}}}'},
                    {"id": "Priorite", "type": "Choice",
                     "widgetOptions": '{"choices":["Haute","Normale","Basse"]}'},
                    {"id": "DateLimite", "type": "Date"}, {"id": "Description", "type": "Text"}
                ]],
                ["BulkAddOrReplaceRecord", "Taches", [None]*6, {
                    "Titre": ["Maquettes UX", "Dev homepage", "Tests", "Cahier des charges", "Choix prestataire", "Bilan formation"],
                    "ProjetId": [1, 1, 1, 2, 2, 3],
                    "Assigne": ["Alice", "Bob", "Claire", "Alice", "Bob", "Claire"],
                    "Statut": ["Fait", "En cours", "Todo", "En cours", "Todo", "Fait"],
                    "Priorite": ["Haute", "Haute", "Normale", "Haute", "Normale", "Basse"]
                }]
            ]}
        ],
        "artefacts": [
            {"name": "KanbanTaches", "type": "html", "is_doc": False,
             "description": "Kanban par statut des tâches du projet sélectionné"},
            {"name": "DashboardProjets", "type": "html", "is_doc": True,
             "description": "Vue globale : projets en cours, tâches urgentes, charge par personne"},
            {"name": "Gantt", "type": "html", "is_doc": False,
             "description": "Diagramme de Gantt simplifié par projet"}
        ],
        "pages": [
            {"name": "Projets", "table": "Projets", "scenario": "master-detail"},
            {"name": "Tâches", "table": "Taches", "scenario": "kanban"},
            {"name": "Dashboard", "table": "Projets", "scenario": "dashboard"}
        ]
    },
    "immobilier": {
        "domain": "Immobilier / Gestion de biens",
        "tables": [
            {"name": "Biens", "actions": [
                ["AddTable", "Biens", [
                    {"id": "Reference", "type": "Text"}, {"id": "Adresse", "type": "Text"},
                    {"id": "Ville", "type": "Text"}, {"id": "CodePostal", "type": "Text"},
                    {"id": "Lat", "type": "Numeric"}, {"id": "Lon", "type": "Numeric"},
                    {"id": "Type", "type": "Choice",
                     "widgetOptions": '{"choices":["Appartement","Maison","Bureau","Local commercial","Terrain"]}'},
                    {"id": "Surface", "type": "Numeric"}, {"id": "NbPieces", "type": "Numeric"},
                    {"id": "PrixVente", "type": "Numeric"}, {"id": "Loyer", "type": "Numeric"},
                    {"id": "Statut", "type": "Choice",
                     "widgetOptions": '{"choices":["Disponible","Loué","Vendu","En travaux"]}'},
                    {"id": "DPE", "type": "Choice",
                     "widgetOptions": '{"choices":["A","B","C","D","E","F","G"]}'},
                    {"id": "Notes", "type": "Text"}
                ]],
                ["BulkAddOrReplaceRecord", "Biens", [None]*4, {
                    "Reference": ["B001", "B002", "B003", "B004"],
                    "Adresse": ["12 rue de la Paix", "45 av. Victor Hugo", "8 place du Marché", "23 rue des Lilas"],
                    "Ville": ["Paris", "Lyon", "Bordeaux", "Nantes"],
                    "CodePostal": ["75001", "69002", "33000", "44000"],
                    "Type": ["Appartement", "Bureau", "Local commercial", "Maison"],
                    "Surface": [65, 120, 80, 110],
                    "NbPieces": [3, 0, 0, 5],
                    "PrixVente": [450000, 0, 180000, 320000],
                    "Loyer": [1800, 2400, 1500, 0],
                    "Statut": ["Loué", "Loué", "Disponible", "Vendu"],
                    "DPE": ["C", "B", "D", "A"]
                }]
            ]},
            {"name": "Proprietaires", "actions": [
                ["AddTable", "Proprietaires", [
                    {"id": "Nom", "type": "Text"}, {"id": "Prenom", "type": "Text"},
                    {"id": "Email", "type": "Text"}, {"id": "Telephone", "type": "Text"},
                    {"id": "IBAN", "type": "Text"}
                ]],
                ["BulkAddOrReplaceRecord", "Proprietaires", [None]*3, {
                    "Nom": ["Durand", "Lefèvre", "Martin"],
                    "Prenom": ["Michel", "Isabelle", "Stéphane"],
                    "Email": ["m.durand@mail.fr", "i.lefevre@mail.fr", "s.martin@mail.fr"],
                    "Telephone": ["06 12 34 56 78", "06 98 76 54 32", "07 11 22 33 44"]
                }]
            ]}
        ],
        "artefacts": [
            {"name": "CarteBiens", "type": "html", "is_doc": True,
             "description": "Carte Leaflet des biens avec filtres statut/type (BAN geocoding si Lat/Lon vides)"},
            {"name": "FicheBien", "type": "html", "is_doc": False,
             "description": "Fiche détaillée avec photos, localisation carte, DPE visuel"},
            {"name": "DashboardImmo", "type": "html", "is_doc": True,
             "description": "KPIs : surface totale, revenus locatifs, taux occupation, biens dispo"}
        ],
        "pages": [
            {"name": "Biens", "table": "Biens", "scenario": "fiche"},
            {"name": "Carte", "table": "Biens", "scenario": "dashboard"},
            {"name": "Dashboard", "table": "Biens", "scenario": "dashboard"}
        ]
    },
    "association": {
        "domain": "Association / Gestion membres et activités",
        "tables": [
            {"name": "Membres", "actions": [
                ["AddTable", "Membres", [
                    {"id": "Prenom", "type": "Text"}, {"id": "Nom", "type": "Text"},
                    {"id": "Email", "type": "Text"}, {"id": "Telephone", "type": "Text"},
                    {"id": "DateAdhesion", "type": "Date"},
                    {"id": "Statut", "type": "Choice",
                     "widgetOptions": '{"choices":["Actif","Inactif","Honoraire","Bienfaiteur"]}'},
                    {"id": "Cotisation", "type": "Numeric"},
                    {"id": "CotisationPayee", "type": "Bool"}
                ]],
                ["BulkAddOrReplaceRecord", "Membres", [None]*5, {
                    "Prenom": ["Marie", "Jean", "Sophie", "Pierre", "Lucas"],
                    "Nom": ["Fontaine", "Leblanc", "Perrot", "Girard", "Roy"],
                    "Email": ["m.fontaine@mail.fr", "j.leblanc@mail.fr", "s.perrot@mail.fr", "p.girard@mail.fr", "l.roy@mail.fr"],
                    "Statut": ["Actif", "Actif", "Actif", "Honoraire", "Inactif"],
                    "Cotisation": [50, 50, 50, 0, 50], "CotisationPayee": [True, True, False, True, False]
                }]
            ]},
            {"name": "Activites", "actions": [
                ["AddTable", "Activites", [
                    {"id": "Titre", "type": "Text"}, {"id": "Date", "type": "Date"},
                    {"id": "Lieu", "type": "Text"}, {"id": "Responsable", "type": "Text"},
                    {"id": "NbPlaces", "type": "Numeric"}, {"id": "Statut", "type": "Choice",
                     "widgetOptions": '{"choices":["Planifiée","En cours","Terminée","Annulée"]}'},
                    {"id": "Description", "type": "Text"}
                ]],
                ["BulkAddOrReplaceRecord", "Activites", [None]*3, {
                    "Titre": ["Atelier cuisine", "Randonnée forêt", "AG annuelle"],
                    "Lieu": ["Salle communale", "Départ parking mairie", "Salle des fêtes"],
                    "Responsable": ["Marie Fontaine", "Pierre Girard", "Jean Leblanc"],
                    "NbPlaces": [20, 30, 100], "Statut": ["Planifiée", "Terminée", "Planifiée"]
                }]
            ]}
        ],
        "artefacts": [
            {"name": "DashboardAsso", "type": "html", "is_doc": True,
             "description": "Membres actifs, cotisations en attente, prochaines activités"},
            {"name": "FicheMembre", "type": "html", "is_doc": False,
             "description": "Fiche membre avec historique participations et statut cotisation"}
        ],
        "pages": [
            {"name": "Membres", "table": "Membres", "scenario": "fiche"},
            {"name": "Activités", "table": "Activites", "scenario": "table"},
            {"name": "Dashboard", "table": "Membres", "scenario": "dashboard"}
        ]
    },
    "restaurant": {
        "domain": "Restaurant / Gestion commandes et service",
        "tables": [
            {"name": "Tables", "actions": [
                ["AddTable", "Tables", [
                    {"id": "Numero", "type": "Integer"}, {"id": "NbCouverts", "type": "Integer"},
                    {"id": "Zone", "type": "Choice",
                     "widgetOptions": '{"choices":["Salle","Terrasse","Bar","Privé"]}'},
                    {"id": "Statut", "type": "Choice",
                     "widgetOptions": '{"choices":["Libre","Occupée","Réservée","Hors service"]}'}
                ]],
                ["BulkAddOrReplaceRecord", "Tables", [None]*6, {
                    "Numero": [1, 2, 3, 4, 5, 6],
                    "NbCouverts": [2, 4, 4, 6, 8, 2],
                    "Zone": ["Salle", "Salle", "Terrasse", "Salle", "Privé", "Bar"],
                    "Statut": ["Libre", "Occupée", "Libre", "Réservée", "Libre", "Occupée"]
                }]
            ]},
            {"name": "Menu", "actions": [
                ["AddTable", "Menu", [
                    {"id": "Nom", "type": "Text"}, {"id": "Categorie", "type": "Choice",
                     "widgetOptions": '{"choices":["Entrée","Plat","Dessert","Boisson","Formule"]}'},
                    {"id": "Prix", "type": "Numeric"}, {"id": "Description", "type": "Text"},
                    {"id": "Disponible", "type": "Bool"}, {"id": "Allergenes", "type": "Text"}
                ]],
                ["BulkAddOrReplaceRecord", "Menu", [None]*6, {
                    "Nom": ["Soupe à l'oignon", "Entrecôte frites", "Salade César", "Tarte tatin", "Formule midi", "Eau minérale"],
                    "Categorie": ["Entrée", "Plat", "Entrée", "Dessert", "Formule", "Boisson"],
                    "Prix": [8.5, 22, 12, 7, 15, 4],
                    "Disponible": [True, True, True, True, True, True],
                    "Allergenes": ["", "Gluten", "Lactose, Gluten", "Gluten, Œufs, Lactose", "", ""]
                }]
            ]}
        ],
        "artefacts": [
            {"name": "PlanSalle", "type": "html", "is_doc": True,
             "description": "Plan de salle interactif : statut tables, couleur par état, clic pour détails"},
            {"name": "CarteMenu", "type": "html", "is_doc": True,
             "description": "Carte digitale avec filtres catégorie, allergènes, prix"}
        ],
        "pages": [
            {"name": "Service", "table": "Tables", "scenario": "dashboard"},
            {"name": "Menu", "table": "Menu", "scenario": "table"}
        ]
    },
    "formation": {
        "domain": "Centre de formation / Suivi apprenants",
        "tables": [
            {"name": "Formations", "actions": [
                ["AddTable", "Formations", [
                    {"id": "Titre", "type": "Text"}, {"id": "Domaine", "type": "Choice",
                     "widgetOptions": '{"choices":["Informatique","Management","Langues","Technique","Soft skills"]}'},
                    {"id": "DureeHeures", "type": "Numeric"},
                    {"id": "Modalite", "type": "Choice",
                     "widgetOptions": '{"choices":["Présentiel","Distanciel","Hybride"]}'},
                    {"id": "Prix", "type": "Numeric"}, {"id": "Formateur", "type": "Text"},
                    {"id": "Description", "type": "Text"}
                ]],
                ["BulkAddOrReplaceRecord", "Formations", [None]*4, {
                    "Titre": ["Python pour données", "Management d'équipe", "Excel avancé", "Communication"],
                    "Domaine": ["Informatique", "Management", "Informatique", "Soft skills"],
                    "DureeHeures": [21, 14, 7, 14],
                    "Modalite": ["Présentiel", "Distanciel", "Hybride", "Présentiel"],
                    "Prix": [1500, 1200, 600, 900],
                    "Formateur": ["Alice M.", "Bob D.", "Claire L.", "David S."]
                }]
            ]},
            {"name": "Apprenants", "actions": [
                ["AddTable", "Apprenants", [
                    {"id": "Prenom", "type": "Text"}, {"id": "Nom", "type": "Text"},
                    {"id": "Email", "type": "Text"}, {"id": "Entreprise", "type": "Text"},
                    {"id": "FormationId", "type": "Ref:Formations", "visibleCol": "Titre"},
                    {"id": "Statut", "type": "Choice",
                     "widgetOptions": '{"choices":["Inscrit","En cours","Certifié","Abandonné"]}'},
                    {"id": "Note", "type": "Numeric"}, {"id": "DateDebut", "type": "Date"}
                ]],
                ["BulkAddOrReplaceRecord", "Apprenants", [None]*5, {
                    "Prenom": ["Emma", "Thomas", "Léa", "Hugo", "Camille"],
                    "Nom": ["Blanc", "Noir", "Rouge", "Vert", "Bleu"],
                    "Entreprise": ["TechCorp", "StartupXYZ", "PME Loire", "TechCorp", "Indépendant"],
                    "FormationId": [1, 2, 1, 3, 4],
                    "Statut": ["Certifié", "En cours", "Inscrit", "Certifié", "En cours"],
                    "Note": [87, None, None, 92, None]
                }]
            ]}
        ],
        "artefacts": [
            {"name": "DashboardFormation", "type": "html", "is_doc": True,
             "description": "Taux de certification, apprenants en cours, revenus par formation"},
            {"name": "FicheApprenant", "type": "html", "is_doc": False,
             "description": "Parcours apprenant, progression, attestation générée"}
        ],
        "pages": [
            {"name": "Formations", "table": "Formations", "scenario": "fiche"},
            {"name": "Apprenants", "table": "Apprenants", "scenario": "fiche"},
            {"name": "Dashboard", "table": "Formations", "scenario": "dashboard"}
        ]
    },
}

RESOURCE_TEMPLATES = [
    {"uriTemplate":  "grist-coder://examples/{domain}",
     "name":         "Domain Examples",
     "description":  "Schema + donnees exemple prets a l emploi pour un domaine metier. Valeurs: crm | rh | stock | projets | immobilier | association | restaurant | formation",
     "mimeType":     "application/json"},

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

    {"uriTemplate":  "grist-coder://plan/{token}",
     "name":         "Project Plan",
     "description":  "Plan de projet persistant : besoin qualifie, tables, artefacts, pages, status de construction. Lire en debut de session pour reprendre. Mis a jour par plan_update.",
     "mimeType":     "application/json"},

    {"uriTemplate":  "grist-coder://schema-diagram/{token}",
     "name":         "Schema Diagram",
     "description":  "erDiagram mermaid auto-generé depuis le schema Grist courant. Tables + colonnes + relations (Ref:). Passer directement comme code dans canvas_wizard(type=preview, code_type=mermaid) ou utiliser source='schema' pour auto-generation transparente.",
     "mimeType":     "text/plain"},

    {"uriTemplate":  "grist-coder://context/{token}/page/{page_id}",
     "name":         "Page Context",
     "description":  "Contexte detaille d une page Grist : schema de la table source, colonnes disponibles (dont formules), liaisons entrantes/sortantes (linkSrcSectionRef), artefact configure, et suggestions de construction. Lire avant de coder un artefact ou creer une page liee.",
     "mimeType":     "application/json"},
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

    if uri == "grist-coder://docs/wizard":
        return {"uri": uri, "mimeType": "text/plain", "text": DOCS_WIZARD}

    if uri == "grist-coder://docs/qualification":
        return {"uri": uri, "mimeType": "text/plain", "text": DOCS_QUALIFICATION}

    if uri == "grist-coder://docs/services-geo":
        return {"uri": uri, "mimeType": "text/plain", "text": DOCS_SERVICES_GEO}

    if uri == "grist-coder://docs/services-data":
        return {"uri": uri, "mimeType": "text/plain", "text": DOCS_SERVICES_DATA}

    if uri == "grist-coder://docs/services-ai":
        return {"uri": uri, "mimeType": "text/plain", "text": DOCS_SERVICES_AI}

    if uri == "grist-coder://docs/publication":
        return {"uri": uri, "mimeType": "text/plain", "text": DOCS_PUBLICATION}

    if uri.startswith("grist-coder://examples/"):
        domain = uri.split("/")[-1]
        ex = EXAMPLES.get(domain)
        if not ex:
            available = " | ".join(EXAMPLES.keys())
            return {"uri": uri, "mimeType": "application/json",
                    "text": json.dumps({"error": f"Domaine inconnu : '{domain}'",
                                        "disponibles": available}, ensure_ascii=False)}
        return {"uri": uri, "mimeType": "application/json",
                "text": json.dumps(ex, ensure_ascii=False, indent=2)}

    if uri.startswith("grist-coder://plan/"):
        token = uri.split("/")[-1]
        ctx = registry.resolve(uid_key, token)
        if not ctx: raise ValueError(f"Session inconnue : {token}")
        plan = dict(ctx.project_plan)
        # Lazy-restore project need from Artefacts table if memory plan is empty
        # (server restart or first call after fresh session)
        if not plan.get("need"):
            meta = await _read_project_meta(ctx)
            if meta.get("need"):
                plan["need"] = meta["need"]
                ctx.project_plan["need"] = meta["need"]  # cache for future calls
                if not plan.get("status"):
                    plan["status"] = "qualifying"  # at least we have a need
        plan.setdefault("status", "not_started")
        plan.setdefault("doc_title", ctx.doc_title)
        status = plan["status"]
        _next_steps = {
            "not_started": "canvas_wizard(type='input', id='collect-need') -> collecter le besoin utilisateur",
            "qualifying":  "canvas_wizard(type='choice'/'form') -> affiner besoin, puis plan_update(status='assessing' ou 'designing')",
            "assessing":   "grist_schema + grist_records -> explorer doc, puis plan_update(status='designing') avec tables/artefacts cibles",
            "designing":   "canvas_wizard(type='confirm') -> valider plan avec user, puis plan_update(status='building') + artefact_init()",
            "building":    "context/{token} -> verifier etat reel, continuer construction selon plan.tables/artefacts/pages",
            "verifying":   "context/{token} -> snapshot final, canvas_wizard(type='confirm') -> livraison, plan_update(status='done')",
            "done":        "App livree — plan_update() pour modifier, ou nouvelle session pour nouveau projet",
        }
        plan["_next_step"] = _next_steps.get(status, "plan_update(status=...) pour avancer")
        plan["_hint"] = ("Plan vide — commencer par Phase 1 (canvas_wizard type=input + docs/qualification)"
                         if not plan.get("need") else
                         f"Plan en cours — status: {status}")
        # Timeline of status transitions (most recent first, max 10 entries shown)
        if ctx.plan_history:
            plan["_history"] = [
                {"status": h["status"], "from": h.get("from"),
                 "summary": h["summary"],
                 "ago_seconds": int(time.time() - h["ts"])}
                for h in list(ctx.plan_history)[:10]
            ]
        return {"uri": uri, "mimeType": "application/json",
                "text": json.dumps(plan, ensure_ascii=False, indent=2)}

    if uri.startswith("grist-coder://playbook/"):
        scenario = uri.split("/")[-1]
        content = PLAYBOOKS.get(scenario)
        if not content:
            available = " | ".join(PLAYBOOKS.keys())
            content = f"Scenario inconnu : '{scenario}'\nDisponibles : {available}"
        return {"uri": uri, "mimeType": "text/plain", "text": content}

    if uri.startswith("grist-coder://schema-diagram/"):
        token = uri.split("/")[-1]
        ctx = registry.resolve(uid_key, token)
        if not ctx: raise ValueError(f"Session inconnue : {token}")
        schema = await _fetch_schema(ctx)
        return {"uri": uri, "mimeType": "text/plain", "text": _schema_to_mermaid(schema)}

    if uri.startswith("grist-coder://context/") and "/page/" not in uri:
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
        # Delta plan vs realite
        plan = ctx.project_plan
        if plan.get("need"):
            def _name(x): return (x.get("name", x) if isinstance(x, dict) else x).strip().lower()
            plan_tables = {_name(t) for t in plan.get("tables", [])}
            actual_tables = {tid.lower() for tid in snapshot.get("tables", [])
                             if tid.lower() != "artefacts"}
            plan_arts = {_name(a) for a in plan.get("artefacts", [])}
            actual_arts = {a["nom"].lower() for a in snapshot.get("artefacts", [])}
            plan_pages = {_name(p) for p in plan.get("pages", [])}
            actual_pages = {p["name"].lower() for p in snapshot.get("pages", []) if p.get("name")}
            snapshot["_delta"] = {
                "plan_status": plan.get("status", "not_started"),
                "tables":   {"missing": sorted(plan_tables - actual_tables),
                             "present": sorted(plan_tables & actual_tables),
                             "extra":   sorted(actual_tables - plan_tables)},
                "artefacts":{"missing": sorted(plan_arts - actual_arts),
                             "present": sorted(plan_arts & actual_arts),
                             "extra":   sorted(actual_arts - plan_arts)},
                "pages":    {"missing": sorted(plan_pages - actual_pages),
                             "present": sorted(plan_pages & actual_pages),
                             "extra":   sorted(actual_pages - plan_pages)},
            }
        # Suggestions basees sur l etat reel du doc
        suggestions = []
        arts = snapshot.get("artefacts", [])
        pages = snapshot.get("pages", [])
        tables = snapshot.get("tables", [])
        art_names = {a["nom"].lower() for a in arts}
        page_names = {p["name"].lower() for p in pages if p.get("name")}
        has_dashboard = any("dashboard" in n for n in art_names | page_names)
        has_pages = bool(pages)
        has_artefacts = bool(arts)
        non_empty_tables = [t for t in tables if t.lower() not in ("artefacts",)]
        if not has_dashboard and non_empty_tables:
            suggestions.append("no_dashboard: creer un artefact Dashboard (vue globale) — canvas_write + grist_view_create")
        if not has_artefacts and non_empty_tables:
            suggestions.append("no_artefacts: aucun artefact — artefact_init() puis canvas_write()")
        if not has_pages and non_empty_tables:
            suggestions.append("no_pages: aucune page Grist — grist_view_create() pour chaque table principale")
        if non_empty_tables and has_artefacts and not has_pages:
            suggestions.append("artefacts_sans_pages: artefacts crees mais pas de page Grist — grist_view_create()")
        if snapshot.get("_delta", {}).get("artefacts", {}).get("missing"):
            missing = snapshot["_delta"]["artefacts"]["missing"]
            suggestions.append(f"plan_missing_artefacts: {missing} — canvas_write pour chacun")
        if snapshot.get("_delta", {}).get("tables", {}).get("missing"):
            missing = snapshot["_delta"]["tables"]["missing"]
            suggestions.append(f"plan_missing_tables: {missing} — grist_apply([AddTable, ...])")
        if suggestions:
            snapshot["_suggestions"] = suggestions
        # Relations graph (FK from Ref: columns)
        relations = []
        schema = snapshot.get("schema", {})
        for table_id, cols in schema.items():
            for col in cols:
                col_type = col.get("type", "")
                if col_type.startswith("Ref:"):
                    target = col_type[4:]
                    relations.append({"from": table_id, "col": col["id"], "to": target})
                elif col_type.startswith("RefList:"):
                    target = col_type[8:]
                    relations.append({"from": table_id, "col": col["id"], "to": target, "list": True})
        snapshot["relations"] = relations
        # Quality analysis
        quality_issues = []
        pages_data = snapshot.get("pages", [])
        tables_set = set(snapshot.get("tables", []))
        tables_with_page = set()
        for page in pages_data:
            for sec in page.get("sections", []):
                tbl = sec.get("table")
                if tbl:
                    tables_with_page.add(tbl)
                if sec.get("type") == "custom" and not sec.get("artefact"):
                    quality_issues.append({
                        "issue": "custom_section_no_artefact",
                        "page": page.get("name"),
                        "section_id": sec.get("id")
                    })
        for tbl in tables_set:
            if tbl.lower() == "artefacts":
                continue
            if tbl not in tables_with_page:
                quality_issues.append({"issue": "table_without_page", "table": tbl})
        for tbl, cols in schema.items():
            for col in cols:
                col_type = col.get("type", "")
                if col_type.startswith("Ref:") or col_type.startswith("RefList:"):
                    # Check if visibleCol defined (formula with displayCol pattern or label)
                    # We approximate: if no label set and no formula, flag it
                    if not col.get("label") and not col.get("formula"):
                        quality_issues.append({
                            "issue": "ref_no_visiblecol",
                            "table": tbl, "col": col["id"], "type": col_type
                        })
        if quality_issues:
            snapshot["_quality"] = quality_issues

        # Read persisted project need (the only state not derivable from live Grist)
        meta = await _read_project_meta(ctx)
        if meta:
            snapshot["_meta"] = {"need": meta.get("need", "")}

        # Filter out the special _project_meta record from the artefacts list
        # (it's metadata, not a real artefact)
        snapshot["artefacts"] = [a for a in snapshot.get("artefacts", [])
                                 if a.get("nom") != PROJECT_META_NAME]

        # Inferred plan: status, completeness ratio, recommended next actions —
        # all derived from the live Grist state, no parallel persistence needed
        inferred_status, completeness, next_actions = _infer_status_from_snapshot(snapshot)
        snapshot["_inferred_plan"] = {
            "status":       inferred_status,
            "completeness": round(completeness, 2),
            "next_actions": next_actions,
            "rationale":    f"Etat reel : {len(snapshot.get('tables',[]))-1} tables metier, "
                            f"{len(snapshot.get('artefacts',[]))} artefacts, "
                            f"{len(snapshot.get('pages',[]))} pages",
        }
        # If memory plan disagrees with inferred status, expose the divergence
        plan_status = ctx.project_plan.get("status")
        if plan_status and plan_status != inferred_status:
            snapshot["_inferred_plan"]["memory_status_diverges"] = plan_status

        return {"uri": uri, "mimeType": "application/json",
                "text": json.dumps(snapshot, ensure_ascii=False, indent=2)}

    if uri.startswith("grist-coder://context/") and "/page/" in uri:
        # context/{token}/page/{page_id}
        parts = uri.replace("grist-coder://context/", "").split("/page/")
        token, page_id_str = parts[0], parts[1] if len(parts) > 1 else ""
        ctx = registry.resolve(uid_key, token)
        if not ctx: raise ValueError(f"Session inconnue : {token}")
        try:
            page_id = int(page_id_str)
        except ValueError:
            raise ValueError(f"page_id invalide : {page_id_str}")
        # Fetch pages + sections
        sections_resp = await grist_get(ctx, "tables/_grist_Views_section/records")
        tables_meta   = await grist_get(ctx, "tables/_grist_Tables/records")
        table_ref_map = {r["id"]: r["fields"].get("tableId","") for r in tables_meta.get("records",[])}
        # Find target page
        pages_resp = await grist_get(ctx, "tables/_grist_Pages/records")
        views_resp = await grist_get(ctx, "tables/_grist_Views/records")
        views_map  = {r["id"]: r["fields"].get("name","") for r in views_resp.get("records",[])}
        target_page = None
        for r in pages_resp.get("records", []):
            if r["id"] == page_id:
                view_ref = r["fields"].get("viewRef", 0)
                target_page = {"page_id": page_id, "name": views_map.get(view_ref, ""), "view_ref": view_ref}
                break
        if not target_page:
            raise ValueError(f"Page {page_id} introuvable")
        # Sections for this page
        def _extract_artefact_url(options_str):
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
        sections = []
        all_section_ids = set()
        for r in sections_resp.get("records", []):
            f = r["fields"]
            if f.get("parentId", 0) == target_page["view_ref"]:
                table_id = table_ref_map.get(f.get("tableRef", 0), "")
                sec = {
                    "id": r["id"],
                    "type": f.get("parentKey", ""),
                    "table": table_id,
                    "artefact": _extract_artefact_url(f.get("options", "")),
                    "linked_to": f.get("linkSrcSectionRef") or None,
                }
                sections.append(sec)
                all_section_ids.add(r["id"])
        # Resolve incoming links (other sections pointing to this page's sections)
        incoming_links = []
        for r in sections_resp.get("records", []):
            f = r["fields"]
            if f.get("linkSrcSectionRef") and f["linkSrcSectionRef"] in all_section_ids:
                incoming_table = table_ref_map.get(f.get("tableRef", 0), "")
                incoming_links.append({
                    "from_section": f["linkSrcSectionRef"],
                    "to_section": r["id"],
                    "to_table": incoming_table,
                })
        # Build column list for each table in sections
        # schema_full = {table_id: [{id, type, label?}]}
        table_schemas = {}
        unique_tables = {sec.get("table") for sec in sections if sec.get("table")}
        for tid in unique_tables:
            try:
                cols_resp = await grist_get(ctx, f"tables/{tid}/columns")
                table_schemas[tid] = [
                    {"id": c["id"],
                     "type": c["fields"].get("type",""),
                     "label": c["fields"].get("label","") or None,
                     "formula": c["fields"].get("formula","") or None,
                     "isFormula": c["fields"].get("isFormula", False)}
                    for c in cols_resp.get("columns", [])
                    if not c["id"].startswith("gristHelper_")
                ]
            except Exception:
                table_schemas[tid] = []
        page_ctx = {
            "page": target_page,
            "sections": sections,
            "table_schemas": table_schemas,
            "incoming_links": incoming_links,
            "_hint": (
                "Sections custom sans artefact : configurer via grist_section_configure(). "
                "Colonnes Ref: disponibles pour liaison maitre-detail via grist_view_add_widget(linkSrcSectionRef=...)."
            )
        }
        return {"uri": uri, "mimeType": "application/json",
                "text": json.dumps(page_ctx, ensure_ascii=False, indent=2)}

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
            {"uri": f"grist-coder://plan/{t}",
             "name": f"Plan — {title}",
             "description": "Plan de projet persistant. Lire en debut de session pour reprendre.",
             "mimeType": "application/json"},
        ]
    return res

# ── TOOL CALL ─────────────────────────────────────────────────────────────────

_UX_RESOURCES_BY_PHASE = {
    "qualifying": ["docs/qualification", "docs/wizard"],
    "assessing":  ["docs/schema", "docs/wizard"],
    "designing":  ["docs/schema", "docs/artefacts", "docs/wizard"],
    "building":   ["docs/artefacts", "docs/wizard", "docs/formulas"],
    "verifying":  ["docs/wizard"],
    "done":       [],
}

def _enrich_wizard_response(resp: dict, step_id: str, step_type: str, ctx) -> None:
    """Injecte _ux_context + _ux_task + _next dans toute réponse wizard bloquante."""
    plan        = ctx.project_plan
    status      = plan.get("status", "qualifying")
    values_str  = json.dumps(resp.get("values", {}), ensure_ascii=False)
    resp["_ux_context"] = {
        "plan_status":          status,
        "active_cards":         list(ctx._active_wizard_cards.keys()),
        "resources_contextuels": _UX_RESOURCES_BY_PHASE.get(status, ["docs/wizard"]),
    }
    resp["_ux_task"] = (
        f"L utilisateur a repondu au step '{step_id}' (type:{step_type}) : {values_str}. "
        f"Plan status: {status}. Besoin: {plan.get('need', 'non qualifie')}. "
        f"Propose le step wizard suivant le plus pertinent pour continuer le flux naturellement."
    )
    resp["_next"] = (
        "Traiter la reponse. Puis : "
        "si transition de phase logique -> plan_update(status=...) ; "
        "sinon -> subagent_call(role='ux-navigator', task=_ux_task) pour composer le step suivant."
    )

_active_tokens: dict[str, str] = {}

# ── Mermaid schema helpers ────────────────────────────────────────────────────

async def _fetch_schema(ctx) -> dict:
    """Retourne {table_id: [{id, type, label?}]} depuis l'API Grist."""
    tables_resp = await grist_get(ctx, "tables")
    table_ids = [t["id"] for t in tables_resp.get("tables", [])
                 if not t["id"].startswith("_grist_")]
    async def _cols(tid):
        try:
            resp = await grist_get(ctx, f"tables/{tid}/columns")
            return tid, [
                {"id": c["id"], "type": c["fields"].get("type",""),
                 "label": c["fields"].get("label","") or None}
                for c in resp.get("columns", [])
                if not c["id"].startswith("gristHelper_")
            ]
        except Exception:
            return tid, []
    return dict(await asyncio.gather(*[_cols(t) for t in table_ids]))

async def _fetch_doc_structure(ctx) -> dict:
    """Retourne {views, sections} depuis les meta-tables Grist."""
    views_resp, sections_resp, tables_resp = await asyncio.gather(
        grist_get(ctx, "tables/_grist_Views/records"),
        grist_get(ctx, "tables/_grist_Views_section/records"),
        grist_get(ctx, "tables/_grist_Tables/records"),
    )
    table_map = {r["id"]: r["fields"].get("tableId", "") for r in tables_resp.get("records", [])}
    views = [{"id": r["id"], "name": r["fields"].get("name", f"Page {r['id']}")}
             for r in views_resp.get("records", [])
             if not r["fields"].get("name", "").startswith("_")]
    view_ids = {v["id"] for v in views}
    sections = []
    for r in sections_resp.get("records", []):
        f = r["fields"]
        view_id  = f.get("parentId", 0)
        table_id = table_map.get(f.get("tableRef", 0), "")
        if not view_id or not table_id or table_id.startswith("_grist_"): continue
        if view_id not in view_ids: continue  # skip hidden/raw views
        link_ref = f.get("linkSrcSectionRef", 0)
        sections.append({"id": r["id"], "viewId": view_id, "tableId": table_id,
                          "type": f.get("parentKey", "record"),
                          "title": f.get("title") or "",
                          "linkSrcId": link_ref if link_ref else None})
    return {"views": views, "sections": sections}


def _doc_map_to_mermaid(structure: dict, schema: dict) -> str:
    """graph LR complet : pages + sections + tables + FK."""
    SEC_TYPE = {"record": "grid", "detail": "card", "single": "card",
                "chart": "chart", "form": "form", "custom": "widget", "custom.api": "widget"}
    def _safe(s): return re.sub(r"[^A-Za-z0-9_]", "_", s)
    lines = ["graph LR"]
    lines.append("  subgraph DB[Tables]")
    for tid in schema:
        if tid == "Artefacts": continue
        lines.append(f'    T_{_safe(tid)}["{tid}"]')
    lines.append("  end")
    lines.append("")
    for v in structure["views"]:
        vid = v["id"]; vname = v["name"].replace('"', "'")
        secs = [s for s in structure["sections"] if s["viewId"] == vid]
        if not secs: continue
        lines.append(f'  subgraph PG{vid}["{vname}"]')
        for s in secs:
            stype = SEC_TYPE.get(s.get("type", "record"), "grid")
            label = (s.get("title") or s.get("tableId", "?")).replace('"', "'")
            lines.append(f'    S{s["id"]}["{label} - {stype}"]')
        lines.append("  end")
        lines.append("")
    for s in structure["sections"]:
        tid = s.get("tableId")
        if tid and tid != "Artefacts":
            lines.append(f'  S{s["id"]} -.->|data| T_{_safe(tid)}')
    sec_ids = {s["id"] for s in structure["sections"]}
    for s in structure["sections"]:
        lsrc = s.get("linkSrcId")
        if lsrc and lsrc in sec_ids:
            lines.append(f'  S{lsrc} -->|filter| S{s["id"]}')
    for table, cols in schema.items():
        if table == "Artefacts": continue
        for c in cols:
            ctype = c.get("type", "")
            if ctype.startswith("Ref:") or ctype.startswith("RefList:"):
                target = ctype.split(":", 1)[1]
                if target in schema and target != "Artefacts":
                    lines.append(f'  T_{_safe(table)} -->|"{c["id"]}"| T_{_safe(target)}')
    return chr(10).join(lines)


def _pages_to_mermaid(structure: dict) -> str:
    """graph TD des pages et sections uniquement."""
    SEC_TYPE = {"record": "grid", "detail": "card", "single": "card",
                "chart": "chart", "form": "form", "custom": "widget", "custom.api": "widget"}
    sec_ids = {s["id"] for s in structure["sections"]}
    lines = ["graph TD"]
    for v in structure["views"]:
        vid = v["id"]; vname = v["name"].replace('"', "'")
        secs = [s for s in structure["sections"] if s["viewId"] == vid]
        if not secs: continue
        lines.append(f'  subgraph PG{vid}["{vname}"]')
        for s in secs:
            stype = SEC_TYPE.get(s.get("type", "record"), "grid")
            label = (s.get("title") or s.get("tableId", "?")).replace('"', "'")
            lines.append(f'    S{s["id"]}["{label} - {stype}"]')
        lines.append("  end")
    for s in structure["sections"]:
        lsrc = s.get("linkSrcId")
        if lsrc and lsrc in sec_ids:
            lines.append(f'  S{lsrc} -->|filter| S{s["id"]}')
    return chr(10).join(lines)


def _flow_to_mermaid(structure: dict, schema: dict) -> str:
    """graph LR sections -> tables + FK uniquement."""
    SEC_TYPE = {"record": "grid", "detail": "card", "single": "card",
                "chart": "chart", "form": "form", "custom": "widget", "custom.api": "widget"}
    def _s(x): return re.sub(r"[^A-Za-z0-9_]", "_", x)
    lines = ["graph LR", "  subgraph DB[Tables]"]
    for tid in schema:
        if tid == "Artefacts": continue
        lines.append(f'    T_{_s(tid)}["{tid}"]')
    lines.append("  end")
    lines.append("")
    for v in structure["views"]:
        vid = v["id"]; vname = v["name"].replace('"', "'")
        secs = [s for s in structure["sections"]
                if s["viewId"] == vid and s.get("tableId") != "Artefacts"]
        if not secs: continue
        lines.append(f'  subgraph PG{vid}["{vname}"]')
        for s in secs:
            stype = SEC_TYPE.get(s.get("type", "record"), "grid")
            label = (s.get("title") or s.get("tableId", "?")).replace('"', "'")
            lines.append(f'    S{s["id"]}["{label} - {stype}"]')
        lines.append("  end")
        lines.append("")
    for s in structure["sections"]:
        tid = s.get("tableId")
        if tid and tid != "Artefacts":
            lines.append(f'  S{s["id"]} -.->|data| T_{_s(tid)}')
    for table, cols in schema.items():
        if table == "Artefacts": continue
        for c in cols:
            ctype = c.get("type", "")
            if ctype.startswith("Ref:") or ctype.startswith("RefList:"):
                target = ctype.split(":", 1)[1]
                if target in schema and target != "Artefacts":
                    lines.append(f'  T_{_s(table)} -->|"{c["id"]}"| T_{_s(target)}')
    return chr(10).join(lines)


def _build_doc_overview_html(schema: dict, structure: dict) -> str:
    """HTML 3 onglets : Etat des donnees / Pages & Vues / Flux donnees."""
    import base64 as _b64
    def _enc(s): return _b64.b64encode(s.encode('utf-8')).decode('ascii')
    d0 = _enc(_schema_to_mermaid(schema))
    d1 = _enc(_pages_to_mermaid(structure))
    d2 = _enc(_flow_to_mermaid(structure, schema))
    CDN = "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"
    sc  = '</s' + 'cript>'
    return (
        '<!DOCTYPE html><html><head><meta charset="UTF-8"><style>'
        '*{box-sizing:border-box;margin:0;padding:0}'
        'body{font-family:Inter,system-ui,sans-serif;background:#f8fafc;height:100vh;display:flex;flex-direction:column;overflow:hidden}'
        '.tabs{display:flex;border-bottom:1px solid #e2e8f0;background:#fff;padding:0 12px;flex-shrink:0;gap:4px}'
        '.tab{padding:9px 14px;font-size:.78rem;color:#64748b;cursor:pointer;border-bottom:2px solid transparent;transition:all .15s;user-select:none}'
        '.tab.active{color:#3e5de7;border-bottom-color:#3e5de7;font-weight:600}'
        '.tab:hover:not(.active){color:#334155}'
        '.panel{display:none;flex:1;overflow:auto;padding:16px}'
        '.panel.active{display:block}'
        '.mermaid{background:#fff;border-radius:8px;padding:20px;box-shadow:0 1px 4px rgba(0,0,0,.06);overflow:auto}''.mermaid svg{max-width:none!important;height:auto}'
        '</style>'
        '<script>'
        'var _D={"0":"' + d0 + '","1":"' + d1 + '","2":"' + d2 + '"};'
        'var _R={};'
        'function _dec(s){try{return decodeURIComponent(escape(atob(s)));}catch(e){return atob(s);}}'
        'function show(i){'
        '  document.querySelectorAll(".tab").forEach(function(t,j){t.classList.toggle("active",i===j)});'
        '  document.querySelectorAll(".panel").forEach(function(p,j){p.classList.toggle("active",i===j)});'
        '  var el=document.getElementById("d"+i);'
        '  if(!_R[i]){el.textContent=_dec(_D[""+i]);if(typeof mermaid!=="undefined"){mermaid.run({nodes:[el]});_R[i]=1;}}'
        '}'
        + sc +
        '</head><body>'
        '<div class="tabs">'
        '<div class="tab active" onclick="show(0)">\u00c9tat des donn\u00e9es</div>'
        '<div class="tab" onclick="show(1)">Pages &amp; Vues</div>'
        '<div class="tab" onclick="show(2)">Flux donn\u00e9es</div>'
        '</div>'
        '<div id="p0" class="panel active"><div id="d0" class="mermaid"></div></div>'
        '<div id="p1" class="panel"><div id="d1" class="mermaid"></div></div>'
        '<div id="p2" class="panel"><div id="d2" class="mermaid"></div></div>'
        f'<script src="{CDN}" onload="mermaid.initialize({{startOnLoad:false,theme:\'neutral\'}});show(0);">' + sc +
        '</body></html>'
    )


def _schema_to_mermaid(schema: dict) -> str:
    """Génère un erDiagram mermaid depuis un schema {table: [{id, type, label?}]}."""
    TYPE_MAP = {
        "Text": "string", "Numeric": "float", "Int": "int", "Bool": "boolean",
        "Date": "date", "DateTime": "datetime", "Choice": "string",
        "ChoiceList": "string", "Attachments": "blob",
    }
    lines = ["erDiagram"]
    relations = []
    for table, cols in schema.items():
        if table == "Artefacts": continue  # table interne
        entity_lines = []
        for c in cols:
            ctype = c.get("type", "Text")
            if ctype.startswith("Ref:") or ctype.startswith("RefList:"):
                is_list = ctype.startswith("RefList:")
                target = ctype.split(":", 1)[1]
                entity_lines.append(f"    int {c['id']} FK")
                card = "}o--o{" if is_list else "}o--||"
                relations.append(f"  {table} {card} {target} : \"{c['id']}\"")
            else:
                base = ctype.split(":")[0]
                md_type = TYPE_MAP.get(base, "string")
                entity_lines.append(f"    {md_type} {c['id']}")
        if entity_lines:
            lines.append(f"  {table} {{")
            lines.extend(entity_lines)
            lines.append("  }")
    lines.extend(relations)
    return "\n".join(lines)

# ── GUIDAGE — rappels de savoir-faire injectes dans les reponses d ecriture ────

def _apply_next(actions):
    """Analyse des UserActions et retourne un rappel _next contextuel (ou None).

    Remet le savoir-faire au point de decision : apres un AddTable, rappeler
    visibleCol sur les Ref, les donnees exemple, la creation de page."""
    if not isinstance(actions, list):
        return None
    has_add_table = has_ref_col = has_bulk = False
    for a in actions:
        if not isinstance(a, list) or not a:
            continue
        verb = a[0]
        if verb == "AddTable":
            has_add_table = True
            cols = a[2] if len(a) > 2 and isinstance(a[2], list) else []
            for c in cols:
                if isinstance(c, dict) and str(c.get("type", "")).startswith("Ref"):
                    has_ref_col = True
        elif verb == "AddColumn":
            spec = a[3] if len(a) > 3 and isinstance(a[3], dict) else {}
            if str(spec.get("type", "")).startswith("Ref"):
                has_ref_col = True
        elif verb in ("BulkAddRecord", "AddRecord", "BulkAddOrReplaceRecord"):
            has_bulk = True
    tips = []
    if has_add_table and not has_bulk:
        tips.append("ajouter 3-5 lignes exemple (BulkAddRecord) pour rendre l app vivante")
    if has_ref_col:
        tips.append("definir visibleCol sur chaque colonne Ref: "
                    "(UpdateRecord _grist_Tables_column, sinon champ vide dans l UI)")
    if has_add_table:
        tips.append("colonnes derivables (totaux, statuts, jours restants) -> isFormula:true, jamais saisie")
        tips.append("creer la page metier : grist_view_create(table, artefact) ; verifier ensuite context/{token} (_quality)")
    return " ; ".join(tips) if tips else None

_META_TABLE_HINT = ("Tables meta _grist_Views* : ne JAMAIS utiliser grist_records_patch (REST) "
                    "-> crash frontend. Utiliser grist_apply(['UpdateRecord', '_grist_Views_section', id, {...}]).")

def _ref_target(coltype):
    """Retourne la table cible d une colonne Ref:/RefList:, ou None."""
    t = str(coltype or "")
    for pfx in ("RefList:", "Ref:"):
        if t.startswith(pfx):
            return t[len(pfx):]
    return None

_FORMULA_REF_RE = re.compile(r'\$([A-Za-z_][A-Za-z0-9_]*)')

def _validate_formula(formula, colset):
    """Heuristique (PAS de dry-run sandbox) : detecte dans une formule Grist les
    references $Colonne mal casees ou inexistantes vis-a-vis de colset.
    Retourne {valid, corrected_formula|None, issues:[{type:'case'|'unknown',found,correct?/suggestion?}]}.
    - 'case' : la colonne existe avec une autre casse -> SUR, auto-corrigeable (le piege 500 #1).
    - 'unknown' : reference introuvable -> incertain (difflib), a signaler en WARNING seulement."""
    if not formula or not isinstance(formula, str):
        return {"valid": True, "corrected_formula": None, "issues": []}
    variants = {c.lower(): c for c in colset}
    ids = list(colset)
    issues, fixed = [], formula
    for ref in _FORMULA_REF_RE.findall(formula):
        if ref in colset:
            continue
        low = ref.lower()
        if low in variants and variants[low] != ref:
            correct = variants[low]
            issues.append({"type": "case", "found": ref, "correct": correct})
            fixed = re.sub(r'\$' + re.escape(ref) + r'\b', '$' + correct, fixed)
        else:
            near = difflib.get_close_matches(ref, ids, 1, 0.6)
            issues.append({"type": "unknown", "found": ref, "suggestion": near[0] if near else None})
    return {"valid": not any(x["type"] == "case" for x in issues),
            "corrected_formula": fixed if fixed != formula else None,
            "issues": issues}

def _validate_actions(actions, existing_tables, existing_cols=None):
    """Pre-vol : detecte les erreurs frequentes AVANT grist_apply.

    - Ref: vers une table creee PLUS TARD dans le meme batch (ou inexistante) -> sandbox error a l insert
    - formule avec $Colonne mal casee/inconnue (dans une table du batch) -> 500 sandbox opaque
    - formats d action invalides
    - rappelle les visibleCol a definir sur les Ref
    existing_tables : ids des tables deja presentes dans le doc.
    existing_cols (optionnel) : {table: {colId,...}} pour valider aussi les formules
    ajoutees sur des tables PRE-existantes (sinon on ne juge que les tables du batch)."""
    errors, warnings = [], []
    if not isinstance(actions, list):
        return {"ok": False, "errors": ["'actions' doit etre une liste de UserActions"], "warnings": []}
    known = set(existing_tables)  # tables qui existeront au moment de chaque action du batch
    existing_cols = existing_cols or {}
    table_cols = {t: set(cs) for t, cs in existing_cols.items()}  # colset connu par table
    complete = set(existing_cols.keys())  # tables dont on connait TOUTES les colonnes
    formulas = []  # (idx, table, colId, formula) a valider en 2e passe
    for i, a in enumerate(actions):
        if not isinstance(a, list) or not a:
            errors.append(f"action {i}: format invalide (liste [verbe, ...] attendue)")
            continue
        verb = a[0]
        if verb == "AddTable":
            tname = a[1] if len(a) > 1 else None
            cols = a[2] if len(a) > 2 and isinstance(a[2], list) else []
            tcols = table_cols.setdefault(tname, set())
            for c in cols:
                if not isinstance(c, dict):
                    continue
                if c.get("id"):
                    tcols.add(c.get("id"))
                tgt = _ref_target(c.get("type"))
                if tgt and tgt not in known and tgt != tname:
                    errors.append(
                        f"action {i} (AddTable '{tname}'): la colonne '{c.get('id')}' reference la table "
                        f"'{tgt}' qui n'existe pas encore a ce point du batch. Creer '{tgt}' AVANT cette "
                        f"AddTable, ou ajouter la colonne Ref via AddColumn apres. Sinon : sandbox error "
                        f"\"NoneType object has no attribute 'table_id'\" au premier insert.")
                if tgt:
                    warnings.append(f"colonne Ref '{c.get('id')}' de '{tname}' -> penser a definir visibleCol apres creation")
                if c.get("isFormula") or c.get("formula"):
                    formulas.append((i, tname, c.get("id"), c.get("formula")))
            if tname:
                known.add(tname); complete.add(tname)
        elif verb == "AddColumn":
            tname = a[1] if len(a) > 1 else None
            colid = a[2] if len(a) > 2 and isinstance(a[2], str) else None
            spec = a[3] if len(a) > 3 and isinstance(a[3], dict) else {}
            if not colid and isinstance(spec, dict):
                colid = spec.get("id")
            if tname and colid:
                table_cols.setdefault(tname, set()).add(colid)
            tgt = _ref_target(spec.get("type"))
            if tgt and tgt not in known:
                errors.append(f"action {i} (AddColumn sur '{tname}'): reference la table '{tgt}' inexistante a ce point.")
            if tgt:
                warnings.append(f"colonne Ref ajoutee sur '{tname}' -> definir visibleCol")
            if spec.get("isFormula") or spec.get("formula"):
                formulas.append((i, tname, colid, spec.get("formula")))
        elif verb == "RemoveTable":
            tname = a[1] if len(a) > 1 else None
            if tname:
                known.discard(tname)
    # 2e passe : valider les formules quand la colset de la table est COMPLETE
    # (table creee dans ce batch, ou colonnes fournies via existing_cols).
    for (i, tname, colid, formula) in formulas:
        if tname not in complete:
            continue  # colset incomplet -> on ne juge pas (evite les faux positifs)
        res = _validate_formula(formula, table_cols.get(tname, set()))
        for iss in res["issues"]:
            if iss["type"] == "case":
                errors.append(
                    f"action {i} (formule de '{tname}.{colid}'): $%s mal casee -> $%s. "
                    f"Formule corrigee : %s" % (iss["found"], iss["correct"], res["corrected_formula"]))
            elif iss["type"] == "unknown":
                sug = f" (proche : ${iss['suggestion']})" if iss.get("suggestion") else ""
                warnings.append(
                    f"action {i} (formule de '{tname}.{colid}'): $%s introuvable dans '{tname}'%s" %
                    (iss["found"], sug))
    return {"ok": not errors, "errors": errors, "warnings": warnings}

# ── SURVEY MANIFEST : conformité schéma Grist <-> manifest (convergence CEREMA) ─
# Contrat partage (cf. docs/survey-manifest-consumption.md §5) : mapping des types
# canoniques du Survey Manifest vers le type de BASE Grist attendu dans la table Reponses.
_SURVEY_TYPE_TO_GRIST = {
    "likert5": "Int", "choice": "Choice", "choice_list": "ChoiceList",
    "bool": "Bool", "text": "Text", "datetime": "DateTime",
    "rank_place": "Choice", "geojson": "Text", "int": "Int", "numeric": "Numeric",
}
# Colonnes meta/audit tolerees (ecrites par le bridge/runtime, hors questions).
_SURVEY_META_COLS = {"Horodatage", "DureeSecondes", "manifest_version",
                     "_audit_hash", "previous_hash", "integrity_hash"}

def _survey_expected_columns(manifest: dict) -> dict:
    """Survey Manifest -> {colId: type_grist_attendu} pour la table Reponses.
    Inclut les colonnes de questions + les gates (Bool)."""
    cols = {}
    for sec in (manifest.get("sections") or []):
        if not isinstance(sec, dict):
            continue
        gate = sec.get("gate")
        if gate:
            cols[gate] = "Bool"
        for q in (sec.get("questions") or []):
            if not isinstance(q, dict):
                continue
            cid = q.get("colId")
            if cid:
                cols[cid] = _SURVEY_TYPE_TO_GRIST.get(q.get("type", "text"), "Text")
    return cols

def _survey_schema_diff(manifest: dict, actual_cols: dict) -> dict:
    """Diffe le schema attendu (depuis le manifest) vs le schema Grist reel de Reponses.
    Compare le type de BASE (avant ':') pour tolerer Ref:X / DateTime:TZ / Choice sous-types.
    Retourne {ok, schema_diff:{missing, type_mismatch, extra}}."""
    expected = _survey_expected_columns(manifest)
    missing, mismatch = [], []
    for cid, exp in expected.items():
        act = actual_cols.get(cid)
        if act is None:
            missing.append(cid)
            continue
        if str(act).split(":", 1)[0] != exp:
            mismatch.append({"colId": cid, "expected": exp, "actual": act})
    extra = [c for c in actual_cols
             if c not in expected and c not in _SURVEY_META_COLS and not c.startswith("gristHelper_")]
    return {"ok": not missing and not mismatch,
            "schema_diff": {"missing": missing, "type_mismatch": mismatch, "extra": extra}}

async def call_tool(uid_key, mcp_sid, name, args):
    if name == "sessions_list":
        s = registry.list_sessions(uid_key)
        if s:
            # Determine _next based on whether a plan exists in any session
            result = list(s)
            first_token = s[0]["token"] if s else None
            first_ctx = registry.resolve(uid_key, first_token) if first_token else None
            has_plan = bool(first_ctx and first_ctx.project_plan.get("need"))
            plan_status = first_ctx.project_plan.get("status", "not_started") if first_ctx else "not_started"
            if has_plan:
                _next = (f"Plan existant (status={plan_status}) — lire plan/{first_token} "
                         "pour reprendre, puis context/{token} pour etat reel du doc.")
            else:
                _next = ("Nouveau projet — lire docs/qualification pour choisir la categorie, "
                         "puis canvas_wizard(type='input') pour collecter le besoin utilisateur.")
            return {"sessions": result, "_next": _next,
                    "_hint": ("Apres selection : plan/{token} si reprise, docs/qualification si debut. "
                              "Plusieurs sessions ? Passer token=<token> a chaque outil plutot que "
                              "session_select : routage par appel, robuste et parallelisable.")}
        # Debug: lister tous les users connus pour diagnostiquer le mismatch d uid
        all_uids = {u: len(d["sessions"]) for u, d in registry._users.items()}
        return {
            "info": "Aucun widget connecte.",
            "hint": "Ouvrez le widget Grist Coder dans Grist. Connexion automatique.",
            "_debug_caller_uid": uid_key,
            "_debug_all_uids": all_uids,
        }

    if name == "grist_doc_create":
        doc_name = (args.get("name") or "").strip()
        if not doc_name:
            return {"ok": False, "error": "name requis (nom du nouveau document a creer)."}
        prov_key, key_source = _provision_key(uid_key)
        if not prov_key:
            return dict(_erreur_cle_absente(uid_key), ok=False)
        site_url = _provision_site_url(uid_key)
        root = urllib.parse.urlsplit(site_url)
        if not root.netloc:
            return {"ok": False, "error": (
                "URL du site Grist introuvable : aucune session widget active et GRIST_SITE_URL "
                "absent cote serveur. Ouvrir le widget Coder dans Grist, ou definir GRIST_SITE_URL "
                "(ex: https://grist.numerique.gouv.fr).")}
        base = f"{root.scheme}://{root.netloc}"
        ws_id = str(args.get("workspace_id") or os.getenv("GRIST_PROVISION_WORKSPACE_ID", "")).strip()
        org = ""
        phdr = {"Authorization": f"Bearer {prov_key}", "Content-Type": "application/json"}
        async with _grist_client() as c:
            if ws_id:
                # Org du workspace cible : necessaire pour construire l'URL /o/{org}/...
                try:
                    rw = await c.get(f"{base}/api/workspaces/{ws_id}", headers=phdr)
                    rw.raise_for_status()
                    _w = rw.json() or {}
                    org = str((_w.get("org") or {}).get("domain") or "")
                except Exception:
                    org = ""
            else:
                # Pas de workspace impose : on enumere ce que la cle voit reellement.
                # Un seul candidat -> choix evident, on enchaine. Plusieurs -> on rend
                # la main plutot que de creer le doc au mauvais endroit.
                wss = await _provision_workspaces(c, base, phdr,
                                                  os.getenv("GRIST_PROVISION_ORG", "").strip())
                if len(wss) == 1:
                    ws_id, org = str(wss[0]["id"]), wss[0]["org"]
                elif wss:
                    return {"ok": False,
                            "error": "Plusieurs workspaces disponibles : preciser workspace_id.",
                            "workspaces_disponibles": wss,
                            "_next": "grist_doc_create(name=..., workspace_id=<id choisi>)"}
                else:
                    return {"ok": False, "error": (
                        f"Aucun workspace accessible avec cette cle ({key_source}) sur {base}. "
                        "Verifier que la cle est valide et qu'elle a acces a au moins un workspace.")}
            try:
                r = await c.post(f"{base}/api/workspaces/{ws_id}/docs", headers=phdr,
                                 content=json.dumps({"name": doc_name}))
                r.raise_for_status()
                doc_id = (r.text or "").strip().strip('"')
            except httpx.HTTPStatusError as e:
                return {"ok": False, "error": _scrub_secrets(
                    f"Echec creation du document (HTTP {e.response.status_code}) dans le workspace "
                    f"{ws_id} sur {base}. Cle utilisee : {key_source}. "
                    f"Detail: {e.response.text[:200]}")}
            except Exception as e:
                return {"ok": False, "error": _scrub_secrets(f"Echec creation du document : {e}")}
        slug = (re.sub(r'[^a-zA-Z0-9]+', '-', doc_name).strip('-').lower() or "document")
        org = org or (os.getenv("GRIST_PROVISION_ORG", "").strip() or "docs")
        doc_url = f"{base}/o/{org}/{doc_id}/{slug}"
        out = {"ok": True, "doc_id": doc_id, "name": doc_name, "url": doc_url,
               "workspace_id": ws_id, "org": org, "_key_source": key_source,
               "_next": f"Document cree. Ouvrir : {doc_url}"}
        if args.get("add_coder_widget", True):
            # URL avec app_token : sans lui la garde du pod refuse POST /register
            # et le widget s'affiche sans jamais ouvrir de session.
            widget_url = _coder_widget_url(avec_token=True)
            out["coder_widget_url"] = widget_url
            if "localhost" in widget_url or "127.0.0.1" in widget_url:
                out["coder_widget_warning"] = (
                    f"L'URL du widget ({widget_url}) est locale : elle ne fonctionnera que depuis "
                    "cette machine. Definir GRIST_CODER_WIDGET_URL ou PUBLIC_URL pour un pod en ligne.")
            try:
                added = await _provision_coder_widget(base, doc_id, prov_key, widget_url)
                out["coder_widget_added"] = bool(added)
                if not added:
                    out["coder_widget_hint"] = (
                        f"Widget non injecte automatiquement. Dans le doc, ajouter une page "
                        f"'Custom' et coller l'URL {widget_url}.")
            except Exception as e:
                out["coder_widget_added"] = False
                out["coder_widget_hint"] = _scrub_secrets(
                    f"Injection auto du widget echouee ({e}). Ajouter manuellement une page "
                    f"Custom Widget avec l'URL {widget_url}.")
        return out

    if name == "session_open":
        doc_id = (args.get("doc_id") or "").strip()
        if not doc_id:
            return {"error": "doc_id requis (visible dans l URL du document Grist)."}
        cle, source = _provision_key(uid_key)
        if not cle:
            return _erreur_cle_absente(uid_key)
        site = (args.get("site_url") or "").strip().rstrip("/") or _provision_site_url(uid_key)
        root = urllib.parse.urlsplit(site)
        if not root.netloc:
            return {"error": ("Base du site Grist inconnue : fournir site_url, ou definir "
                              "GRIST_SITE_URL cote serveur.")}
        try:
            async with _grist_client() as c:
                r = await c.get(f"{site}/api/docs/{doc_id}",
                                headers={"Authorization": f"Bearer {cle}"})
                if r.status_code in (401, 403):
                    return {"error": f"Acces refuse au document {doc_id} avec cette cle ({source})."}
                if r.status_code == 404:
                    return {"error": f"Document {doc_id} introuvable sur {site}.",
                            "_hint": "Verifier l id du document et le site (ex: .../o/<org>)."}
                r.raise_for_status()
                meta = r.json() or {}
        except Exception as e:
            return {"error": _scrub_secrets(f"Ouverture impossible : {e}")}
        titre = meta.get("name") or doc_id
        registry.provision(uid_key, grist_key=cle, site=site)
        ctx = registry.register_session(uid_key, doc_id, titre, site, grist_key=cle)
        _active_tokens[mcp_sid] = ctx.token
        return {"ok": True, "token": ctx.token, "doc_id": doc_id, "doc_title": titre,
                "site_url": site, "_cle": source, "sans_widget": True,
                "_next": (f"Session ouverte sans navigateur. Passer token='{ctx.token}' aux outils. "
                          "canvas_screenshot et les ecritures meta lourdes de artefact_publish "
                          "demandent en revanche un widget Coder ouvert.")}

    if name == "session_select":
        ctx = registry.resolve(uid_key, args["token"])
        if not ctx: return {"error": f"Token inconnu : {args['token']}"}
        _active_tokens[mcp_sid] = ctx.token
        ctx.touch()
        return {"ok": True, "selected": ctx.meta(),
                "_next": "session_info pour snapshot complet, ou plan/{token} si reprise d un projet existant.",
                "_hint": ("Cet epinglage vaut pour CETTE connexion MCP. Un client qui ne renvoie "
                          "pas l'en-tete Mcp-Session-Id le perd : passer token=... directement a "
                          "chaque outil est plus sur et permet de travailler en parallele.")}

    # Routage de session. Priorite : token PASSE DANS L'APPEL > epinglage de la
    # connexion > unique session ouverte. On NE retombe JAMAIS silencieusement sur la
    # session "la plus recente" du compte : sous un meme uid (widget + connecteur +
    # Claude Code), ca faisait operer un client sur le doc d'un autre.
    explicit = str(args.pop("token", "") or "").strip()
    if explicit:
        if not registry.resolve(uid_key, explicit):
            return {"error": f"Token de session inconnu : {explicit}",
                    "sessions": registry.list_sessions(uid_key),
                    "_next": "sessions_list() pour obtenir un token valide."}
        # SANS effet de bord : on n'epingle pas la connexion. Deux agents qui passent
        # chacun leur token sur le meme credential ne doivent jamais s'ecraser l'un
        # l'autre — c'est tout l'interet du routage par appel.
        token = explicit
    else:
        token = _active_tokens.get(mcp_sid)
    if not token:
        _sess = registry.list_sessions(uid_key)
        if len(_sess) == 1:
            token = _sess[0]["token"]
            _active_tokens[mcp_sid] = token
        elif len(_sess) > 1:
            return {"error": "Plusieurs documents ouverts sous ce compte : preciser la session "
                             "cible. Le plus fiable est de passer token=... a l'outil lui-meme "
                             f"(ex: {name}(token='{_sess[0]['token']}', ...)) — l'epinglage par "
                             "session_select peut etre perdu si le client ne renvoie pas "
                             "l'en-tete Mcp-Session-Id.",
                    "sessions": _sess,
                    "_next": f"Relancer {name} avec token=<token du doc vise>."}
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
                recs = [r for r in data.get("records", [])
                        if r["fields"].get("Nom") != PROJECT_META_NAME]
                info["artefacts_count"] = len(recs)
                info["artefacts"] = [
                    {"nom": r["fields"].get("Nom",""), "type": r["fields"].get("Type",""),
                     "isDoc": bool(r["fields"].get("IsDoc",False))}
                    for r in recs
                ]
                info["hint"] = f"Lire grist-coder://context/{ctx.token} pour snapshot complet"
            except Exception:
                info["artefacts_count"] = 0
        # Cards actives dans le canvas (visibles par l'utilisateur)
        if ctx._active_wizard_cards:
            info["active_cards"] = [
                {"id": cid, "type": ev.get("step", {}).get("type","?"),
                 "async": ev.get("_is_async", False)}
                for cid, ev in ctx._active_wizard_cards.items()
            ]
        # Réponses wizard async en attente
        if ctx._async_wizard_responses:
            info["wizard_responses"] = dict(ctx._async_wizard_responses)
            info["_next"] = (
                "wizard_responses disponibles — traiter les reponses, "
                "puis canvas_wizard_close(card_id=...) pour fermer chaque card traitee."
            )
        return info

    # ── Canvas
    if name == "canvas_select":
        art_nom = args["art_nom"]
        if not ctx.doc_id or not ctx.site_url:
            return {"error": "Pas de document connecte"}
        if art_nom == PROJECT_META_NAME:
            return {"error": f"'{PROJECT_META_NAME}' est un record meta interne, pas un artefact selectionnable."}
        try:
            sql_resp = await grist_post(ctx, "sql",
                {"sql": "SELECT id, Type, Code FROM Artefacts WHERE Nom = ?", "args": [art_nom]})
            recs = sql_resp.get("records", [])
            if not recs:
                return {"error": f"Artefact '{art_nom}' introuvable"}
            rec = recs[0]["fields"]
            art_id   = rec["id"]
            art_type = rec.get("Type", "html")
            code     = rec.get("Code", "") or ""
            ctx.canvas           = code
            ctx.current_art_id   = art_id
            ctx.current_art_nom  = art_nom
            ctx.current_art_type = art_type
            # Push art_select SSE to widget — switches dropdown without saving
            _push(uid_key, {"type": "art_select", "token": ctx.token,
                            "art_nom": art_nom, "art_id": art_id, "art_type": art_type})
            return {"ok": True, "art_nom": art_nom, "art_id": art_id, "art_type": art_type,
                    "code_length": len(code), "code": code,
                    "_next": "canvas_read() pour relire, canvas_write() pour modifier, canvas_patch() pour ajuster."}
        except Exception as e:
            return {"error": f"Erreur lecture artefact: {e}"}

    if name == "canvas_read":
        # If canvas was written in-memory (e.g. by canvas_write) and not yet saved to Grist,
        # return the in-memory version to avoid overwriting it with empty Grist DB value.
        if not ctx.canvas and ctx.current_art_id and ctx.doc_id and ctx.site_url:
            try:
                sql_resp = await grist_post(ctx, "sql",
                    {"sql": "SELECT Code FROM Artefacts WHERE id = ?", "args": [ctx.current_art_id]})
                recs = sql_resp.get("records", [])
                if recs:
                    code = recs[0]["fields"].get("Code", "") or ""
                    if code:
                        ctx.canvas = code
            except Exception:
                pass
        return ctx.canvas or ""

    if name == "canvas_write":
        code     = args["code"]
        sha      = hashlib.sha1(code.encode()).hexdigest()[:8]
        art_nom  = args.get("art_nom") or ctx.current_art_nom or f"Draft_{sha}"
        art_type = args.get("art_type") or _detect_type(code)
        art_id   = int(args["art_id"]) if args.get("art_id") else ctx.current_art_id
        is_new   = False

        # Lookup or create artefact in Grist — use SQL POST (no URL-encoding issues)
        explicit_type = bool(args.get("art_type"))  # true only if LLM/widget explicitly set it
        if ctx.doc_id and ctx.site_url:
            try:
                sql_resp = await grist_post(ctx, "sql",
                    {"sql": "SELECT id, Type FROM Artefacts WHERE Nom = ?", "args": [art_nom]})
                recs = sql_resp.get("records", [])
                if recs:
                    art_id   = recs[0]["fields"]["id"]
                    old_type = recs[0]["fields"].get("Type", "html")
                    # Only patch type if caller explicitly provided it — never from auto-detect
                    if explicit_type and old_type != art_type:
                        await grist_patch(ctx, "tables/Artefacts/records",
                                          {"records": [{"id": art_id, "fields": {"Type": art_type}}]})
                    else:
                        art_type = old_type  # keep existing type untouched
                else:
                    # New artefact: create with auto-detected or explicit type
                    created = await grist_post(ctx, "tables/Artefacts/records",
                                               {"records": [{"fields": {"Nom": art_nom, "Type": art_type, "Code": ""}}]})
                    art_id  = created["records"][0]["id"]
                    is_new  = True
            except Exception as _e:
                pass  # Grist unavailable — widget will handle save

        ctx.canvas           = code
        ctx.current_art_id   = art_id
        ctx.current_art_nom  = art_nom
        ctx.current_art_type = art_type
        ctx.history.appendleft({"ts": time.time(), "sha": sha, "op": "write"})
        ev = {"type": "canvas_updated", "token": ctx.token, "sha": sha,
              "art_nom": art_nom, "art_type": art_type, "code": code}
        if art_id: ev["art_id"] = art_id
        if is_new: ev["is_new"] = True
        _push(uid_key, ev)
        _notify_resource(uid_key, f"grist-coder://context/{ctx.token}")
        _next = "canvas_screenshot pour valider le rendu."
        if is_new:
            _next = "canvas_screenshot pour valider, puis grist_view_create pour creer la page associee."
        sortie_w = {"ok": True, "sha": sha, "art_nom": art_nom, "art_type": art_type,
                    "art_id": art_id, "created": is_new, "_next": _next}
        # Diagnostic du rendu declenche par cette ecriture. L'agent apprend dans la
        # MEME reponse si son code s'execute — sans capture d'ecran ni intervention.
        _diag = _lire_diag(await _attendre_diag(uid_key, ctx))
        if _diag:
            sortie_w["diagnostic"] = _diag
            sortie_w["_next"] = _diag.pop("_next")
        # Un artefact qui importe des paquets npm rend BLANC en previsualisation :
        # le navigateur ne resout pas les imports. Il sera bundle a la publication.
        # On le dit ici plutot que de laisser un canvas vide inexplique.
        _pk = imports_npm(code)
        if _pk:
            sortie_w["imports_npm"] = _pk
            sortie_w["avertissement_preview"] = (
                "Cet artefact importe des paquets npm : la PREVISUALISATION restera blanche "
                "(le navigateur ne resout pas les imports). Il sera bundle automatiquement a "
                "artefact_publish, et c'est la qu'on verra le rendu reel.")
            sortie_w["_next"] = ("artefact_publish pour bundler et voir le rendu. "
                                 "canvas_screenshot ne montrera rien d'utile avant.")
        return sortie_w

    if name == "canvas_patch":
        old, new = args["old_str"], args["new_str"]
        canvas = ctx.canvas

        # Strategy 1 — exact match (fast path)
        idx = canvas.find(old)
        n_matches = canvas.count(old) if idx >= 0 else 0

        # Strategy 2 — normalize line endings (CRLF/LF) on both sides
        if idx < 0:
            canvas_lf = canvas.replace("\r\n", "\n").replace("\r", "\n")
            old_lf    = old.replace("\r\n", "\n").replace("\r", "\n")
            new_lf    = new.replace("\r\n", "\n").replace("\r", "\n")
            idx2 = canvas_lf.find(old_lf)
            if idx2 >= 0:
                n2 = canvas_lf.count(old_lf)
                if n2 > 1:
                    return {"error": f"Fragment trouve {n2} fois (apres normalisation des sauts de ligne) — elargir le contexte pour le rendre unique."}
                canvas, old, new = canvas_lf, old_lf, new_lf
                idx, n_matches = idx2, 1

        # Strategy 3 — normalize trailing whitespace on each line
        if idx < 0:
            def _strip_trailing(s):
                return "\n".join(line.rstrip() for line in s.split("\n"))
            canvas_t = _strip_trailing(canvas)
            old_t    = _strip_trailing(old)
            idx3 = canvas_t.find(old_t)
            if idx3 >= 0:
                n3 = canvas_t.count(old_t)
                if n3 > 1:
                    return {"error": f"Fragment trouve {n3} fois (apres normalisation des espaces) — elargir le contexte."}
                canvas, old = canvas_t, old_t
                idx, n_matches = idx3, 1

        # Strategy 4 — leading-whitespace tolerant (re-indent old_str to match canvas)
        if idx < 0 and old.strip():
            old_lines = old.split("\n")
            first_nonblank = next((l for l in old_lines if l.strip()), "")
            if first_nonblank:
                core = first_nonblank.lstrip()
                old_indent = first_nonblank[:len(first_nonblank) - len(first_nonblank.lstrip())]
                for ln in canvas.split("\n"):
                    if ln.lstrip() == core and ln != first_nonblank:
                        canvas_indent = ln[:len(ln) - len(ln.lstrip())]
                        if canvas_indent.startswith(old_indent):
                            delta = canvas_indent[len(old_indent):]
                            old_reindent = "\n".join((delta + l) if l.strip() else l for l in old_lines)
                            new_reindent = "\n".join((delta + l) if l.strip() else l for l in new.split("\n"))
                            idx4 = canvas.find(old_reindent)
                            if idx4 >= 0 and canvas.count(old_reindent) == 1:
                                old, new = old_reindent, new_reindent
                                idx, n_matches = idx4, 1
                                break

        # Strategy 5 — line-based fuzzy block match (≥92% similarity, single best)
        fuzzy_warning = None
        if idx < 0 and old.strip():
            import difflib
            old_lines_list = old.split("\n")
            canvas_lines_list = canvas.split("\n")
            n_old = len(old_lines_list)
            best_ratio = 0.0
            best_start = -1
            # Slide window of size n_old over canvas_lines
            for i in range(len(canvas_lines_list) - n_old + 1):
                window = "\n".join(canvas_lines_list[i:i + n_old])
                ratio = difflib.SequenceMatcher(None, old, window, autojunk=False).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_start = i
                    if ratio == 1.0:
                        break
            if best_ratio >= 0.92 and best_start >= 0:
                # Compute char index of best window in canvas
                char_idx = sum(len(l) + 1 for l in canvas_lines_list[:best_start])
                matched = "\n".join(canvas_lines_list[best_start:best_start + n_old])
                old = matched
                idx = char_idx
                n_matches = 1
                fuzzy_warning = f"match approximatif ({int(best_ratio * 100)}% similarite) — verifier le resultat"

        if idx < 0:
            # Fuzzy hint: find the 3 most similar lines in the canvas
            import difflib
            old_first_line = next((l for l in old.split("\n") if l.strip()), old[:80])
            canvas_lines = [l for l in canvas.split("\n") if l.strip()]
            close = difflib.get_close_matches(old_first_line, canvas_lines, n=3, cutoff=0.6)
            hint = ""
            if close:
                hint = " | Lignes similaires : " + " ;; ".join(repr(l[:80]) for l in close)
            return {"error": f"Fragment introuvable : {old[:80]!r}{hint}",
                    "_hint": "Verifier indentation/sauts de ligne. Appeler canvas_read() pour relire le contenu exact."}

        if n_matches > 1:
            return {"error": f"Fragment trouve {n_matches} fois — elargir le contexte (old_str) pour le rendre unique."}

        # Apply the patch
        ctx.canvas = canvas[:idx] + new + canvas[idx + len(old):]
        sha = hashlib.sha1(ctx.canvas.encode()).hexdigest()[:8]
        ctx.history.appendleft({"ts": time.time(), "sha": sha, "op": "patch"})
        _push(uid_key, {"type": "canvas_patched", "token": ctx.token, "sha": sha,
              "art_nom": ctx.current_art_nom, "art_type": ctx.current_art_type,
              "art_id": ctx.current_art_id, "code": ctx.canvas})
        _notify_resource(uid_key, f"grist-coder://context/{ctx.token}")
        result = {"ok": True, "sha": sha}
        if fuzzy_warning:
            result["_warning"] = fuzzy_warning
        return result

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
        # Cancel any prior pending screenshot waiter for this token to avoid
        # orphaning futures when the LLM calls canvas_screenshot rapidly in succession.
        prior = _screenshot_waiters.pop(ctx.token, None)
        if prior and not prior.done():
            prior.cancel()
        loop = asyncio.get_event_loop()
        fut  = loop.create_future()
        _screenshot_waiters[ctx.token] = fut
        _push(uid_key, {"type": "screenshot_request", "token": ctx.token})
        try:
            image_b64 = await asyncio.wait_for(fut, timeout=15.0)
            raw = image_b64.split(",")[1] if "," in image_b64 else image_b64
            mime = "image/jpeg" if image_b64.startswith("data:image/jpeg") else "image/png"
            return {"ok": True, "_image_b64": raw, "mime": mime}
        except asyncio.TimeoutError:
            return {"error": "Timeout 15s : widget ferme ou iframe vide."}
        finally:
            # Only pop if this future is still the registered one (avoid clobbering
            # a newer waiter that took our slot).
            if _screenshot_waiters.get(ctx.token) is fut:
                _screenshot_waiters.pop(ctx.token, None)

    if name == "canvas_type":
        art_type = args.get("type","html")
        valid = ["html","react","grist","markdown","mermaid","python","sql","svg","app"]
        if art_type not in valid:
            return {"error": f"Type invalide. Valeurs: {valid}"}
        art_id = ctx.current_art_id
        if not art_id:
            return {"error": "Aucun artefact selectionne. Appeler session_select puis onArtSelect."}
        await grist_patch(ctx, f"tables/Artefacts/records",
                          {"records": [{"id": art_id, "fields": {"Type": art_type}}]})
        _push(uid_key, {"type": "type_changed", "token": ctx.token, "artType": art_type})
        return {"ok": True, "type": art_type, "art_id": art_id}

    # ── Wizard
    if name == "canvas_wizard":
        step = args.get("step", {})
        if isinstance(step, str):
            try: step = json.loads(step)
            except (json.JSONDecodeError, TypeError): pass
        # Auto-génération depuis le schema/structure Grist si source= défini
        source = step.get("source")
        if source in ("schema", "doc-map", "doc-overview") and not step.get("code"):
            try:
                schema    = await _fetch_schema(ctx)
                structure = await _fetch_doc_structure(ctx)
                step = dict(step)
                if source == "doc-overview":
                    step["code"]      = _build_doc_overview_html(schema, structure)
                    step["code_type"] = "html"
                    step["bridge"]    = False
                    step.setdefault("height", 520)
                elif source == "doc-map":
                    step["code"]      = _doc_map_to_mermaid(structure, schema)
                    step["code_type"] = "mermaid"
                    step.setdefault("height", 460)
                else:
                    step["code"]      = _schema_to_mermaid(schema)
                    step["code_type"] = "mermaid"
                    step.setdefault("height", 420)
                step.setdefault("type", "preview")
            except Exception as e:
                step = dict(step)
                step.setdefault("type", "info")
                step["content"] = f"Erreur génération {source} : {e}"
        step_type = step.get("type", "info")
        interactive = step_type in ("choice", "form", "confirm", "input", "preview", "data-import") or (
            step_type == "info" and (
                step.get("actions") or step.get("choices") or
                step.get("fields") or step.get("input")
            )
        )
        is_async = bool(step.get("async"))
        timeout  = float(step.get("timeout", 300))
        default  = step.get("default")  # valeur par défaut si timeout
        step_id  = step.get("id") or uuid.uuid4().hex[:8]
        ev = {"type": "wizard_step", "token": ctx.token, "step": step}
        ctx._active_wizard_cards[step_id] = {**ev, "_is_async": is_async}
        _push(uid_key, ev)
        # Mode non-bloquant : non-interactif OU async explicite
        if not interactive or is_async:
            result = {"ok": True, "status": "async" if is_async else "displayed", "step_id": step_id}
            if is_async:
                result["_next"] = (
                    "Card affichee — LLM libre de continuer d autres appels. "
                    "Appeler session_info pour lire wizard_responses quand l utilisateur repond."
                )
            return result
        # Mode bloquant standard
        event = asyncio.Event()
        ctx._wizard_events[step_id] = event
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            # Lire la reponse de CE step (pas la tete de deque, qui peut etre celle d un autre wizard)
            resp_data = ctx._async_wizard_responses.pop(step_id, None)
            resp = dict(resp_data) if resp_data else {"status": "no_response", "step_id": step_id}
            _enrich_wizard_response(resp, step_id, step_type, ctx)
            return resp
        except asyncio.TimeoutError:
            if default is not None:
                resp = {"status": "timeout_default", "step_id": step_id,
                        "values": default, "source": "default"}
                _enrich_wizard_response(resp, step_id, step_type, ctx)
                return resp
            return {"status": "timeout", "step_id": step_id,
                    "hint": "L utilisateur n a pas repondu dans le delai imparti."}
        finally:
            ctx._wizard_events.pop(step_id, None)
            ctx._active_wizard_cards.pop(step_id, None)
            ctx._async_wizard_responses.pop(step_id, None)

    if name == "canvas_wizard_close":
        card_id = args.get("card_id")
        if card_id:
            ctx._active_wizard_cards.pop(card_id, None)
            ctx._async_wizard_responses.pop(card_id, None)
        else:
            ctx._active_wizard_cards.clear()
            ctx._async_wizard_responses.clear()
        _push(uid_key, {"type": "wizard_close", "token": ctx.token,
                        **({"card_id": card_id} if card_id else {})})
        return {"ok": True}

    # ── Plan meta-control
    if name == "plan_update":
        prev_status = ctx.project_plan.get("status")
        prev_need = ctx.project_plan.get("need")
        # MCP args may arrive as JSON strings from some clients (Claude Code)
        # — parse list/dict fields that should be structured
        for k in ("tables", "artefacts", "pages", "integrations", "decisions"):
            v = args.get(k)
            if isinstance(v, str):
                try: args[k] = json.loads(v)
                except (json.JSONDecodeError, TypeError): pass
        for k, v in args.items():
            if v is not None:
                ctx.project_plan[k] = v
        ctx.project_plan["updated_at"] = time.time()
        plan = ctx.project_plan
        status = plan.get("status", "qualifying")
        # Persist project need to the Artefacts table (the only piece of state
        # that can't be inferred from live Grist data). Fire-and-forget.
        new_need = plan.get("need")
        if new_need and new_need != prev_need:
            asyncio.create_task(_write_project_meta(ctx, new_need))
        # Record timeline entry on status transition or first plan update
        if status != prev_status or not ctx.plan_history:
            summary_parts = []
            if args.get("need"): summary_parts.append(f"need: {str(args['need'])[:60]}")
            if args.get("tables"): summary_parts.append(f"{len(args['tables'])} tables")
            if args.get("artefacts"): summary_parts.append(f"{len(args['artefacts'])} artefacts")
            if args.get("pages"): summary_parts.append(f"{len(args['pages'])} pages")
            if args.get("notes"): summary_parts.append(f"note: {str(args['notes'])[:50]}")
            ctx.plan_history.appendleft({
                "ts": time.time(),
                "status": status,
                "from": prev_status,
                "summary": " · ".join(summary_parts) or "update",
            })
        _notify_resource(uid_key, f"grist-coder://plan/{ctx.token}")
        # Auto-push/update progress card dans le wizard overlay (single source of truth)
        prog_step = _plan_progress_step(ctx)
        interactive = bool(args.get("actions") or args.get("input"))
        if args.get("actions"): prog_step["actions"] = args["actions"]
        if args.get("input"):   prog_step["input"]   = args["input"]
        prog_ev = {"type": "wizard_step", "token": ctx.token, "step": prog_step}
        ctx._active_wizard_cards["ctx-plan-progress"] = {**prog_ev, "_is_async": True}
        _push(uid_key, prog_ev)
        # Update context and notify tools/list change if status changed
        prev_context = ctx.current_context
        ctx.current_context = status if status in CONTEXT_TOOLS else "qualifying"
        if ctx.current_context != prev_context:
            _push(uid_key, {"type": "context_changed", "token": ctx.token,
                            "context": ctx.current_context})
            _push(uid_key, {"type": "mcp_notification",
                            "method": "notifications/tools/list_changed", "params": {}})
        _next_resources = {
            "qualifying":  ["docs/qualification", "docs/wizard"],
            "assessing":   [f"context/{ctx.token}", "docs/schema"],
            "designing":   ["docs/schema", "docs/artefacts", "docs/playbook"],
            "building":    [f"context/{ctx.token}", "docs/artefacts", "docs/formulas"],
            "verifying":   [f"context/{ctx.token}", f"code/{ctx.token}"],
            "done":        [],
        }.get(status, [])
        if not interactive:
            return {"ok": True, "status": status, "plan_keys": list(plan.keys()),
                    "_next_resources": _next_resources,
                    "_next": f"Lire : {', '.join(_next_resources)}" if _next_resources else "Plan termine."}
        timeout = float(args.get("timeout", 300))
        event = asyncio.Event()
        ctx._wizard_events["ctx-plan-progress"] = event
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            resp = ctx._async_wizard_responses.pop("ctx-plan-progress", None)
            return resp or {"status": "no_response"}
        except asyncio.TimeoutError:
            return {"status": "timeout"}
        finally:
            ctx._wizard_events.pop("ctx-plan-progress", None)
            ctx._async_wizard_responses.pop("ctx-plan-progress", None)

    # ── Context panel
    if name == "canvas_context_update":
        card_id = args.get("card_id")
        panel = {k: args[k] for k in ("title", "sections", "progress", "actions", "input") if k in args}
        interactive = bool(args.get("actions") or args.get("input"))
        if card_id:
            # Push as independent named wizard card
            step = {"id": card_id, "type": "info", **panel}
            if interactive:
                step["type"] = "choice" if args.get("actions") else "input"
            tok = uuid.uuid4().hex[:8]
            _push(uid_key, {"type": "wizard_step", "token": ctx.token,
                            "step": step, "wizard_token": tok})
        else:
            # Push as ctx-plan card (same surface as plan_update)
            _push(uid_key, {"type": "context_update", "token": ctx.token, "panel": panel})
        if not interactive:
            return {"ok": True, **({"card_id": card_id} if card_id else {})}
        timeout = float(args.get("timeout", 300))
        ev_key = card_id or "ctx-plan"
        event = asyncio.Event()
        ctx._wizard_events[ev_key] = event
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            resp = ctx.wizard_responses[0] if ctx.wizard_responses else None
            return resp or {"status": "no_response"}
        except asyncio.TimeoutError:
            return {"status": "timeout"}
        finally:
            ctx._wizard_events.pop(ev_key, None)

    # ── Chat reply
    if name == "chat_reply":
        message = args.get("message", "").strip()
        if not message:
            return {"error": "message vide"}
        wait    = args.get("wait", False)
        timeout = float(args.get("timeout", 600))
        ts = time.time()
        ctx._chat_history.append({"role": "assistant", "content": message, "ts": ts})
        _push(uid_key, {"type": "chat_message", "token": ctx.token,
                        "role": "assistant", "content": message, "ts": ts})
        if not wait:
            return {"ok": True}
        # wait=True : bloquer jusqu'à la prochaine réponse user (même mécanique que wait_for_chat)
        loop = asyncio.get_event_loop()
        fut  = loop.create_future()
        _chat_waiters[uid_key] = fut
        try:
            text = await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
            return {"text": text, "ts": time.time()}
        except asyncio.TimeoutError:
            return {"status": "timeout"}
        finally:
            _chat_waiters.pop(uid_key, None)

    # ── Wait for chat (boucle autonome)
    if name == "wait_for_chat":
        blocking = args.get("blocking", True)
        timeout  = float(args.get("timeout", 600))
        if not blocking:
            last = ctx._chat_history[-1] if ctx._chat_history else None
            return {"ok": True, "blocking": False, "last_message": last}
        loop = asyncio.get_event_loop()
        fut  = loop.create_future()
        _chat_waiters[uid_key] = fut
        try:
            text = await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
            return {"text": text, "ts": time.time()}
        except asyncio.TimeoutError:
            return {"status": "timeout"}
        finally:
            _chat_waiters.pop(uid_key, None)

    # ── Subagent call via sampling
    if name == "subagent_call":
        role      = args.get("role", "assistant")
        task      = args.get("task", "")
        context   = args.get("context", "")
        max_tok   = int(args.get("max_tokens", 2048))
        ROLE_PROMPTS = {
            "data-architect": (
                "Tu es un architecte de donnees expert Grist. "
                "Tu analyses les structures, proposes des schemas relationnels optimaux, "
                "des types de colonnes (Text/Numeric/Date/Bool/Choice/Ref:Table) et des formules Grist natives. "
                "IMPORTANT : Reponds TOUJOURS en JSON valide avec ces cles exactes :\n"
                '{"tables":[{"name":"X","columns":[{"id":"Y","type":"Z","label":"L","formula":""}]}],'
                '"artefacts":[{"name":"X","type":"react|html|grist"}],'
                '"pages":[{"name":"X","table":"Y","type":"grid|master-detail|dashboard"}],'
                '"notes":"considerations architecturales cles"}'
            ),
            "ui-designer": (
                "Tu es un designer UI expert en artefacts Grist (HTML/CSS/JS/React). "
                "Tu generes des interfaces utilisateur elegantes, responsives et fonctionnelles. "
                "FRAMEWORK : utilise exclusivement le DSFR (Systeme de Design de l Etat) — "
                "classes fr-* (fr-container, fr-grid-row, fr-col-*, fr-card, fr-table, fr-btn, "
                "fr-alert, fr-badge, fr-tag, fr-input-group, fr-callout, fr-tile). "
                "PAS de Tailwind, PAS de CSS custom sauf ajustements mineurs. "
                "Pour un composant DSFR inconnu, chercher dans la collection Albert DSFR (id:142091). "
                "INTERACTIVITE OBLIGATOIRE : un artefact d application n est JAMAIS un affichage passif. "
                "Des que l utilisateur doit agir sur les donnees, prevois des interactions REELLES cablees au "
                "bridge Grist : formulaire d ajout (grist.docApi.applyUserActions [['BulkAddRecord',table,[null],{...}]]), "
                "edition en ligne (['UpdateRecord',table,rowId,{...}]), chargement via grist.docApi.fetchTable, "
                "re-render via grist.onRecords. Depuis le navigateur : BulkAddRecord (pas BulkAddOrReplaceRecord). "
                "IMPORTANT : Reponds TOUJOURS en JSON valide avec ces cles exactes :\n"
                '{"artefacts":[{"name":"X","type":"react|html","description":"role UX","dsfr_components":["fr-card","fr-table"],'
                '"interactions":[{"kind":"add|edit|filter|delete","table":"T","fields":["col1"],"action":"BulkAddRecord|UpdateRecord"}]}],'
                '"pages":[{"name":"X","widgets":["grid","custom"]}],'
                '"styles":{"palette":"DSFR default (#000091/#e3e3fd)","layout":"fr-grid-row"},'
                '"notes":"decisions design cles + justification des interactions"}'
            ),
            "page-architect": (
                "Tu es un architecte de pages Grist expert. "
                "Tu proposes des layouts de pages optimaux selon le besoin metier. "
                "Tu choisis les bons scenarios (dashboard/fiche/master-detail/kanban/calendar), "
                "les liaisons entre sections (linkSrcSectionRef), les widgets (grille/custom/chart). "
                "IMPORTANT : Reponds TOUJOURS en JSON valide avec ces cles exactes :\n"
                '{"pages":[{"name":"X","scenario":"dashboard|fiche|master-detail|kanban|calendar",'
                '"table":"X","widgets":[{"type":"grid|custom|chart","linked_to":"section?"}],'
                '"artefact":"NomArtefact?","notes":"pourquoi ce layout"}],'
                '"sequence":"ordre de creation recommande",'
                '"notes":"decisions architecturales cles"}'
            ),
            "data-analyst": (
                "Tu es un analyste de donnees. "
                "Tu interpretes les donnees Grist, generes des requetes SQL, "
                "des formules d agregation et des insights metier."
            ),
            "integrator": (
                "Tu es un expert en integration. "
                "Tu concois des webhooks, des flux de donnees, des connexions entre services "
                "et des automatisations. Tu proposes des architectures simples et robustes."
            ),
            "assistant": (
                "Tu es un assistant IA expert en developpement d applications Grist. "
                "Tu reponds de maniere concise et actionnable."
            ),
            "ux-navigator": (
                "Tu es un compositeur d experience utilisateur (UX Navigator) pour applications Grist. "
                "Tu analyses la situation courante du document et la reponse utilisateur recente "
                "pour proposer le step wizard suivant le plus pertinent.\n\n"
                "WIZARD STEP TYPES DISPONIBLES :\n"
                "- input       : collecte texte libre (suggestions=chips rapides)\n"
                "- choice      : selection parmi options visuelles (choices=[{id,icon,label,desc}])\n"
                "- form        : formulaire structure (fields=[{id,type,label,required,options}])\n"
                "- confirm     : validation markdown + boutons (content, actions=[{id,label,style}])\n"
                "- progress    : liste avancement non-bloquante\n"
                "- data-import : fetch API externe + apercu + import Grist direct\n"
                "- preview     : iframe live du code + actions contextuelles\n\n"
                "RESSOURCES COMPOSANTS DISPONIBLES (a recommander selon le contexte) :\n"
                "- grist-coder://docs/services-geo  : Leaflet, BAN API geocoding, OSM — cartes interactives\n"
                "- grist-coder://docs/services-data : SIRENE, DVF, data.gouv — import donnees publiques\n"
                "- grist-coder://docs/services-ai   : patterns IA (sync, webhook async, bridge, subagent)\n"
                "- grist-coder://docs/artefacts     : templates HTML/React/Grist, composants standard\n"
                "- grist-coder://docs/wizard        : reference step types + exemples complets\n"
                "- grist-coder://playbook/kanban    : layout kanban drag-drop\n"
                "- grist-coder://playbook/calendar  : FullCalendar CDN\n"
                "- grist-coder://examples/{domain}  : patterns metier (crm/rh/stock/projets/immobilier/restaurant/formation/association)\n\n"
                "LOGIQUE DE CHOIX :\n"
                "- Colonnes adresse/lat/lng detectees -> proposer vue carte (docs/services-geo)\n"
                "- Donnees a importer depuis API externe -> data-import + docs/services-data\n"
                "- Tables sans artefacts -> choice entre types de vues (dashboard/fiche/liste)\n"
                "- Artefacts sans pages -> confirm creation de page\n"
                "- Besoin metier identifie -> orienter vers examples/{domain} ou playbook adapte\n"
                "- Reponse utilisateur ouvre une nouvelle piste -> enchaîner avec le step logique suivant\n\n"
                "FORMAT DE REPONSE JSON STRICT :\n"
                '{"next_step": {<step canvas_wizard complet, pret a passer directement>}, '
                '"resources_a_lire": ["grist-coder://..."], '
                '"reasoning": "pourquoi ce choix en 1-2 phrases"}'
            ),
        }
        system = ROLE_PROMPTS.get(role, ROLE_PROMPTS["assistant"])
        if ctx.doc_title:
            system += f"\n\nDocument courant : {ctx.doc_title}"
        # ux-navigator : injection automatique de l etat complet du doc
        if role == "ux-navigator":
            nav_state = {
                "tables":           list(ctx.project_plan.get("tables", [])) or "(non charge)",
                "artefacts":        [a.get("nom") for a in (ctx.project_plan.get("artefacts") or [])],
                "pages":            list(ctx.project_plan.get("pages", [])) or "(non charge)",
                "plan_status":      ctx.project_plan.get("status", "qualifying"),
                "wizard_responses": dict(ctx._async_wizard_responses) if ctx._async_wizard_responses else {},
                "active_cards":     list(ctx._active_wizard_cards.keys()),
            }
            system += f"\n\nETAT ACTUEL DU DOCUMENT :\n{json.dumps(nav_state, ensure_ascii=False, indent=2)}"
        if context:
            system += f"\n\nContexte supplementaire :\n{context}"
        msgs = [{"role": "user", "content": {"type": "text", "text": task}}]
        caps = _client_capabilities.get(uid_key, {})
        sampling_ok = "sampling" in caps and uid_key in _mcp_client_sids
        if sampling_ok:
            try:
                text = await _do_sample(uid_key, msgs, system, max_tok)
                result = {"role": role, "response": text}
                if role in ("data-architect", "ui-designer", "page-architect", "ux-navigator"):
                    try:
                        m = re.search(r'\{[\s\S]*\}', text)
                        if m:
                            result["structured"] = json.loads(m.group(0))
                    except Exception:
                        pass
                return result
            except Exception as e:
                pass  # Fallback si sampling échoue en cours de route
        # ── Fallback : pas de sampling → retourner contexte pour auto-exécution par le LLM
        return {
            "fallback_mode": True,
            "role": role,
            "system_prompt": system,
            "task": task,
            "instruction": (
                f"ENTRER EN MODE ROLE : '{role}'. "
                f"1. Suspendre le role d orchestrateur. "
                f"2. Adopter integralement l identite, les competences et la perspective decrites dans system_prompt. "
                f"3. Executer la tache (champ 'task') en restant strictement dans ce role — pas de meta-commentaire, pas de reference au LLM principal. "
                f"4. Produire la reponse structuree attendue pour ce role (JSON si structured=True, texte sinon). "
                f"5. FIN DU ROLE '{role}' — reprendre le role d orchestrateur et continuer le flux principal avec le resultat obtenu."
            )
        }

    # ── Artefact
    if name == "artefact_init":
        _next_init = "canvas_write(code, art_nom, art_type) pour creer le premier artefact."
        try:
            tables_data = await grist_get(ctx, "tables")
            if "Artefacts" in [t["id"] for t in tables_data.get("tables", [])]:
                cols = await grist_get(ctx, "tables/Artefacts/columns")
                return {"ok": True, "status": "exists",
                        "columns": [c["id"] for c in cols.get("columns", [])],
                        "_next": _next_init}
        except Exception:
            pass
        try:
            await grist_post(ctx, "tables", ARTEFACTS_TABLE_DEF)
            return {"ok": True, "status": "created",
                    "columns": ["Nom","Type","Code","Description","Dependencies",
                                "IsDoc","Output","UpdatedAt"],
                    "_next": _next_init}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Grist lecture
    if name == "grist_schema":
        table_id = args.get("table_id")
        _next_schema = "lire docs/artefacts avant canvas_write, ou examples/{domain} si domaine identifie."
        if table_id:
            cols = await grist_get(ctx, f"tables/{table_id}/columns")
            return {"table_id": table_id, "columns": cols.get("columns", []), "_next": _next_schema}
        result = await grist_get(ctx, "tables")
        result["_next"] = _next_schema
        return result

    if name == "grist_records":
        lim  = int(args.get("limit", 50))
        off  = int(args.get("offset", 0))
        path = f"tables/{args['table_id']}/records?limit={lim}"
        if off:               path += f"&offset={off}"
        if "filter" in args:  path += f"&filter={urllib.parse.quote(json.dumps(args['filter']))}"
        if "sort"   in args:  path += f"&sort={urllib.parse.quote(args['sort'])}"
        data    = await grist_get(ctx, path)
        records = data.get("records", [])
        return {"records": records, "total": len(records), "returned": len(records)}

    if name == "grist_sql":
        body = {"sql": args.get("sql") or args.get("query", "")}
        if "args" in args: body["args"] = args["args"]
        return await grist_post(ctx, "sql", body)

    # ── Grist ecriture
    if name == "grist_records_add":
        table_id = args["table_id"]
        # Garde-fou pedagogique : ecrire dans une meta-table via REST casse le frontend
        if table_id.startswith("_grist_"):
            return {"error": f"Ecriture REST interdite sur la meta-table {table_id}. {_META_TABLE_HINT}"}
        result = await _ecrire_donnees(uid_key, ctx, table_id, args["records"], mode="add")
        ids = [r["id"] for r in (result or {}).get("records", [])]
        _notify_resource(uid_key, f"grist-coder://context/{ctx.token}")
        return {"ok": True, "created": len(ids), "ids": ids,
                "_next": f"Verifier context/{ctx.token} (_quality) ; toute table metier -> une page Grist."}

    if name == "grist_records_patch":
        table_id = args["table_id"]
        if table_id.startswith("_grist_"):
            return {"error": f"Patch REST interdit sur la meta-table {table_id}. {_META_TABLE_HINT}"}
        await grist_patch(ctx, f"tables/{table_id}/records",
                          {"records": args["records"]})
        _notify_resource(uid_key, f"grist-coder://context/{ctx.token}")
        return {"ok": True, "updated": len(args["records"])}

    if name == "grist_upsert":
        table_id = args["table_id"]
        if table_id.startswith("_grist_"):
            return {"error": f"Upsert REST interdit sur la meta-table {table_id}. {_META_TABLE_HINT}"}
        res = await _ecrire_donnees(uid_key, ctx, table_id, args["records"], mode="upsert")
        _notify_resource(uid_key, f"grist-coder://context/{ctx.token}")
        sortie = {"ok": True, "upserted": len(args["records"])}
        if res.get("_via"):
            sortie["_via"] = res["_via"]; sortie["_note"] = res.get("_note")
        return sortie

    # ── Document structure
    if name == "grist_apply":
        actions = args.get("actions")
        # Pre-vol automatique des batches STRUCTURELS (AddTable/AddColumn) : echouer proprement
        # avec un message actionnable plutot que de subir un 500 sandbox (ex: Ref forward).
        has_structural = isinstance(actions, list) and any(
            isinstance(a, list) and a and a[0] in ("AddTable", "AddColumn") for a in actions)
        if has_structural:
            try:
                schema = await grist_get(ctx, "tables")
                existing = {t["id"] for t in schema.get("tables", [])}
            except Exception:
                existing = set()
            # Colonnes des tables EXISTANTES touchees (AddColumn/ModifyColumn/UpdateRecord)
            # -> permet de valider la casse des colonnes et les $refs de formule AVANT
            # d'ecrire (evite le 500 sandbox opaque en EDITION d'app deja construite).
            existing_cols = {}
            touched = {a[1] for a in actions
                       if isinstance(a, list) and len(a) >= 2 and isinstance(a[1], str)
                       and a[0] in ("AddColumn", "ModifyColumn", "UpdateRecord")
                       and a[1] in existing}
            for t in touched:
                try:
                    cols = await grist_get(ctx, f"tables/{t}/columns")
                    existing_cols[t] = {c["id"] for c in cols.get("columns", [])}
                except Exception:
                    pass
            check = _validate_actions(actions, existing, existing_cols or None)
            if not check["ok"]:
                return {"error": "Validation pre-vol echouee (rien applique).",
                        "validation_errors": check["errors"],
                        "_hint": "Corriger l'ordre/les references puis re-appeler. Voir grist_validate."}
        try:
            result = await grist_apply(ctx, actions)
            _notify_resource(uid_key, f"grist-coder://context/{ctx.token}")
            resp = {"ok": True, "result": result}
            nxt = _apply_next(actions)
            if nxt:
                resp["_next"] = nxt
            return resp
        except Exception as e:
            # Erreur pedagogique : orienter vers la cause probable plutot qu un dump brut
            msg = str(e)
            hint = None
            if "not blank" in msg or "required" in msg.lower():
                hint = "Une colonne requise/formule est peut-etre saisie manuellement. Verifier docs/schema."
            elif "400" in msg:
                hint = ("Verifier : casse exacte des colonnes (grist_schema), "
                        "type des valeurs, et que les tables meta passent bien par UpdateRecord.")
            out = {"error": msg}
            if hint:
                out["_hint"] = hint
            return out

    if name == "grist_webhooks":
        action = args["action"]
        try:
            if action == "list":
                result = await grist_get(ctx, "webhooks")
                return {"ok": True, "webhooks": result.get("webhooks", result)}
            if action == "create":
                fields = args.get("fields", {})
                result = await grist_post(ctx, "webhooks",
                                          {"webhooks": [{"fields": fields}]}, no_token=True)
                return {"ok": True, "result": result}
            if action == "update":
                wid = args.get("webhook_id")
                if not wid:
                    return {"error": "webhook_id requis pour update"}
                fields = args.get("fields", {})
                result = await grist_patch(ctx, "webhooks",
                                           {"webhooks": [{"id": wid, "fields": fields}]}, no_token=True)
                return {"ok": True, "result": result}
            if action == "delete":
                wid = args.get("webhook_id")
                if not wid:
                    return {"error": "webhook_id requis pour delete"}
                result = await grist_delete(ctx, f"webhooks/{wid}", no_token=True)
                return {"ok": True, "result": result}
            return {"error": f"action inconnue: {action}"}
        except Exception as e:
            return {"error": str(e)}

    if name == "grist_validate":
        actions = args.get("actions")
        try:
            schema = await grist_get(ctx, "tables")
            existing = {t["id"] for t in schema.get("tables", [])}
        except Exception:
            existing = set()
        result = _validate_actions(actions, existing)
        if result["ok"]:
            result["_next"] = ("Validation OK -> grist_apply(actions). Ensuite : visibleCol sur les Ref "
                               "(UpdateRecord _grist_Tables_column) + BulkAddRecord (3-5 lignes) + page metier.")
        else:
            result["_next"] = ("Corriger les 'errors' avant grist_apply — surtout l'ordre des AddTable : "
                               "creer les tables cibles des Ref AVANT les tables qui les referencent.")
        return result

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
                    fields["linkSrcColRef"]     = int(args.get("link_src_col_ref") or 0)
                    fields["linkTargetColRef"]  = int(args.get("link_target_col_ref") or 0)  # colonne Ref: -> master-detail par colonne
                await grist_apply(ctx, [["UpdateRecord", "_grist_Views_section", section_ref, fields]])
                _notify_resource(uid_key, f"grist-coder://context/{ctx.token}")
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
                sects_before = await grist_get(ctx, "tables/_grist_Views_section/records")
                ids_before = {s["id"] for s in sects_before.get("records", [])}
                await grist_apply(ctx, [["CreateViewSection", table_ref, view_ref, "custom", None, None]])
                # Retrouver la nouvelle section en comparant avant/apres
                sects_after = await grist_get(ctx, "tables/_grist_Views_section/records")
                new_sects = [s for s in sects_after.get("records", [])
                             if s["id"] not in ids_before
                             and s["fields"].get("parentId") == view_ref]
                if not new_sects:
                    return {"error": "Section custom non trouvee apres CreateViewSection"}
                section_ref = new_sects[-1]["id"]
                # Sections existantes pour le layout (hors nouvelle)
                view_sects = [s["id"] for s in sects_after.get("records", [])
                              if s["fields"].get("parentId") == view_ref
                              and s["id"] != section_ref]
                # Configurer widget + lien + layout
                section_fields: dict = {"options": _make_widget_options(artefact, "")}
                if grid_section_ref:
                    section_fields["linkSrcSectionRef"] = grid_section_ref
                    section_fields["linkSrcColRef"]     = int(args.get("link_src_col_ref") or 0)
                    section_fields["linkTargetColRef"]  = int(args.get("link_target_col_ref") or 0)  # colonne Ref: -> master-detail par colonne
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
                _notify_resource(uid_key, f"grist-coder://context/{ctx.token}")
                return {"ok": True, "section_ref": section_ref, "view_ref": view_ref,
                        "artefact": artefact or "(coding mode)",
                        "hint": f"Widget ajoute a la vue {view_ref}."}
            except Exception as e:
                # Cleanup orphan section if it was created before the error
                try:
                    if 'section_ref' in dir():
                        await grist_apply(ctx, [["RemoveViewSection", section_ref]])
                except Exception:
                    pass
                return {"error": str(e)}

    if name == "grist_view_create":
        table_id   = args.get("table_id")
        page_name  = args.get("page_name") or table_id
        widget_url = args.get("widget_url", HOST_URL + "/")
        artefact   = args.get("artefact")
        # Page widget-seul (dashboard/fiche) : pas de grille native Grist a cote du widget.
        widget_only = bool(args.get("widget_only")) or args.get("layout") == "widget_only"
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

            if widget_only:
                # Une unique section custom = la page est le widget, sans grille native.
                await grist_apply(ctx, [["CreateViewSection", table_ref, 0, "custom", None, None]])
                views_resp = await grist_get(ctx, "tables/_grist_Views/records")
                view_ref = max((r["id"] for r in views_resp.get("records",[])), default=0)
                sects_resp = await grist_get(ctx, "tables/_grist_Views_section/records")
                custom_sections = [r for r in sects_resp.get("records",[])
                                   if r["fields"].get("parentId") == view_ref
                                   and r["fields"].get("parentKey") == "custom"]
                custom_ref = max((r["id"] for r in custom_sections), default=0)
                grid_ref = 0
                if not custom_ref:
                    return {"error": "Section custom non trouvee apres creation (widget_only)"}
            else:
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
                _ltc = int(args.get("link_target_col_ref") or 0)
                if _ltc:
                    section_fields["linkSrcColRef"]    = int(args.get("link_src_col_ref") or 0)
                    section_fields["linkTargetColRef"] = _ltc  # colonne Ref: -> master-detail par colonne
            await grist_apply(ctx, [["UpdateRecord", "_grist_Views_section", custom_ref, section_fields]])

            # Etape 4 : definir le layout et le nom via User Action UpdateRecord
            # IMPORTANT: grist_patch sur _grist_Views stocke les JSON strings comme objets
            # ce qui casse le parsing cote Grist frontend. UpdateRecord via /apply est correct.
            if widget_only:
                layout_spec = json.dumps({"children": [{"leaf": custom_ref}], "collapsed": []})
            else:
                layout_spec = json.dumps({
                    "children": [{"children": [{"leaf": grid_ref}, {"leaf": custom_ref}]}],
                    "collapsed": []
                })
            update_fields: dict = {"layoutSpec": layout_spec}
            if page_name:
                update_fields["name"] = page_name
            await grist_apply(ctx, [["UpdateRecord", "_grist_Views", view_ref, update_fields]])

            _notify_resource(uid_key, f"grist-coder://context/{ctx.token}")
            _next = ("grist_view_add_widget si besoin d un 2e widget. "
                     "PAS de grist_section_configure — page entierement configuree.")
            return {
                "ok": True, "view_ref": view_ref, "page_name": page_name,
                "grid_section": grid_ref or None, "widget_section": custom_ref,
                "widget_url": widget_url, "widget_only": widget_only,
                "hint": (f"Page '{page_name}' creee : widget seul (pas de grille native)."
                         if widget_only else
                         f"Page '{page_name}' creee : grille {table_id} + widget custom lies."),
                "_next": _next,
            }
        except Exception as e:
            return {"error": str(e)}

    if name == "artefact_publish":
        mode        = (args.get("mode") or "artefact").strip().lower()
        artefact    = (args.get("artefact") or "").strip()
        section_ref = int(args.get("section_ref") or 0)
        table_id    = args.get("table_id") or ""
        page_name   = args.get("page_name") or artefact or "Application"
        if mode not in ("artefact", "app"):
            return {"error": f"mode inconnu : {mode} (attendus : artefact, app)"}
        if mode == "artefact" and not artefact:
            return {"error": "artefact requis"}
        try:
            if mode == "app":
                # Magasin d'app : on publie le SHELL, pas un artefact. Les ecrans
                # restent des lignes de la table, montees a la demande par le shell.
                prefixe = (args.get("prefixe") or APP_MODULE_PREFIXE)
                entree  = (args.get("entree") or "").strip()
                mods = await grist_post(ctx, "sql", {
                    "sql": "SELECT Nom, Code FROM Artefacts WHERE Nom LIKE ? ORDER BY Nom",
                    "args": [prefixe + "%"]})
                noms = [r["fields"]["Nom"] for r in mods.get("records", [])]
                # Les ecrans ne sont PAS bundles : le shell les lit tels quels dans la
                # table au moment du montage, ce qui permet de les modifier sans
                # republier. Un ecran qui importe un paquet npm ne peut donc pas
                # s'executer — il monte, mais son script echoue et il rend vide.
                # Asymetrie assumee avec le mode artefact ; on la signale.
                ecrans_a_imports = {r["fields"]["Nom"]: imports_npm(r["fields"].get("Code") or "")
                                    for r in mods.get("records", [])}
                ecrans_a_imports = {k: v for k, v in ecrans_a_imports.items() if v}
                if not noms:
                    return {"error": f"Aucun ecran '{prefixe}...' dans la table Artefacts.",
                            "_hint": (f"Creer les ecrans comme artefacts nommes {prefixe}Accueil, "
                                      f"{prefixe}Detail, ... puis republier en mode app.")}
                if entree and entree not in noms:
                    return {"error": f"Ecran d'entree '{entree}' introuvable.",
                            "ecrans_disponibles": noms}
                art_type = "html"
                code     = _app_shell_html(entree or noms[0], prefixe)
                modules_publies = noms
            else:
                # 1. Lire la source dans la table Artefacts
                rows = await grist_post(ctx, "sql", {
                    "sql": "SELECT id, Nom, Type, Code FROM Artefacts WHERE Nom = ?",
                    "args": [artefact]})
                recs = rows.get("records", [])
                if not recs:
                    return {"error": f"Artefact '{artefact}' introuvable dans la table Artefacts"}
                art_row  = recs[0]["fields"]
                art_type = (art_row.get("Type") or "html").lower()
                code     = art_row.get("Code") or ""
                modules_publies = None
                if not code.strip():
                    return {"error": f"Artefact '{artefact}' : Code vide dans Grist — "
                                     "sauvegarder l artefact (canvas_write + Save) avant de publier"}
            # Bundling AVANT la garde : un artefact qui importe des paquets npm ne
            # peut pas tourner tel quel dans un navigateur (page blanche). On resout
            # ses imports et on inline tout. La source reste intacte dans Artefacts —
            # seul le code publie est bundle.
            infos_bundle = bundler_artefact(code)
            if not infos_bundle.get("ok"):
                return {"error": infos_bundle.get("erreur", "Bundling impossible."),
                        **{k: v for k, v in infos_bundle.items()
                           if k in ("paquets", "detail", "catalogue", "_hint")}}
            if infos_bundle.get("bundle"):
                code = infos_bundle["code"]

            # Garde d'autonomie : un widget publie ne doit dependre ni du pod ni
            # d'un CDN. Verifie, plutot que promis par la documentation.
            bloquants, cdn = _audit_autonomie(code)
            if bloquants:
                return {"error": ("Publication refusee : cet artefact dependrait du serveur MCP "
                                  "et cesserait de fonctionner a l'arret du pod."),
                        "dependances": bloquants,
                        "_hint": ("Un widget publie voyage avec le document. Remplacer ces appels "
                                  "par grist.docApi (donnees du doc) ou par un calcul local. "
                                  "Pour un artefact destine a rester servi par le pod, utiliser "
                                  "grist_view_create au lieu de artefact_publish.")}
            if art_type not in ("html", "svg"):
                return {"error": f"Type '{art_type}' non publiable en widget autonome "
                                 "(supportes : html, svg).",
                        "_hint": ("Convertir l artefact en html avant de publier : React.createElement "
                                  "ou HTML+JS vanilla (le JSX/Babel de react|app ne survit pas au split). "
                                  "Lire docs/publication.")}

            # 2. Transformer : markup -> _html, scripts inline -> _js (+ grist.ready)
            html_part, js_part = _split_html_js(code)

            # 3. widgetDef du builder (galerie de l instance, fallback statique)
            builder_def = await _fetch_builder_def(ctx)
            custom_view = json.dumps({
                "mode": "url", "url": None, "access": "full",
                "widgetDef": builder_def, "pluginId": "", "sectionId": "",
                "renderAfterReady": True, "widgetId": builder_def.get("widgetId", _BUILDER_WIDGET_ID),
                "widgetOptions": {"_html": html_part, "_js": js_part},
                "columnsMapping": None,
            })

            view_ref = None
            if section_ref:
                # Reconfigurer une section existante en preservant ses autres options
                sect = await grist_post(ctx, "sql", {
                    "sql": "SELECT options FROM _grist_Views_section WHERE id = ?",
                    "args": [section_ref]})
                sect_recs = sect.get("records", [])
                if not sect_recs:
                    return {"error": f"Section {section_ref} introuvable (voir grist_views_list)"}
                try:
                    options = json.loads(sect_recs[0]["fields"].get("options") or "{}")
                except Exception:
                    options = {}
                options["customView"] = custom_view
                # Ecriture lourde (HTML dans widgetOptions) -> WAF/droits meta cote serveur.
                # On l'execute dans le navigateur (bypass WAF + privileges owner).
                ok, apply_err = await _apply_meta(uid_key, ctx, [
                    ["UpdateRecord", "_grist_Views_section", section_ref,
                     {"options": json.dumps(options)}]])
                if not ok:
                    return {"error": f"Publication : ecriture des options de section echouee. {apply_err}"}
            else:
                # Nouvelle page dediee : une seule section custom pleine page
                if not table_id:
                    return {"error": "table_id requis pour creer une nouvelle page "
                                     "(ou fournir section_ref pour une section existante)"}
                tables_meta = await grist_get(ctx, "tables/_grist_Tables/records")
                table_ref = next((r["id"] for r in tables_meta.get("records", [])
                                  if r["fields"].get("tableId") == table_id), None)
                if not table_ref:
                    return {"error": f"Table '{table_id}' non trouvee dans le document"}
                await grist_apply(ctx, [["CreateViewSection", table_ref, 0, "custom", None, None]])
                views_resp = await grist_get(ctx, "tables/_grist_Views/records")
                view_ref = max((r["id"] for r in views_resp.get("records", [])), default=0)
                sects_resp = await grist_get(ctx, "tables/_grist_Views_section/records")
                custom_sections = [r for r in sects_resp.get("records", [])
                                   if r["fields"].get("parentId") == view_ref
                                   and r["fields"].get("parentKey") == "custom"]
                section_ref = max((r["id"] for r in custom_sections), default=0)
                if not section_ref:
                    return {"error": "Section custom non trouvee apres creation de la page"}
                options = json.dumps({
                    "verticalGridlines": True, "horizontalGridlines": True,
                    "zebraStripes": False, "numFrozen": 0,
                    "customView": custom_view,
                })
                # Ecriture lourde (HTML dans widgetOptions) -> via navigateur (bypass WAF).
                ok, apply_err = await _apply_meta(uid_key, ctx, [
                    ["UpdateRecord", "_grist_Views_section", section_ref, {"options": options}],
                    ["UpdateRecord", "_grist_Views", view_ref,
                     {"name": page_name,
                      "layoutSpec": json.dumps({"children": [{"leaf": section_ref}], "collapsed": []})}],
                ])
                if not ok:
                    return {"error": f"Publication : ecriture des options de section echouee. {apply_err}"}

            # 4. Tracer la publication sur l artefact source (best-effort)
            try:
                await grist_patch(ctx, "tables/Artefacts/records", {"records": [
                    {"id": art_row.get("id"),
                     "fields": {"Output": json.dumps({
                         "published": {"section": section_ref, "view": view_ref,
                                       "at": int(time.time())}})}}]})
            except Exception:
                pass

            _notify_resource(uid_key, f"grist-coder://context/{ctx.token}")
            sortie = {
                "ok": True, "artefact": artefact,
                "section_ref": section_ref, "view_ref": view_ref,
                "page_name": page_name if view_ref else None,
                "autonomous": True,
                "hint": ("Widget fige dans le doc (options de section, builder galerie). "
                         "Aucune dependance au serveur MCP au runtime — le widget survit a "
                         "l arret du pod et voyage avec le doc. Il reste servi par le widget "
                         "de galerie 'custom-widget-builder' (heberge hors du pod)."),
                "_next": "Recharger la page Grist pour verifier le rendu. "
                         "Pour mettre a jour : modifier l artefact source puis republier.",
            }
            if modules_publies:
                sortie["mode"] = "app"
                sortie["ecrans"] = modules_publies
                sortie["hint"] = (
                    "Shell d'application publie. Les ecrans restent des lignes de la table "
                    "Artefacts : les modifier puis recharger la page suffit, PAS besoin de "
                    "republier. Republier seulement pour changer l'ecran d'entree.")
                if ecrans_a_imports:
                    sortie["avertissement_ecrans"] = (
                        "Ces ecrans importent des paquets npm et rendront VIDE : les ecrans "
                        "d'une application ne sont pas bundles, le shell les lit tels quels "
                        "pour qu'ils restent modifiables sans republier. Les reecrire sans "
                        "import — h(...) / React.createElement, ou une librairie en "
                        "<script src=...> CDN qui, elle, fonctionne dans un widget publie.")
                    sortie["ecrans_a_corriger"] = ecrans_a_imports
                sortie["_next"] = ("Ajouter un ecran = ajouter une ligne " + APP_MODULE_PREFIXE
                                   + "Nom dans Artefacts. Depuis un ecran : app.navigate(nom), "
                                     "app.setState(o), app.emit(ev,d), app.on(ev,cb).")
            if infos_bundle.get("bundle"):
                sortie["bundle"] = {"paquets": infos_bundle.get("paquets"),
                                    "versions": infos_bundle.get("versions"),
                                    "taille": infos_bundle.get("taille")}
                sortie["hint"] = (sortie["hint"] + " Artefact bundle : les librairies "
                                  "importees sont inlinees, zero requete reseau a l execution.")
                if infos_bundle.get("avertissement_poids"):
                    sortie["avertissement_poids"] = infos_bundle["avertissement_poids"]
            if cdn:
                sortie["avertissement_cdn"] = (
                    "Ce widget dependra de ces CDN a l execution. Ils fonctionnent (verifie), "
                    "mais l autonomie n est alors plus totale : le widget casse si le CDN "
                    "devient injoignable. Inliner la librairie dans le code si l artefact doit "
                    "survivre a tout, y compris hors ligne.")
                sortie["cdn_requis_au_runtime"] = cdn
            return sortie
        except Exception as e:
            return {"error": str(e)}

    return {"error": f"outil inconnu : {name}"}

# ── DISPATCH ──────────────────────────────────────────────────────────────────

async def dispatch(uid_key, mcp_sid, method, params):
    if method == "initialize":
        # Stocker les capabilities du client pour activer sampling si supporté
        _client_capabilities[uid_key] = params.get("capabilities", {})
        return {
            "protocolVersion": MCP_VER,
            "serverInfo": {"name": "grist-coder", "version": "5.14",
                           "instructions": SERVER_INSTRUCTIONS},
            "capabilities": {
                "tools":     {"listChanged": True},
                "resources": {"subscribe": True, "listChanged": True},
                "prompts":   {"listChanged": False},
                "logging":   {},
            },
        }
    if method == "tools/list":
        # Always return all tools — context-based filtering via notifications/tools/list_changed
        # is not reliably acted upon by all MCP clients (e.g. Claude Code). Context is guidance only.
        return {"tools": TOOLS}
    if method == "tools/call":
        name   = params["name"]
        result = await call_tool(uid_key, mcp_sid, name, params.get("arguments", {}))
        if name == "canvas_screenshot" and isinstance(result, dict) and result.get("ok") and "_image_b64" in result:
            return {"content": [
                {"type": "image", "data": result["_image_b64"], "mimeType": result.get("mime","image/jpeg")},
                {"type": "text",  "text": json.dumps({
                    "ok": True,
                    "_next": "Rendu correct → grist_view_create pour publier la page si pas encore fait. "
                             "Correction necessaire → canvas_patch pour ajuster."
                }, ensure_ascii=False)},
            ]}
        return {"content": [{"type": "text",
                             "text": _scrub_secrets(json.dumps(result, ensure_ascii=False, indent=2))}]}
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

async def _purge_loop():
    """Purge periodique des sessions inactives + nettoyage des maps annexes."""
    while True:
        await asyncio.sleep(3600)
        try:
            tokens, uids = registry.purge_expired(SESSION_TTL)
            _oauth_purge()
            for t in tokens:
                _token_to_uid.pop(t, None)
            for u in uids:
                _client_capabilities.pop(u, None)
                _mcp_client_sids.pop(u, None)
                _chat_waiters.pop(u, None)
            if tokens:
                print(f"[purge] {len(tokens)} session(s) expiree(s), {len(uids)} user(s) nettoye(s)",
                      file=sys.stderr)
        except Exception as e:
            print(f"[purge] erreur : {type(e).__name__}", file=sys.stderr)

@asynccontextmanager
async def lifespan(app):
    print(f"Grist Coder v5.14 · {HOST_URL}")
    print(f"  tools: {len(TOOLS)}  prompts: {len(PROMPTS)}")
    print(f"  resources: {len(STATIC_RESOURCES)} static + {len(RESOURCE_TEMPLATES)} templates")
    print(f"  widget: {'widget.html' if WIDGET_PATH.exists() else 'MANQUANT'}")
    purge_task = asyncio.create_task(_purge_loop())
    try:
        yield
    finally:
        purge_task.cancel()

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"], expose_headers=["Mcp-Session-Id"])

def _auth(auth):
    if not auth: return None
    parts = auth.split()
    return parts[1] if len(parts) == 2 and parts[0].lower() == "bearer" else None

async def _resolve_uid_key(raw_bearer, x_grist_site):
    if not raw_bearer: return None
    if raw_bearer.startswith("gco-"):          # access token OAuth -> uid mappe
        return _oauth_uid_from_token(raw_bearer)
    if raw_bearer.startswith("gc-"):
        return _token_to_uid.get(raw_bearer)
    uid_key = registry.get_uid_for_grist_key(raw_bearer)
    if uid_key:
        if x_grist_site:
            registry.provision(uid_key, grist_key=raw_bearer, site=x_grist_site.rstrip("/"))
        return uid_key
    site = (x_grist_site or "").rstrip("/")
    if not site:
        # La passerelle (ex: connecteur claude.ai) n'envoie generalement PAS le header
        # X-Grist-Site -> sans repli, le lookup profil n'etait JAMAIS tente et la cle
        # retombait sur un uid key:{sha1} vide. Replis : env, puis site d'un user connu.
        site = os.getenv("GRIST_SITE_URL", "").strip().rstrip("/")
    if not site:
        for udata in registry._users.values():
            s = (udata.get("site") or "").rstrip("/")
            if not s:
                for sctx in udata["sessions"].values():
                    if getattr(sctx, "site_url", ""):
                        s = sctx.site_url
                        break
            if s:
                site = s
                break
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


# ── OAuth 2.1 · Authorization Server embarque (connecteurs en ligne) ────────────
# Surface 100% ADDITIVE, active uniquement en ligne (PUBLIC_URL non vide, injecte par
# le chart Onyxia). Le pod est son propre Authorization Server. Le << login >> du
# consentement valide la cle API Grist (meme identite uid:{id} + owner-lock que le
# reste du serveur). Le client ne recoit qu'un token opaque gco-... revocable : la cle
# Grist ne repart JAMAIS vers le client (pas de token passthrough). En dev local
# (PUBLIC_URL vide) tous ces endpoints renvoient 404 -> comportement inchange, et le
# chemin legacy (Bearer cle Grist + X-App-Token) reste accepte en parallele.
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
_OAUTH_CODE_TTL  = 120                  # code d'autorisation : tres court
_OAUTH_TOKEN_TTL = 30 * 24 * 3600       # access token : 30 jours
_oauth_clients: dict[str, dict] = {}    # client_id   -> {redirect_uris, created}
_oauth_codes:   dict[str, dict] = {}    # code        -> {uid_key, redirect_uri, code_challenge, ...}
_oauth_tokens:  dict[str, dict] = {}    # gco-access  -> {uid_key, expires}
_oauth_refresh: dict[str, dict] = {}    # gcr-refresh -> {uid_key}


def _oauth_enabled() -> bool:
    return bool(PUBLIC_URL)


def _oauth_purge():
    """Expire codes et access tokens perimes (appele par _purge_loop)."""
    now = time.time()
    for code, d in list(_oauth_codes.items()):
        if now > d["expires"]: _oauth_codes.pop(code, None)
    for tok, d in list(_oauth_tokens.items()):
        if now > d["expires"]: _oauth_tokens.pop(tok, None)


# Tokens OAuth SANS STOCKAGE. Ils etaient conserves en memoire : chaque
# redemarrage du pod invalidait tous les connecteurs, y compris pour une simple
# mise a jour — une deconnexion par deploiement. Le token porte desormais son
# uid et son expiration, signes ; le serveur verifie sans rien memoriser, donc
# ils survivent aux redemarrages.
#
# Revocation : il n'existait aucun endpoint de revocation, le terme etait
# descriptif. Elle se fait en changeant APP_AUTH_TOKEN, ce qui invalide d'un coup
# tous les tokens emis — c'est le geste qu'un proprietaire de pod ferait de toute
# facon, puisque ce secret garde aussi l'acces au pod.

_OAUTH_SECRET_REPLI = secrets.token_bytes(32)   # dev local sans APP_AUTH_TOKEN


def _oauth_secret() -> bytes:
    """Secret de signature. APP_AUTH_TOKEN est injecte par le chart depuis un Secret
    Kubernetes : il est stable entre redemarrages. Sans lui (dev local), on tire un
    secret de process et les tokens ne survivent pas au redemarrage — sans gravite."""
    if APP_AUTH_TOKEN:
        return hashlib.sha256(b"gco-signature:" + APP_AUTH_TOKEN.encode()).digest()
    return _OAUTH_SECRET_REPLI


# La cle Grist recueillie au consentement voyage SCELLEE dans le token, chiffree
# avec le meme secret que la signature. Sans ca, elle ne vivait qu'en memoire :
# depuis que les tokens survivent aux redemarrages, le client n'a plus de raison
# de reconsentir, donc la cle n'etait jamais redonnee — on se retrouvait connecte
# mais sans cle. Exiger de la reconfigurer dans le chart aurait annule l'interet
# du connecteur, dont la promesse est justement « colle ta cle une fois ».
#
# Exposition : qui detient le token a deja l'acces complet au pod. Le scellement
# n'ajoute rien de ce cote, et protege en revanche la cle Grist BRUTE, qui donne
# un acces bien plus large. Faire tourner APP_AUTH_TOKEN invalide d'un coup les
# tokens et les cles scellees — la revocation reste coherente.

def _sceller(txt: str) -> str:
    if not txt:
        return ""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except Exception:
        return ""                       # sans la lib, on degrade sans casser
    nonce = secrets.token_bytes(12)
    ct = AESGCM(_oauth_secret()).encrypt(nonce, txt.encode(), None)
    return base64.urlsafe_b64encode(nonce + ct).rstrip(b"=").decode()


def _desceller(blob: str) -> str:
    if not blob:
        return ""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        brut = base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4))
        return AESGCM(_oauth_secret()).decrypt(brut[:12], brut[12:], None).decode()
    except Exception:
        return ""                       # secret tourne, token altere : on ignore


def _oauth_forge(prefixe: str, uid_key: str, ttl: int, cle_grist: str = "") -> str:
    charge = {"u": uid_key, "e": int(time.time() + ttl)}
    scelle = _sceller(cle_grist)
    if scelle:
        charge["k"] = scelle
    corps = base64.urlsafe_b64encode(
        json.dumps(charge, separators=(",", ":")).encode()).rstrip(b"=").decode()
    sig = base64.urlsafe_b64encode(
        hmac.new(_oauth_secret(), corps.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    return f"{prefixe}{corps}.{sig}"


def _oauth_lire(prefixe: str, token: str) -> tuple[str | None, str]:
    """(uid_key, cle_grist) portes par un token signe. (None, "") si invalide,
    expire ou contrefait. La cle est vide si le token n'en scelle pas."""
    vide = (None, "")
    if not token or not token.startswith(prefixe):
        return vide
    reste = token[len(prefixe):]
    if "." not in reste:
        return vide
    corps, sig = reste.rsplit(".", 1)
    attendu = base64.urlsafe_b64encode(
        hmac.new(_oauth_secret(), corps.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    if not hmac.compare_digest(sig, attendu):
        return vide
    try:
        p = json.loads(base64.urlsafe_b64decode(corps + "=" * (-len(corps) % 4)))
    except Exception:
        return vide
    if time.time() > p.get("e", 0):
        return vide
    return (p.get("u") or None), _desceller(p.get("k", ""))


def _oauth_uid_from_token(bearer) -> str | None:
    """Resout un access token OAuth gco- en uid_key. Repose au passage la cle Grist
    scellee dans le token : c'est ce qui la fait survivre aux redemarrages du pod
    sans rien demander a l'utilisateur."""
    uid, cle = _oauth_lire("gco-", bearer)
    if uid:
        if cle:
            u = registry._users.get(uid)
            if not u or not (u.get("grist_key") or "").strip():
                registry.provision(uid, grist_key=cle,
                                   site=os.getenv("GRIST_SITE_URL", "").strip().rstrip("/"))
        return uid
    # Repli : tokens opaques emis avant le passage aux tokens signes.
    d = _oauth_tokens.get(bearer)
    if not d:
        return None
    if time.time() > d["expires"]:
        _oauth_tokens.pop(bearer, None)
        return None
    return d["uid_key"]


def _pkce_ok(verifier: str, challenge: str, method: str = "S256") -> bool:
    """Verifie le code_verifier PKCE contre le code_challenge stocke (S256 par defaut)."""
    if not challenge:
        return True   # PKCE non demande (Claude envoie toujours S256, on reste tolerant)
    if not verifier:
        return False
    if method == "plain":
        return hmac.compare_digest(verifier, challenge)
    computed = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return hmac.compare_digest(computed, challenge)


def _oauth_issue(uid_key: str, cle_grist: str = "") -> dict:
    """Emet une paire access/refresh token SIGNEE, scellant la cle Grist : rien
    n'est memorise cote serveur, donc rien n'est perdu au redemarrage du pod."""
    return {"access_token": _oauth_forge("gco-", uid_key, _OAUTH_TOKEN_TTL, cle_grist),
            "token_type": "Bearer", "expires_in": _OAUTH_TOKEN_TTL,
            "refresh_token": _oauth_forge("gcr-", uid_key, 365 * 24 * 3600, cle_grist),
            "scope": "mcp"}


def _oauth_check_client(client_id: str, redirect_uri: str):
    """(client, None) si client_id connu et redirect_uri autorise, sinon (None, message)."""
    client = _oauth_clients.get(client_id)
    if not client:
        return None, "Client OAuth inconnu. Relance la connexion depuis le connecteur."
    uris = client.get("redirect_uris") or []
    if uris and redirect_uri not in uris:
        return None, "redirect_uri non autorise pour ce client."
    return client, None


def _oauth_page(body_html: str, status: int = 200) -> HTMLResponse:
    html = (
        "<!doctype html><html lang=fr><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>GristCoder - Connexion</title><style>"
        "body{font-family:system-ui,Segoe UI,sans-serif;background:#f5f5f7;margin:0;"
        "display:flex;min-height:100vh;align-items:center;justify-content:center}"
        ".card{background:#fff;max-width:420px;width:92%;padding:32px;border-radius:14px;"
        "box-shadow:0 6px 30px rgba(0,0,0,.08)}h1{font-size:19px;margin:0 0 6px}"
        "p{color:#555;font-size:14px;line-height:1.5}label{display:block;font-size:13px;"
        "font-weight:600;margin:18px 0 6px}input{width:100%;box-sizing:border-box;padding:11px;"
        "border:1px solid #ccc;border-radius:8px;font-size:14px}"
        "button{margin-top:20px;width:100%;padding:12px;border:0;border-radius:8px;"
        "background:#000091;color:#fff;font-size:15px;font-weight:600;cursor:pointer}"
        ".err{background:#ffe9e9;color:#a10000;padding:10px;border-radius:8px;font-size:13px;margin-top:14px}"
        ".hint{font-size:12px;color:#888;margin-top:8px}</style></head><body>"
        f"<div class=card>{body_html}</div></body></html>"
    )
    return HTMLResponse(html, status_code=status)


def _oauth_consent_form(params: dict, error: str = "") -> str:
    def esc(s):
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    hidden = "".join(
        f'<input type=hidden name={k} value="{esc(params.get(k, ""))}">'
        for k in ("client_id", "redirect_uri", "state", "code_challenge",
                  "code_challenge_method", "scope"))
    err = f"<div class=err>{esc(error)}</div>" if error else ""
    return (
        "<h1>Connecter GristCoder</h1>"
        "<p>Colle ta cle API Grist pour autoriser ce connecteur. Elle sert uniquement a "
        "verifier ton identite ; le connecteur ne recevra qu'un jeton revocable.</p>"
        f"{err}"
        "<form method=post action=/oauth/authorize>"
        f"{hidden}"
        "<label>Cle API Grist</label>"
        "<input name=grist_key type=password autocomplete=off required placeholder='ex: 1a2b3c...'>"
        "<div class=hint>Grist &rarr; Profile settings &rarr; API key.</div>"
        "<button type=submit>Autoriser</button></form>"
    )


@app.get("/.well-known/oauth-protected-resource")
@app.get("/.well-known/oauth-protected-resource/{path:path}")
async def oauth_protected_resource(path: str = ""):
    if not _oauth_enabled():
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse({
        "resource": f"{PUBLIC_URL}/mcp",
        "authorization_servers": [PUBLIC_URL],
        "bearer_methods_supported": ["header"],
    })


# Variante suffixee obligatoire : quand le serveur est declare avec un chemin
# (ex: https://pod/mcp), les clients MCP font une decouverte << path-aware >> et
# demandent /.well-known/oauth-authorization-server/mcp (RFC 8414, insertion du
# chemin de la ressource). Sans cette route, ils prenaient un 404 et affichaient
# un message trompeur << Unable to connect >> au lieu d'un probleme de decouverte.
# oauth-protected-resource ci-dessus avait deja les deux formes ; c'etait l'asymetrie.
@app.get("/.well-known/oauth-authorization-server")
@app.get("/.well-known/oauth-authorization-server/{path:path}")
async def oauth_as_metadata(path: str = ""):
    if not _oauth_enabled():
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse({
        "issuer": PUBLIC_URL,
        "authorization_endpoint": f"{PUBLIC_URL}/oauth/authorize",
        "token_endpoint": f"{PUBLIC_URL}/oauth/token",
        "registration_endpoint": f"{PUBLIC_URL}/oauth/register",
        "scopes_supported": ["mcp"],
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    })


@app.post("/oauth/register")
async def oauth_register(request: Request):
    """Dynamic Client Registration (RFC 7591). Permissif : c'est ce qui permet a Claude
    de s'enregistrer automatiquement (corrige l'erreur << Impossible de s'inscrire >>)."""
    if not _oauth_enabled():
        return JSONResponse({"error": "not_found"}, status_code=404)
    data, err = await _read_json(request)
    if err or not isinstance(data, dict):
        data = {}   # corps vide ou invalide accepte (client public)
    redirect_uris = data.get("redirect_uris") or []
    client_id = "gcc-" + secrets.token_urlsafe(16)
    _oauth_clients[client_id] = {"redirect_uris": redirect_uris, "created": time.time()}
    return JSONResponse({
        "client_id": client_id,
        "client_id_issued_at": int(time.time()),
        "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }, status_code=201)


@app.get("/oauth/authorize")
async def oauth_authorize_get(request: Request):
    if not _oauth_enabled():
        return Response("not found", status_code=404)
    q = dict(request.query_params)
    _, err = _oauth_check_client(q.get("client_id", ""), q.get("redirect_uri", ""))
    if err:
        return _oauth_page(f"<h1>Connexion impossible</h1><div class=err>{err}</div>", status=400)
    return _oauth_page(_oauth_consent_form(q))


@app.post("/oauth/authorize")
async def oauth_authorize_post(request: Request):
    if not _oauth_enabled():
        return Response("not found", status_code=404)
    raw = (await request.body()).decode("utf-8", "replace")
    data = {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}
    client_id, redirect_uri = data.get("client_id", ""), data.get("redirect_uri", "")
    _, err = _oauth_check_client(client_id, redirect_uri)
    if err:
        return _oauth_page(f"<h1>Connexion impossible</h1><div class=err>{err}</div>", status=400)
    grist_key = (data.get("grist_key") or "").strip()
    if not grist_key:
        return _oauth_page(_oauth_consent_form(data, "Cle API Grist requise."), status=400)
    # Le << login >> = validation de la cle Grist -> uid:{id} (via profil), puis owner-lock.
    uid_key = await _resolve_uid_key(grist_key, None)
    if not uid_key or not uid_key.startswith("uid:"):
        return _oauth_page(_oauth_consent_form(data, "Cle Grist invalide ou compte introuvable."),
                           status=401)
    if not _owner_gate(uid_key):
        return _oauth_page("<h1>Acces refuse</h1><div class=err>Ce pod appartient a un autre "
                           "compte Grist (owner-lock).</div>", status=403)
    code = "gca-" + secrets.token_urlsafe(24)
    _oauth_codes[code] = {
        "uid_key": uid_key, "redirect_uri": redirect_uri, "grist_key": grist_key,
        "code_challenge": data.get("code_challenge", ""),
        "code_challenge_method": (data.get("code_challenge_method") or "S256"),
        "expires": time.time() + _OAUTH_CODE_TTL,
    }
    sep = "&" if "?" in redirect_uri else "?"
    loc = f"{redirect_uri}{sep}code={urllib.parse.quote(code)}"
    state = data.get("state")
    if state:
        loc += f"&state={urllib.parse.quote(state)}"
    return RedirectResponse(loc, status_code=302)


@app.post("/oauth/token")
async def oauth_token(request: Request):
    if not _oauth_enabled():
        return JSONResponse({"error": "invalid_request"}, status_code=404)
    raw = (await request.body()).decode("utf-8", "replace")
    data = {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}
    grant = data.get("grant_type", "")
    no_store = {"Cache-Control": "no-store", "Pragma": "no-cache"}
    if grant == "authorization_code":
        cd = _oauth_codes.pop(data.get("code", ""), None)
        if not cd or time.time() > cd["expires"]:
            return JSONResponse({"error": "invalid_grant"}, status_code=400, headers=no_store)
        ruri = data.get("redirect_uri")
        if ruri and cd.get("redirect_uri") and ruri != cd["redirect_uri"]:
            return JSONResponse({"error": "invalid_grant"}, status_code=400, headers=no_store)
        if not _pkce_ok(data.get("code_verifier", ""), cd.get("code_challenge", ""),
                        cd.get("code_challenge_method", "S256")):
            return JSONResponse({"error": "invalid_grant", "error_description": "PKCE"},
                                status_code=400, headers=no_store)
        return JSONResponse(_oauth_issue(cd["uid_key"], cd.get("grist_key", "")), headers=no_store)
    if grant == "refresh_token":
        rtok = data.get("refresh_token", "")
        uid, cle = _oauth_lire("gcr-", rtok)
        if not uid:
            rd = _oauth_refresh.get(rtok)          # repli : refresh opaque d'avant
            uid, cle = (rd.get("uid_key") if rd else None), ""
        if not uid:
            return JSONResponse({"error": "invalid_grant"}, status_code=400, headers=no_store)
        return JSONResponse(_oauth_issue(uid, cle), headers=no_store)
    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400, headers=no_store)


@app.post("/mcp")
async def mcp_post(request: Request,
                   authorization: str | None = Header(default=None),
                   mcp_session_id: str | None = Header(default=None, alias="Mcp-Session-Id"),
                   x_grist_site: str | None = Header(default=None, alias="X-Grist-Site")):
    raw_bearer = _auth(authorization)
    if not raw_bearer:
        hdrs = ({"WWW-Authenticate": f'Bearer resource_metadata="{PUBLIC_URL}'
                 '/.well-known/oauth-protected-resource"'} if _oauth_enabled() else {})
        return JSONResponse({"error": "Authorization: Bearer requis"}, status_code=401, headers=hdrs)
    # Token OAuth (gco-) : la garde du pod est portee par le token lui-meme -> pas de X-App-Token.
    if not raw_bearer.startswith("gco-") and not _check_app_token(request):
        return JSONResponse({"error": "Garde du pod : en-tete X-App-Token manquant ou invalide."},
                            status_code=401)
    uid_key = await _resolve_uid_key(raw_bearer, x_grist_site)
    if not uid_key:
        return JSONResponse({"error": "Token inconnu ou expire. Rechargez le widget."}, status_code=401)
    if not _owner_gate(uid_key):
        return JSONResponse({"error": "Pod verrouille sur un autre compte (owner-lock). "
                             "Ce pod appartient a son proprietaire."}, status_code=403)
    # Identite de connexion. Un uuid tire a chaque requete quand le client n'echo pas
    # Mcp-Session-Id (cas des passerelles : connecteur claude.ai) rendait _active_tokens
    # inutilisable — session_select ecrivait sous un sid jamais revu, donc la selection
    # etait perdue des l'appel suivant. Repli deterministe sur le credential.
    mcp_sid  = mcp_session_id or ("cred:" + hashlib.sha1(raw_bearer.encode()).hexdigest()[:12])
    # Auto-epinglage : un bearer gc- EST le token de session du widget -> cette connexion
    # est liee a SON doc, jamais routee vers "le plus recent" du compte. Idempotent.
    if raw_bearer.startswith("gc-") and registry.resolve(uid_key, raw_bearer):
        _active_tokens[mcp_sid] = raw_bearer
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": -32700, "message": "Parse error : corps JSON invalide"}},
            status_code=400, headers={"Mcp-Session-Id": mcp_sid})
    is_batch = isinstance(body, list)
    reqs     = body if is_batch else [body]
    responses = []
    for req in reqs:
        req_id = req.get("id")
        # JSON-RPC response (no "method") = réponse du client à une sampling request
        if "method" not in req:
            if req_id and req_id in _sampling_waiters:
                fut = _sampling_waiters.pop(req_id, None)
                if fut and not fut.done():
                    if "result" in req: fut.set_result(req["result"])
                    else: fut.set_exception(Exception(str(req.get("error", "sampling error"))))
            continue
        if req_id is None: continue
        try:
            result = await dispatch(uid_key, mcp_sid, req.get("method",""), req.get("params",{}))
            responses.append({"jsonrpc":"2.0","id":req_id,"result":result})
        except Exception as e:
            responses.append({"jsonrpc":"2.0","id":req_id,"error":{"code":-32603,"message":_scrub_secrets(str(e))}})
    if not responses: return Response(status_code=202)
    payload = responses if is_batch else responses[0]
    return JSONResponse(payload, headers={"Mcp-Session-Id": mcp_sid})


@app.get("/mcp")
async def mcp_sse(request: Request,
                  authorization: str | None = Header(default=None),
                  mcp_session_id: str | None = Header(default=None, alias="Mcp-Session-Id")):
    raw_bearer = _auth(authorization)
    is_oauth = bool(raw_bearer and raw_bearer.startswith("gco-"))
    if not is_oauth and not _check_app_token(request):
        return Response("Garde du pod : app_token manquant ou invalide.", status_code=401)
    uid_key = None
    is_mcp_client = False
    if raw_bearer:
        uid_key = await _resolve_uid_key(raw_bearer, None)
        # Connexion Claude Desktop (pas un widget token gc-)
        is_mcp_client = bool(uid_key and not raw_bearer.startswith("gc-"))
    if not uid_key:
        arto_token = request.query_params.get("token")
        if arto_token:
            uid_key = _token_to_uid.get(arto_token)
    if not uid_key:
        hdrs = ({"WWW-Authenticate": f'Bearer resource_metadata="{PUBLIC_URL}'
                 '/.well-known/oauth-protected-resource"'} if _oauth_enabled() else {})
        return Response("Authorization requis", status_code=401, headers=hdrs)
    if not _owner_gate(uid_key):
        return Response("Pod verrouille sur un autre compte (owner-lock).", status_code=403)
    sid = mcp_session_id or str(uuid.uuid4())
    is_display = request.query_params.get("display") == "1"
    _queues[sid] = asyncio.Queue(maxsize=64)
    _sid_uid[sid] = uid_key
    _sid_token[sid] = request.query_params.get("token") or ""   # document couvert
    if is_display:
        _display_sids.add(sid)
    if is_mcp_client:
        _mcp_client_sids[uid_key] = sid  # Track pour sampling
    # Re-push active wizard cards on reconnect (SSE restore) — sauf pour display mode
    if not is_display:
        # Replay cible la session du widget (?token=), pas "la plus recente" du compte.
        _rt = request.query_params.get("token")
        ctx_for_replay = registry.resolve(uid_key, _rt) if _rt else registry.resolve(uid_key, None)
        # Restaurer le bandeau plan + la phase depuis project_plan (source persistante),
        # meme si la card ephemere ctx-plan-progress a ete fermee entre-temps.
        if ctx_for_replay and ctx_for_replay.project_plan.get("status"):
            plan_step = _plan_progress_step(ctx_for_replay)
            if plan_step:
                try:
                    _queues[sid].put_nowait({"type": "wizard_step", "token": ctx_for_replay.token,
                                             "step": plan_step, "_user": uid_key})
                    _queues[sid].put_nowait({"type": "context_changed", "token": ctx_for_replay.token,
                                             "context": ctx_for_replay.current_context, "_user": uid_key})
                except asyncio.QueueFull: pass
        if ctx_for_replay and ctx_for_replay._active_wizard_cards:
            for card_ev in ctx_for_replay._active_wizard_cards.values():
                # ctx-plan-progress deja restaure ci-dessus depuis project_plan
                if card_ev.get("step", {}).get("id") == "ctx-plan-progress":
                    continue
                try: _queues[sid].put_nowait({**card_ev, "_user": uid_key})
                except asyncio.QueueFull: pass
        # Re-push chat history on reconnect
        if ctx_for_replay and ctx_for_replay._chat_history:
            for msg in list(ctx_for_replay._chat_history)[-20:]:
                try:
                    _queues[sid].put_nowait({
                        "type": "chat_message", "_user": uid_key,
                        "role": msg["role"], "content": msg["content"], "ts": msg["ts"]
                    })
                except asyncio.QueueFull: pass
    async def stream():
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(_queues[sid].get(), timeout=20)
                    if ev.get("_user") != uid_key: continue
                    if "_raw_rpc" in ev:
                        # sampling/createMessage request → envoyé tel quel au client MCP
                        yield f"data: {json.dumps(ev['_raw_rpc'])}\n\n"
                    elif ev.get("type") == "mcp_notification":
                        yield f"data: {json.dumps({'jsonrpc':'2.0','method':ev['method'],'params':ev['params']})}\n\n"
                    else:
                        yield f"data: {json.dumps(ev)}\n\n"
                except asyncio.TimeoutError:
                    # Vrai event heartbeat (le widget arme un watchdog dessus) —
                    # un commentaire ": ping" ne declenche pas EventSource.onmessage.
                    yield 'data: {"type":"heartbeat"}\n\n'
        finally:
            _queues.pop(sid, None)
            _sid_uid.pop(sid, None)
            _sid_token.pop(sid, None)
            _display_sids.discard(sid)
            # Nettoyer le sid MCP client si c'était lui + cancel any sampling
            # waiters that were targeting this disconnected client
            if is_mcp_client and _mcp_client_sids.get(uid_key) == sid:
                _mcp_client_sids.pop(uid_key, None)
                # Cancel any pending sampling futures: the client is gone, no
                # response will ever arrive. Without this they wait the full 120s.
                for waiter_id in list(_sampling_waiters.keys()):
                    fut = _sampling_waiters.get(waiter_id)
                    if fut and not fut.done():
                        fut.set_exception(Exception("MCP client disconnected"))
                        _sampling_waiters.pop(waiter_id, None)
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Mcp-Session-Id": sid})


@app.delete("/mcp")
async def mcp_delete(mcp_session_id: str | None = Header(default=None, alias="Mcp-Session-Id")):
    _queues.pop(mcp_session_id, None)
    _sid_uid.pop(mcp_session_id, None)
    _sid_token.pop(mcp_session_id, None)
    return Response(status_code=204)


@app.post("/register")
async def register(request: Request):
    data, err = await _read_json(request)
    if err: return err
    # Garde du pod : le widget presente APP_AUTH_TOKEN (header X-App-Token, query
    # app_token, ou champ appToken du body). Vide (dev) -> pas de garde.
    if APP_AUTH_TOKEN:
        tok = (request.headers.get("X-App-Token") or request.query_params.get("app_token")
               or (data.get("appToken") or "").strip())
        if not (tok and hmac.compare_digest(tok, APP_AUTH_TOKEN)):
            return JSONResponse({"error": "Garde du pod : appToken manquant ou invalide."},
                                status_code=401)
    access_token = data.get("accessToken", "").strip()
    grist_key    = data.get("gristKey", "").strip()
    site_url     = data.get("siteUrl", "").rstrip("/")
    doc_id       = data.get("docId", "")
    doc_title    = data.get("docTitle", "") or doc_id
    bearer = access_token or grist_key
    if not bearer:
        return JSONResponse({"error": "accessToken manquant"}, status_code=400)
    grist_user_id = None
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
    if not _owner_gate(uid_key):
        return JSONResponse({"error": "Pod verrouille sur un autre compte (owner-lock). "
                             "Ce pod appartient a son proprietaire ; connecte-toi avec ce compte."},
                            status_code=403)
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


@app.post("/run")
async def run_python(request: Request):
    """Execute Python canvas with Grist tables injected as context."""
    body, err = await _read_json(request)
    if err: return err
    token   = body.get("token", "")
    record  = body.get("record") or {}

    uid_key = _token_to_uid.get(token)
    if not uid_key:
        return JSONResponse({"error": "token inconnu"}, status_code=401)
    ctx = registry.resolve(uid_key, token)
    if not ctx:
        return JSONResponse({"error": "session introuvable"}, status_code=404)

    code = ctx.canvas
    if not code or not code.strip():
        return JSONResponse({"stdout": "", "stderr": "Canvas vide — sélectionner un artefact Python.",
                             "returncode": 1, "elapsed": 0})

    # Fetch all tables fresh from Grist (max 2000 rows each)
    tables: dict = {}
    try:
        schema_resp = await grist_get(ctx, "tables")
        for tbl in (schema_resp.get("tables") or []):
            tid = tbl["id"]
            try:
                data = await grist_get(ctx, f"tables/{tid}/records?limit=2000")
                tables[tid] = [{"id": r["id"], **r["fields"]} for r in data.get("records", [])]
            except Exception:
                pass
    except Exception:
        pass

    # Exécution isolée via subprocess (évite exec() in-process)
    wrapper = (
        "import json as _j, sys as _s\n"
        "_d = _j.loads(_s.stdin.read())\n"
        "tables = _d['tables']; record = _d['record']; rec = record\n"
    ) + code

    import tempfile, os, time as _time
    t0 = _time.time()
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(wrapper)
        fname = f.name
    try:
        r = subprocess.run(
            [sys.executable, fname],
            input=json.dumps({"tables": tables, "record": record}),
            capture_output=True, text=True, timeout=30,
        )
        rc = r.returncode
        out, err = r.stdout[-6000:], r.stderr[-2000:]
    except subprocess.TimeoutExpired:
        rc, out, err = 1, "", "timeout 30s"
    except Exception as exc:
        rc, out, err = 1, "", f"erreur subprocess: {type(exc).__name__}"
    finally:
        try: os.unlink(fname)
        except OSError: pass

    return JSONResponse({
        "stdout": out, "stderr": err,
        "returncode": rc, "elapsed": round(_time.time() - t0, 3),
    })


@app.post("/screenshot")
async def screenshot(request: Request):
    data, err = await _read_json(request)
    if err: return err
    token = data.get("token", "")
    image = data.get("image", "")
    fut   = _screenshot_waiters.pop(token, None)
    if fut and not fut.done():
        fut.set_result(image)
        return {"ok": True}
    return {"ok": False, "error": "Pas de waiter actif pour ce token."}


@app.post("/apply-result/{token}")
async def apply_result(token: str, request: Request):
    """Ack du widget apres execution navigateur des UserActions (voir _browser_apply).
    Body : {} en succes, ou {"error": "..."} si applyUserActions a echoue cote navigateur."""
    body, err = await _read_json(request, default={})
    if err: return err
    fut = _apply_waiters.pop(token, None)
    if fut and not fut.done():
        fut.set_result(body or {})
        return {"ok": True}
    return {"ok": False, "error": "Pas de waiter actif pour ce token."}


@app.post("/art-diag/{token}")
async def art_diag(token: str, request: Request):
    """Diagnostic de rendu remonte par un artefact (capteur injecte dans son iframe).

    Le capteur envoie plusieurs fois : a chaque erreur, puis apres mesure du rendu.
    On garde le dernier etat, cumulatif cote artefact, et on debloque canvas_write
    des que la mesure de rendu est arrivee — c'est elle qui clot l'observation."""
    body, err = await _read_json(request, default={})
    if err: return err
    diag = (body or {}).get("diag") or {}
    _dernier_diag[token] = {"artefact": (body or {}).get("artefact"), **diag}
    if diag.get("rendu") is not None:
        fut = _diag_waiters.pop(token, None)
        if fut and not fut.done():
            fut.set_result(_dernier_diag[token])
    return {"ok": True}


@app.post("/wizard/{token}")
async def wizard_response(token: str, request: Request):
    """Reçoit la réponse utilisateur depuis le widget wizard et débloque le tool call en attente."""
    uid_key = _token_to_uid.get(token)
    if not uid_key:
        return JSONResponse({"error": "token inconnu"}, status_code=404)
    ctx = registry.resolve(uid_key, token)
    if not ctx:
        return JSONResponse({"error": "session introuvable"}, status_code=404)
    body, err = await _read_json(request)
    if err: return err
    response = {**body, "ts": time.time()}
    ctx.wizard_responses.appendleft(response)  # conserve pour compat session_info
    step_id = body.get("step_id")
    if not step_id:
        # Sans step_id on ne peut router la reponse a aucun wizard precis :
        # ignorer plutot que debloquer tous les wizards en attente (corruption de flux).
        return {"ok": True, "warning": "step_id manquant — reponse ignoree"}
    # Stocker la reponse indexee par step_id (mode bloquant ET async) AVANT de debloquer,
    # pour que le lecteur bloquant lise exactement la reponse de SON step.
    ctx._async_wizard_responses[step_id] = response
    ev = ctx._wizard_events.get(step_id)
    if ev:
        ev.set()
    return {"ok": True}


@app.post("/chat/{token}")
async def chat_message(token: str, request: Request):
    """Reçoit un message utilisateur depuis l'interface chat du widget."""
    uid_key = _token_to_uid.get(token)
    if not uid_key:
        return JSONResponse({"error": "token inconnu"}, status_code=404)
    ctx = registry.resolve(uid_key, token)
    if not ctx:
        return JSONResponse({"error": "session introuvable"}, status_code=404)
    body, err = await _read_json(request)
    if err: return err
    message = body.get("message", "").strip()
    if not message:
        return JSONResponse({"error": "message vide"}, status_code=400)
    ts = time.time()
    # Stocker dans l'historique chat
    ctx._chat_history.append({"role": "user", "content": message, "ts": ts})
    # Echo immédiat au widget (bulle user)
    _push(uid_key, {"type": "chat_message", "token": token,
                    "role": "user", "content": message, "ts": ts})
    # Débloquer wait_for_chat si en attente
    fut = _chat_waiters.get(uid_key)
    if fut and not fut.done():
        fut.set_result(message)
    # Si le client supporte sampling → réponse automatique
    caps = _client_capabilities.get(uid_key, {})
    if "sampling" in caps:
        asyncio.create_task(_handle_chat_sample(uid_key, ctx, message))
    else:
        # Fallback : notifier Claude via resource pour qu'il lise chat/{token} et appelle chat_reply
        _notify_resource(uid_key, f"grist-coder://chat/{token}")
    return {"ok": True}


@app.post("/webhook-receive/{doc_id}")
async def webhook_receive(doc_id: str, request: Request):
    """Reçoit les événements webhook Grist et les fan-out via SSE aux widgets connectés.
    URL a configurer dans Grist : HOST_URL/webhook-receive/{docId}
    Fonctionne uniquement si HOST_URL est publiquement accessible (pas localhost).
    Protéger avec WEBHOOK_SECRET dans .env — ajouter l'en-tête X-Webhook-Secret côté Grist.
    """
    if WEBHOOK_SECRET:
        provided = request.headers.get("X-Webhook-Secret", "")
        if not secrets.compare_digest(provided, WEBHOOK_SECRET):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    notified = 0
    for uid, udata in registry._users.items():
        for token, ctx in udata.get("sessions", {}).items():
            if ctx.doc_id == doc_id:
                _push(uid, {"type": "webhook_event", "doc_id": doc_id, "payload": payload})
                notified += 1
    return {"ok": True, "notified": notified}


@app.get("/health")
async def health(request: Request):
    total = sum(len(u["sessions"]) for u in registry._users.values())
    base = {"ok": True, "version": "5.14", "mcp_protocol": MCP_VER,
            "sessions": total, "users": len(registry._users),
            "tools": len(TOOLS), "prompts": len(PROMPTS),
            "resources": {"static": len(STATIC_RESOURCES), "templates": len(RESOURCE_TEMPLATES)},
            "widget": WIDGET_PATH.exists()}
    # Optional diagnostic detail (?diag=1) — for debugging desync without log access
    if request.query_params.get("diag") == "1":
        active_arts = []
        wizard_pending = 0
        for u in registry._users.values():
            for tk, sctx in u.get("sessions", {}).items():
                if sctx.current_art_nom:
                    active_arts.append({"token": tk, "doc": sctx.doc_title,
                                        "art": sctx.current_art_nom, "type": sctx.current_art_type,
                                        "context": sctx.current_context,
                                        "wizard_cards": list(sctx._active_wizard_cards.keys())})
                wizard_pending += len(sctx._wizard_events)
        base["_diagnostic"] = {
            "queues": {sid[:8]: {"depth": q.qsize(), "display": sid in _display_sids}
                       for sid, q in _queues.items()},
            "wizard_pending_events": wizard_pending,
            "screenshot_waiters": len(_screenshot_waiters),
            "sampling_waiters": len(_sampling_waiters),
            "mcp_clients": len(_mcp_client_sids),
            "active_artefacts": active_arts,
        }
    return base


# ── SURVEY SCHEMA-CHECK — conformité schéma Grist <-> Survey Manifest ──────────
# Appelé server-to-server par le publish de qgis-sspcloud (_check_publish_ready) pour
# valider, AVANT publication d'un livrable d'enquête, que la table Reponses du doc Grist
# est alignée sur le manifest. Contrat convergence CEREMA (#survey-manifest).
@app.get("/survey/{doc_id}/schema-check")
async def survey_schema_check(doc_id: str, request: Request):
    manifest_url = request.query_params.get("manifest_url")
    table = request.query_params.get("table", "Reponses")
    if not manifest_url:
        return JSONResponse({"ok": False, "error": "manifest_url requis"}, status_code=400)
    # Auth : Bearer <grist_key> fourni par l'appelant, sinon clé de service server-side.
    auth = request.headers.get("Authorization", "")
    key = auth.split("Bearer ", 1)[1].strip() if auth.startswith("Bearer ") else ""
    if not key:
        key = os.getenv("GRIST_PROVISION_KEY", "").strip()
    if not key:
        return JSONResponse({"ok": False, "error": "auth requise : header Authorization: Bearer "
                             "<grist_key>, ou GRIST_PROVISION_KEY configuree server-side."}, status_code=401)
    # site_url : env, sinon une session widget active.
    site_url = os.getenv("GRIST_SITE_URL", "").strip()
    if not site_url:
        for u in registry._users.values():
            for sctx in u.get("sessions", {}).values():
                if getattr(sctx, "site_url", ""):
                    site_url = sctx.site_url
                    break
            if site_url:
                break
    if not site_url:
        return JSONResponse({"ok": False, "error": "GRIST_SITE_URL introuvable (ni env ni session active)."},
                            status_code=500)
    # 1) récupérer le manifest (contrat déclaratif).
    try:
        async with _grist_client() as c:
            rm = await c.get(manifest_url)
            rm.raise_for_status()
            manifest = rm.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": _scrub_secrets(f"manifest injoignable : {e}")}, status_code=502)
    # 2) récupérer les colonnes réelles de la table Reponses.
    ctx = SessionCtx(doc_id, "", site_url, grist_key=key)
    try:
        cols_resp = await grist_get(ctx, f"tables/{table}/columns")
        actual = {c["id"]: (c.get("fields", {}) or {}).get("type", "")
                  for c in (cols_resp.get("columns", []) or [])}
    except Exception as e:
        return JSONResponse({"ok": False, "error": _scrub_secrets(
            f"lecture schéma Grist échouée (doc {doc_id}, table {table}) : {e}")}, status_code=502)
    # 3) diff manifest <-> schéma réel.
    result = _survey_schema_diff(manifest, actual)
    result["doc_id"] = doc_id
    result["table"] = table
    return JSONResponse(result)


# ── LLM proxy (Albert, SSPCloud, etc.) — bypasses CORS for browser widgets ──

@app.api_route("/llm-proxy/{path:path}", methods=["GET", "POST", "OPTIONS"])
async def llm_proxy(path: str, request: Request):
    """Proxy LLM API calls to avoid CORS. Widget sends to /llm-proxy/..., we forward server-side.

    SECURITE : la cible doit figurer dans LLM_PROXY_ALLOWED_HOSTS (CSV en .env).
    Sans allowlist configuree, l endpoint est desactive (evite open-proxy / SSRF)."""
    import httpx
    if request.method == "OPTIONS":
        return Response(status_code=204, headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization, X-LLM-Base",
        })
    if not _check_app_token(request):
        return JSONResponse({"error": "Garde du pod : X-App-Token manquant ou invalide."},
                            status_code=401)
    if not LLM_PROXY_ALLOWED_HOSTS:
        return JSONResponse(
            {"error": "llm-proxy desactive : definir LLM_PROXY_ALLOWED_HOSTS dans .env"},
            status_code=403)
    # Base cible : header/query, sinon defaut serveur injecte par le chart (LLM_BASE_URL).
    target_base = (request.headers.get("X-LLM-Base") or request.query_params.get("base")
                   or LLM_BASE_URL_DEFAULT)
    if not target_base:
        return JSONResponse({"error": "Missing X-LLM-Base header or ?base= param"}, status_code=400)
    target_base = target_base.rstrip("/")
    parsed = urllib.parse.urlsplit(target_base)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return JSONResponse({"error": "URL cible invalide"}, status_code=400)
    if parsed.hostname.lower() not in LLM_PROXY_ALLOWED_HOSTS:
        return JSONResponse(
            {"error": f"Hote non autorise : {parsed.hostname}"}, status_code=403)
    target_url = f"{target_base}/{path}"
    # Relaye l'auth + les headers utiles aux API LLM (Anthropic exige anthropic-version).
    # Liste blanche pour ne pas propager d'en-tetes navigateur non pertinents.
    headers = {}
    if auth := request.headers.get("authorization"):
        headers["Authorization"] = auth
    else:
        # Le widget n'envoie pas de cle (deploiement Onyxia : cle server-side) ->
        # injecter la cle du pod (env explicite ou config datalab SSPCloud).
        srv_key = await _resolve_llm_key()
        if srv_key:
            headers["Authorization"] = f"Bearer {srv_key}"
    for h in ("anthropic-version", "anthropic-beta", "openai-organization", "x-api-key"):
        v = request.headers.get(h)
        if v:
            headers[h] = v
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            if request.method == "GET":
                resp = await client.get(target_url, headers=headers)
            else:
                body = await request.body()
                headers["Content-Type"] = "application/json"
                resp = await client.post(target_url, content=body, headers=headers)
        return Response(
            content=resp.content, status_code=resp.status_code,
            headers={
                "Content-Type": resp.headers.get("content-type", "application/json"),
                "Access-Control-Allow-Origin": "*",
            },
        )
    except Exception as e:
        return JSONResponse({"error": _scrub_secrets(str(e))}, status_code=502)


HARNESS_DIR = Path(__file__).parent / "harness"
_HARNESS_MIME = {".js": "application/javascript", ".css": "text/css",
                 ".json": "application/json", ".map": "application/json"}

@app.get("/harness/{filename:path}")
async def harness_file(filename: str):
    """Sert les modules JS du harness agent embarque (widget). Meme origine que le widget."""
    # Anti path-traversal : nom de fichier simple, pas de séparateur ni '..'
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        return JSONResponse({"error": "nom de fichier invalide"}, status_code=400)
    target = (HARNESS_DIR / filename).resolve()
    if not str(target).startswith(str(HARNESS_DIR.resolve())) or not target.is_file():
        return JSONResponse({"error": "fichier introuvable"}, status_code=404)
    mime = _HARNESS_MIME.get(target.suffix.lower(), "text/plain")
    return Response(target.read_bytes(), media_type=mime,
                    headers={"Cache-Control": "no-cache"})


@app.get("/", response_class=HTMLResponse)
async def widget_html():
    if WIDGET_PATH.exists():
        return HTMLResponse(WIDGET_PATH.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>widget.html manquant</h1>", status_code=503)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("grist_coder:app",
                host=os.getenv("HOST", "0.0.0.0"),
                port=int(os.getenv("PORT", "8742")),
                reload=os.getenv("RELOAD", "true").lower() == "true")
