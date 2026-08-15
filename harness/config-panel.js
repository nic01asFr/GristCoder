/*
 * harness/config-panel.js — window.HarnessConfig
 * ------------------------------------------------------------------
 * ROLE : configuration LLM (localStorage) + panneau UI + bouton "Lancer".
 *
 * Le harness fait vivre un agent LLM DANS le navigateur (widget.html).
 * Ce module ne fait AUCUN appel LLM : il gere uniquement la config
 * (provider / base / modele / cle / max_tokens) persistee dans
 * localStorage sous la cle 'gcHarnessLLM', injecte un bouton robot dans
 * #navbar, et cable le bouton "Lancer" -> HarnessAgent.start(cfg).
 *
 * CONTRAT PROXY (grist_coder.py /llm-proxy/{path}) : le navigateur POST
 * sur BASE+'/llm-proxy/<path>' avec 'X-LLM-Base: <base>' et
 * 'Authorization: Bearer <cle>'. Le proxy ne relaie QUE Authorization
 * (pas anthropic-version / x-api-key) et exige que l'hote de X-LLM-Base
 * figure dans LLM_PROXY_ALLOWED_HOSTS (sinon 403). Consequence :
 * seule une API OpenAI-compatible a auth Bearer (Albert / etalab) est
 * pleinement fonctionnelle. Le preset Anthropic est donc marque
 * EXPERIMENTAL et affiche un avertissement (il 401/400 tant que le core
 * ne relaie pas anthropic-version).
 *
 * SECURITE : la cle LLM n'est JAMAIS en dur, elle est saisie par
 * l'utilisateur, jamais loggee (console) et est scrubbee des messages
 * d'erreur affiches. Elle reste stockee en clair dans localStorage
 * (acceptable pour un outil local, lisible par tout script de l'origine).
 *
 * Vanilla JS autonome. Aucune dependance de build. Expose window.HarnessConfig.
 */
(function () {
  'use strict';

  var LS_KEY = 'gcHarnessLLM';

  // Ce que le pod sait deja (GET /llm-config) : base, modele, et surtout s il
  // dispose lui-meme d une cle. Tant que ce n etait pas demande, chaque poste
  // devait ressaisir une cle qui finissait dans le localStorage du navigateur.
  var SERVEUR = { base: '', modele: '', cle_serveur: false, charge: false };

  function _origine() {
    return (typeof window !== 'undefined' && window.BASE)
      || (typeof window !== 'undefined' && window.location && window.location.origin)
      || '';
  }

  function chargerServeur() {
    if (SERVEUR.charge) return Promise.resolve(SERVEUR);
    var h = {};
    if (typeof window !== 'undefined' && window.__APP_TOKEN__) {
      h['X-App-Token'] = window.__APP_TOKEN__;
    }
    return fetch(_origine() + '/llm-config', { headers: h })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        SERVEUR.charge = true;
        if (d) {
          SERVEUR.base = d.base || '';
          SERVEUR.modele = d.modele || '';
          SERVEUR.cle_serveur = !!d.cle_serveur;
        }
        return SERVEUR;
      })
      .catch(function () { SERVEUR.charge = true; return SERVEUR; });
  }

  var DEFAULTS = {
    provider: 'openai',   // 'openai' (Albert / etalab) | 'anthropic' (experimental)
    baseUrl: '',          // hote+prefixe passe en X-LLM-Base (sans le path adaptateur)
    model: '',
    apiKey: '',
    maxTokens: 4096,
    temperature: 0.25     // bas par defaut : le tool-calling gagne en determinisme
  };

  // Presets. Seul 'openai' (Albert) est pleinement fonctionnel via le proxy.
  var PRESETS = {
    albert: {
      label: 'Albert (Etalab)',
      provider: 'openai',
      baseUrl: 'https://albert.api.etalab.gouv.fr',
      model: 'albert-large',
      maxTokens: 4096,
      experimental: false,
      note: ''
    },
    sspcloud: {
      label: 'SSPCloud',
      provider: 'openai',
      baseUrl: 'https://llm.lab.sspcloud.fr/api',
      model: '',
      maxTokens: 4096,
      experimental: false,
      note: 'API OpenAI-compatible (Bearer). Renseigner le MODELE (voir la plateforme '
          + 'SSPCloud, ex. via /api/v1/models) et votre token SSPCloud dans CLE LLM.'
    },
    anthropic: {
      label: 'Anthropic (experimental)',
      provider: 'anthropic',
      baseUrl: 'https://api.anthropic.com',
      model: 'claude-sonnet-4-5',
      maxTokens: 4096,
      experimental: true,
      note: 'Le proxy ne relaie que Authorization (pas anthropic-version). '
          + 'Cible probablement 401/400 tant que le core n\'ajoute pas le relais. '
          + 'Preferer un endpoint OpenAI-compatible (Albert / SSPCloud).'
    }
  };

  // ---- helpers ------------------------------------------------------

  function _cloneDefaults() {
    return {
      provider: DEFAULTS.provider,
      baseUrl: DEFAULTS.baseUrl,
      model: DEFAULTS.model,
      apiKey: DEFAULTS.apiKey,
      maxTokens: DEFAULTS.maxTokens
    };
  }

  // Scrub la cle LLM d'une chaine (sécurité : jamais afficher/logger la clé)
  function _scrub(text, key) {
    var s = String(text == null ? '' : text);
    if (key && key.length >= 6) {
      // remplace toute occurrence litterale de la cle
      var esc = key.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      try { s = s.replace(new RegExp(esc, 'g'), '***'); } catch (e) { /* noop */ }
    }
    // scrub generique des tokens type "Bearer xxxx"
    s = s.replace(/Bearer\s+[A-Za-z0-9._\-]+/g, 'Bearer ***');
    return s;
  }

  function _isHttpUrl(u) {
    if (!u) return false;
    try {
      var url = new URL(u);
      return url.protocol === 'http:' || url.protocol === 'https:';
    } catch (e) { return false; }
  }

  // ---- persistence --------------------------------------------------

  function get() {
    var cfg = _cloneDefaults();
    try {
      var raw = window.localStorage.getItem(LS_KEY);
      if (raw) {
        var parsed = JSON.parse(raw);
        if (parsed && typeof parsed === 'object') {
          if (parsed.provider === 'openai' || parsed.provider === 'anthropic') cfg.provider = parsed.provider;
          if (typeof parsed.baseUrl === 'string') cfg.baseUrl = parsed.baseUrl.trim();
          if (typeof parsed.model === 'string') cfg.model = parsed.model.trim();
          if (typeof parsed.apiKey === 'string') cfg.apiKey = parsed.apiKey;
          var mt = parseInt(parsed.maxTokens, 10);
          if (isFinite(mt) && mt > 0) cfg.maxTokens = mt;
          if (parsed.temperature != null) {
            var tp = parseFloat(parsed.temperature);
            if (isFinite(tp) && tp >= 0 && tp <= 2) cfg.temperature = tp;
          }
        }
      }
    } catch (e) {
      // localStorage corrompu / indisponible -> defaults, ne pas crasher
    }
    // Ce que l utilisateur n a pas saisi, le pod le sait souvent : base et modele
    // sont deja dans son environnement. Les redemander n apportait rien.
    if (!cfg.baseUrl && SERVEUR.base) cfg.baseUrl = SERVEUR.base;
    if (!cfg.model && SERVEUR.modele) cfg.model = SERVEUR.modele;
    return cfg;
  }

  function set(cfg) {
    var cur = get();
    var next = {
      provider: (cfg && (cfg.provider === 'openai' || cfg.provider === 'anthropic')) ? cfg.provider : cur.provider,
      baseUrl: (cfg && typeof cfg.baseUrl === 'string') ? cfg.baseUrl.trim() : cur.baseUrl,
      model: (cfg && typeof cfg.model === 'string') ? cfg.model.trim() : cur.model,
      apiKey: (cfg && typeof cfg.apiKey === 'string') ? cfg.apiKey : cur.apiKey,
      maxTokens: cur.maxTokens,
      temperature: cur.temperature
    };
    if (cfg && cfg.maxTokens != null) {
      var mt = parseInt(cfg.maxTokens, 10);
      if (isFinite(mt) && mt > 0) next.maxTokens = mt;
    }
    if (cfg && cfg.temperature != null) {
      var tp = parseFloat(cfg.temperature);
      if (isFinite(tp) && tp >= 0 && tp <= 2) next.temperature = tp;
    }
    try {
      window.localStorage.setItem(LS_KEY, JSON.stringify(next));
    } catch (e) {
      // quota / mode prive : on retourne quand meme la config en memoire
    }
    return next;
  }

  function validate(cfg) {
    cfg = cfg || {};
    if (cfg.provider !== 'openai' && cfg.provider !== 'anthropic') {
      return { ok: false, field: 'provider', error: 'Provider invalide (openai | anthropic).' };
    }
    if (!_isHttpUrl(cfg.baseUrl)) {
      return { ok: false, field: 'baseUrl', error: 'Base URL invalide : attendu une URL http(s), ex. https://albert.api.etalab.gouv.fr' };
    }
    if (!cfg.model || !String(cfg.model).trim()) {
      return { ok: false, field: 'model', error: 'Modele requis.' };
    }
    // Cle exigee seulement si le pod n en a pas. Quand il en a une, la laisser
    // vide est le bon choix : elle reste cote serveur au lieu de dormir dans le
    // localStorage de chaque navigateur.
    if ((!cfg.apiKey || !String(cfg.apiKey).trim()) && !SERVEUR.cle_serveur) {
      return { ok: false, field: 'apiKey',
               error: 'Cle LLM requise : ce pod n en fournit pas (ni LLM_API_KEY, ni Secret datalab).' };
    }
    var mt = parseInt(cfg.maxTokens, 10);
    if (!isFinite(mt) || mt <= 0) {
      return { ok: false, field: 'maxTokens', error: 'max_tokens doit etre un entier positif.' };
    }
    return { ok: true };
  }

  // ---- UI -----------------------------------------------------------

  var _injected = false;

  var STYLE = [
    '#hcLLMBtn{display:flex;align-items:center;justify-content:center;',
    '  width:30px;height:28px;border-radius:6px;border:1px solid var(--border,#e2e8f0);',
    '  background:#fff;color:var(--text2,#64748b);cursor:pointer;transition:all .15s;flex-shrink:0}',
    '#hcLLMBtn:hover{background:var(--bg2,#f1f5f9);color:var(--text,#0f172a)}',
    '#hcLLMBtn.on{background:#3e5de7;color:#fff;border-color:#3e5de7}',
    '#hcPanel{position:fixed;top:46px;right:12px;z-index:9999;width:300px;',
    '  background:#fff;border:1px solid var(--border,#e2e8f0);border-radius:10px;',
    '  box-shadow:0 8px 30px rgba(15,23,42,.18);padding:14px;font-size:.8rem;',
    '  color:var(--text,#0f172a);font-family:inherit}',
    '#hcPanel[hidden]{display:none}',
    '#hcPanel h4{margin:0 0 10px;font-size:.82rem;font-weight:700;color:#3e5de7}',
    '#hcPanel label{display:block;font-size:.7rem;font-weight:600;color:var(--text2,#64748b);',
    '  margin:8px 0 3px;text-transform:uppercase;letter-spacing:.02em}',
    '#hcPanel input,#hcPanel select{width:100%;box-sizing:border-box;padding:6px 8px;',
    '  border:1px solid var(--border,#e2e8f0);border-radius:6px;background:var(--bg2,#f8fafc);',
    '  color:var(--text,#0f172a);font-size:.78rem;font-family:inherit}',
    '#hcPanel input:focus,#hcPanel select:focus{outline:none;border-color:#3e5de7}',
    '.hc-presets{display:flex;gap:6px;margin-bottom:8px}',
    '.hc-preset{flex:1;padding:5px 6px;border:1px solid var(--border,#e2e8f0);border-radius:6px;',
    '  background:#fff;color:var(--text2,#64748b);font-size:.72rem;font-weight:600;cursor:pointer}',
    '.hc-preset:hover{background:var(--bg2,#f1f5f9);color:var(--text,#0f172a)}',
    '.hc-preset.active{background:#eef1fe;border-color:#3e5de7;color:#3e5de7}',
    '.hc-actions{display:flex;gap:8px;margin-top:12px}',
    '.hc-actions button{flex:1;padding:7px;border-radius:6px;border:1px solid transparent;',
    '  font-size:.76rem;font-weight:700;cursor:pointer;font-family:inherit}',
    '#hcSave{background:#fff;border-color:var(--border,#e2e8f0);color:var(--text,#0f172a)}',
    '#hcSave:hover{background:var(--bg2,#f1f5f9)}',
    '#hcLaunch{background:#3e5de7;color:#fff}',
    '#hcLaunch:hover{background:#2845c1}',
    '#hcLaunch:disabled{opacity:.55;cursor:not-allowed}',
    '.hc-msg{margin-top:10px;padding:7px 9px;border-radius:6px;font-size:.72rem;line-height:1.35;',
    '  white-space:pre-wrap;word-break:break-word}',
    '.hc-msg[hidden]{display:none}',
    '.hc-msg.err{background:#fef2f2;color:#b91c1c;border:1px solid #fecaca}',
    '.hc-msg.warn{background:#fffbeb;color:#92400e;border:1px solid #fde68a}',
    '.hc-msg.ok{background:#f0fdf4;color:#15803d;border:1px solid #bbf7d0}'
  ].join('');

  var BOT_SVG =
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    + 'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    + '<path d="M12 8V4H8"/><rect width="16" height="12" x="4" y="8" rx="2"/>'
    + '<path d="M2 14h2"/><path d="M20 14h2"/><path d="M15 13v2"/><path d="M9 13v2"/></svg>';

  function _ensureStyle() {
    if (document.getElementById('hcConfigStyles')) return;
    var st = document.createElement('style');
    st.id = 'hcConfigStyles';
    st.textContent = STYLE;
    document.head.appendChild(st);
  }

  function _panelHTML() {
    return ''
      + '<h4>Agent LLM (local)</h4>'
      + '<div class="hc-presets">'
      + '  <button type="button" class="hc-preset" data-preset="albert">Albert</button>'
      + '  <button type="button" class="hc-preset" data-preset="sspcloud">SSPCloud</button>'
      + '  <button type="button" class="hc-preset" data-preset="anthropic">Anthropic</button>'
      + '</div>'
      + '<label for="hcProvider">Provider</label>'
      + '<select id="hcProvider">'
      + '  <option value="openai">openai (Bearer, Albert)</option>'
      + '  <option value="anthropic">anthropic (experimental)</option>'
      + '</select>'
      + '<label for="hcBase">Base API</label>'
      + '<input id="hcBase" type="text" placeholder="https://albert.api.etalab.gouv.fr" autocomplete="off" spellcheck="false">'
      + '<label for="hcModel">Modele</label>'
      + '<input id="hcModel" type="text" placeholder="albert-large" autocomplete="off" spellcheck="false">'
      + '<label for="hcKey">Cle LLM</label>'
      + '<input id="hcKey" type="password" placeholder="jamais loggee" autocomplete="off" spellcheck="false">'
      + '<label for="hcMaxTokens">max_tokens</label>'
      + '<input id="hcMaxTokens" type="number" min="1" step="1" placeholder="4096">'
      + '<label for="hcTemp">temperature (bas = plus fiable pour les outils)</label>'
      + '<input id="hcTemp" type="number" min="0" max="2" step="0.05" placeholder="0.25">'
      + '<div id="hcMsg" class="hc-msg" hidden></div>'
      + '<div class="hc-actions">'
      + '  <button type="button" id="hcSave">Enregistrer</button>'
      + '  <button type="button" id="hcStop" hidden>Arreter</button>'
      + '  <button type="button" id="hcLaunch">Lancer</button>'
      + '</div>';
  }

  function _q(id) { return document.getElementById(id); }

  function _readForm() {
    return {
      provider: _q('hcProvider') ? _q('hcProvider').value : DEFAULTS.provider,
      baseUrl: _q('hcBase') ? _q('hcBase').value.trim() : '',
      model: _q('hcModel') ? _q('hcModel').value.trim() : '',
      apiKey: _q('hcKey') ? _q('hcKey').value : '',
      maxTokens: _q('hcMaxTokens') ? parseInt(_q('hcMaxTokens').value, 10) : DEFAULTS.maxTokens,
      temperature: _q('hcTemp') && _q('hcTemp').value !== '' ? parseFloat(_q('hcTemp').value) : DEFAULTS.temperature
    };
  }

  function _fillForm(cfg) {
    if (_q('hcProvider')) _q('hcProvider').value = cfg.provider || 'openai';
    if (_q('hcBase')) _q('hcBase').value = cfg.baseUrl || '';
    if (_q('hcModel')) _q('hcModel').value = cfg.model || '';
    if (_q('hcKey')) _q('hcKey').value = cfg.apiKey || '';
    if (_q('hcMaxTokens')) _q('hcMaxTokens').value = cfg.maxTokens || DEFAULTS.maxTokens;
    if (_q('hcTemp')) _q('hcTemp').value = (cfg.temperature != null ? cfg.temperature : DEFAULTS.temperature);
    _syncPresetHighlight();
    _syncProviderWarning();
  }

  function _msg(kind, text) {
    var el = _q('hcMsg');
    if (!el) return;
    if (!text) { el.hidden = true; el.textContent = ''; return; }
    el.hidden = false;
    el.className = 'hc-msg ' + kind;
    el.textContent = text;
  }

  function _syncPresetHighlight() {
    var base = _q('hcBase') ? _q('hcBase').value.trim() : '';
    var prov = _q('hcProvider') ? _q('hcProvider').value : '';
    var btns = document.querySelectorAll('.hc-preset');
    for (var i = 0; i < btns.length; i++) {
      var p = PRESETS[btns[i].getAttribute('data-preset')];
      var active = p && p.baseUrl === base && p.provider === prov;
      btns[i].classList.toggle('active', !!active);
    }
  }

  // Avertit si provider anthropic (mort a travers le proxy actuel)
  function _syncProviderWarning() {
    var prov = _q('hcProvider') ? _q('hcProvider').value : '';
    if (prov === 'anthropic') {
      _msg('warn', PRESETS.anthropic.note);
    } else {
      var el = _q('hcMsg');
      // n'efface que si le message courant est l'avertissement anthropic
      if (el && !el.hidden && el.classList.contains('warn')) _msg(null, '');
    }
  }

  function applyPreset(name) {
    var p = PRESETS[name];
    if (!p) return null;
    var cur = get();
    var cfg = {
      provider: p.provider,
      baseUrl: p.baseUrl,
      model: p.model,
      apiKey: cur.apiKey,          // on ne remplace jamais la cle par un preset
      maxTokens: p.maxTokens
    };
    _fillForm(cfg);
    return cfg;
  }

  // Etat des boutons Lancer / Arreter selon que l agent tourne ou non.
  function _refletAgent() {
    var lch = _q('hcLaunch');
    var stp = _q('hcStop');
    var actif = !!(window.HarnessAgent
                   && typeof window.HarnessAgent.isStarted === 'function'
                   && window.HarnessAgent.isStarted());
    if (lch) lch.hidden = actif;
    if (stp) stp.hidden = !actif;
  }

  function stopAgent() {
    if (window.HarnessAgent && typeof window.HarnessAgent.stop === 'function') {
      try { window.HarnessAgent.stop(); } catch (e) { showError(e); }
    }
    if (window.HarnessRender && typeof window.HarnessRender.setDriver === 'function') {
      window.HarnessRender.setDriver('sse');   // le rendu repasse au pilote serveur
    }
    _refletAgent();
    _msg('ok', 'Agent arrete. Le rendu repasse au pilote serveur (SSE).');
  }

  function _refletServeur() {
    var k = _q('hcKey');
    if (!k) return;
    k.placeholder = SERVEUR.cle_serveur
      ? 'Fournie par le pod — laisser vide'
      : 'Cle LLM (ce pod n en fournit pas)';
  }

  function openPanel() {
    _ensureStyle();
    var panel = _q('hcPanel');
    if (!panel) return;
    _fillForm(get());
    // Le panneau s ouvre tout de suite ; ce que le pod sait arrive juste apres et
    // vient completer les champs restes vides.
    chargerServeur().then(function () { _fillForm(get()); _refletServeur(); });
    _refletAgent();
    panel.hidden = false;
    var btn = _q('hcLLMBtn');
    if (btn) btn.classList.add('on');
    var base = _q('hcBase');
    if (base && !base.value) base.focus();
  }

  function closePanel() {
    var panel = _q('hcPanel');
    if (panel) panel.hidden = true;
    var btn = _q('hcLLMBtn');
    if (btn) btn.classList.remove('on');
  }

  function togglePanel() {
    var panel = _q('hcPanel');
    if (!panel || panel.hidden) openPanel();
    else closePanel();
  }

  function _saveFromForm() {
    var cfg = _readForm();
    var v = validate(cfg);
    if (!v.ok) { _msg('err', v.error); return null; }
    var saved = set(cfg);
    _msg('ok', 'Configuration enregistree.');
    _syncPresetHighlight();
    return saved;
  }

  // Traduit une erreur (status ou message) en texte lisible + scrub cle.
  // Public : appelable par agent-loop/llm-client quand un appel LLM echoue.
  function showError(err) {
    var status = 0;
    var raw = '';
    if (err && typeof err === 'object') {
      status = err.status || err.code || 0;
      raw = err.message || err.error || err.detail || '';
      if (!raw) { try { raw = JSON.stringify(err); } catch (e) { raw = String(err); } }
    } else {
      raw = String(err == null ? '' : err);
      var m = raw.match(/\b(40[013]|4\d\d|5\d\d)\b/);
      if (m) status = parseInt(m[1], 10);
    }
    var key = get().apiKey;
    raw = _scrub(raw, key);

    var text;
    if (status === 403) {
      text = 'Erreur 403 — hote LLM non autorise par le proxy.\n'
           + 'L\'hote de la Base API doit figurer dans LLM_PROXY_ALLOWED_HOSTS cote serveur. '
           + '(config serveur, non geree par le harness)';
    } else if (status === 401) {
      text = 'Erreur 401 — cle LLM refusee. Verifie la cle saisie.';
    } else if (status === 400) {
      text = 'Erreur 400 — requete refusee par l\'API LLM.\n'
           + (get().provider === 'anthropic'
              ? 'Le proxy ne relaie pas anthropic-version : provider anthropic non fonctionnel en l\'etat.'
              : raw);
    } else if (status >= 500) {
      text = 'Erreur ' + status + ' — panne cote proxy/LLM. Reessaie.';
    } else {
      text = raw || 'Erreur inconnue lors de l\'appel LLM.';
    }
    // s'assure que le panneau est visible pour montrer l'erreur
    var panel = _q('hcPanel');
    if (panel && panel.hidden) openPanel();
    _msg('err', text);
    return text;
  }

  function launch() {
    var cfg = _saveFromForm();
    if (!cfg) return;                 // validation deja affichee

    if (!window.HarnessAgent || typeof window.HarnessAgent.start !== 'function') {
      _msg('err', 'HarnessAgent indisponible (module agent-loop.js non charge).');
      return;
    }

    var btn = _q('hcLaunch');
    if (btn) btn.disabled = true;

    try {
      // Bascule le rendu en pilote local (agent navigateur) si dispo.
      if (window.HarnessRender && typeof window.HarnessRender.setDriver === 'function') {
        window.HarnessRender.setDriver('local');
      }
      // Ouvre la barre de chat (classe active de #chatSection) — l'agent y
      // affichera son say d'accueil. On l'active ici pour un feedback immediat.
      var cs = document.getElementById('chatSection');
      if (cs) cs.classList.add('active');
      var sp = document.getElementById('statusPanel');
      if (sp) sp.style.display = 'block';

      var ret = window.HarnessAgent.start(cfg);
      _refletAgent();
      closePanel();
      // start() peut etre async : capte un rejet eventuel pour l'afficher.
      if (ret && typeof ret.then === 'function') {
        ret.catch(function (e) { showError(e); });
      }
    } catch (e) {
      showError(e);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function _wire(panel) {
    var save = _q('hcSave');
    var lch = _q('hcLaunch');
    if (save) save.addEventListener('click', function () { _saveFromForm(); });
    if (lch) lch.addEventListener('click', function () { launch(); });

    // HarnessAgent.stop() existait mais rien ne l appelait : un agent lance ne
    // pouvait plus etre interrompu autrement qu en rechargeant la page.
    var stp = _q('hcStop');
    if (stp) stp.addEventListener('click', function () { stopAgent(); });

    var presets = panel.querySelectorAll('.hc-preset');
    for (var i = 0; i < presets.length; i++) {
      (function (b) {
        b.addEventListener('click', function () { applyPreset(b.getAttribute('data-preset')); });
      })(presets[i]);
    }

    var prov = _q('hcProvider');
    if (prov) prov.addEventListener('change', function () { _syncPresetHighlight(); _syncProviderWarning(); });
    var base = _q('hcBase');
    if (base) base.addEventListener('input', function () { _syncPresetHighlight(); });

    // Fermeture au clic hors panneau
    document.addEventListener('mousedown', function (ev) {
      var p = _q('hcPanel');
      if (!p || p.hidden) return;
      var btn = _q('hcLLMBtn');
      if (p.contains(ev.target)) return;
      if (btn && btn.contains(ev.target)) return;
      closePanel();
    });
    // Fermeture a Echap
    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape') closePanel();
    });
  }

  function injectUI() {
    if (_injected) return;
    var navbar = document.getElementById('navbar');
    if (!navbar) return;   // pas encore de DOM : reessaye plus tard
    _injected = true;
    _ensureStyle();

    var btn = document.createElement('button');
    btn.id = 'hcLLMBtn';
    btn.type = 'button';
    btn.title = 'Agent LLM (config + lancer)';
    btn.innerHTML = BOT_SVG;
    btn.addEventListener('click', function (e) { e.stopPropagation(); togglePanel(); });

    var toggle = document.getElementById('panelToggle');
    if (toggle && toggle.parentNode === navbar) navbar.insertBefore(btn, toggle);
    else navbar.appendChild(btn);

    var panel = document.createElement('div');
    panel.id = 'hcPanel';
    panel.hidden = true;
    panel.innerHTML = _panelHTML();
    document.body.appendChild(panel);

    _wire(panel);
    _fillForm(get());
  }

  function _autoInject() {
    injectUI();
    // Interroger le pod des le depart : le bouton « Lancer » doit pouvoir marcher
    // sans passer par le panneau quand la configuration vient deja du serveur.
    chargerServeur().then(_refletServeur);
    // Si #navbar n'existait pas encore, retente brievement (widget async).
    if (!_injected) {
      var tries = 0;
      var iv = setInterval(function () {
        tries++;
        injectUI();
        if (_injected || tries > 40) clearInterval(iv);  // ~10s max
      }, 250);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _autoInject);
  } else {
    _autoInject();
  }

  // ---- API publique -------------------------------------------------
  window.HarnessConfig = {
    LS_KEY: LS_KEY,
    DEFAULTS: DEFAULTS,
    presets: PRESETS,
    get: get,
    set: set,
    validate: validate,
    serveur: SERVEUR,
    chargerServeur: chargerServeur,
    applyPreset: applyPreset,
    injectUI: injectUI,
    openPanel: openPanel,
    closePanel: closePanel,
    togglePanel: togglePanel,
    launch: launch,
    stopAgent: stopAgent,
    showError: showError
  };
})();
