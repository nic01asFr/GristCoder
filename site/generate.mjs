#!/usr/bin/env node
/**
 * La vitrine : ce que le dépôt montre à quelqu'un qui arrive.
 *
 * Le README s'adresse à qui va installer le serveur. Rien, jusqu'ici, ne
 * présentait le produit — et la page GitHub ne montrait qu'un mur de 800 lignes
 * de documentation technique.
 *
 * RIEN N'EST ÉCRIT À LA MAIN. `site/dist/**` est régénéré depuis `vitrine.json`
 * et depuis le code du serveur ; ce script est le seul écrivain autorisé. C'est
 * la leçon d'un manifeste voisin, édité une fois à la main et parti en URLs
 * fictives que personne n'a vues pendant des semaines.
 *
 * COROLLAIRE QUI COMMANDE `inventaire()` : le nombre d'outils MCP n'est pas
 * recopié dans le JSON. Il est relu dans `grist_coder.py` à chaque génération,
 * parce que la version recopiée dérive toujours — au moment d'écrire ces lignes,
 * le README annonçait 34 outils et la docstring 32, pour 35 réellement exposés.
 *
 * LA CHARTE EST UNE DONNÉE, PAS DU CODE. `vitrine.json.theme` porte la palette
 * et la typographie ; ce fichier ne connaît aucune couleur en dur. Un produit de
 * la gamme se présente donc avec l'identité de l'application qu'il sert, sans
 * forker le générateur. Ici, le tricolore vient du logo Grist de La Suite
 * numérique et le rythme typographique du DSFR.
 *
 * Usage : node site/generate.mjs
 */
import fs from "fs";
import path from "path";
import { execFileSync } from "child_process";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const RACINE = path.join(__dirname, "..");
const VITRINE = path.join(__dirname, "vitrine.json");
const DIST = path.join(__dirname, "dist");
const SERVEUR = path.join(RACINE, "grist_coder.py");
const ORIGINE = "https://nic01asfr.github.io";
const RAW = "https://raw.githubusercontent.com/nic01asFr/GristCoder/master/";

/* ------------------------------------------------------------------ */
/* Lecture et validation                                               */
/* ------------------------------------------------------------------ */

export function echapper(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

const CHAMPS_LANGUE = ["nom", "titreHero", "titrePage", "pitch", "tags", "libelles", "points", "produit"];
const CHAMPS_THEME = ["primaire", "secondaire", "surface", "fond", "encre", "attenue", "bordure", "police", "policeBase", "marque"];

export function chargerVitrine(fichier = VITRINE) {
  const v = JSON.parse(fs.readFileSync(fichier, "utf8"));
  for (const k of ["theme", "base", "depot", "langues", "mcp"]) {
    if (v[k] == null) throw new Error(`vitrine.json : champ requis manquant : ${k}`);
  }
  for (const k of CHAMPS_THEME) {
    if (!v.theme[k]) throw new Error(`vitrine.json : champ requis manquant : theme.${k}`);
  }
  if (!v.langues.fr) throw new Error("vitrine.json : la langue fr est obligatoire (page par défaut)");
  for (const [code, l] of Object.entries(v.langues)) {
    for (const k of CHAMPS_LANGUE) {
      if (l[k] == null || (Array.isArray(l[k]) && l[k].length === 0)) {
        throw new Error(`vitrine.json : champ requis manquant ou vide : langues.${code}.${k}`);
      }
    }
    if (!l.produit?.sequence?.length) {
      throw new Error(`vitrine.json : langues.${code}.produit.sequence requis`);
    }
  }
  return v;
}

/* ------------------------------------------------------------------ */
/* Ce que le code dit de lui-même                                      */
/* ------------------------------------------------------------------ */

/**
 * Compte les entrées d'une liste Python de premier niveau (`NOM = [ ... ]`).
 *
 * On suit la profondeur de crochets pour délimiter le bloc, puis on compte les
 * ouvertures d'entrée en tête de ligne — c'est la seule chose stable dans ce
 * fichier de 8 000 lignes, et ça évite d'y importer un parseur.
 */
export function compterEntrees(source, nom, motif) {
  const lignes = source.split(/\r?\n/);
  const debut = lignes.findIndex((l) => l.startsWith(`${nom} = [`));
  if (debut === -1) throw new Error(`grist_coder.py : liste ${nom} introuvable`);
  let profondeur = 0;
  let n = 0;
  for (let i = debut; i < lignes.length; i += 1) {
    const l = lignes[i];
    profondeur += (l.match(/\[/g) || []).length - (l.match(/\]/g) || []).length;
    if (motif.test(l)) n += 1;
    if (profondeur === 0 && i > debut) break;
  }
  // Un zéro signifierait que le format du source a changé sous nos pieds. Mieux
  // vaut casser la génération que publier une page qui annonce « 0 outil ».
  if (n === 0) throw new Error(`grist_coder.py : aucune entrée trouvée dans ${nom}`);
  return n;
}

export function inventaire(fichier = SERVEUR) {
  const src = fs.readFileSync(fichier, "utf8");
  const version = src.match(/MCP Server v([\d.]+)/)?.[1];
  if (!version) throw new Error("grist_coder.py : version introuvable dans la docstring");
  return {
    version,
    outils: compterEntrees(src, "TOOLS", /^\s{4}\{"name":/),
    prompts: compterEntrees(src, "PROMPTS", /^\s{4}\{"name":/),
    ressources:
      compterEntrees(src, "STATIC_RESOURCES", /^\s{4}\{"uri/) +
      compterEntrees(src, "RESOURCE_TEMPLATES", /^\s{4}\{"uriTemplate/),
  };
}

/**
 * La date de mise à jour se lit dans l'historique git, jamais dans `new Date()` :
 * une date générée daterait d'aujourd'hui une page qui n'a pas bougé depuis des
 * mois. En CI, cela suppose `fetch-depth: 0`, sans quoi il n'y a pas d'historique.
 */
export function dateMaj(racine = RACINE) {
  try {
    const d = execFileSync("git", ["log", "-1", "--format=%cI", "--", "grist_coder.py"], {
      cwd: racine,
      encoding: "utf8",
    }).trim();
    return d ? d.slice(0, 10) : null;
  } catch {
    return null;
  }
}

/* ------------------------------------------------------------------ */
/* Blocs                                                               */
/* ------------------------------------------------------------------ */

function blocTete(v, code, l, inv) {
  const url = `${ORIGINE}${v.base}${code === "fr" ? "" : `${code}/`}`;
  const image = v.captures?.[0] ? `${RAW}${v.captures[0].fichier}` : "";
  const alternes = Object.keys(v.langues)
    .map((c) => {
      const u = `${ORIGINE}${v.base}${c === "fr" ? "" : `${c}/`}`;
      return `<link rel="alternate" hreflang="${c}" href="${echapper(u)}">`;
    })
    .join("\n  ");
  const jsonld = {
    "@context": "https://schema.org",
    "@type": "SoftwareApplication",
    name: l.nom,
    description: l.pitch,
    applicationCategory: "DeveloperApplication",
    operatingSystem: "Linux, macOS, Windows",
    softwareVersion: inv.version,
    url,
    codeRepository: v.depot,
    license: "https://opensource.org/licenses/MIT",
    author: { "@type": "Organization", name: v.auteur },
    offers: { "@type": "Offer", price: "0", priceCurrency: "EUR" },
  };
  // Le littéral fermant de script est coupé : ce fichier n'est pas du HTML, mais
  // la maison coupe partout, pour qu'un copier-coller vers un gabarit ne tronque rien.
  const script = `<script type="application/ld+json">${JSON.stringify(jsonld)}</` + `script>`;
  return `  <link rel="canonical" href="${echapper(url)}">
  ${alternes}
  <link rel="alternate" hreflang="x-default" href="${echapper(ORIGINE + v.base)}">
  <meta name="theme-color" content="${echapper(v.theme.primaire)}">
  <meta property="og:type" content="website">
  <meta property="og:site_name" content="${echapper(l.nom)}">
  <meta property="og:locale" content="${code === "fr" ? "fr_FR" : "en_US"}">
  <meta property="og:url" content="${echapper(url)}">
  <meta property="og:title" content="${echapper(l.titrePage)}">
  <meta property="og:description" content="${echapper(l.pitch)}">
  ${image ? `<meta property="og:image" content="${echapper(image)}">` : ""}
  <meta name="twitter:card" content="${image ? "summary_large_image" : "summary"}">
  <meta name="twitter:title" content="${echapper(l.titrePage)}">
  <meta name="twitter:description" content="${echapper(l.pitch)}">
  ${script}`;
}

/**
 * La marque du produit, telle qu'elle apparaît déjà dans le widget : deux
 * chevrons encadrant le point d'état, puis « Coder ». Le point est vert parce
 * que, dans le widget, vert veut dire connecté — la vitrine montre donc le
 * produit dans l'état où l'utilisateur le voit quand tout va bien.
 */
function blocMarque(t) {
  const m = t.marque;
  const chevron = (points) =>
    `<svg width="7" height="14" viewBox="0 0 24 24" fill="none" stroke="${echapper(m.chevrons)}" stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="${points}"/></svg>`;
  return `<span class="marque">${chevron("14 6 8 12 14 18")}<span class="point"></span>${chevron("10 18 16 12 10 6")}<b>${echapper(m.libelle)}</b></span>`;
}

function blocSecurite(l) {
  const s = l.securite;
  if (!s?.items?.length) return "";
  return `<section class="bande alt" id="securite">
  <div class="wrap">
    ${surtitre(s.surtitre, "rouge")}
    <h2>${echapper(s.titre)}</h2>
    <p class="accroche">${echapper(s.chapo)}</p>
    <div class="garde-grid">
${s.items
  .map(
    (x) => `      <article>
        <h3>${echapper(x.titre)}</h3>
        <p>${echapper(x.texte)}</p>
      </article>`
  )
  .join("\n")}
    </div>
  </div>
</section>`;
}

/** Le surtitre coloré des sections — repris de la présentation Grist de La Suite. */
function surtitre(texte, variante = "") {
  if (!texte) return "";
  return `<p class="surtitre${variante ? ` ${variante}` : ""}">${echapper(texte)}</p>`;
}

function blocInventaire(v, l, inv) {
  const m = v.mcp;
  const chiffres = [
    { valeur: String(inv.outils), libelle: l.libelles.inventaireOutils },
    { valeur: String(inv.prompts), libelle: l.libelles.inventairePrompts },
    { valeur: String(inv.ressources), libelle: l.libelles.inventaireRessources },
    { valeur: `v${inv.version}`, libelle: m.transport },
  ];
  const clients = m.clients
    .map(
      (c) => `      <article>
        <h3>${echapper(c.nom)}</h3>
        <pre><code>${echapper(c.code)}</code></pre>
      </article>`
    )
    .join("\n");
  return `<section class="bande" id="mcp">
  <div class="wrap">
    ${surtitre(l.libelles.surtitreMcp)}
    <h2>${echapper(l.libelles.inventaireTitre)}</h2>
    <div class="chiffres">
${chiffres.map((c) => `      <div><strong>${echapper(c.valeur)}</strong><span>${echapper(c.libelle)}</span></div>`).join("\n")}
    </div>
    <p class="note">${echapper(l.libelles.inventaireNote)}</p>
    <dl class="ident">
      <dt>${echapper(l.libelles.identifiant)}</dt><dd><code>${echapper(m.identifiant)}</code></dd>
      <dt>Transport</dt><dd><code>${echapper(m.transport)}</code> — ${echapper(m.spec)}, ${echapper(l.libelles.sur)} <code>${echapper(m.endpoint)}</code></dd>
      <dt>Image</dt><dd><code>${echapper(m.image)}</code></dd>
    </dl>
    <div class="clients">
${clients}
    </div>
  </div>
</section>`;
}

function blocPoints(l) {
  return `<ul class="points">
${l.points.map((p) => `      <li><b>${echapper(p.titre)}</b><span>${echapper(p.texte)}</span></li>`).join("\n")}
    </ul>`;
}

function blocCaptures(v, code, l) {
  if (!v.captures?.length) return "";
  return `<section class="bande alt" id="captures">
  <div class="wrap">
    ${surtitre(l.libelles.surtitreCaptures)}
    <h2>${echapper(l.libelles.captures)}</h2>
    <div class="shots">
${v.captures
  .map(
    (c) => `      <figure>
        <img src="${echapper(RAW + c.fichier)}" alt="${echapper(c[code] || "")}" loading="lazy">
        <figcaption>${echapper(c[code] || "")}</figcaption>
      </figure>`
  )
  .join("\n")}
    </div>
  </div>
</section>`;
}

function blocFonctionnalites(l) {
  const f = l.fonctionnalites || [];
  if (!f.length) return "";
  return `<section class="bande" id="fonctionnalites">
  <div class="wrap">
    ${surtitre(l.libelles.surtitreFonctionnalites)}
    <h2>${echapper(l.titreFonctionnalites)}</h2>
    <div class="feat-list">
${f
  .map(
    (x) => `      <article>
        <h3>${echapper(x.titre)}</h3>
        <p>${echapper(x.texte)}</p>
        ${x.pourQui ? `<p class="pour-qui">${echapper(x.pourQui)}</p>` : ""}
      </article>`
  )
  .join("\n")}
    </div>
  </div>
</section>`;
}

function blocSequence(l) {
  const produit = l.produit;
  return `<section class="bande alt" id="parcours">
  <div class="wrap">
    ${surtitre(l.libelles.surtitreParcours)}
    <h2>${echapper(produit.titreSequence)}</h2>
    <ol class="sequence">
${produit.sequence
  .map(
    (s, i) =>
      `      <li><span class="n">${i + 1}</span><div><b>${echapper(s.titre)}</b><p>${echapper(s.texte)}</p></div></li>`
  )
  .join("\n")}
    </ol>
  </div>
</section>`;
}

function blocContextes(l) {
  const c = l.produit.contextes || [];
  if (!c.length) return "";
  return `<section class="bande" id="usages">
  <div class="wrap">
    ${surtitre(l.libelles.surtitreUsages)}
    <h2>${echapper(l.produit.titreContextes)}</h2>
    <div class="ctx-grid">
${c
  .map(
    (x) => `      <article>
        <h3>${echapper(x.titre)}</h3>
        <p>${echapper(x.texte)}</p>
        ${x.pourquoi ? `<p class="pourquoi">${echapper(x.pourquoi)}</p>` : ""}
      </article>`
  )
  .join("\n")}
    </div>
  </div>
</section>`;
}

function blocEncart(l) {
  const encart = l.encart;
  if (!encart) return "";
  const lien = encart.lien
    ? `<p class="cta-line"><a class="btn ghost" href="${echapper(encart.lien.url)}">${echapper(encart.lien.libelle)}</a></p>`
    : "";
  return `<section class="bande">
  <div class="wrap">
    <aside class="encart">
      <h2>${echapper(encart.titre)}</h2>
      <p>${echapper(encart.texte)}</p>
      ${lien}
    </aside>
  </div>
</section>`;
}

function blocJournal(l) {
  const j = l.journal || [];
  if (!j.length) return "";
  return `<section class="bande alt" id="journal">
  <div class="wrap">
    <h2>${echapper(l.libelles.journal)}</h2>
    <div class="journal">
${j.map((x) => `      <div><b>${echapper(x.version)}</b><p>${echapper(x.texte)}</p></div>`).join("\n")}
    </div>
  </div>
</section>`;
}

/* ------------------------------------------------------------------ */
/* Rendu                                                               */
/* ------------------------------------------------------------------ */

function policeMarianne(theme) {
  const face = (poids, fichier) => `    @font-face {
      font-family: "${theme.police}";
      src: url("${theme.policeBase}${fichier}") format("woff2");
      font-weight: ${poids};
      font-style: normal;
      font-display: swap;
    }`;
  return [
    face(400, "Marianne-Regular.woff2"),
    face(500, "Marianne-Medium.woff2"),
    face(700, "Marianne-Bold.woff2"),
  ].join("\n");
}

export function rendreHtml(v, code, inv, maj) {
  const l = v.langues[code];
  const t = v.theme;
  const tags = l.tags.map((x) => `<span class="tag">${echapper(x)}</span>`).join("");
  const autre = Object.keys(v.langues).find((c) => c !== code);
  const urlAutre = `${v.base}${autre === "fr" ? "" : `${autre}/`}`;
  const depotCourt = v.depot.replace(/^https?:\/\//, "");

  return `<!DOCTYPE html>
<html lang="${echapper(code)}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>${echapper(l.titrePage)}</title>
  <meta name="description" content="${echapper(l.pitch)}">
${blocTete(v, code, l, inv)}
  <link rel="preconnect" href="https://cdn.jsdelivr.net" crossorigin>
  <style>
${policeMarianne(t)}
    :root {
      --bleu: ${echapper(t.primaire)};
      --rouge: ${echapper(t.secondaire)};
      --surface: ${echapper(t.surface)};
      --fond: ${echapper(t.fond)};
      --encre: ${echapper(t.encre)};
      --attenue: ${echapper(t.attenue)};
      --bordure: ${echapper(t.bordure)};
      --police: "${echapper(t.police)}", "Segoe UI", system-ui, sans-serif;
    }
    * { box-sizing: border-box; }
    html { scroll-behavior: smooth; }
    body {
      margin: 0;
      font-family: var(--police);
      font-size: 1.0625rem;
      line-height: 1.6;
      color: var(--encre);
      background: var(--fond);
      -webkit-font-smoothing: antialiased;
    }
    a { color: var(--bleu); }
    .wrap { width: min(1080px, calc(100% - 3rem)); margin: 0 auto; }

    /* Le filet tricolore reprend le logo Grist de La Suite : bleu, blanc, rouge. */
    .tricolore { display: flex; height: 4px; }
    .tricolore i { flex: 1; }
    .tricolore i:nth-child(1) { background: var(--bleu); }
    .tricolore i:nth-child(2) { background: var(--fond); }
    .tricolore i:nth-child(3) { background: var(--rouge); }

    .barre {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 1rem;
      padding: 0.9rem 0;
      border-bottom: 1px solid var(--bordure);
    }
    .marque { display: flex; align-items: center; gap: 4px; font-size: 0.95rem; }
    .marque b { color: ${echapper(t.marque.chevrons)}; font-weight: 700; letter-spacing: 0.01em; margin-left: 3px; }
    .marque .point {
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: ${echapper(t.marque.point)};
      box-shadow: 0 0 0 2px ${echapper(t.marque.point)}2e;
    }
    .barre a { color: var(--attenue); text-decoration: none; font-size: 0.9rem; }
    .barre a:hover { color: var(--bleu); }

    header.hero { padding: clamp(3rem, 8vw, 5.5rem) 0 clamp(2.5rem, 6vw, 4rem); }
    .brand {
      font-size: clamp(2.4rem, 6vw, 3.75rem);
      font-weight: 700;
      letter-spacing: -0.03em;
      line-height: 1.05;
      margin: 0 0 1.1rem;
      max-width: 20ch;
    }
    .pitch { font-size: clamp(1.05rem, 2vw, 1.3rem); max-width: 46rem; color: #3a3a3a; margin: 0 0 1.75rem; }
    .tags { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-bottom: 2rem; }
    .tag {
      font-size: 0.75rem;
      font-weight: 500;
      letter-spacing: 0.02em;
      padding: 0.3rem 0.7rem;
      border-radius: 999px;
      background: var(--surface);
      color: var(--bleu);
    }
    .cta { display: flex; flex-wrap: wrap; gap: 0.75rem; }
    .btn {
      font-family: var(--police);
      font-weight: 500;
      text-decoration: none;
      padding: 0.75rem 1.35rem;
      border-radius: 4px;
      display: inline-block;
      transition: background 0.15s ease, color 0.15s ease;
    }
    .btn.primary { background: var(--bleu); color: #fff; }
    .btn.primary:hover { background: #1b2f96; }
    .btn.ghost { border: 1px solid var(--bleu); color: var(--bleu); }
    .btn.ghost:hover { background: var(--surface); }

    .bande { padding: clamp(2.75rem, 6vw, 4.5rem) 0; }
    .bande.alt { background: var(--surface); }
    .surtitre {
      font-size: 0.8rem;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--bleu);
      margin: 0 0 0.6rem;
    }
    .surtitre.rouge { color: var(--rouge); }
    h2 { font-size: clamp(1.6rem, 3vw, 2.1rem); font-weight: 700; letter-spacing: -0.02em; margin: 0 0 1.6rem; }
    h3 { font-size: 1.1rem; font-weight: 700; margin: 0 0 0.5rem; }
    .accroche { font-size: 1.15rem; max-width: 46rem; color: #3a3a3a; margin: -0.6rem 0 2rem; }

    .chiffres { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 1rem; }
    .chiffres div { padding: 1.2rem 1.3rem; background: var(--fond); border: 1px solid var(--bordure); border-top: 3px solid var(--bleu); border-radius: 4px; }
    .bande:not(.alt) .chiffres div { background: var(--surface); border-color: transparent; }
    .chiffres strong { font-size: 2.1rem; font-weight: 700; display: block; line-height: 1.1; color: var(--bleu); letter-spacing: -0.03em; }
    .chiffres span { color: var(--attenue); font-size: 0.9rem; }
    .note { color: var(--attenue); font-size: 0.9rem; margin: 1rem 0 0; max-width: 46rem; }

    .ident { display: grid; grid-template-columns: max-content 1fr; gap: 0.4rem 1.5rem; margin: 2rem 0 0; font-size: 0.95rem; }
    .ident dt { font-weight: 700; color: var(--attenue); }
    .ident dd { margin: 0; }
    code { font-family: ui-monospace, "Cascadia Mono", Menlo, monospace; font-size: 0.9em; }
    .clients { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 1.2rem; margin-top: 2rem; }
    .clients article { min-width: 0; }
    .clients pre {
      margin: 0;
      padding: 1rem 1.1rem;
      background: #1b1b35;
      color: #f4f4fb;
      border-radius: 4px;
      overflow-x: auto;
      font-size: 0.82rem;
      line-height: 1.55;
    }

    .points { list-style: none; padding: 0; margin: 0; display: grid; gap: 1.4rem; }
    .points li { display: grid; gap: 0.3rem; padding-left: 1.1rem; border-left: 3px solid var(--bleu); }
    .points b { font-size: 1.05rem; }
    .points span { color: #3a3a3a; }

    .shots { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 1.5rem; }
    .shots figure { margin: 0; }
    .shots img { width: 100%; display: block; border: 1px solid var(--bordure); border-radius: 4px; background: var(--fond); }
    .shots figcaption { color: var(--attenue); font-size: 0.88rem; margin-top: 0.6rem; }

    .feat-list { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 1.2rem; }
    .feat-list article {
      padding: 1.4rem 1.5rem;
      background: var(--surface);
      border-radius: 4px;
      border-top: 3px solid var(--bleu);
    }
    .bande.alt .feat-list article { background: var(--fond); }
    .feat-list h3 { color: var(--bleu); }
    .feat-list p { margin: 0; color: #3a3a3a; }
    .pour-qui { margin-top: 0.8rem !important; font-size: 0.9rem; color: var(--attenue) !important; }

    .sequence { list-style: none; padding: 0; margin: 0; display: grid; gap: 1.2rem; counter-reset: etape; }
    .sequence li { display: grid; grid-template-columns: 2.6rem 1fr; gap: 1rem; align-items: start; }
    .sequence .n {
      width: 2.1rem;
      height: 2.1rem;
      display: grid;
      place-items: center;
      border-radius: 999px;
      background: var(--bleu);
      color: #fff;
      font-weight: 700;
      font-size: 0.95rem;
    }
    .sequence b { font-size: 1.05rem; }
    .sequence p { margin: 0.15rem 0 0; color: #3a3a3a; }

    .garde-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 1.2rem; }
    .garde-grid article { padding: 1.3rem 1.4rem; background: var(--fond); border-radius: 4px; }
    .garde-grid h3 { color: var(--bleu); font-size: 1.02rem; }
    .garde-grid p { margin: 0; color: #3a3a3a; }
    .ctx-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 1.2rem; }
    .ctx-grid article { padding: 1.4rem 1.5rem; border: 1px solid var(--bordure); border-radius: 4px; }
    .ctx-grid p { color: #3a3a3a; margin: 0; }
    .pourquoi { color: var(--attenue) !important; font-size: 0.92rem; margin-top: 0.8rem !important; padding-top: 0.8rem; border-top: 1px solid var(--bordure); }

    .encart { padding: 1.8rem 2rem; background: var(--surface); border-left: 4px solid var(--bleu); border-radius: 4px; }
    .encart h2 { margin-bottom: 0.8rem; }
    .encart p { color: #3a3a3a; max-width: 52rem; }
    .cta-line { margin-bottom: 0; }

    .journal { display: grid; gap: 1.1rem; }
    .journal div { display: grid; grid-template-columns: 4rem 1fr; gap: 1rem; align-items: baseline; }
    .journal b { font-family: ui-monospace, Menlo, monospace; font-size: 0.85rem; color: var(--bleu); }
    .journal p { margin: 0; color: #3a3a3a; }

    footer { padding: 2.5rem 0 3rem; border-top: 1px solid var(--bordure); color: var(--attenue); font-size: 0.9rem; }
    footer p { margin: 0 0 0.4rem; }

    @media (max-width: 560px) {
      .ident { grid-template-columns: 1fr; gap: 0.1rem 0; }
      .ident dd { margin-bottom: 0.7rem; }
      .journal div { grid-template-columns: 1fr; gap: 0.1rem; }
    }
    @media (prefers-reduced-motion: no-preference) {
      .hero .brand, .hero .pitch, .hero .tags, .hero .cta { animation: rise 0.6s ease both; }
      .hero .pitch { animation-delay: 0.06s; }
      .hero .tags { animation-delay: 0.1s; }
      .hero .cta { animation-delay: 0.14s; }
      @keyframes rise {
        from { opacity: 0; transform: translateY(10px); }
        to { opacity: 1; transform: none; }
      }
    }
  </style>
</head>
<body>
  <div class="tricolore"><i></i><i></i><i></i></div>
  <div class="wrap barre">
    ${blocMarque(t)}
    <a href="${echapper(urlAutre)}">${echapper(l.libelles.autreLangue)}</a>
  </div>
  <header class="hero">
    <div class="wrap">
      <h1 class="brand">${echapper(l.titreHero)}</h1>
      <p class="pitch">${echapper(l.pitch)}</p>
      <div class="tags">${tags}</div>
      <div class="cta">
        <a class="btn primary" href="${echapper(v.depot)}">${echapper(l.libelles.codeSource)}</a>
        <a class="btn ghost" href="#mcp">${echapper(l.libelles.demarrer)}</a>
      </div>
    </div>
  </header>
  <main>
    ${blocInventaire(v, l, inv)}
    <section class="bande alt" id="promesse">
      <div class="wrap">
        ${surtitre(l.libelles.surtitrePromesse)}
        <h2>${echapper(l.libelles.promesse)}</h2>
        <p class="accroche">${echapper(l.produit.accroche)}</p>
        ${blocPoints(l)}
      </div>
    </section>
    ${blocCaptures(v, code, l)}
    ${blocFonctionnalites(l)}
    ${blocSequence(l)}
    ${blocContextes(l)}
    ${blocSecurite(l)}
    ${blocEncart(l)}
    ${blocJournal(l)}
  </main>
  <footer>
    <div class="wrap">
      <p>${echapper(l.nom)} — ${echapper(v.licence)} · <a href="${echapper(v.depot)}">${echapper(depotCourt)}</a>${maj ? ` · ${echapper(l.libelles.majLe)} ${echapper(maj)}` : ""}</p>
      <p>${echapper(l.libelles.piedNote)}</p>
    </div>
  </footer>
</body>
</html>
`;
}

export function rendreSitemap(v, maj) {
  const urls = Object.keys(v.langues).map((c) => `${ORIGINE}${v.base}${c === "fr" ? "" : `${c}/`}`);
  return `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
${urls
  .map((u) => `  <url><loc>${echapper(u)}</loc>${maj ? `<lastmod>${echapper(maj)}</lastmod>` : ""}</url>`)
  .join("\n")}
</urlset>
`;
}

/* ------------------------------------------------------------------ */
/* Génération                                                          */
/* ------------------------------------------------------------------ */

export function generate({ vitrinePath = VITRINE, distDir = DIST, serveurPath = SERVEUR, racine = RACINE } = {}) {
  const v = chargerVitrine(vitrinePath);
  const inv = inventaire(serveurPath);
  const maj = dateMaj(racine);
  const ecrits = [];

  for (const code of Object.keys(v.langues)) {
    // Le français est la page d'accueil ; toute autre langue vit dans son dossier.
    const dossier = code === "fr" ? distDir : path.join(distDir, code);
    fs.mkdirSync(dossier, { recursive: true });
    const out = path.join(dossier, "index.html");
    fs.writeFileSync(out, rendreHtml(v, code, inv, maj), "utf8");
    ecrits.push(out);
  }

  const sitemap = path.join(distDir, "sitemap.xml");
  fs.writeFileSync(sitemap, rendreSitemap(v, maj), "utf8");
  ecrits.push(sitemap);

  // GitHub Pages passe les fichiers par Jekyll sans ce marqueur, ce qui écarte
  // silencieusement tout chemin commençant par un tiret bas.
  const nojekyll = path.join(distDir, ".nojekyll");
  fs.writeFileSync(nojekyll, "", "utf8");
  ecrits.push(nojekyll);

  return { ecrits, inventaire: inv, maj };
}

const estPrincipal = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (estPrincipal) {
  const r = generate();
  console.log(`[site] ${r.inventaire.outils} outils, ${r.inventaire.prompts} prompts, ${r.inventaire.ressources} ressources (v${r.inventaire.version}), maj ${r.maj ?? "inconnue"}`);
  for (const f of r.ecrits) console.log(`[site] écrit ${path.relative(RACINE, f)}`);
}
