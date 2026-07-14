/*
 * harness/boot.js — Câblage et hook de démarrage du harness agent LLM navigateur.
 *
 * Namespace : window.HarnessBoot
 * Rôle      : point d'entrée unique appelé depuis widget.html à la fin de
 *             doRegister (une fois _token défini). Chargé EN DERNIER, après
 *             agent-memory / llm-client / mcp-tools / render-bridge / agent-loop
 *             / config-panel.
 *
 * Responsabilités (volontairement minimales) :
 *   1. Garde anti-double-init (idempotent sur refresh de token).
 *   2. HarnessConfig.injectUI() une seule fois (panneau + bouton "Lancer").
 *   3. S'assurer que HarnessRender a bien installé ses wrappers (wzPost/sendChat).
 *   4. NE PAS démarrer l'agent : on attend le clic "Lancer" (via config-panel)
 *      pour ne pas casser le pilote SSE par défaut (agent externe).
 *   5. Exposer HarnessBoot.ready (flag booléen) + HarnessBoot.token.
 *
 * Contraintes : vanilla JS, pas de build, autonome, tout sur window.HarnessBoot.
 * Ne touche à aucun autre fichier ; les autres modules sont chargés avant.
 */
(function (global) {
  'use strict';

  // ── État interne ──────────────────────────────────────────────────────────
  var _inited = false;      // init() a déjà réussi au moins une fois
  var _uiInjected = false;  // HarnessConfig.injectUI() a déjà été appelé
  var _token = null;        // dernier token de session connu

  // Log défensif : n'échoue jamais si la console est absente.
  function _log(level, msg, err) {
    try {
      var prefix = '[HarnessBoot] ';
      if (level === 'error' && global.console && console.error) {
        console.error(prefix + msg, err || '');
      } else if (global.console && console.warn) {
        console.warn(prefix + msg, err || '');
      }
    } catch (_) { /* noop */ }
  }

  // Toast optionnel réutilisant celui du widget s'il existe.
  function _toast(msg, type) {
    try {
      if (typeof global.showToast === 'function') global.showToast(msg, type || 'error');
    } catch (_) { /* noop */ }
  }

  // ── Étapes internes ───────────────────────────────────────────────────────

  // (2) Injection de l'UI de config (panneau + bouton "Lancer"), une seule fois.
  function _ensureUI() {
    if (_uiInjected) return;
    var Config = global.HarnessConfig;
    if (!Config || typeof Config.injectUI !== 'function') {
      _log('warn', 'HarnessConfig.injectUI indisponible — panneau non injecté ' +
        '(config-panel.js chargé ?)');
      return;
    }
    try {
      Config.injectUI();
      _uiInjected = true;
    } catch (e) {
      _log('error', 'injectUI a échoué', e);
      _toast('Harness: injection UI échouée — ' + (e && e.message || e), 'error');
    }
  }

  // (3) S'assurer que les wrappers de rendu (wzPost/sendChat) sont installés.
  //     render-bridge.js s'auto-installe au load ; on appelle une fonction
  //     d'installation idempotente si elle est exposée, sinon on vérifie
  //     simplement la présence de la façade.
  function _ensureRender() {
    var Render = global.HarnessRender;
    if (!Render) {
      _log('warn', 'HarnessRender indisponible — rendu local inactif ' +
        '(render-bridge.js chargé ?)');
      return;
    }
    try {
      // Nom de méthode d'installation non garanti : on tente les variantes
      // idempotentes usuelles sans jamais planter si aucune n'existe.
      if (typeof Render.ensureInstalled === 'function') Render.ensureInstalled();
      else if (typeof Render.install === 'function') Render.install();
      // Sinon : l'auto-install au load suffit, rien à faire.
    } catch (e) {
      _log('error', 'installation des wrappers de rendu échouée', e);
    }
  }

  // ── API publique ──────────────────────────────────────────────────────────

  /**
   * init(token) — hook de démarrage appelé depuis widget.html (fin de doRegister).
   * Idempotent : sur refresh de token, met simplement à jour le token courant
   * sans ré-injecter l'UI ni redémarrer quoi que ce soit.
   * Ne démarre JAMAIS l'agent (attend le clic "Lancer" géré par config-panel).
   *
   * @param {string} token  token de session gc- (window._token du widget).
   * @returns {boolean} true si le harness est prêt.
   */
  function init(token) {
    try {
      // Mise à jour du token courant (utile au refresh 80% TTL).
      if (token) {
        _token = token;
        HarnessBoot.token = token;
      }

      // (1) Garde anti-double-init : une seule initialisation complète.
      if (_inited) {
        // Le token vient d'être rafraîchi ; on garde l'UI et les wrappers en place.
        return HarnessBoot.ready === true;
      }

      // Sanity : au moins un token pour que l'agent local puisse dispatcher
      // les outils MCP via tool(). Sans token on injecte quand même l'UI
      // (config LLM saisissable), mais on prévient.
      if (!token && !global._token) {
        _log('warn', 'init() sans token — l\'agent local ne pourra pas ' +
          'appeler les outils MCP tant qu\'une session n\'est pas établie');
      }

      _ensureUI();      // (2)
      _ensureRender();  // (3)

      // (4) On NE démarre PAS l'agent ici : le pilote SSE reste le pilote par
      //     défaut (HarnessRender.setDriver('sse')). L'agent local ne prend la
      //     main qu'au clic "Lancer" (config-panel -> HarnessRender.setDriver
      //     ('local') + HarnessAgent.start).

      _inited = true;
      HarnessBoot.ready = true;  // (5)
      return true;
    } catch (e) {
      _log('error', 'init() a échoué', e);
      _toast('Harness: démarrage échoué — ' + (e && e.message || e), 'error');
      HarnessBoot.ready = false;
      return false;
    }
  }

  // Exposition — objet stable, on n'écrase pas s'il existe déjà (re-load safe).
  var HarnessBoot = global.HarnessBoot || {};
  HarnessBoot.init = init;
  if (typeof HarnessBoot.ready !== 'boolean') HarnessBoot.ready = false;
  if (!('token' in HarnessBoot)) HarnessBoot.token = null;
  global.HarnessBoot = HarnessBoot;

})(typeof window !== 'undefined' ? window : this);
