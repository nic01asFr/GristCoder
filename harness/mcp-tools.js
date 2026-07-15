/*
 * harness/mcp-tools.js — window.HarnessTools
 *
 * ROLE : catalogue d'outils exposes au LLM + execution.
 *   - Outils "core" : recuperes via tools/list du serveur MCP, executes via tool().
 *   - Outils "synthetiques" (locaux) : ask_user / say, routes vers window.HarnessRender.
 *     Le plan/avancement (Zone 1) est pilote AUTOMATIQUEMENT par le harness depuis
 *     context/{token} (_inferred_plan) — pas un outil LLM.
 *
 * Dependances (globals installes par widget.html) :
 *   - window.tool(name, args)  -> POST /mcp tools/call, renvoie le resultat parse
 *   - window._token            -> token de session gc- (Bearer)
 *   - window.BASE              -> origin du serveur MCP
 *   - window.HarnessRender     -> { say(text), renderCard(step)->Promise, updatePlan(step) }
 *
 * Aucune dependance ES modules : chargement via <script src="/harness/mcp-tools.js">.
 */
(function (global) {
  'use strict';

  // ── Outils de coordination-pilote a EXCLURE ────────────────────────────────
  // Ils font double emploi avec le pilote local (le harness rend lui-meme les
  // cartes / le plan / le chat via HarnessRender) ou bloqueraient la boucle LLM
  // en attendant une reponse cote serveur (wait_for_chat).
  var BLOCKLIST = {
    canvas_wizard: 1,
    canvas_wizard_close: 1,
    plan_update: 1,
    canvas_context_update: 1,
    chat_reply: 1,
    wait_for_chat: 1,
    // Les specialistes sont pilotes par le harness (HarnessAgent.askSpecialist),
    // pas par le LLM : en fallback_mode subagent_call renvoie un system_prompt
    // (echo) que le LLM ne saurait pas exploiter. On le retire de sa palette.
    subagent_call: 1
  };

  // Prefixes des familles d'outils conservees (garde-fou secondaire, informatif).
  var ALLOWED_PREFIXES = ['canvas_', 'grist_', 'artefact_', 'session_', 'sessions_', 'subagent_'];

  // Cap de serialisation d'un resultat d'outil renvoye au LLM (evite de saturer
  // le contexte avec un gros dump JSON).
  var MAX_RESULT_CHARS = 20000;

  // ── Outils synthetiques locaux ─────────────────────────────────────────────
  // Executes cote navigateur via HarnessRender, jamais envoyes au serveur MCP.
  var SYNTHETIC = [
    {
      name: 'ask_user',
      description:
        'Pose une question a l\'utilisateur via une carte interactive (wizard) et attend sa reponse. ' +
        'Carte BLOQUANTE. Formats de card selon le besoin :\n' +
        '  - choix : {"type":"choice","title":"...","choices":[{"id":"a","label":"Option A"},...]} -> reponse {selected:id}\n' +
        '  - confirmation : {"type":"confirm","title":"...","content":"markdown","actions":[{"id":"ok","label":"Valider"}]}\n' +
        '  - formulaire : {"type":"form","title":"...","fields":[{"id":"nom","type":"text","label":"Nom"}]}\n' +
        '  - texte libre : {"type":"input","title":"...","placeholder":"..."} -> reponse {text}\n' +
        'Pour un choix, TOUJOURS fournir choices en tableau d\'objets {id,label}.',
      inputSchema: {
        type: 'object',
        properties: {
          card: {
            type: 'object',
            description: 'Carte wizard. Recommande : {type, title, choices|fields|actions|placeholder}.',
            properties: {
              type: { type: 'string', enum: ['choice', 'form', 'confirm', 'info', 'input'] },
              title: { type: 'string' },
              content: { type: 'string', description: 'Markdown (confirm/info)' },
              placeholder: { type: 'string', description: 'input : texte d\'aide' },
              choices: {
                type: 'array', description: 'choice : options',
                items: { type: 'object', properties: { id: { type: 'string' }, label: { type: 'string' }, desc: { type: 'string' } } }
              },
              fields: {
                type: 'array', description: 'form : champs',
                items: { type: 'object', properties: { id: { type: 'string' }, type: { type: 'string' }, label: { type: 'string' } } }
              },
              actions: {
                type: 'array', description: 'confirm : boutons',
                items: { type: 'object', properties: { id: { type: 'string' }, label: { type: 'string' } } }
              }
            },
            required: ['type', 'title']
          }
        },
        required: ['card']
      }
    },
    {
      name: 'say',
      description:
        'Affiche un message textuel de l\'assistant a l\'utilisateur dans le fil de ' +
        'discussion. Non bloquant. Utilise pour expliquer ce que tu fais, commenter un ' +
        'resultat, ou donner la reponse finale.',
      inputSchema: {
        type: 'object',
        properties: {
          text: { type: 'string', description: 'Texte a afficher.' }
        },
        required: ['text']
      }
    }
  ];

  var _synthNames = (function () {
    var s = {};
    for (var i = 0; i < SYNTHETIC.length; i++) s[SYNTHETIC[i].name] = SYNTHETIC[i];
    return s;
  })();

  // ── Etat interne ───────────────────────────────────────────────────────────
  // _tools : liste normalisee { name, description, inputSchema, synthetic } des
  // outils exposables au LLM (core retenus + synthetiques).
  var _tools = [];
  var _byName = {};
  var _loaded = false;

  function _isBlocked(name) {
    return !!BLOCKLIST[name];
  }

  function _matchesAllowed(name) {
    for (var i = 0; i < ALLOWED_PREFIXES.length; i++) {
      if (name.indexOf(ALLOWED_PREFIXES[i]) === 0) return true;
    }
    return false;
  }

  function _rebuildIndex() {
    _byName = {};
    for (var i = 0; i < _tools.length; i++) _byName[_tools[i].name] = _tools[i];
  }

  // ── load() : recupere le catalogue d'outils du core via tools/list ─────────
  async function load() {
    var token = global._token;
    var base = global.BASE || (global.location && global.location.origin) || '';
    if (!token) throw new Error('HarnessTools.load: widget non connecte (pas de _token)');

    var resp;
    try {
      resp = await fetch(base + '/mcp', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: 'Bearer ' + token
        },
        body: JSON.stringify({
          jsonrpc: '2.0',
          id: 'harness-tools-list',
          method: 'tools/list',
          params: {}
        })
      });
    } catch (e) {
      throw new Error('HarnessTools.load: echec reseau tools/list — ' + (e && e.message || e));
    }

    var data;
    try {
      data = await resp.json();
    } catch (e) {
      throw new Error('HarnessTools.load: reponse tools/list non-JSON (HTTP ' + resp.status + ')');
    }
    if (data && data.error) {
      throw new Error('HarnessTools.load: ' + (data.error.message || 'erreur tools/list'));
    }
    var coreTools = (data && data.result && data.result.tools) || [];

    var kept = [];
    for (var i = 0; i < coreTools.length; i++) {
      var t = coreTools[i];
      if (!t || !t.name) continue;
      if (_isBlocked(t.name)) continue;
      // Blocklist prioritaire ; on conserve tout le reste. Le garde-fou par
      // prefixe reste informatif et n'ecarte rien d'inattendu du core.
      kept.push({
        name: t.name,
        description: t.description || '',
        inputSchema: t.inputSchema || { type: 'object', properties: {} },
        synthetic: false
      });
    }

    // Ajoute les outils synthetiques locaux.
    var synth = [];
    for (var j = 0; j < SYNTHETIC.length; j++) {
      synth.push({
        name: SYNTHETIC[j].name,
        description: SYNTHETIC[j].description,
        inputSchema: SYNTHETIC[j].inputSchema,
        synthetic: true
      });
    }

    _tools = kept.concat(synth);
    _rebuildIndex();
    _loaded = true;
    return _tools.slice();
  }

  // NB : la conversion des definitions d'outils vers le format natif (openai/anthropic)
  // est faite par HarnessLLM (adapter.buildBody). Le loop passe list() (defs brutes avec
  // inputSchema) ; pas de conversion ici pour eviter une double logique.

  // ── Serialisation bornee d'un retour d'outil (pour reinjection au LLM) ──────
  function _serialize(value) {
    var s;
    if (typeof value === 'string') {
      s = value;
    } else if (value === undefined) {
      s = 'null';
    } else {
      try {
        s = JSON.stringify(value);
      } catch (e) {
        try {
          s = String(value);
        } catch (e2) {
          s = '[unserializable result]';
        }
      }
    }
    if (s == null) s = 'null';
    if (s.length > MAX_RESULT_CHARS) {
      s = s.slice(0, MAX_RESULT_CHARS) +
        '\n... [tronque : ' + (s.length - MAX_RESULT_CHARS) + ' caracteres supplementaires]';
    }
    return s;
  }

  // ── Execution d'un outil synthetique via HarnessRender ─────────────────────
  async function _execSynthetic(name, args) {
    var R = global.HarnessRender;
    if (!R) throw new Error('HarnessRender indisponible');
    args = args || {};

    if (name === 'ask_user') {
      var card = args.card || args; // tolere un appel a plat
      if (!card || typeof card !== 'object') {
        throw new Error('ask_user : argument "card" (objet) requis');
      }
      var answer = await R.renderCard(card);
      // answer = { type, values }
      return answer || {};
    }

    if (name === 'say') {
      var text = args.text;
      if (typeof text !== 'string') text = _serialize(text);
      R.say(text);
      return { ok: true };
    }

    throw new Error('Outil synthetique inconnu : ' + name);
  }

  // ── execute(name, args) : dispatch + serialisation + capture d'erreur ──────
  // Renvoie TOUJOURS une string destinee au LLM ; ne throw jamais, pour ne pas
  // casser la boucle de tool-calling (une erreur devient un message 'ERROR: ...').
  async function execute(name, args) {
    try {
      var result;
      if (_synthNames[name]) {
        result = await _execSynthetic(name, args);
      } else {
        if (typeof global.tool !== 'function') {
          throw new Error('fonction tool() indisponible dans le widget');
        }
        if (_loaded && !_byName[name]) {
          throw new Error('outil inconnu ou filtre : ' + name);
        }
        result = await global.tool(name, args || {});
      }
      return _serialize(result);
    } catch (e) {
      var msg = (e && e.message) ? e.message : String(e);
      return 'ERROR: ' + msg;
    }
  }

  // ── Accesseurs utilitaires ─────────────────────────────────────────────────
  function list() {
    return _tools.slice();
  }
  function has(name) {
    return !!_byName[name];
  }
  function isSynthetic(name) {
    return !!_synthNames[name];
  }
  function isLoaded() {
    return _loaded;
  }

  global.HarnessTools = {
    load: load,
    execute: execute,
    list: list,
    has: has,
    isSynthetic: isSynthetic,
    isLoaded: isLoaded,
    // Constantes exposees pour introspection / tests.
    BLOCKLIST: BLOCKLIST,
    SYNTHETIC_NAMES: Object.keys(_synthNames)
  };
})(typeof window !== 'undefined' ? window : this);
