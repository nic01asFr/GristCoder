# Assistant SURFAC²E — étude de contexte & cadrage (v0.1)

Branche `feat/surfac2e-assistant`. But : spécialiser notre service (grist-coder + harness
agent) en **copilote métier borné à un doc Grist**, premier cas = **SURFAC²E**. Ce document
capitalise l'étude du projet surfac2e (repo local `GT SURFAC2E 2026`) et définit ce que
l'assistant doit faire, comment, et sous quelles garanties.

---

## 1. Ce qu'est SURFAC²E (contexte métier)

Méthode CEREMA de **diagnostic et de pilotage du patrimoine bâti public** (bâtiments des
collectivités : communes, EPCI, départements, régions, État). Elle **cote** chaque
bâtiment/site sur un **référentiel multicritère** pour produire une **aide à la décision
immobilière/foncière** : où rénover en priorité, quels signaux réglementaires traiter,
quelle trajectoire énergétique.

Enjeux de politique publique servis :
- **Sobriété / décret tertiaire — OPERAT** (assujettissement au cumul ≥ 1 000 m² SDP sur une
  unité foncière ; année de référence 2019 ; intensité kWh/m² ; trajectoire de réduction).
- **Adaptation au changement climatique** (canicule, inondation débordement/ruissellement,
  RGA retrait-gonflement des argiles, fortes pluies).
- **Qualité d'usage & fonctionnalité** du bâti.

Cadre : groupe de travail **« Fiabilisation de la méthode SURFAC²E »** actif en 2026.
Livrable : une **app (widget Grist + PWA/APK terrain)** ; utilisateurs = **agents des
collectivités** (saisie terrain + pilotage bureau).

---

## 2. Le doc Grist cible (modèle de données v2)

> Deux générations de schéma coexistent dans le repo : **v1** (MVP démo, table `Batiments`
> cotée directement, 6 items en dur) et **v2** (cible générale, table pivot `Entites` +
> `Referentiel_items`). **Le doc cible = v2** (tables observées : Entites, Sites, Batiments,
> Assise_parcellaire, Zones_fonctionnelles, Referentiel_items, Cotations, Artefacts).

Hiérarchie spatiale (4 mailles via `Entites.niveau`) :
```
Site ──< Assise_parcellaire (unité foncière) ──< Batiment (rnb_id) ──< Zone_fonctionnelle
```

Tables pivots (v2) :
| Table | Rôle | Colonnes clés |
|---|---|---|
| `Entites` | objet coté polymorphe | `objet_id`, `niveau` (site/assise/batiment/zone_fonctionnelle), `libelle` |
| `Referentiel_items` | **catalogue de cotation** | `item`, `brique`, `niveau`, `regime` (SAISIE/AUTO/SEMI), `condition`, `definition`, `lib_1..4`, `potentiel_pertinent`, `profondeur_min`, `producteur` |
| `Cotations` | notes portées | `objet`→Entites, `item`, `brique`, `note` (1-4), `note_potentiel`, `statut` (propose/valide), `prov` (terrain/referentiel), `profondeur`, `auteur`, `date_cotation`, formule `applicable` |
| `Batiments` | inventaire | pivot `rnb`, `surface`, `annee`, `usage`, `dpe`, `rga`, `inondation`, `completude`, `fiabilite`, `signal`, `action_*` |
| `Sites` | unité foncière | `cerema_site_id`, `parcelles`, `unite_fonciere`, `surface_plancher`, `operat_assujetti`, `operat_motif` |
| énergie | `Factures`, `Consommations`, `Operat` | pipeline OCR/VLM → suivi réglementaire |

Pivots : **`rnb`** (Référentiel National des Bâtiments) relie bâtiment ↔ 3D ↔ terrain,
jamais dupliqué ; `cerema_site_id` pour le site.

---

### 2 bis. Schéma LIVE autoritatif (doc `2Lk1zX9oZW9F`, lu via grist_schema 2026-07-26)

Le doc live est **plus riche que le repo** et porte la logique de calcul EN FORMULES Grist
(donc lisible/dérivable — pas besoin des xlsx pour les pondérations). 13 tables métier :

| Table | Colonnes clés (live) |
|---|---|
| `Entites` | objet_id, niveau, libelle, actif, **parent (Ref:Entites → hiérarchie)**, chemin(f), racine_site(f), **agg(f)** = moteur d'agrégation, **score_{etat_technique,environnement,attractivite,confort_sante,surete_securite,fonctionnalite,reglementaire,adaptation_climat}(f)**, note_globale(f), etat_validite(f: sans_objet/non_comparable/partiel/complet), taux_objectivation(f), profondeur_dominante(f), manque_recolte(f), manque_referentiel(f) |
| `Referentiel_items` | item, brique (8 valeurs), niveau, **poids_global (Numeric = LA pondération)**, regime (AUTO/SEMI/SAISIE), producteur, source, condition, definition, lib_1..4, potentiel_pertinent, profondeur_min, version_ref |
| `Cotations` | objet (Ref:Entites), item, brique, note (Int), note_potentiel, potentiel_commentaire, profondeur, prov (terrain/referentiel/**calcule**), statut (propose/valide/rejete), confiance (Numeric), date_cotation, auteur, niveau(f), **applicable(f)** = moteur d'applicabilité |
| `Batiments` | id_bat, rnb, nom, adresse, annee, surface_sdp, usage, famille, type_etablissement, dans_perimetre_gestion, entite (Ref) |
| `Sites` | cerema_site_id, nom, commune, code_insee, adresse, famille, gestionnaire_ref, entite(f Ref) |
| `Assise_parcellaire` | parcelle_id, contenance_m2, commune, entite(f Ref) |
| `Zones_fonctionnelles` | activite, sous_categorie_operat, amplitude_usage, surface_sdp, entite(f Ref) |
| `Enrichissement` | **champ, valeur, source, confiance, date_maj, statut (propose/valide/rejete), entite** — collecteur des données AUTO/SEMI (clé-valeur horodatée, humain valide) |
| `Config` | cle, valeur, type_valeur (texte/nombre/booleen/json), description |
| `Referentiel_version` | composant (schema/referentiel/config), version, installe_le |
| `Acteurs` | nom, organisme, role, courriel |
| `Droits` | niveau_droit (lecture/saisie/validation/administration), entite (Ref), acteur (Ref:Acteurs) |
| `Pieces_reglementaires` | type_piece, reference, date_piece, echeance, entite (Ref) |

Conséquences majeures pour l'assistant :
- **Les pondérations et le score sont DANS le doc** (`Referentiel_items.poids_global` + `Entites.agg`
  qui fait moyenne pondérée par brique sur cotations `valide`). L'assistant **lit/explique** ces
  scores, il ne les recalcule pas. Les xlsx de pondération deviennent secondaires.
- **La complétude/objectivation est calculée** (`taux_objectivation`, `manque_recolte`,
  `manque_referentiel`, `etat_validite`) → le contrôle qualité s'appuie dessus, pas à réinventer.
- **Deux exutoires d'écriture distincts** : les notes → `Cotations` ; les données de contexte
  enrichies (adresse, DPE, risques…) → **`Enrichissement`** (champ/valeur/statut). L'assistant
  propose dans l'un OU l'autre selon la nature, toujours `statut=propose`.
- **Couche de gouvernance** : `Acteurs`/`Droits` (droits par entité) — l'assistant doit respecter
  le `niveau_droit` de l'utilisateur (lecture/saisie/validation/administration).
- **Hiérarchie réelle** = `Entites.parent` (self-ref), pas des jointures externes.

## 3. Règles de cotation (le cœur, non négociable)

- **Échelle ordinale 1→4**, polarité **positive** (1 = défavorable/rouge, 4 = favorable/vert).
  `0` = non renseigné, `999` = sans objet. Libellés propres à l'item (`lib_1..lib_4`).
- **Double note** : `note` (état constaté) + `note_potentiel` (meilleure note atteignable
  technique+réglementaire sans limite budgétaire), si `potentiel_pertinent`.
- **Régime → statut/provenance par défaut** :
  - `SAISIE` (dire d'expert terrain) → `prov=terrain`, `statut=valide` ;
  - `AUTO`/`SEMI` (producteur machine) → `prov=referentiel`, `statut=propose` (à confirmer).
- **Profondeur jamais vide** : `simplifie` | `complet` (deux niveaux d'exigence).
- **Applicabilité pilotée par le référentiel** : mini-grammaire `champ=valeur`,
  `champ in (a,b,c)`, `champ <|>|<=|>= n`. Condition vide → applicable ; **non reconnue →
  applicable MAIS signalée** (on préfère l'écart visible à une complétude faussée).
- **Scores/complétude = formules Grist (Python)**, pas calculés côté client ; `applicabilite.js`
  en est le **miroir JS**. Le terrain **capture**, Grist **agrège**.
- **Frontière invariant/configurable** (`_core/config.js`) : verrouillés = colId, jointures,
  forme du référentiel, formules ; configurable par instance (table `Config`) = libellés,
  listes, seuils. Ne jamais écrire un libellé en dur.

---

## 4. Ce que l'assistant SURFAC²E doit faire (mode operator)

L'assistant **opère** le doc, il ne le construit pas. Fonctions priorisées (ROI décroissant),
toutes ancrées sur l'existant (`modules/coter/module.js`, `_core/`) :

1. **Cotation guidée par le référentiel** — lit `Referentiel_items` (definition, `lib_1..4`,
   condition, potentiel), propose une note **avec justification**, explique l'échelle, gère
   « sans objet » et le potentiel. Extension directe du module Coter.
2. **Contrôle qualité / cohérence** — croise `Cotations` × `Batiments`/`Entites` : incohérences
   (DPE `E` ↔ chauffage coté 4 ; `annee<1997` sans items réglementaires), items applicables
   **non cotés** (complétude), cotations `propose` en attente, provenances anormales →
   liste de tâches priorisée (nourrit l'écran Actions).
3. **Réconciliation énergie** — prolonge l'OCR (`/api/ocr`, VLM gemma) : appariement
   `Factures`→bâtiment/PDL, déduplication, trous de couverture `Consommations`, transitions
   de statut.
4. **Narrateur complétude / fiabilité** — explique *pourquoi* un bâtiment est `Faible/Moyenne`
   et *ce qui manque* pour monter d'un cran.
5. **Synthèses / atlas** — restitution narrative par site/patrimoine (volet restitution).
6. **Aide localisation géo** — résout `rnb`, remplit `centroid` depuis la BAN, remonte les
   risques Géorisques.

Contraintes d'intégration :
- **Ne pas modifier `app/`** (app hôte stable) ; l'assistant agit sur les **données** du doc.
- **Écrire via `applyUserActions`** (contrat de `_core/grist-client.js`), jamais reconstruire
  le schéma. Respecter statut/prov/profondeur. Compatible chemin hors-ligne (file ordonnée)
  pour ce qui touche le terrain.
- **Ne rien inventer** : une note proposée est `statut=propose` (humain dans la boucle).

---

## 5. Mécanisme de spécialisation — Assistant Manifest

Principe : **le runtime générique (grist-coder) + un Assistant Manifest = un copilote métier**.
Frère déclaratif du Survey Manifest / Scene Manifest de l'écosystème CEREMA.

Ce que porte le manifeste (le **non-déductible** du doc) :
- `persona` : rôle, ton, expertise (« expert diagnostic patrimoine bâti SURFAC²E »).
- `knowledge` : les **règles de méthode** (§3), pas le catalogue d'items.
- `mode` : `operator` (sélectionne le jeu d'outils lecture/CRUD/analyse, **exclut** AddTable/drop).
- `playbooks` : cotation guidée, contrôle qualité, réconciliation énergie, synthèse.
- `bindings` : tables clés et leur rôle (Referentiel_items = catalogue, Entites/Cotations = données).
- `guardrails` : jamais de méta-écriture, écriture bornée à Cotations/Factures via applyUserActions,
  statut `propose` sur toute proposition machine.

Ce que le manifeste **ne porte PAS** (dérivé live du doc) :
- le schéma exact (via `grist_schema`), le **catalogue d'items** (lu dans `Referentiel_items`),
  les libellés/seuils configurables (table `Config`). → zéro duplication, zéro dérive.

Binding : **pod-level** (une valeur chart `ASSISTANT_MANIFEST_URL` → le pod EST l'assistant
SURFAC²E, cohérent avec le modèle Onyxia par-pod) ou **doc-level** (ressource/lecture au démarrage
de session). À trancher (cf. §7).

---

## 6. Zones d'ombre à lever (binaires non lus par l'étude)

Portent des règles importantes, non extraits (fichiers `.xlsx`/`.odg`/`.boxnote`) :
- `SURFAC2E_dictionnaire_donnees.xlsx` — dictionnaire de données autoritaire (colonnes exactes
  Assise_parcellaire / Zones_fonctionnelles).
- `3_indicateurs/*.xlsx` + `.odg` — **pondérations** des indicateurs (axes État / Usage).
- `Cotation brique réglementaire_V0/V1.xlsx` — brique réglementaire.
- `Doc travail définition espace zone.boxnote` — définition fine des Zones_fonctionnelles.

→ à ouvrir manuellement (ou fournir en CSV) avant d'opérationnaliser la restitution pondérée.

---

## 7. Décisions à trancher (avant de coder)

1. **Binding** : pod-level (`ASSISTANT_MANIFEST_URL`) vs doc-level (ressource) vs les deux.
2. **Périmètre v0** : commencer par **cotation guidée** (fonction 1) seule, ou + **contrôle
   qualité** (fonction 2) ? (recommandé : 1 puis 2.)
3. **Écriture terrain** : l'assistant écrit-il directement `Cotations`, ou propose-t-il des
   valeurs que l'agent valide dans le module Coter existant ? (respect du contrat offline).
4. **Source du référentiel étendu** : les 645 items `inputs_simplifies_*.csv` sont-ils déjà
   chargés dans `Referentiel_items` du doc cible, ou à provisionner ?

---

## 8. Prochaines étapes

- Esquisse `shared/assistant_manifest.py` (Pydantic v0.1) + instance `surfac2e`.
- Côté serveur : overlay `SERVER_INSTRUCTIONS` + jeu d'outils `operator` (réutilise
  `CONTEXT_TOOLS`), lecture du manifeste au démarrage de session.
- Valider avec un vrai doc SURFAC²E v2 (le doc `2Lk1zX9oZW9F` a les tables ; vérifier
  `Referentiel_items` peuplé).
