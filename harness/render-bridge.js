/*
 * harness/render-bridge.js — window.HarnessRender
 *
 * ROLE : API de rendu NEUTRE au pilote. Point de convergence entre deux
 * producteurs d'evenements d'interface :
 *   - le pilote "sse"   : un agent LLM EXTERNE (Claude Desktop) dont les steps
 *                         arrivent par le flux SSE. listenSSE() appelle deja
 *                         showWizardCard / appendChatMsg / updatePlanBanner
 *                         DIRECTEMENT (widget.html). HarnessRender ne fait que
 *                         preserver ce comportement historique intact.
 *   - le pilote "local" : l'agent LLM qui VIT DANS LE NAVIGATEUR (HarnessAgent).
 *                         Il utilise EXACTEMENT le meme rendu wizard, mais les
 *                         reponses humaines (soumission de carte, saisie chat)
 *                         resolvent des Promises locales au lieu de partir en
 *                         POST /wizard|/chat vers le serveur.
 *
 * Ce module NE REIMPLEMENTE AUCUN RENDU. Il delegue tout aux fonctions wizard
 * existantes de widget.html (showWizardCard, _wzBuildCard via elles,
 * updatePlanBanner, appendChatMsg, closeWizardCard) et se contente d'envelopper
 * window.wzPost et window.sendChat pour router selon le pilote courant.
 *
 * Dependances (globals widget.html, resolues au moment de l'appel, pas au load) :
 *   showWizardCard(step, token), updatePlanBanner(step), appendChatMsg(role,text),
 *   closeWizardCard(id), window.wzPost(cid,type,values), window.sendChat(),
 *   window._token.
 * Dependance croisee harness (optionnelle au load, requise en pilote local) :
 *   window.HarnessAgent.userTurn(text).
 *
 * Vanilla JS, aucun build, aucun import. Expose window.HarnessRender.
 */
(function () {
  'use strict';

  // Installation idempotente : le fichier est charge une seule fois via
  // <script src>, mais on se protege d'un double chargement accidentel.
  if (window.HarnessRender && window.HarnessRender.__installed) {
    return;
  }

  // ── Etat prive ────────────────────────────────────────────────────────────
  var _driver = 'sse';            // 'local' | 'sse' ; defaut = comportement historique
  var _local = Object.create(null); // cid -> { resolve, reject, promise }

  // Refs originales capturees UNE SEULE FOIS (avant enveloppement).
  var _origWzPost = (typeof window.wzPost === 'function') ? window.wzPost : null;
  var _origSendChat = (typeof window.sendChat === 'function') ? window.sendChat : null;

  // ── Utilitaires ───────────────────────────────────────────────────────────
  function _log(msg, err) {
    try { console.error('[HarnessRender] ' + msg, err || ''); } catch (e) {}
  }

  function _genId() {
    return 'h-' + Date.now().toString(36) + '-' +
      Math.random().toString(36).slice(2, 9);
  }

  function _token() {
    return (typeof window._token !== 'undefined') ? window._token : undefined;
  }

  // Cree (ou recupere) un waiter local pour un cid : { resolve, reject, promise }.
  function _ensureWaiter(cid) {
    var w = _local[cid];
    if (w) return w;
    w = {};
    w.promise = new Promise(function (resolve, reject) {
      w.resolve = resolve;
      w.reject = reject;
    });
    _local[cid] = w;
    return w;
  }

  // ── API publique ──────────────────────────────────────────────────────────

  // setDriver('local'|'sse') : change le pilote courant. Tout autre valeur est
  // ignoree (on conserve le pilote precedent) pour rester robuste.
  function setDriver(mode) {
    if (mode === 'local' || mode === 'sse') {
      _driver = mode;
    } else {
      _log('setDriver: mode invalide "' + mode + '" (ignore)');
    }
    return _driver;
  }

  function getDriver() { return _driver; }

  // say(text) : bulle assistant. Reuse direct de appendChatMsg.
  function say(text) {
    try {
      if (typeof appendChatMsg === 'function') {
        appendChatMsg('assistant', (text == null ? '' : String(text)));
      } else {
        _log('say: appendChatMsg indisponible');
      }
    } catch (e) { _log('say a echoue', e); }
  }

  // echoUser(text) : bulle user (echo de la saisie humaine).
  function echoUser(text) {
    try {
      if (typeof appendChatMsg === 'function') {
        appendChatMsg('user', (text == null ? '' : String(text)));
      } else {
        _log('echoUser: appendChatMsg indisponible');
      }
    } catch (e) { _log('echoUser a echoue', e); }
  }

  // _normalizeCard(step) : garantit qu'une carte issue du LLM (ask_user) est
  // TOUJOURS repondable. Un LLM peut mal former une carte (choix manquants,
  // type absent) ; sans ca la carte bloquante n'aurait aucun controle et
  // l'utilisateur serait coince. On infere le type et on replie en saisie
  // libre si les choix/champs sont invalides.
  function _normChoices(arr) {
    if (!Array.isArray(arr)) return null;
    var out = [];
    for (var i = 0; i < arr.length; i++) {
      var c = arr[i];
      if (c == null) continue;
      if (typeof c === 'string') { out.push({ id: c, label: c }); continue; }
      if (typeof c === 'object') {
        var id = (c.id != null) ? c.id : (c.value != null ? c.value : (c.label != null ? c.label : String(i)));
        var label = (c.label != null) ? c.label : (c.text != null ? c.text : String(id));
        var o = { id: String(id), label: String(label) };
        if (c.icon) o.icon = c.icon;
        if (c.desc || c.description) o.desc = c.desc || c.description;
        out.push(o);
      }
    }
    return out.length ? out : null;
  }

  function _normalizeCard(step) {
    var s = step;
    if (!s.title && s.text) s.title = String(s.text).slice(0, 120);
    if (!s.title) s.title = 'Question';
    var VALID = { choice: 1, form: 1, confirm: 1, info: 1, input: 1, preview: 1, 'data-import': 1 };
    var choices = _normChoices(s.choices || s.options || s.choix);
    var t = (s.type || '').toLowerCase();
    if (!VALID[t]) {
      if (choices) t = 'choice';
      else if (Array.isArray(s.fields) && s.fields.length) t = 'form';
      else if (Array.isArray(s.actions) && s.actions.length) t = 'confirm';
      else t = 'input';
    }
    if (t === 'choice') {
      if (choices) { s.choices = choices; }
      else { t = 'input'; }               // choix demandes mais invalides -> saisie libre
    }
    if (t === 'form' && !(Array.isArray(s.fields) && s.fields.length)) t = 'input';
    if (t === 'confirm') {
      if (!Array.isArray(s.actions) || !s.actions.length) {
        s.actions = [{ id: 'ok', label: 'OK', style: 'primary' }, { id: 'cancel', label: 'Annuler' }];
      }
      if (!s.content) s.content = s.text || s.subtitle || s.title;
    }
    if (t === 'input' && !s.placeholder) s.placeholder = 'Votre reponse…';
    s.type = t;
    return s;
  }

  // renderCard(step) -> Promise<{type, values}>
  //   - normalise la carte (toujours repondable), assigne step.id si absent
  //   - enregistre un waiter local dans _local[step.id]
  //   - delegue le rendu a showWizardCard(step, token) : routage Zone1/2/3,
  //     blocking, mermaid, tous types (plan|choice|form|confirm|info|input|
  //     preview|data-import) geres par le wizard existant.
  //   La Promise se resout quand l'utilisateur soumet (via le wrapper wzPost).
  function renderCard(step) {
    if (!step || typeof step !== 'object') {
      return Promise.reject(new Error('renderCard: step invalide'));
    }
    step = _normalizeCard(step);
    if (!step.id) step.id = _genId();
    var w = _ensureWaiter(step.id);
    try {
      if (typeof showWizardCard === 'function') {
        showWizardCard(step, _token());
      } else {
        throw new Error('showWizardCard indisponible');
      }
    } catch (e) {
      _log('renderCard: rendu echoue', e);
      // On rejette et on nettoie le waiter : pas de Promise fantome.
      delete _local[step.id];
      if (w && w.reject) w.reject(e);
      return Promise.reject(e);
    }
    return w.promise;
  }

  // awaitAnswer(id) -> Promise<{type, values}>
  //   Renvoie la Promise du waiter deja enregistre par renderCard. Si aucun
  //   waiter n'existe (ex : carte rendue par le SSE, ou attente anticipee), on
  //   en cree un en attente : une soumission ulterieure via wzPost (pilote
  //   local) le resoudra.
  function awaitAnswer(id) {
    if (!id) return Promise.reject(new Error('awaitAnswer: id manquant'));
    return _ensureWaiter(id).promise;
  }

  // updatePlan(step) : Zone 1 (bandeau). Reuse de updatePlanBanner.
  function updatePlan(step) {
    try {
      if (typeof updatePlanBanner === 'function') {
        updatePlanBanner(step);
      } else {
        _log('updatePlan: updatePlanBanner indisponible');
      }
    } catch (e) { _log('updatePlan a echoue', e); }
  }

  // closeCard(id) : ferme la carte + resout/rejette proprement un waiter local
  // encore en attente (evite une Promise jamais tenue si l'agent ferme une
  // carte sans reponse humaine).
  function closeCard(id) {
    try {
      if (typeof closeWizardCard === 'function') closeWizardCard(id);
    } catch (e) { _log('closeCard: closeWizardCard a echoue', e); }
    var w = _local[id];
    if (w) {
      delete _local[id];
      try { if (w.resolve) w.resolve({ type: 'closed', values: null }); }
      catch (e) { _log('closeCard: resolve a echoue', e); }
    }
  }

  // ── Wrappers de convergence (installes UNE SEULE FOIS) ─────────────────────

  // window.wzPost : en pilote local, si un waiter existe pour ce cid, on ferme
  // la carte, on retire le waiter et on resout sa Promise avec la reponse — AU
  // LIEU du POST /wizard/{token}. Sinon (pilote sse, ou carte SSE sans waiter
  // local) : comportement d'origine intact.
  window.wzPost = function (cid, type, values) {
    var w = _local[cid];
    if (_driver === 'local' && w) {
      try {
        if (typeof closeWizardCard === 'function') closeWizardCard(cid);
      } catch (e) { _log('wzPost(local): closeWizardCard a echoue', e); }
      delete _local[cid];
      try {
        w.resolve({ type: type, values: values });
      } catch (e) { _log('wzPost(local): resolve a echoue', e); }
      return;
    }
    if (_origWzPost) return _origWzPost.apply(this, arguments);
    _log('wzPost: aucune implementation originale disponible (pilote sse)');
  };

  // window.sendChat : signature d'origine SANS argument (lit/vide #chatInput,
  // respecte inp.disabled). En pilote local, on replique cette lecture/clear et
  // le garde inp.disabled, puis on echo la bulle user et on lance un userTurn
  // au lieu du POST /chat/{token}. En pilote sse : comportement d'origine.
  window.sendChat = function () {
    if (_driver === 'local') {
      var inp = document.getElementById('chatInput');
      // Reprise fidele des gardes de l'original : rien si vide ou desactive
      // (une carte bloquante ouverte desactive l'input -> pas de userTurn
      // concurrent).
      if (!inp || !inp.value.trim() || inp.disabled) return;
      var text = inp.value.trim();
      inp.value = '';
      inp.style.height = '';
      echoUser(text);
      try {
        if (window.HarnessAgent && typeof window.HarnessAgent.userTurn === 'function') {
          window.HarnessAgent.userTurn(text);
        } else {
          _log('sendChat(local): HarnessAgent.userTurn indisponible');
          say('Agent local indisponible (HarnessAgent non charge).');
        }
      } catch (e) {
        _log('sendChat(local): userTurn a echoue', e);
        say('Erreur agent local : ' + (e && e.message ? e.message : e));
      }
      return;
    }
    if (_origSendChat) return _origSendChat.apply(this, arguments);
    _log('sendChat: aucune implementation originale disponible (pilote sse)');
  };

  // ── Export ────────────────────────────────────────────────────────────────
  window.HarnessRender = {
    __installed: true,
    setDriver: setDriver,
    getDriver: getDriver,
    say: say,
    echoUser: echoUser,
    renderCard: renderCard,
    awaitAnswer: awaitAnswer,
    updatePlan: updatePlan,
    closeCard: closeCard
  };
})();
