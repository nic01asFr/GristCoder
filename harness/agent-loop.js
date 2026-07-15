// harness/agent-loop.js — window.HarnessAgent
// ---------------------------------------------------------------------------
// ROLE : boucle d'agent LLM cote navigateur.
//   - installe le pilote local (HarnessRender.setDriver('local'))
//   - charge les outils MCP (HarnessTools.load)
//   - seed le system prompt "construire une app Grist" + schema Grist courant
//   - orchestre les tours : user -> LLM -> tool-calls -> resultats -> LLM ...
//   - garde-fous : budget d'iterations, timeout par appel LLM (< 120s proxy),
//     etat 'busy' anti-concurrence, scrub de la cle LLM dans les erreurs.
//
// DEPENDANCES (autres modules du harness, memes namespaces window.*) :
//   HarnessRender  : setDriver(mode), say(text)                      [render-bridge.js]
//   HarnessTools   : load(), definitions()->[{name,description,inputSchema}], execute(name,args)  [mcp-tools.js]
//   HarnessMemory  : create()->mem ; mem.add(msg), mem.messages()->[],
//                    mem.setSystem(text), mem.setDoc(schema), mem.compactIfNeeded(summarizeFn)  [agent-memory.js]
//   HarnessLLM     : complete({provider, baseUrl, model, apiKey, maxTokens, system, messages, tools, signal})
//                    -> {text, toolCalls:[{id,name,args}], assistantMessage, stopReason}   [llm-client.js]
//                    ATTEND des messages NATIFS (openai/anthropic) ; c'est _toNative()
//                    ci-dessous qui convertit la memoire neutre -> natif au moment de l'appel.
//   HarnessConfig  : source de la cfg {provider, baseUrl, model, apiKey, maxTokens}  [config-panel.js]
//
// CONTRAT MEMOIRE (neutre) — l'agent n'ecrit que des messages normalises ;
// _toNative() traduit vers openai / anthropic juste avant l'appel LLM :
//   {role:'system',    content}
//   {role:'user',      content}
//   {role:'assistant', content, toolCalls:[{id,name,args}]}   // toolCalls optionnel
//   {role:'tool',      toolCallId, name, content}             // resultat d'un outil (string)
//
// CONTRAT DE BOUCLE (voir aussi review LOOP-CLOSURE) : le message ASSISTANT
// porteur des tool_calls est appende AVANT les tool_results, sinon l'API rejette
// (400) des le 2e tour.
// ---------------------------------------------------------------------------
(function () {
  'use strict';

  // ── Constantes / garde-fous ──────────────────────────────────────────────
  // Budget d'iterations "productives" (un tour d'outils = >=1 outil non ask_user).
  // Volontairement large : un build d'app enchaine plusieurs grist_/canvas_ calls.
  var MAX_ITERATIONS = 32;
  // Plafond dur sur le nombre total d'appels LLM d'un userTurn (borne les boucles
  // ask_user qui, elles, ne consomment pas MAX_ITERATIONS).
  var HARD_CAP = MAX_ITERATIONS * 4;
  // Timeout par appel LLM. STRICTEMENT < 120s (httpx timeout du proxy core) pour
  // que l'abort client precede le drop serveur.
  var TIMEOUT_MS = 115000;

  // ── Etat interne ─────────────────────────────────────────────────────────
  var _cfg = null;
  var _mem = null;
  var _tools = [];
  var _busy = false;
  var _started = false;
  var _stopped = false;
  var _activeController = null; // AbortController de l'appel LLM en cours

  // ── System prompt ────────────────────────────────────────────────────────
  var SYSTEM_PROMPT = [
    "Tu es l'agent Grist Coder : un assistant qui CONSTRUIT une application metier complete a l'interieur d'un document Grist, en dialoguant avec l'utilisateur directement dans le widget.",
    "",
    "Tu batis l'app via 4 couches, en t'appuyant sur les outils MCP disponibles :",
    "  1. Donnees      — tables Grist + colonnes/formules (grist_apply AddTable/AddColumn, grist_schema, grist_sql, grist_records*).",
    "  2. Interface    — artefacts HTML/React/markdown/... (canvas_write, canvas_patch, grist_upsert) et pages Grist (grist_view_create, grist_view_add_widget).",
    "  3. Logique      — CRUD et UserActions (grist_records_*, grist_apply), requetes (grist_sql).",
    "  4. Integrations — webhooks vers services externes (grist_webhooks).",
    "",
    "REGLES :",
    "- ELICITATION D'ABORD (au demarrage d'une demande de CONSTRUCTION d'app). Ne fonce PAS construire tete baissee :",
    "    1. Si le besoin n'est pas deja precis, pose 1 a 3 questions de CADRAGE via ask_user (choix ou formulaire) : nom de l'app, entites/donnees principales, qui l'utilise, besoin de tableau de bord et/ou de saisie/edition. Pose UNIQUEMENT l'essentiel qui change le plan ; n'inonde pas de questions.",
    "    2. PUIS, AVANT d'ecrire la moindre table/artefact, presente le PLAN via ask_user (type confirm) : liste concise des tables (avec colonnes cles), des artefacts et des pages que tu vas creer. Demande validation. Adapte le plan a la reponse.",
    "    3. Ne construis qu'APRES validation du plan.",
    "  Echappatoire : si l'utilisateur a deja tout precise, ou dit 'fais au mieux' / 'vas-y' / 'peu importe', n'insiste pas — propose un plan par defaut (ou construis directement pour une demande simple et sans ambiguite).",
    "- REUTILISE l'existant PERTINENT. Avant de creer, inspecte : appelle grist_schema pour les tables et regarde les artefacts deja presents. N'implemente que l'optimal et peu complexe au regard de ce qui existe. Pas de sur-architecture.",
    "- MAIS : la presence de tables/artefacts SANS RAPPORT avec la demande courante ne signifie PAS que la tache est faite. Le but est defini par la DEMANDE de l'utilisateur, pas par l'etat du document. Si ce que l'utilisateur demande n'existe pas encore, CONSTRUIS-le. Ne reste pas a analyser/verifier un document deja rempli d'autres choses : agis, ecris (grist_apply AddTable, grist_upsert+canvas_write). Une seule lecture de schema suffit — ne la repete pas en boucle.",
    "- CONTEXTE ARTEFACT : les outils canvas_* operent sur 'l'artefact selectionne'. AVANT tout canvas_write/canvas_patch/canvas_read, etablis ce contexte : cree/initialise l'artefact (grist_upsert avec Code vide PUIS canvas_write, ou artefact_init) et selectionne-le (canvas_select). Ne suppose jamais qu'un artefact est deja selectionne.",
    "- DIALOGUE : pour PARLER a l'utilisateur, appelle l'outil say. Pour lui POSER une question ou demander une decision, appelle ask_user (formulaire/choix rendus dans le widget). L'avancement est affiche AUTOMATIQUEMENT (barre de plan) : tu n'as PAS a annoncer chaque action.",
    "- SOIS ECONOME EN MOTS : n'ecris PAS un say avant chaque outil. Enchaine les outils directement. Utilise say UNIQUEMENT pour (1) un jalon important, (2) une question via ask_user, (3) le resume final. Pas de 'je vais...', pas de 'X ajoute !', pas d'excuses.",
    "- INTERACTIVITE (qualite) : un artefact d'application n'est JAMAIS un simple affichage passif. Des que l'utilisateur doit consulter ET agir sur des donnees, l'artefact DOIT embarquer de l'interaction reelle cablee au document via le bridge Grist :",
    "    * init : dans l'artefact, appelle grist.ready({requiredAccess:'full'}) puis charge les donnees avec grist.docApi.fetchTable('MaTable') (renvoie {colId:[valeurs...]}).",
    "    * AJOUTER une ligne (formulaire) : grist.docApi.applyUserActions([['BulkAddRecord','MaTable',[null],{Colonne:valeur, Ref:rowId}]]) — depuis un artefact navigateur utilise BulkAddRecord (add-only), JAMAIS BulkAddOrReplaceRecord (bloque cote navigateur).",
    "    * MODIFIER une ligne : applyUserActions([['UpdateRecord','MaTable',rowId,{Colonne:valeur}]]).",
    "    * rafraichir apres ecriture : re-fetch (fetchTable) ou grist.onRecords(cb). Toujours re-render apres une action.",
    "  Concretement : un tableau de bord a des filtres/actions ; une fiche a un formulaire d'edition ; une liste a un bouton d'ajout. Pas d'artefact 'lecture seule' quand la saisie/edition a du sens.",
    "- Ne demande a l'humain que ce qui est reellement necessaire (besoin, choix structurants). Sinon, avance.",
    "- ERREUR 500 sur un outil (grist_apply, grist_records_*) : c'est le plus souvent un alea TRANSITOIRE de l'instance (WAF), PAS un document casse. Le serveur reessaie deja tout seul. Si tu la vois quand meme : attends implicitement puis REESSAIE LA MEME action une ou deux fois. Ne conclus JAMAIS que le document est casse, ne demande JAMAIS a l'utilisateur de creer un nouveau document. Persiste : cree les tables une par une si un lot echoue.",
    "- Sois concis et factuel. Reponds en francais.",
    "- Quand la demande est satisfaite, resume ce qui a ete construit via say et arrete."
  ].join("\n");

  // ── Accesseurs defensifs (tolerent des variantes de nommage entre modules) ─
  function _render() { return window.HarnessRender || null; }

  function _say(text) {
    if (text == null || text === '') return;
    var R = _render();
    try {
      if (R && typeof R.say === 'function') { R.say(String(text)); return; }
    } catch (_) { /* ignore */ }
    // Fallback ultime : reutilise directement le rendu chat du widget.
    try { if (typeof window.appendChatMsg === 'function') window.appendChatMsg('assistant', String(text)); } catch (__) {}
  }

  function _setDriverLocal() {
    var R = _render();
    if (R && typeof R.setDriver === 'function') { try { R.setDriver('local'); } catch (_) {} }
  }

  function _toolDefinitions() {
    var T = window.HarnessTools;
    if (!T) return [];
    var fns = [T.definitions, T.list, T.tools, T.getDefinitions];
    for (var i = 0; i < fns.length; i++) {
      if (typeof fns[i] === 'function') {
        try {
          var out = fns[i].call(T);
          if (out && out.length != null) return out;
        } catch (_) {}
      }
    }
    if (T.definitions && T.definitions.length != null) return T.definitions;
    return [];
  }

  function _execTool(name, args) {
    var T = window.HarnessTools;
    if (!T || typeof T.execute !== 'function') {
      return Promise.reject(new Error('HarnessTools.execute indisponible'));
    }
    return Promise.resolve().then(function () { return T.execute(name, args); });
  }

  function _memAdd(msg) { if (_mem && typeof _mem.add === 'function') _mem.add(msg); }

  function _memMessages() {
    if (!_mem) return [];
    var fns = ['messages', 'getMessages', 'list', 'all'];
    for (var i = 0; i < fns.length; i++) {
      if (typeof _mem[fns[i]] === 'function') {
        try { var m = _mem[fns[i]](); if (m && m.length != null) return m; } catch (_) {}
      }
    }
    return _mem.messages && _mem.messages.length != null ? _mem.messages : [];
  }

  function _memSetSystem(text) {
    if (!_mem) return;
    if (typeof _mem.setSystem === 'function') { try { _mem.setSystem(text); return; } catch (_) {} }
    _memAdd({ role: 'system', content: text });
  }

  function _memSetDoc(schema) {
    if (!_mem) return;
    if (typeof _mem.setDoc === 'function') { try { _mem.setDoc(schema); } catch (_) {} }
  }

  // Convertit la memoire NEUTRE ({role, content, toolCalls, toolCallId}) vers le
  // format NATIF attendu par HarnessLLM.complete (openai ou anthropic). Les messages
  // system sont extraits et concatenes dans `system` (passe a part, jamais dans le
  // tableau messages). C'est ici que se ferme la boucle tool_use -> tool_result.
  function _toNative(neutral, provider) {
    neutral = neutral || [];
    var systemParts = [];
    var msgs = [];
    var pending = []; // anthropic : regroupe les tool_result consecutifs en 1 message user
    function flush() {
      if (pending.length) { msgs.push({ role: 'user', content: pending }); pending = []; }
    }
    for (var i = 0; i < neutral.length; i++) {
      var m = neutral[i] || {};
      if (m.role === 'system') { if (m.content) systemParts.push(String(m.content)); continue; }
      if (provider === 'anthropic') {
        if (m.role === 'user') { flush(); msgs.push({ role: 'user', content: String(m.content || '') }); }
        else if (m.role === 'assistant') {
          flush();
          var blocks = [];
          if (m.content) blocks.push({ type: 'text', text: String(m.content) });
          (m.toolCalls || []).forEach(function (tc) {
            blocks.push({ type: 'tool_use', id: tc.id, name: tc.name, input: tc.args || {} });
          });
          msgs.push({ role: 'assistant', content: blocks.length ? blocks : String(m.content || '') });
        } else if (m.role === 'tool') {
          pending.push({ type: 'tool_result', tool_use_id: m.toolCallId,
                         content: String(m.content == null ? '' : m.content) });
        }
      } else { // openai-compatible (Albert / etalab)
        if (m.role === 'user') msgs.push({ role: 'user', content: String(m.content || '') });
        else if (m.role === 'assistant') {
          var am = { role: 'assistant', content: m.content ? String(m.content) : '' };
          if (m.toolCalls && m.toolCalls.length) {
            am.tool_calls = m.toolCalls.map(function (tc) {
              var a; try { a = JSON.stringify(tc.args || {}); } catch (_) { a = '{}'; }
              return { id: tc.id, type: 'function', function: { name: tc.name, arguments: a } };
            });
            if (!am.content) am.content = null; // openai : content null autorise avec tool_calls
          }
          msgs.push(am);
        } else if (m.role === 'tool') {
          msgs.push({ role: 'tool', tool_call_id: m.toolCallId,
                      content: String(m.content == null ? '' : m.content) });
        }
      }
    }
    if (provider === 'anthropic') flush();
    return { system: systemParts.join('\n\n'), messages: msgs };
  }

  // ── Scrub de la cle LLM dans les messages d'erreur ────────────────────────
  function normalizeError(err) {
    var msg = (err && err.message) ? err.message : String(err);
    var key = _cfg && _cfg.apiKey;
    if (key && key.length > 6) {
      try { msg = msg.split(key).join('***'); } catch (_) {}
    }
    // Masque aussi tout token Bearer eventuellement inclus dans un dump.
    msg = msg.replace(/Bearer\s+[A-Za-z0-9._\-]+/g, 'Bearer ***');
    return msg;
  }

  // ── Appel LLM : convertit la memoire neutre -> natif, injecte la config du
  // panneau, delegue a HarnessLLM.complete (qui gere son propre timeout 115s < proxy).
  function _callLLM(neutralMsgs, tools, maxTokensOverride) {
    var L = window.HarnessLLM;
    if (!L || typeof L.complete !== 'function') {
      return Promise.reject(new Error('HarnessLLM.complete indisponible'));
    }
    var provider = (_cfg && _cfg.provider) || 'openai';
    var conv = _toNative(neutralMsgs, provider);
    var ctrl = null;
    try { ctrl = new AbortController(); } catch (_) { ctrl = null; }
    _activeController = ctrl;
    return L.complete({
      provider: provider,
      baseUrl:  _cfg && _cfg.baseUrl,
      model:    _cfg && _cfg.model,
      apiKey:   _cfg && _cfg.apiKey,
      maxTokens: maxTokensOverride || (_cfg && _cfg.maxTokens) || 2048,
      temperature: (_cfg && typeof _cfg.temperature === 'number') ? _cfg.temperature : 0.25,
      system:   conv.system,
      messages: conv.messages,
      tools:    tools || [],
      signal:   ctrl ? ctrl.signal : undefined
    }).finally(function () {
      if (_activeController === ctrl) _activeController = null;
    });
  }

  // ── Sous-agents specialistes (WS0) ─────────────────────────────────────────
  // Reutilise les prompts de roles du core SANS duplication : subagent_call en
  // fallback_mode renvoie {system_prompt, task}, puis on fait un appel LLM dedie
  // (hors memoire principale, sans outils). Roles : data-architect, ui-designer,
  // page-architect, ux-navigator, data-analyst, integrator, assistant.
  function _extractJson(text) {
    if (typeof text !== 'string') return null;
    // Tente le premier objet JSON equilibre dans le texte (gemma prefixe parfois).
    var start = text.indexOf('{');
    if (start < 0) return null;
    var depth = 0, inStr = false, esc = false;
    for (var i = start; i < text.length; i++) {
      var ch = text[i];
      if (esc) { esc = false; continue; }
      if (ch === '\\') { esc = true; continue; }
      if (ch === '"') { inStr = !inStr; continue; }
      if (inStr) continue;
      if (ch === '{') depth++;
      else if (ch === '}') { depth--; if (depth === 0) {
        try { return JSON.parse(text.slice(start, i + 1)); } catch (_) { return null; }
      } }
    }
    return null;
  }

  // _askSpecialist(role, task, context?) -> Promise<objet structure | {response} | {error}>
  function _askSpecialist(role, task, context) {
    role = role || 'assistant';
    var toolFn = window.tool;
    if (typeof toolFn !== 'function') {
      return Promise.resolve({ error: 'fonction tool() indisponible' });
    }
    return Promise.resolve()
      .then(function () {
        return toolFn('subagent_call', { role: role, task: String(task || ''), context: String(context || '') });
      })
      .then(function (res) {
        // Chemin sampling (client MCP capable) : reponse deja produite par le core.
        if (res && res.structured) return res.structured;
        if (res && res.response) {
          return _extractJson(res.response) || { response: String(res.response) };
        }
        // Chemin fallback (navigateur, defaut) : on execute nous-memes le prompt.
        var sys = res && res.system_prompt;
        if (!sys) return { error: 'subagent_call: pas de system_prompt pour role ' + role };
        var userMsg = res.task || String(task || '');
        return _callLLM([
          { role: 'system', content: sys },
          { role: 'user', content: userMsg }
        ], [], 1536).then(function (r) {
          var text = (r && (r.text != null ? r.text : r.content)) || '';
          return _extractJson(text) || { response: String(text) };
        });
      })
      .catch(function (e) {
        return { error: (e && e.message) ? e.message : String(e) };
      });
  }

  // _askSpecialistsParallel([{role,task,context?}, ...]) -> Promise<Array>
  // SSPCloud illimite -> consultation en parallele. Cap prudent (6 concurrents)
  // pour ne pas saturer le proxy ; au-dela, on met en file par lots.
  function _askSpecialistsParallel(specs, concurrency) {
    specs = Array.isArray(specs) ? specs : [];
    var cap = concurrency || 6;
    var out = new Array(specs.length);
    var idx = 0;
    function worker() {
      if (idx >= specs.length) return Promise.resolve();
      var my = idx++;
      var s = specs[my] || {};
      return _askSpecialist(s.role, s.task, s.context).then(function (r) {
        out[my] = r;
        return worker();
      });
    }
    var starters = [];
    for (var k = 0; k < Math.min(cap, specs.length); k++) starters.push(worker());
    return Promise.all(starters).then(function () { return out; });
  }

  // ── Compaction : resume court via un appel LLM sans outils ─────────────────
  function _summarize(payload) {
    var content;
    try { content = (typeof payload === 'string') ? payload : JSON.stringify(payload); }
    catch (_) { content = String(payload); }
    if (content && content.length > 12000) content = content.slice(0, 12000);
    return _callLLM([
      { role: 'system', content: 'Resume en 6 lignes maximum le contexte de conversation suivant : garde le besoin utilisateur, les decisions prises, et les tables/artefacts crees. Ne fais aucun appel d\'outil.' },
      { role: 'user', content: content }
    ], [], 400).then(function (r) {
      return (r && (r.text != null ? r.text : r.content)) || '';
    }).catch(function () {
      return ''; // degrade : l'echec de compaction ne doit jamais casser le tour
    });
  }

  function _maybeCompact() {
    if (!_mem || typeof _mem.compactIfNeeded !== 'function') return Promise.resolve();
    return Promise.resolve()
      .then(function () { return _mem.compactIfNeeded(_summarize); })
      .catch(function () { /* compaction best-effort */ });
  }

  // ── Normalisation de la reponse LLM ───────────────────────────────────────
  function _normResp(resp) {
    resp = resp || {};
    var toolCalls = resp.toolCalls || resp.tool_calls || [];
    if (!toolCalls || toolCalls.length == null) toolCalls = [];
    var text = (resp.text != null) ? resp.text
             : (resp.content != null) ? resp.content
             : '';
    return { text: text, toolCalls: toolCalls };
  }

  function _stringifyResult(res) {
    if (res == null) return '';
    if (typeof res === 'string') return res;
    try { return JSON.stringify(res); } catch (_) { return String(res); }
  }

  // ── Seed du schema Grist courant dans la memoire + system prompt ──────────
  function _seedContext() {
    // Produit le docSnapshot manquant : appelle grist_schema au demarrage.
    return _execTool('grist_schema', {}).then(function (schema) {
      _memSetDoc(schema);
      var summary;
      try { summary = JSON.stringify(schema); } catch (_) { summary = ''; }
      if (summary && summary.length > 4000) summary = summary.slice(0, 4000) + ' …(tronque)';
      var note = "Schema Grist actuel du document (etat initial, tables et colonnes existantes a REUTILISER) :\n" +
                 (summary || '(schema indisponible)');
      _memAdd({ role: 'system', content: note });
    }).catch(function (e) {
      // Non bloquant : document non connecte / standalone / outil absent.
      _memAdd({ role: 'system', content: 'Schema Grist initial indisponible (' + normalizeError(e) + '). Appelle grist_schema toi-meme avant de creer des tables.' });
    });
  }

  // ── Plan pilote par le CONTEXTE SERVEUR (context/{token} -> _inferred_plan) ─
  // Le core calcule deja, a partir de l'etat REEL du doc, le status + la
  // completude + les anomalies. On lit ce snapshot et on le projette sur le
  // bandeau de plan : progression deterministe et juste, sans dependre de la
  // discipline du LLM. Throttle pour ne pas marteler le serveur.
  var _lastPlanFetch = 0;
  var _planFetchInFlight = false;

  function _fetchContextSnapshot() {
    var token = window._token;
    var base = window.BASE || (window.location && window.location.origin) || '';
    if (!token) return Promise.resolve(null);
    return fetch(base + '/mcp', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token },
      body: JSON.stringify({
        jsonrpc: '2.0', id: 'harness-ctx', method: 'resources/read',
        params: { uri: 'grist-coder://context/' + token }
      })
    }).then(function (r) { return r.json(); }).then(function (data) {
      var c = data && data.result && data.result.contents && data.result.contents[0];
      if (!c || !c.text) return null;
      try { return JSON.parse(c.text); } catch (_) { return null; }
    }).catch(function () { return null; });
  }

  function _planStepFromContext(snap) {
    var inf = (snap && snap._inferred_plan) || {};
    var phase = inf.status || 'building';
    var pct = Math.round(((typeof inf.completeness === 'number') ? inf.completeness : 0) * 100);
    var tables = (snap.tables || []).filter(function (t) { return String(t).toLowerCase() !== 'artefacts'; });
    var arts = (snap.artefacts || []).filter(function (a) { return a && !a.isDoc; });
    var pages = snap.pages || [];
    var quality = snap._quality || [];
    var sections = [
      { label: 'Tables', style: 'code',
        content: String(tables.length) + (tables.length ? ' — ' + tables.slice(0, 4).join(', ') + (tables.length > 4 ? '…' : '') : '') },
      { label: 'Artefacts', style: 'code', content: String(arts.length) },
      { label: 'Pages', style: (pages.length ? 'code' : 'warn'), content: String(pages.length) }
    ];
    if (quality.length) sections.push({ label: 'A finaliser', style: 'warn', content: quality.length + ' point(s)' });
    return {
      id: 'ctx-plan-progress', type: 'progress', phase: phase, progress: pct,
      title: 'Plan · construction en cours', sections: sections
    };
  }

  function _refreshPlanFromContext(force) {
    var now = Date.now();
    if (!force && (_planFetchInFlight || (now - _lastPlanFetch) < 1500)) return;
    _planFetchInFlight = true; _lastPlanFetch = now;
    _fetchContextSnapshot().then(function (snap) {
      if (!snap) return;
      var step = _planStepFromContext(snap);
      var R = _render();
      if (R && typeof R.updatePlan === 'function') { try { R.updatePlan(step); } catch (_) {} }
    }).catch(function () {}).then(function () { _planFetchInFlight = false; });
  }

  // ── API publique ──────────────────────────────────────────────────────────

  // start(cfg) : initialise le pilote local, charge les outils, cree la memoire,
  // seed le system prompt + le schema Grist. Ne declenche PAS de tour.
  function start(cfg) {
    _cfg = cfg || (window.HarnessConfig && typeof window.HarnessConfig.get === 'function' ? window.HarnessConfig.get() : {}) || {};
    _stopped = false;

    // 1. Pilote local : le rendu (cartes, chat) route desormais vers l'agent local.
    _setDriverLocal();

    // 2. Charge les definitions d'outils MCP (+ synthetiques ask_user/say).
    var loadP = (window.HarnessTools && typeof window.HarnessTools.load === 'function')
      ? Promise.resolve().then(function () { return window.HarnessTools.load(); })
      : Promise.resolve();

    return loadP.then(function () {
      _tools = _toolDefinitions();

      // 3. Memoire de conversation + system prompt.
      //    Cle PAR SESSION (token) pour isoler les docs, + RESET a chaque Lancer :
      //    sinon create() restaure et ACCUMULE l'ancienne conversation (system prompt
      //    duplique, schemas de docs differents empiles, contexte gonfle a 15k+ tokens).
      if (window.HarnessMemory && typeof window.HarnessMemory.create === 'function') {
        var docKey = (typeof window._token === 'string' && window._token) ? window._token : '_default';
        _mem = window.HarnessMemory.create(docKey);
        if (typeof _mem.reset === 'function') { try { _mem.reset(); } catch (_) {} }
      } else {
        // Fallback minimal si le module memoire n'est pas encore charge.
        _mem = _fallbackMemory();
      }
      _memSetSystem(SYSTEM_PROMPT);

      // 4. Seed du contexte Grist (schema).
      return _seedContext();
    }).then(function () {
      _started = true;
      return true;
    }).catch(function (e) {
      _say('Initialisation de l\'agent impossible : ' + normalizeError(e));
      throw e;
    });
  }

  // userTurn(text) : traite un message utilisateur. L'echo user est deja fait
  // par le wrapper sendChat (render-bridge). Ici : append user -> boucle
  // complete()/execute() jusqu'a reponse finale ou garde-fou.
  function userTurn(text) {
    if (!_started) {
      _say('Agent non demarre. Configure et lance l\'agent d\'abord.');
      return Promise.resolve();
    }
    if (_busy) {
      _say('Un instant, je termine l\'action en cours…');
      return Promise.resolve();
    }
    if (text == null || String(text).trim() === '') return Promise.resolve();

    _busy = true;
    _stopped = false;
    // Relit la config a chaud : un Enregistrer dans le panneau s'applique au prochain
    // message sans avoir a re-Lancer l'agent.
    if (window.HarnessConfig && typeof window.HarnessConfig.get === 'function') {
      try { _cfg = window.HarnessConfig.get() || _cfg; } catch (_) {}
    }
    _memAdd({ role: 'user', content: String(text) });

    var iter = 0;        // tours d'outils "productifs" (>=1 outil non ask_user)
    var completions = 0; // total d'appels LLM (borne dure)

    function step() {
      if (_stopped) return Promise.resolve();
      if (iter >= MAX_ITERATIONS || completions >= HARD_CAP) {
        _say("J'ai atteint la limite d'etapes pour ce tour et je m'arrete ici. Dis-moi comment continuer, ou reformule si besoin.");
        return Promise.resolve();
      }
      completions++;

      return _callLLM(_memMessages(), _tools).then(function (raw) {
        var resp = _normResp(raw);

        // Pas de tool call -> texte final, fin de tour.
        if (!resp.toolCalls.length) {
          _say(resp.text || '');
          return Promise.resolve();
        }

        // Texte accompagnant les tool calls : le montrer AVANT d'agir (pas de silence).
        if (resp.text) _say(resp.text);

        // LOOP-CLOSURE : appende l'assistant porteur des tool_calls AVANT les resultats.
        _memAdd({ role: 'assistant', content: resp.text || '', toolCalls: resp.toolCalls });

        // Execute chaque outil en sequence, append chaque resultat.
        var productive = false;
        var chain = Promise.resolve();
        resp.toolCalls.forEach(function (tc) {
          chain = chain.then(function () {
            if (_stopped) return;
            var name = tc.name;
            var args = tc.args || tc.arguments || {};
            if (name !== 'ask_user') productive = true;
            return _execTool(name, args).then(function (res) {
              _memAdd({ role: 'tool', toolCallId: tc.id, name: name, content: _stringifyResult(res) });
            }).catch(function (err) {
              // Un echec d'outil n'arrete pas la boucle : on renvoie l'erreur au LLM.
              _memAdd({ role: 'tool', toolCallId: tc.id, name: name, content: 'Erreur outil ' + name + ' : ' + normalizeError(err) });
            });
          });
        });

        return chain.then(function () {
          if (productive) { iter++; _refreshPlanFromContext(false); }  // plan live depuis le contexte reel
          return step(); // re-complete avec les resultats en memoire
        });
      }).catch(function (err) {
        // Erreur reseau / JSON / timeout / abort -> arret propre du tour (pas de crash).
        if (_stopped) return Promise.resolve();
        _say('Erreur pendant le tour : ' + normalizeError(err));
        return Promise.resolve();
      });
    }

    return step()
      .then(function () { _refreshPlanFromContext(true); return _maybeCompact(); })  // plan final depuis l'etat reel
      .catch(function (e) {
        // Dernier filet : ne jamais laisser une exception s'echapper de la boucle.
        _say('Erreur inattendue : ' + normalizeError(e));
      })
      .finally(function () { _busy = false; });
  }

  // stop() : interrompt le tour en cours (abort de l'appel LLM) proprement.
  function stop() {
    _stopped = true;
    if (_activeController) { try { _activeController.abort(); } catch (_) {} }
    _busy = false;
  }

  function isBusy() { return _busy; }
  function isStarted() { return _started; }

  // ── Memoire de secours (si agent-memory.js absent) ────────────────────────
  function _fallbackMemory() {
    var _msgs = [];
    var _sys = null;
    return {
      add: function (m) { _msgs.push(m); },
      messages: function () { return (_sys ? [_sys] : []).concat(_msgs); },
      setSystem: function (t) { _sys = { role: 'system', content: t }; },
      setDoc: function () {},
      compactIfNeeded: function () { return Promise.resolve(); }
    };
  }

  // ── Expose ────────────────────────────────────────────────────────────────
  window.HarnessAgent = {
    start: start,
    userTurn: userTurn,
    stop: stop,
    isBusy: isBusy,
    isStarted: isStarted,
    // Sous-agents specialistes (WS0) : reutilisent les prompts de roles du core.
    askSpecialist: _askSpecialist,
    askSpecialistsParallel: _askSpecialistsParallel,
    // Constantes exposees pour reglage/diagnostic depuis le panneau de config.
    MAX_ITERATIONS: MAX_ITERATIONS,
    TIMEOUT_MS: TIMEOUT_MS,
    SYSTEM_PROMPT: SYSTEM_PROMPT
  };
})();
