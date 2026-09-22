/**
 * Ce que ce test protège, ce ne sont pas des fonctions : ce sont les règles que
 * la vitrine a coûté cher à apprendre ailleurs.
 *
 *   - un chiffre affiché doit être relu dans le code, jamais recopié ;
 *   - une page dans une langue ne doit pas laisser fuir l'autre ;
 *   - un JSON incomplet doit casser la génération, pas produire une page trouée.
 *
 * Usage : node --test site/generate.test.mjs
 */
import test from "node:test";
import assert from "node:assert/strict";
import fs from "fs";
import os from "os";
import path from "path";
import { fileURLToPath } from "url";
import {
  chargerVitrine,
  compterEntrees,
  echapper,
  generate,
  inventaire,
  rendreSitemap,
} from "./generate.mjs";

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const RACINE = path.join(ROOT, "..");
const VITRINE = path.join(ROOT, "vitrine.json");

function tmp(prefixe) {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefixe));
}

/** Un faux serveur, avec un nombre d'outils qu'on choisit. */
function faussSource(nOutils) {
  const entree = (i) => `    {"name": "outil_${i}",\n     "inputSchema": {}},`;
  return [
    '"""',
    "GRIST CODER · MCP Server v9.9 · streamable HTTP",
    '"""',
    "TOOLS = [",
    ...Array.from({ length: nOutils }, (_, i) => entree(i)),
    "]",
    "",
    "PROMPTS = [",
    '    {"name": "p1"},',
    "]",
    "",
    "STATIC_RESOURCES = [",
    '    {"uri": "a"},',
    "]",
    "",
    "RESOURCE_TEMPLATES = [",
    '    {"uriTemplate": "b"},',
    "]",
    "",
  ].join("\n");
}

test("echapper neutralise le HTML", () => {
  assert.equal(echapper(`a<b>&"c`), "a&lt;b&gt;&amp;&quot;c");
});

test("chargerVitrine refuse un JSON incomplet plutôt que de trouer la page", () => {
  const f = path.join(tmp("gc-vit-"), "bad.json");
  fs.writeFileSync(f, JSON.stringify({ couleur: "#000" }));
  assert.throws(() => chargerVitrine(f), /champ requis/);
});

const THEME_MINIMAL = {
  primaire: "#000091",
  secondaire: "#C9191E",
  surface: "#EEE",
  fond: "#FFF",
  encre: "#161616",
  attenue: "#666",
  bordure: "#DDD",
  police: "Marianne",
  policeBase: "https://exemple.invalid/",
  marque: { chevrons: "#3E5DE7", point: "#10B981", libelle: "Coder" },
};

test("chargerVitrine exige le français, page par défaut", () => {
  const f = path.join(tmp("gc-vit-"), "sansfr.json");
  fs.writeFileSync(
    f,
    JSON.stringify({
      theme: THEME_MINIMAL,
      base: "/x/",
      depot: "u",
      mcp: {},
      distribution: { installScript: "u", image: "i", helmRepo: "h" },
      langues: { en: { sspcloud: { etapes: [{ titre: "a", texte: "b" }] } } },
    })
  );
  assert.throws(() => chargerVitrine(f), /langue fr/);
});

test("chargerVitrine refuse une charte incomplète", () => {
  const f = path.join(tmp("gc-vit-"), "sanstheme.json");
  const { police, ...ampute } = THEME_MINIMAL;
  fs.writeFileSync(
    f,
    JSON.stringify({
      theme: ampute,
      base: "/x/",
      depot: "u",
      mcp: {},
      distribution: { installScript: "u", image: "i", helmRepo: "h" },
      langues: { fr: { sspcloud: { etapes: [{ titre: "a", texte: "b" }] } } },
    })
  );
  assert.throws(() => chargerVitrine(f), /theme\.police/);
});

/**
 * La charte est une donnée : le générateur ne doit connaître aucune couleur en
 * dur, sans quoi un autre produit de la gamme devrait le forker pour se
 * présenter avec sa propre identité.
 */
test("aucune couleur n'est codée en dur dans le générateur", () => {
  const src = fs.readFileSync(path.join(ROOT, "generate.mjs"), "utf8");
  const sansCommentaires = src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
  const enDur = sansCommentaires.match(/#[0-9a-fA-F]{3,8}\b/g) || [];
  // Les gris neutres de mise en page restent tolérés ; une couleur de marque, non.
  const marque = enDur.filter((c) => !/^#(1b1b35|f4f4fb|3a3a3a|1b2f96|fff)$/i.test(c));
  assert.deepEqual(marque, [], `couleurs de marque en dur : ${marque.join(", ")}`);
});

test("compterEntrees casse si la liste attendue a disparu du source", () => {
  assert.throws(() => compterEntrees("x = 1", "TOOLS", /^\s{4}\{"name":/), /introuvable/);
});

test("compterEntrees casse plutôt que de rendre zéro", () => {
  assert.throws(() => compterEntrees("TOOLS = [\n]\n", "TOOLS", /^\s{4}\{"name":/), /aucune entrée/);
});

test("l'inventaire est relu dans le vrai serveur et n'est jamais vide", () => {
  const inv = inventaire(path.join(RACINE, "grist_coder.py"));
  assert.ok(inv.outils > 0, "outils");
  assert.ok(inv.prompts > 0, "prompts");
  assert.ok(inv.ressources > 0, "ressources");
  assert.match(inv.version, /^\d+\.\d+/);
});

test("le nombre d'outils affiché vient du source, pas du JSON", () => {
  // La preuve : on change le source, et la page suit.
  const faux = path.join(tmp("gc-src-"), "faux.py");
  fs.writeFileSync(faux, faussSource(7));
  const dist = tmp("gc-dist-");
  generate({ vitrinePath: VITRINE, distDir: dist, serveurPath: faux, racine: RACINE });
  const html = fs.readFileSync(path.join(dist, "index.html"), "utf8");
  assert.match(html, /<strong>7<\/strong>/);
  assert.match(html, /v9\.9/);
});

test("la page française porte ses sections et ses métadonnées de partage", () => {
  const dist = tmp("gc-dist-");
  generate({ vitrinePath: VITRINE, distDir: dist, racine: RACINE });
  const html = fs.readFileSync(path.join(dist, "index.html"), "utf8");
  assert.match(html, /<html lang="fr">/);
  assert.match(html, /id="mcp"/);
  assert.match(html, /id="parcours"/);
  assert.match(html, /id="fonctionnalites"/);
  assert.match(html, /rel="canonical" href="https:\/\/nic01asfr\.github\.io\/GristCoder\/"/);
  assert.match(html, /hreflang="en"/);
  assert.match(html, /hreflang="x-default"/);
  assert.match(html, /"@type":"SoftwareApplication"/);
  assert.match(html, /og:image/);
});

test("la page anglaise existe et ne laisse pas fuir le français", () => {
  const dist = tmp("gc-dist-");
  generate({ vitrinePath: VITRINE, distDir: dist, racine: RACINE });
  const html = fs.readFileSync(path.join(dist, "en", "index.html"), "utf8");
  assert.match(html, /<html lang="en">/);
  assert.match(html, /MCP tools/);
  assert.doesNotMatch(html, /Ce que tu peux faire/);
  assert.doesNotMatch(html, /outils MCP/);
  // Le lien de bascule remonte vers la racine, pas vers /en/en/.
  assert.match(html, /href="\/GristCoder\/"/);
});

/**
 * Le contenu français a déjà été écrit une fois sans accents, en passant par un
 * script intermédiaire. Sur une page publique, ça se voit. On vérifie donc le
 * texte rendu — pas le balisage, où `id="securite"` est légitime.
 */
test("le français rendu porte ses accents", () => {
  const dist = tmp("gc-dist-");
  generate({ vitrinePath: VITRINE, distDir: dist, racine: RACINE });
  const html = fs.readFileSync(path.join(dist, "index.html"), "utf8");
  const texte = html
    .replace(/<style[\s\S]*?<\/style>/g, " ")
    .replace(/<script[\s\S]*?<\/script>/g, " ")
    .replace(/<[^>]+>/g, " ");
  const attendus = [
    "securite", "deploye", "cle", "cles", "donnees", "reponse", "modele",
    "identite", "proprietaire", "numerique", "experimental", "publie",
    "mediterranee", "apercu", "systeme", "integrations", "refusee",
  ];
  const fautes = attendus.filter((m) => new RegExp(`\\b${m}\\b`, "i").test(texte));
  assert.deepEqual(fautes, [], `mots sans accent dans la page française : ${fautes.join(", ")}`);
});

test("le plan de site liste les deux langues", () => {
  const v = chargerVitrine(VITRINE);
  const xml = rendreSitemap(v, "2026-01-01");
  assert.match(xml, /<loc>https:\/\/nic01asfr\.github\.io\/GristCoder\/<\/loc>/);
  assert.match(xml, /<loc>https:\/\/nic01asfr\.github\.io\/GristCoder\/en\/<\/loc>/);
  assert.match(xml, /<lastmod>2026-01-01<\/lastmod>/);
});

/**
 * GitHub Pages publie `master:/docs` sur ce depot — c'est le seul mecanisme qui
 * ait jamais fonctionne ici (les workflows sur `push` n'y ont jamais demarre).
 * La sortie doit donc atterrir dans docs/, pas ailleurs.
 */
test("la sortie par défaut est docs/, ce que GitHub Pages publie", () => {
  const src = fs.readFileSync(path.join(ROOT, "generate.mjs"), "utf8");
  assert.match(src, /const DIST = path\.join\(RACINE, "docs"\)/);
});

/**
 * Les captures vivent a cote de la page : rien ne doit dependre d'un hote
 * externe pour que la vitrine s'affiche.
 */
test("la page ne dépend d'aucun hôte externe pour ses images", () => {
  const dist = tmp("gc-dist-");
  generate({ vitrinePath: VITRINE, distDir: dist, racine: RACINE });
  const html = fs.readFileSync(path.join(dist, "index.html"), "utf8");
  assert.doesNotMatch(html, /<img[^>]+src="https?:\/\//);
  assert.match(html, /<img src="screenshot-widget-home\.png"/);
  assert.match(html, /id="sspcloud"/);
  const en = fs.readFileSync(path.join(dist, "en", "index.html"), "utf8");
  assert.match(en, /<img src="\.\.\/screenshot-widget-home\.png"/);
});

test("le marqueur .nojekyll est posé", () => {
  const dist = tmp("gc-dist-");
  const r = generate({ vitrinePath: VITRINE, distDir: dist, racine: RACINE });
  assert.ok(fs.existsSync(path.join(dist, ".nojekyll")));
  assert.ok(r.ecrits.length >= 4);
});

/**
 * Le manifeste du registre MCP répète une version que seul `grist_coder.py`
 * fait autorité. C'est exactement la dérive qui a laissé le README annoncer
 * 34 outils pour 35 réels — on l'attache donc au source.
 */
test("server.json ne dérive pas du serveur qu'il décrit", () => {
  const m = JSON.parse(fs.readFileSync(path.join(RACINE, "server.json"), "utf8"));
  const inv = inventaire(path.join(RACINE, "grist_coder.py"));
  assert.equal(m.version, inv.version, "version du manifeste");
  assert.equal(m.packages[0].version, inv.version, "version du paquet OCI");
  const chart = fs.readFileSync(path.join(RACINE, "charts/grist-coder/Chart.yaml"), "utf8");
  const appVer = chart.match(/^appVersion:\s*["']?([\d.]+)/m)?.[1];
  assert.equal(appVer, inv.version, "Chart appVersion (tag d'image par defaut)");
  // Contraintes du registre officiel, vérifiées sans dépendance.
  assert.match(m.name, /^[a-zA-Z0-9.-]+\/[a-zA-Z0-9._-]+$/);
  for (const k of ["$schema", "name", "description", "version"]) {
    assert.ok(m[k], `champ requis ${k}`);
  }
  assert.equal(m.packages[0].transport.type, "streamable-http");
  assert.ok(m.packages[0].transport.url, "transport.url requis");
  assert.equal(m.websiteUrl, "https://nic01asfr.github.io/GristCoder/");
});

/**
 * Le README répète les mêmes chiffres. C'est précisément lui qui avait dérivé
 * (34 annoncés, 35 réels) : on l'attache au source au même titre que le reste.
 */
test("le README ne dérive pas du serveur qu'il documente", () => {
  const readme = fs.readFileSync(path.join(RACINE, "README.md"), "utf8");
  const inv = inventaire(path.join(RACINE, "grist_coder.py"));

  const expose = readme.match(/\|\s*Exposes\s*\|\s*(\d+) tools · (\d+) prompts · (\d+) resources\s*\|/);
  assert.ok(expose, "ligne d'identité MCP introuvable dans le README");
  assert.equal(Number(expose[1]), inv.outils, "outils");
  assert.equal(Number(expose[2]), inv.prompts, "prompts");
  assert.equal(Number(expose[3]), inv.ressources, "ressources");

  const titre = readme.match(/^## MCP Tools \((\d+)\)/m);
  assert.ok(titre, "titre « MCP Tools (n) » introuvable");
  assert.equal(Number(titre[1]), inv.outils);

  const sante = readme.match(/"tools":\s*(\d+)/);
  assert.ok(sante, "exemple de réponse /health introuvable");
  assert.equal(Number(sante[1]), inv.outils);

  assert.match(readme, /https:\/\/nic01asfr\.github\.io\/GristCoder\//);
});

/**
 * Trois outils avaient ete livres sans jamais entrer dans le README
 * (grist_doc_create, grist_validate, savoir_faire). Un outil qu'on ne documente
 * pas est un outil que personne n'appelle : on refuse desormais l'oubli.
 */
test("le README documente tous les outils exposés", () => {
  const readme = fs.readFileSync(path.join(RACINE, "README.md"), "utf8");
  const src = fs.readFileSync(path.join(RACINE, "grist_coder.py"), "utf8");
  const outils = [...new Set(src.match(/^ {4}\{"name": "[a-z_]+"/gm).map((m) => m.slice(14, -1)))];
  const absents = outils.filter((o) => !readme.includes(o));
  assert.deepEqual(absents, [], `outils non documentés : ${absents.join(", ")}`);
});

test("le manifeste et la vitrine décrivent le même serveur", () => {
  const m = JSON.parse(fs.readFileSync(path.join(RACINE, "server.json"), "utf8"));
  const v = chargerVitrine(VITRINE);
  assert.equal(m.name, v.mcp.identifiant);
  assert.equal(m.packages[0].identifier, v.mcp.image);
  assert.equal(m.packages[0].transport.type, v.mcp.transport);
  assert.equal(m.repository.url, v.depot);
});

test("la génération est déterministe — deux passes, un seul résultat", () => {
  const a = tmp("gc-dist-");
  const b = tmp("gc-dist-");
  generate({ vitrinePath: VITRINE, distDir: a, racine: RACINE });
  generate({ vitrinePath: VITRINE, distDir: b, racine: RACINE });
  assert.equal(
    fs.readFileSync(path.join(a, "index.html"), "utf8"),
    fs.readFileSync(path.join(b, "index.html"), "utf8")
  );
});
