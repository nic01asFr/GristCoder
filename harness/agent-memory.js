/*
 * harness/agent-memory.js — HarnessMemory
 *
 * ROLE : etat de conversation + plan + snapshot doc, compaction.
 *
 * Store neutre au pilote et a l'adaptateur LLM. Aucune dependance : ni reseau,
 * ni DOM, ni framework. Les messages sont au format NEUTRE :
 *     { role, content }                      (system / user / assistant texte)
 *     { role:'assistant', toolCalls:[...] }  (tour assistant porteur de tool_calls)
 *     { role:'tool', toolResults:[...] }     (resultats d'outils, un tour groupe)
 * L'adaptateur (llm-client.js) convertit ce format neutre vers OpenAI/Anthropic.
 *
 * Fabrique un store PAR DOCUMENT : HarnessMemory.create(docKey).
 *
 * NE FAIT JAMAIS D'APPEL RESEAU. La compaction recoit une summarizeFn (fournie
 * par agent-loop) qui, elle, fera l'appel LLM. Si summarizeFn echoue, on degrade
 * proprement (on saute la compaction) sans casser le tour courant.
 */
(function (global) {
  'use strict';

  // ---- Constantes de reglage -------------------------------------------------
  var CHARS_PER_TOKEN = 4;          // heuristique grossiere ~4 char/token
  var DEFAULT_TOKEN_THRESHOLD = 12000;
  var KEEP_RECENT_TURNS = 6;        // nb de messages recents preserves a la compaction
  var LS_PREFIX = 'gcHarnessConv:';
  var LS_MAX_CHARS = 200000;        // troncature de la persistance localStorage

  // ---- Helpers securises -----------------------------------------------------

  function safeStringify(v) {
    if (v == null) return '';
    if (typeof v === 'string') return v;
    try {
      return JSON.stringify(v);
    } catch (e) {
      try { return String(v); } catch (e2) { return ''; }
    }
  }

  function hasLocalStorage() {
    try {
      return typeof global.localStorage !== 'undefined' && global.localStorage !== null;
    } catch (e) {
      return false;
    }
  }

  // Estimation du poids d'un message en "tokens" (heuristique char/token).
  function messageChars(msg) {
    if (!msg || typeof msg !== 'object') return 0;
    var n = 0;
    if (typeof msg.role === 'string') n += msg.role.length;
    if (msg.content != null) n += safeStringify(msg.content).length;
    if (msg.toolCalls != null) n += safeStringify(msg.toolCalls).length;
    if (msg.toolResults != null) n += safeStringify(msg.toolResults).length;
    // petit overhead structurel par message
    return n + 8;
  }

  function isSystem(msg) {
    return msg && msg.role === 'system';
  }

  // ---- Fabrique de store -----------------------------------------------------

  function create(docKey) {
    var key = (docKey == null || docKey === '') ? '_default' : String(docKey);
    var lsKey = LS_PREFIX + key;

    var state = {
      messages: [],       // format neutre
      plan: null,         // dernier plan connu (objet libre)
      docSnapshot: null,  // snapshot schema Grist courant (objet libre)
      tokenThreshold: DEFAULT_TOKEN_THRESHOLD
    };

    // -- Persistance legere (best-effort) --------------------------------------

    function persist() {
      if (!hasLocalStorage()) return;
      try {
        var payload = {
          v: 1,
          docKey: key,
          messages: state.messages,
          plan: state.plan,
          docSnapshot: state.docSnapshot
        };
        var s = safeStringify(payload);
        if (s.length > LS_MAX_CHARS) {
          // Tronquer en jetant les messages les plus vieux (hors system) jusqu'a
          // rentrer dans le budget, plutot que de perdre toute la persistance.
          var trimmed = trimForStorage(payload);
          s = safeStringify(trimmed);
          if (s.length > LS_MAX_CHARS) return; // toujours trop gros -> on abandonne
        }
        global.localStorage.setItem(lsKey, s);
      } catch (e) {
        // quota depasse ou storage indispo -> ignorer silencieusement
      }
    }

    function trimForStorage(payload) {
      var msgs = payload.messages.slice();
      // Garder tous les system + une queue recente ; supprimer le milieu.
      while (safeStringify({ messages: msgs }).length > LS_MAX_CHARS && msgs.length > 1) {
        // trouver le premier message non-system a supprimer
        var idx = -1;
        for (var i = 0; i < msgs.length; i++) {
          if (!isSystem(msgs[i])) { idx = i; break; }
        }
        if (idx === -1) break; // que des system -> stop
        msgs.splice(idx, 1);
      }
      return {
        v: payload.v,
        docKey: payload.docKey,
        messages: msgs,
        plan: payload.plan,
        docSnapshot: payload.docSnapshot
      };
    }

    function restore() {
      if (!hasLocalStorage()) return;
      try {
        var raw = global.localStorage.getItem(lsKey);
        if (!raw) return;
        var parsed = JSON.parse(raw);
        if (parsed && Array.isArray(parsed.messages)) {
          state.messages = parsed.messages;
          state.plan = (typeof parsed.plan !== 'undefined') ? parsed.plan : null;
          state.docSnapshot = (typeof parsed.docSnapshot !== 'undefined') ? parsed.docSnapshot : null;
        }
      } catch (e) {
        // JSON corrompu -> repartir vide
      }
    }

    // -- API publique du store -------------------------------------------------

    function add(msg) {
      if (!msg || typeof msg !== 'object' || typeof msg.role !== 'string') {
        return store; // ignorer les messages malformes
      }
      // Copie superficielle defensive pour ne conserver que les champs connus.
      var m = { role: msg.role };
      if (typeof msg.content !== 'undefined') m.content = msg.content;
      if (typeof msg.toolCalls !== 'undefined') m.toolCalls = msg.toolCalls;
      if (typeof msg.toolResults !== 'undefined') m.toolResults = msg.toolResults;
      // preserver d'eventuels champs additionnels utiles (ex: name, tool_call_id)
      for (var k in msg) {
        if (Object.prototype.hasOwnProperty.call(msg, k) &&
            k !== 'role' && k !== 'content' && k !== 'toolCalls' && k !== 'toolResults') {
          m[k] = msg[k];
        }
      }
      state.messages.push(m);
      persist();
      return store;
    }

    function all() {
      return state.messages.slice();
    }

    function setPlan(p) {
      state.plan = (typeof p === 'undefined') ? null : p;
      persist();
      return store;
    }

    function getPlan() {
      return state.plan;
    }

    function setDoc(s) {
      state.docSnapshot = (typeof s === 'undefined') ? null : s;
      persist();
      return store;
    }

    function getDoc() {
      return state.docSnapshot;
    }

    function reset() {
      state.messages = [];
      state.plan = null;
      state.docSnapshot = null;
      if (hasLocalStorage()) {
        try { global.localStorage.removeItem(lsKey); } catch (e) { /* ignore */ }
      }
      return store;
    }

    function setThreshold(n) {
      var v = Number(n);
      if (isFinite(v) && v > 0) state.tokenThreshold = v;
      return store;
    }

    function estimateTokens() {
      var chars = 0;
      for (var i = 0; i < state.messages.length; i++) {
        chars += messageChars(state.messages[i]);
      }
      // Le plan courant compte aussi (il est reinjecte dans le contexte).
      if (state.plan != null) chars += safeStringify(state.plan).length;
      return Math.ceil(chars / CHARS_PER_TOKEN);
    }

    /*
     * compactIfNeeded(summarizeFn) -> Promise<boolean>
     *
     * Si estimateTokens() depasse le seuil, remplace les plus vieux tours par un
     * unique message system 'resume' produit via summarizeFn, tout en conservant :
     *   - TOUS les messages system existants (dont le system prompt),
     *   - les KEEP_RECENT_TURNS derniers messages,
     *   - le plan courant (state.plan reste intact, il n'est jamais compacte).
     *
     * summarizeFn(messagesNeutres) -> Promise<string> | string : un appel LLM
     * court FOURNI par agent-loop. HarnessMemory ne fait aucun reseau lui-meme.
     *
     * Robustesse : tout echec de summarizeFn (ou absence) => on saute la
     * compaction et on renvoie false, sans jeter (degrader != casser le tour).
     */
    function compactIfNeeded(summarizeFn) {
      var below = estimateTokens() <= state.tokenThreshold;
      if (below) return Promise.resolve(false);
      if (typeof summarizeFn !== 'function') return Promise.resolve(false);

      // Separer system (toujours conserves, en tete) du reste.
      var systems = [];
      var conversational = [];
      for (var i = 0; i < state.messages.length; i++) {
        if (isSystem(state.messages[i])) systems.push(state.messages[i]);
        else conversational.push(state.messages[i]);
      }

      // Rien a compacter si trop peu de tours conversationnels.
      if (conversational.length <= KEEP_RECENT_TURNS + 1) {
        return Promise.resolve(false);
      }

      var recent = conversational.slice(conversational.length - KEEP_RECENT_TURNS);
      var toSummarize = conversational.slice(0, conversational.length - KEEP_RECENT_TURNS);

      var result;
      try {
        result = summarizeFn(toSummarize.slice());
      } catch (e) {
        return Promise.resolve(false); // echec synchrone -> pas de compaction
      }

      return Promise.resolve(result).then(function (summaryText) {
        var text = safeStringify(summaryText).trim();
        if (!text) return false; // resume vide -> ne pas degrader la memoire

        var summaryMsg = {
          role: 'system',
          content: '[Resume des tours precedents]\n' + text,
          _compacted: true
        };

        // Reconstruire : system d'origine + message resume + tours recents.
        state.messages = systems.concat([summaryMsg], recent);
        persist();
        return true;
      }, function () {
        return false; // rejet de summarizeFn -> saut de compaction
      });
    }

    // Objet store expose.
    var store = {
      docKey: key,
      get messages() { return state.messages; }, // ref vive ; utiliser all() pour une copie
      get plan() { return state.plan; },
      get docSnapshot() { return state.docSnapshot; },
      add: add,
      all: all,
      setPlan: setPlan,
      getPlan: getPlan,
      setDoc: setDoc,
      getDoc: getDoc,
      reset: reset,
      setThreshold: setThreshold,
      estimateTokens: estimateTokens,
      compactIfNeeded: compactIfNeeded
    };

    // Charger une eventuelle conversation persistee pour ce doc.
    restore();

    return store;
  }

  var HarnessMemory = {
    create: create,
    // Constantes exposees pour introspection / tests.
    CHARS_PER_TOKEN: CHARS_PER_TOKEN,
    DEFAULT_TOKEN_THRESHOLD: DEFAULT_TOKEN_THRESHOLD,
    KEEP_RECENT_TURNS: KEEP_RECENT_TURNS
  };

  global.HarnessMemory = HarnessMemory;

})(typeof window !== 'undefined' ? window : this);
