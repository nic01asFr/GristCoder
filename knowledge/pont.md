# Le pont Grist — contrat pour un artefact

> Unité canonique. Ce document fait foi sur ce qu'un artefact peut tenir pour acquis.
> Les documents plus anciens de l'écosystème (`Widgets Grist/skills/bridge.md` et
> `data-conversion.md`, 11 février 2026) décrivent l'état d'avant les helpers : ils
> restent justes sur `fetchTable`, mais suivre leur voie manuelle aujourd'hui revient
> à réécrire ce que le pont fournit — et à retomber dans le piège décrit plus bas.

## Lire une table

`grist.docApi.loadTable(t)` renvoie **un tableau de lignes** : `[{id, Colonne, …}]`, prêt
pour `rows.map(…)`.

```js
const lignes = await grist.docApi.loadTable('Employes');
hote.innerHTML = lignes.map(l => `<tr><td>${grist.util.esc(l.Nom)}</td></tr>`).join('');
```

Ce n'est **pas** une enveloppe indexée par le nom de la table. `resultat.Employes`
vaut `undefined` — et un `|| []` derrière transforme la faute en écran vide sans la
moindre erreur. C'est la faute la plus coûteuse observée : cinq artefacts sur six
affichaient des zéros, sans exception, sans trace.

Le tableau renvoyé se laisse néanmoins demander `.Employes` : il se retourne lui-même
et l'écrit dans la console, que le diagnostic de rendu remonte à l'agent. Le rendu est
sauvé, la source reste à corriger.

`grist.docApi.fetchTable(t)` existe toujours et reste colonnaire — `{id: [...], Col: [...]}` —
quand c'est cette forme qu'on veut.

## Écrire

```js
await grist.docApi.addRow('Demandes', {Employe: 3, Jours: 5});
await grist.docApi.updateRow('Demandes', 12, {Statut: 'approuve'});
await grist.docApi.deleteRow('Demandes', 12);
```

Chacun construit la bonne UserAction **et renvoie la liste à jour** : une écriture est
suivie d'un rendu sans second aller-retour. `applyAndFetch(actions, table)` fait de même
pour des actions écrites à la main.

Depuis un navigateur, utiliser `BulkAddRecord` — jamais `BulkAddOrReplaceRecord`, que
Grist refuse dans ce contexte (`not controlled`).

## Convertir — `grist.util`

| fonction | ce qu'elle évite |
|---|---|
| `toRows(d)` | réécrire la conversion colonnaire → objets |
| `toDate(ts)` / `fromDate(d)` | **Grist stocke les dates en SECONDES** — la valeur brute affiche `1704067200` au lieu d'une date |
| `refIds(l)` / `toRefList(a)` | une RefList est préfixée `'L'` ; la traiter en tableau nu écrit une valeur invalide |
| `esc(t)` | une valeur de cellule placée dans `innerHTML` fait du contenu du document du code |

## Qui regarde l'écran — `grist.user`

`grist.user` vaut `{id, email, nom}` pour l'utilisateur Grist connecté.

N'écris **jamais** un identifiant en dur pour désigner « l'utilisateur courant » : un
`const employeId = 1` montre la première ligne de la table à tout le monde. Rapproche
`grist.user.id` (ou l'e-mail quand il est disponible) d'une colonne de ta table de
personnes, et prévois le cas où la correspondance échoue — un message clair plutôt
qu'une ligne au hasard.

C'est de l'**affichage**. La véritable application des droits passe par les règles
d'accès Grist, évaluées côté serveur, où `user.Email` et `user.Access` sont disponibles ;
le widget ne reçoit déjà que les lignes que l'utilisateur a le droit de voir.

## Le style

Un plancher visuel est injecté dans chaque artefact — police, marges, tableau lisible,
boutons et champs corrects. Il est écrit entièrement en `:where()`, donc de spécificité
**nulle** : la moindre règle de l'artefact l'emporte. Il évite le HTML nu, il ne
remplace pas une identité visuelle.
