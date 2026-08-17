# Grist — faits de plateforme

> Ce que Grist fait vraiment, par opposition à ce qu'on croit. Chaque fait a été
> établi par un incident ou une vérification, pas déduit d'une documentation.
> Aucun de ces faits n'est déductible du code d'un artefact : c'est la raison
> d'être de cette unité.

## Les champs méta sont des chaînes JSON, jamais des objets

`widgetOptions`, `options`, `layoutSpec`, `customView`, `filter`, `rules` sont stockés
par Grist comme des **chaînes contenant du JSON**.

Passer un objet le fait traverser le sandbox Python, qui le sérialise avec `repr()` :
`{'choices': ['a']}`, apostrophes simples. Grist l'accepte sans broncher. Le frontend,
lui, fait `JSON.parse` dessus, échoue — et **le document ne s'ouvre plus**, avec un
`Cannot read properties of undefined`. Le vrai indice est le premier maillon de la
cascade : `Expected property name … at position 1`.

Réparation : `UpdateRecord` sur `_grist_Tables_column` avec une chaîne correctement
encodée.

*Provenance : leçon apprise une première fois en décembre 2025 lors du durcissement de
mcp-server-grist — « sérialisation JSON avec json.dumps() », toujours une chaîne. Elle
n'avait été consignée nulle part d'accessible, et a été repayée le 16 août 2026 au prix
d'un document rendu inouvrable.*

## Écriture concurrente sans verrou = perte silencieuse

Deux sessions agentiques qui écrivent le même widget sans versionnement s'écrasent l'une
l'autre, sans conflit ni avertissement. Constaté sur budget_app : le widget Suivi a été
perdu par réécriture concurrente.

Le risque vaut dès que plusieurs agents ou onglets peuvent toucher le même artefact.
`canvas_write` n'a aujourd'hui aucun verrou ni détection de version.

## Ce que Grist ne fait pas nativement

**L'import mappé** — avec transformation et fusion — existe dans l'interface, mais la
configuration **n'est pas persistée** et **n'est pas exposée en API REST**. Tout import
récurrent ou programmable demande son propre moteur côté service.

**Il n'y a pas d'envoi d'e-mail natif.** Toute notification passe par un webhook et un
service tiers.

*Provenance : audit des capacités natives, projet observatoire-eclext, 6 août 2026.*

## Deux détails d'API qui font gagner du temps

`BulkAddOrUpdateRecord` **retourne les identifiants** des lignes touchées : un upsert
n'a pas besoin d'une relecture pour savoir ce qu'il vient d'écrire. (Grist v1.7.13)

Un custom widget qui veut réagir aux sélections d'autres vues a besoin du champ
`linking` dans ses `InteractionOptions`. Sans lui, il reste **aveugle** aux sélections —
sans erreur, simplement rien ne se passe.

## Le WAF, sur les instances qui en ont un

Sur `grist.numerique.gouv.fr`, Incapsula refuse en 403 les charges utiles contenant des
balises HTML nues — la signature du JSX. Échapper `<` et `>` n'y change rien : le WAF
normalise les échappements JSON. L'écriture bascule alors sur le navigateur, qui écrit en
same-origin. Les requêtes SQL à sous-requêtes ou `UNION` sont également refusées par
moments.

Et le challenge est **intermittent** : un même appel passe, puis échoue, puis repasse.
Les helpers HTTP doivent donc partager un cookie jar et suivre les redirections.
