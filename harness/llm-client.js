/*
 * harness/llm-client.js  ->  window.HarnessLLM
 *
 * Client LLM du harness agent embarque (widget.html).
 * Le navigateur n'appelle JAMAIS l'API LLM en direct (CORS + secret cote client).
 * Il POST sur BASE + '/llm-proxy/<path>' avec :
 *   - header 'X-LLM-Base: <base API LLM>'  (hote qui doit etre whiteliste server-side)
 *   - header 'Authorization: Bearer <cle LLM>'
 * Le serveur (grist_coder.py:/llm-proxy) relaie server-side vers <base>/<path>.
 *
 * CONTRAINTE DURE (grist_coder.py:6276-6279) : le proxy ne transmet QUE le header
 * Authorization. Aucun header custom (ni 'anthropic-version' ni 'x-api-key') n'est
 * relaye. Cible pleinement fonctionnelle = API OpenAI-compatible a auth Bearer
 * (Albert / etalab : POST v1/chat/completions). L'adaptateur 'anthropic' est fourni
 * mais ne fonctionne via ce proxy que si l'endpoint accepte Bearer sans exiger
 * 'anthropic-version' (gateway/OAuth).
 *
 * API publique :
 *   HarnessLLM.complete({provider, baseUrl, model, apiKey, maxTokens,
 *                        system, messages, tools, signal})
 *       -> Promise<{ text, toolCalls:[{id,name,args}], raw,
 *                    assistantMessage, stopReason, provider }>
 *
 *   HarnessLLM.adapters.openai    (Albert, path 'v1/chat/completions')
 *   HarnessLLM.adapters.anthropic (Messages, path 'v1/messages')
 *     chaque adaptateur : { path, buildBody(msgs,tools,cfg), parse(resp),
 *                           formatToolResults(results), stream(reserve, null) }
 *
 *   HarnessLLM.TIMEOUT_MS   (115000, strictement < 120s du proxy httpx)
 *   HarnessLLM.scrub(str, key)
 *
 * Vanilla JS, aucun build, aucun import ES. Charge via <script src="/harness/llm-client.js">.
 */
(function (global) {
  "use strict";

  // Aligne strictement SOUS le timeout httpx du proxy (120.0s, grist_coder.py:6281)
  // pour que l'abort client precede toujours le drop serveur.
  var TIMEOUT_MS = 115000;
  var DEFAULT_MAX_TOKENS = 2048;

  // ---------------------------------------------------------------------------
  // Utilitaires
  // ---------------------------------------------------------------------------

  // BASE : const declaree dans widget.html (window.location.origin). Accessible par
  // nom lexical depuis un <script> charge apres. Fallback defensif.
  function _base() {
    try {
      if (typeof BASE !== "undefined" && BASE) return BASE;
    } catch (_) { /* ReferenceError impossible via typeof, mais on cadre */ }
    if (global.BASE) return global.BASE;
    return (global.location && global.location.origin) || "";
  }

  // Retire toute trace de la cle LLM (et des bearer/sk-*) d'un message affichable.
  function scrub(s, key) {
    if (s == null) return s;
    s = String(s);
    if (key) {
      try { s = s.split(key).join("***"); } catch (_) {}
    }
    s = s.replace(/Bearer\s+[A-Za-z0-9._\-]+/gi, "Bearer ***");
    s = s.replace(/sk-[A-Za-z0-9._\-]{4,}/g, "***");
    return s;
  }

  // Extrait un schema JSON d'une definition d'outil MCP, quelle que soit la cle.
  function _schemaOf(t) {
    var s = t && (t.inputSchema || t.input_schema || t.parameters);
    if (!s || typeof s !== "object") {
      return { type: "object", properties: {} };
    }
    return s;
  }

  // Extrait un detail d'erreur lisible d'une reponse JSON (proxy OU upstream LLM).
  function _errDetail(data) {
    if (!data || typeof data !== "object") return "";
    if (typeof data.error === "string") return data.error;           // format proxy
    if (data.error && typeof data.error === "object") {              // format OpenAI
      return data.error.message || data.error.type || JSON.stringify(data.error);
    }
    // format FastAPI / SSPCloud : {"detail": "Model not found"} ou [{msg,...}]
    if (typeof data.detail === "string") return data.detail;
    if (Array.isArray(data.detail) && data.detail.length) {
      var d0 = data.detail[0];
      return (d0 && (d0.msg || d0.message)) || JSON.stringify(data.detail).slice(0, 200);
    }
    if (typeof data.message === "string") return data.message;       // format Anthropic
    if (data._text) return String(data._text).slice(0, 400);
    return "";
  }

  // Construit une Error lisible a partir d'un statut HTTP non-2xx.
  function _httpError(status, data, key) {
    var detail = scrub(_errDetail(data), key);
    var msg;
    if (status === 403) {
      msg = "Proxy LLM (403) : hote non autorise ou /llm-proxy desactive " +
            "(LLM_PROXY_ALLOWED_HOSTS server-side).";
    } else if (status === 401) {
      msg = "Cle LLM refusee (401) : verifier la cle API et l'endpoint.";
    } else if (status === 502) {
      msg = "Proxy LLM (502) : impossible de joindre l'API LLM cible.";
    } else if (status === 400) {
      msg = "Requete refusee (400) : base LLM manquante/invalide ou corps mal forme.";
    } else if (status === 404) {
      msg = "Endpoint LLM introuvable (404) : verifier baseUrl et le chemin du provider.";
    } else if (status === 429) {
      msg = "Quota LLM depasse (429) : trop de requetes, reessayer plus tard.";
    } else {
      msg = "Erreur LLM (" + status + ").";
    }
    if (detail) msg += " Detail : " + detail;
    var err = new Error(msg);
    err.status = status;
    err.detail = detail;
    return err;
  }

  // ---------------------------------------------------------------------------
  // Transport : POST vers /llm-proxy/<path>, gestion timeout + erreurs scrubees
  // ---------------------------------------------------------------------------
  function _post(path, body, opts) {
    var url = _base() + "/llm-proxy/" + path;
    var headers = {
      "Content-Type": "application/json",
      "X-LLM-Base": opts.baseUrl || "",
      "Authorization": "Bearer " + (opts.apiKey || "")
    };
    // Anthropic exige anthropic-version ; le proxy le relaie (liste blanche).
    if (opts.provider === "anthropic") {
      headers["anthropic-version"] = "2023-06-01";
    }

    // AbortController interne (timeout) chaine avec un signal externe optionnel.
    var ctrl = new AbortController();
    var timedOut = false;
    var timer = setTimeout(function () {
      timedOut = true;
      ctrl.abort();
    }, TIMEOUT_MS);

    if (opts.signal) {
      if (opts.signal.aborted) {
        ctrl.abort();
      } else {
        opts.signal.addEventListener("abort", function () { ctrl.abort(); }, { once: true });
      }
    }

    return fetch(url, {
      method: "POST",
      headers: headers,
      body: JSON.stringify(body),
      signal: ctrl.signal
    }).then(function (res) {
      return res.text().then(function (raw) {
        var data;
        try { data = raw ? JSON.parse(raw) : {}; }
        catch (_) { data = { _text: raw }; }
        if (!res.ok) throw _httpError(res.status, data, opts.apiKey);
        return data;
      });
    }).catch(function (e) {
      if (e && e.name === "AbortError") {
        if (timedOut) {
          var te = new Error("Delai depasse (>" + Math.round(TIMEOUT_MS / 1000) +
                             "s) sur l'appel LLM.");
          te.status = "timeout";
          throw te;
        }
        throw e; // annulation externe deliberee : laisser remonter tel quel
      }
      if (e && typeof e.status !== "undefined") throw e; // deja une _httpError
      throw new Error("Erreur reseau vers le proxy LLM : " +
                      scrub(String((e && e.message) || e), opts.apiKey));
    }).finally(function () {
      clearTimeout(timer);
    });
  }

  // ---------------------------------------------------------------------------
  // Adaptateur OpenAI-compatible (Albert / etalab) — path 'v1/chat/completions'
  // ---------------------------------------------------------------------------
  var openai = {
    path: "v1/chat/completions",
    stream: null, // reserve : hook streaming (stream:true) pour une v2

    // msgs : messages au format OpenAI natif (memoire du loop).
    // cfg  : { model, maxTokens, system }
    buildBody: function (msgs, tools, cfg) {
      cfg = cfg || {};
      var messages = Array.isArray(msgs) ? msgs.slice() : [];
      if (cfg.system) {
        messages = [{ role: "system", content: cfg.system }].concat(messages);
      }
      var body = {
        model: cfg.model,
        messages: messages,
        max_tokens: cfg.maxTokens || DEFAULT_MAX_TOKENS,
        stream: false
      };
      if (tools && tools.length) {
        body.tools = tools.map(function (t) {
          return {
            type: "function",
            function: {
              name: t.name,
              description: t.description || "",
              parameters: _schemaOf(t)
            }
          };
        });
        body.tool_choice = "auto";
      }
      return body;
    },

    parse: function (resp) {
      var choice = (resp && resp.choices && resp.choices[0]) || {};
      var message = choice.message || {};
      var text = typeof message.content === "string" ? message.content : "";
      var toolCalls = [];
      var rawCalls = message.tool_calls || [];
      for (var i = 0; i < rawCalls.length; i++) {
        var tc = rawCalls[i] || {};
        var fn = tc.function || {};
        var args = {};
        try { args = fn.arguments ? JSON.parse(fn.arguments) : {}; }
        catch (_) { args = { _raw: fn.arguments }; } // args illisibles : ne pas crasher
        toolCalls.push({ id: tc.id, name: fn.name, args: args });
      }
      return {
        text: text,
        toolCalls: toolCalls,
        raw: resp,
        // message assistant a re-injecter AVANT les tool_results (LOOP-CLOSURE).
        assistantMessage: message,
        stopReason: choice.finish_reason || null,
        provider: "openai"
      };
    },

    // results : [{ id, name, content(resultString) }]
    // -> un message {role:'tool'} par resultat.
    formatToolResults: function (results) {
      return (results || []).map(function (r) {
        return {
          role: "tool",
          tool_call_id: r.id,
          content: typeof r.content === "string" ? r.content : JSON.stringify(r.content)
        };
      });
    }
  };

  // ---------------------------------------------------------------------------
  // Adaptateur Anthropic 'Messages' — path 'v1/messages'
  // NOTE : ne fonctionne via /llm-proxy que si l'endpoint accepte Bearer sans
  // exiger 'anthropic-version' (le proxy ne relaie pas ce header).
  // ---------------------------------------------------------------------------
  var anthropic = {
    path: "v1/messages",
    stream: null, // reserve : hook streaming pour une v2

    // msgs : messages au format Anthropic natif ({role, content}).
    // cfg  : { model, maxTokens, system }
    buildBody: function (msgs, tools, cfg) {
      cfg = cfg || {};
      var body = {
        model: cfg.model,
        messages: Array.isArray(msgs) ? msgs.slice() : [],
        max_tokens: cfg.maxTokens || DEFAULT_MAX_TOKENS,
        stream: false
      };
      if (cfg.system) body.system = cfg.system;
      if (tools && tools.length) {
        body.tools = tools.map(function (t) {
          return {
            name: t.name,
            description: t.description || "",
            input_schema: _schemaOf(t)
          };
        });
      }
      return body;
    },

    parse: function (resp) {
      var blocks = (resp && resp.content) || [];
      var textParts = [];
      var toolCalls = [];
      for (var i = 0; i < blocks.length; i++) {
        var b = blocks[i] || {};
        if (b.type === "text") {
          textParts.push(b.text || "");
        } else if (b.type === "tool_use") {
          toolCalls.push({ id: b.id, name: b.name, args: b.input || {} });
        }
      }
      return {
        text: textParts.join(""),
        toolCalls: toolCalls,
        raw: resp,
        // message assistant a re-injecter AVANT les tool_results : contenu brut.
        assistantMessage: { role: "assistant", content: blocks },
        stopReason: (resp && resp.stop_reason) || null,
        provider: "anthropic"
      };
    },

    // results : [{ id, name, content(resultString) }]
    // -> un unique message user contenant tous les blocs tool_result.
    formatToolResults: function (results) {
      var content = (results || []).map(function (r) {
        return {
          type: "tool_result",
          tool_use_id: r.id,
          content: typeof r.content === "string" ? r.content : JSON.stringify(r.content)
        };
      });
      return [{ role: "user", content: content }];
    }
  };

  var adapters = { openai: openai, anthropic: anthropic };

  // ---------------------------------------------------------------------------
  // complete() — point d'entree unique, agnostique du provider
  // ---------------------------------------------------------------------------
  function complete(opts) {
    opts = opts || {};
    var provider = opts.provider || "openai";
    var adapter = adapters[provider];
    if (!adapter) {
      return Promise.reject(new Error("Provider LLM inconnu : " + provider));
    }
    if (!opts.baseUrl) {
      return Promise.reject(new Error("baseUrl LLM manquante (config)."));
    }
    if (!opts.model) {
      return Promise.reject(new Error("Modele LLM manquant (config)."));
    }
    var body;
    try {
      body = adapter.buildBody(opts.messages || [], opts.tools || [], {
        model: opts.model,
        maxTokens: opts.maxTokens,
        system: opts.system
      });
    } catch (e) {
      return Promise.reject(new Error("Construction requete LLM impossible : " +
                                      scrub(String((e && e.message) || e), opts.apiKey)));
    }
    return _post(adapter.path, body, opts).then(function (resp) {
      return adapter.parse(resp);
    });
  }

  // ---------------------------------------------------------------------------
  // Export
  // ---------------------------------------------------------------------------
  global.HarnessLLM = {
    complete: complete,
    adapters: adapters,
    scrub: scrub,
    TIMEOUT_MS: TIMEOUT_MS
  };

})(typeof window !== "undefined" ? window : this);
