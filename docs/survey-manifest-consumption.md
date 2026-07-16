# Contrat de consommation du Survey Manifest — côté artefactory-mcp

> Pièce d'apport à `survey_manifest.py v0.1` (cerema-offre-de-service). Définit comment
> **artefactory-mcp (runtime de COLLECTE Grist)** consomme un Survey Manifest, et
> l'**interface exacte** que les Web Components partagés attendent de notre bridge Grist.
> Sujet coordonné sur `#survey-manifest` (WikiChat) avec Formulaires-Claude ; le registre
> (`components_index`/`assemblies_index`) reste la propriété de Composants-Architect —
> rien n'est figé côté registre avant son feu vert.

## 1. Rôle & frontière (invariant)

- **Grist = runtime de COLLECTE.** Notre agent **instancie** un Survey Manifest → un doc
  Grist souverain : schéma (Questions/Reponses), formulaire, dashboard de synthèse,
  exports. RGPD/souveraineté par construction (données dans Grist gouv).
- **qgis-sspcloud = runtime de COMPOSITION/PUBLICATION** (assemblage BlockNote + audit_chain
  + S3 + DSFR), qui consomme **le même manifest** + le **dataset des Reponses**.
- **Le moteur conditionnel n'existe qu'UNE fois** : un Web Component canonique
  `<cerema-survey-form>` (gates/showIf/matrice/Likert), **embarqué** par les deux runtimes.
  Notre agent **n'implémente PAS** la logique conditionnelle — il instancie le composant.
- **Deux chemins, un seul bridge :** pour les **questionnaires**, on embarque
  `<cerema-survey-form>` ; pour les **apps non-questionnaire** (CRM/CRUD libre), on garde
  notre génération React maison. Les deux passent par `grist-bridge.js`.

## 2. Pipeline de génération (les 6 conformités d'un form généré)

À partir d'un Survey Manifest, notre agent produit un form **conforme** ssi :

1. **Manifest-driven** — la vérité est le manifest (pas de logique form bespoke).
2. **Renderer partagé** — embarque `<cerema-survey-form manifest=… >` (UMD/CDN), câblé sur
   le bridge injecté pour l'I/O. Zéro ré-implémentation des gates/showIf.
3. **Schéma** — `ensureSchema` dérivé du manifest (voir §5) : tables + colonnes typées +
   table méta `Questions` + table `Reponses`.
4. **Géo** — sections géométriques via `<cerema-geo-map>` (interactive_map + Scene Manifest),
   **pas de Leaflet maison** ; les géométries sont une couche Scene Manifest.
5. **Synthèse** — dashboard polarity-aware (composant `survey_results`), piloté par le
   **même** manifest (voir §6).
6. **Exports + audit** — CSV réponses + CSV synthèse + GeoJSON ; `_audit_hash` chaîné par
   réponse (voir §7). Le dataset Reponses + le manifest sont **consommables tels quels**
   par le BlockNote editor (audit_chain jusqu'aux réponses brutes).

## 3. Structure du Survey Manifest (esquisse v0.1, dérivée du DEF existant)

Projeté depuis le `DEF` de `gen_schema.py` + la table `Questions` + le `QMETA` du dashboard
(source unique aujourd'hui éparpillée en 4 endroits → une seule ici) :

```jsonc
{
  "manifest_version": "1.0.0",
  "id": "perception-espaces-publics",
  "title": "Perception des espaces publics",
  "classification": "cerema_internal",          // hérité (hub/hub/models Classification)
  "sections": [
    {
      "id": "H", "label": "Propreté",
      "gate": "H0_Concerne",                     // colonne Bool ; si false -> section masquée
      "questions": [
        {
          "colId": "H1", "label": "Propreté du quartier",
          "theme": "Propreté", "type": "likert5",// likert5 = Int 1-5, non-concerné -> null
          "polarity": "fav"                      // fav=vert(haut=mieux) | bes=orange(haut=à traiter) | pref=bleu | ""
        }
      ]
    },
    {
      "id": "E", "label": "Lieux préférés",
      "questions": [
        { "colId": "E1", "type": "rank_place", "label": "1er lieu", "choices_ref": "lieux" },
        { "colId": "E2", "type": "rank_place", "label": "2e lieu",  "choices_ref": "lieux" },
        { "colId": "E3", "type": "rank_place", "label": "3e lieu",  "choices_ref": "lieux" }
        // matrice de classement : 1 lieu = 1 place, échange auto
      ]
    },
    {
      "id": "G", "label": "Localisation",
      "questions": [
        { "colId": "G_Geometries", "type": "geojson", "scene_layer": "reponses_geo" }
        // -> <cerema-geo-map>, couche Scene Manifest
      ]
    }
  ],
  "choices": { "lieux": ["Parc", "Place", "Berge", "…"] }
}
```

**Types canoniques** (mappés depuis le DEF) : `choice`, `choice_list`, `bool`, `likert5`
(Int 1-5, null si non concerné), `text`, `datetime`, `rank_place` (E1/E2/E3), `geojson`
(G_Geometries). **Polarité** ∈ `fav` | `bes` | `pref` | `""`.

## 4. Interface bridge attendue par les Web Components

Les composants partagés **ne parlent jamais à Grist en propre** : ils consomment le bridge
injecté (`window.GristBridge`, cf. `shared/grist-bridge.js`) — c'est LA couche read/write
standard. Contrat minimal :

```ts
interface GristBridge {
  // Lecture prête à l'emploi (fini le colonnaire {id:[...]})
  loadTable(table: string): Promise<Array<{ id: number, [col: string]: any }>>;
  fetchTable(table: string): Promise<{ id: number[], [col: string]: any[] }>; // colonnaire brut
  // Écriture + refresh (renvoient la liste à jour)
  addRow(table: string, fields: Record<string, any>): Promise<Row[]>;
  updateRow(table: string, id: number, fields: Record<string, any>): Promise<Row[]>;
  deleteRow(table: string, id: number): Promise<Row[]>;
  applyAndFetch(actions: any[][], table: string): Promise<Row[]>;
  applyUserActions(actions: any[][]): Promise<any>;               // normalise scalaire->tableau
  util: {
    toRows(d): Row[]; toDate(ts): Date; fromDate(d): number;      // dates Grist en SECONDES
    refIds(refList): number[]; toRefList(ids): any[]; esc(s): string;
  };
}
```

`<cerema-survey-form>` reçoit `manifest` (le contrat) + `bridge` (l'implémentation ci-dessus)
et gère seul gates/showIf/matrice/Likert, en écrivant les réponses via `bridge.addRow('Reponses', …)`.

## 5. Mapping manifest → schéma Grist (`ensureSchema`)

Notre agent dérive du manifest :
- **Table `Reponses`** : une colonne par `question.colId`, typée depuis `type`
  (`likert5`→Int, `choice`→Choice, `geojson`→Text/JSON, …) + `Horodatage` (DateTime),
  `DureeSecondes` (Int), et les colonnes d'audit `_audit_hash` / `manifest_version` /
  `previous_hash` (§7). Les gates `X0_Concerne*` sont des Bool.
- **Table méta `Questions`** : `Ordre`, `colId`, `Libelle`, `Theme`, `Type`, `Polarite` —
  projection du manifest, lisible dans Grist et par le dashboard.
- Écriture via notre pré-vol `_validate_actions` (Ref forward + casse des `$refs` de formule
  détectées AVANT écriture, y compris sur tables existantes) → anti-500 sandbox.

## 6. Dashboard polarity-aware + exports

- **Top-box** = `%(note >= 4)` par question. Orientation par polarité :
  `bes`→orange (haut = à traiter), `fav`→vert (haut = mieux), `pref`→bleu. Bloc
  « Ce qui ressort » : tri `bes` vs `fav` par top-box.
- **Exports** : CSV réponses (libellés lisibles), CSV synthèse (N / moyenne / top-box par
  question), GeoJSON QGIS (couche `reponses_geo`). L'export = **couche UI** (bouton
  client-side dans l'artefact), pas un tool MCP.

## 7. Audit par réponse (`_audit_hash`)

Format arrêté avec Formulaires-Claude, à confirmer par Composants-Architect :

```
integrity_hash = SHA256( canonical_json({colId: value} triés) | manifest_version | horodatage )
previous_hash  = integrity_hash de la réponse précédente (chaînage tamper-evident, optionnel)
```

**Implémenté côté bridge/serveur au moment de l'écriture d'une Reponse** : on calcule
`integrity_hash` et on remplit `_audit_hash` + `manifest_version` (+ `previous_hash`).
Ça remonte proprement dans `AuditChain.signed_hash` au niveau assemblage (qgis-sspcloud) et
s'aligne sur le pattern d'audit existant. **À figer une fois le format validé par Composants-Architect.**

## 8. Exigences sur les Web Components partagés (pour être embeddables chez nous)

Notre runtime d'artefact est **Babel-standalone** (pas de bundler, pas d'`import` ES,
React/ReactDOM globaux via CDN). Donc `<cerema-survey-form>` et `<cerema-geo-map>` DOIVENT :
1. être distribués en **UMD/global via CDN**, chargeables en `<script>` (pas d'`import` npm/ES) ;
2. **scoper leur CSS (Shadow DOM)** — le DSFR global ne doit ni fuiter dans le composant ni
   casser la carto (constaté) ;
3. **consommer le bridge injecté** (`window.GristBridge`) pour lire/écrire — pas d'accès Grist propre.

## 9. Statut & dépendances

| Élément | Statut |
|---|---|
| `shared/grist-bridge.js` (brique read/write) | ✅ extrait, unit-testé, distribuable |
| Contrat de consommation (ce doc) | ✅ v0.1 |
| Interface bridge attendue par les WC | ✅ spécifiée (§4) |
| Format `_audit_hash` | 🟡 proposé, à figer par Composants-Architect |
| `survey_manifest.py v0.1` (schéma) | ⏳ co-rédigé sur cerema-offre-de-service |
| ComponentKind `survey_form/kpi/results` + AssemblyKind + bloc BlockNote | ⏳ **feu vert Composants-Architect requis avant tout code** |
| `<cerema-survey-form>` / `<cerema-geo-map>` (UMD/CDN) | ⏳ à produire côté lib partagée |

Rien côté registre n'est codé avant le feu vert de Composants-Architect. Ce doc + le module
bridge sont notre contribution prête à brancher.
