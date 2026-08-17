# Vérifier, et comprendre ce qui ne va pas

> Un artefact peut échouer sans rien dire. Ces pannes-là coûtent plus cher que les
> autres, parce qu'on les prend pour un résultat.

## Ce que le rendu rapporte tout seul

Un capteur est injecté dans chaque artefact, **avant** le pont et avant le code. Il
retient les exceptions avec leur ligne, les promesses rejetées, les `console.error`, les
ressources qui n'ont pas chargé — et l'état du rendu : combien d'éléments, quel texte,
un canvas, ou rien.

L'écriture du code renvoie ce diagnostic. Il est donc disponible **sans rien demander**,
et le lire avant de déclarer un écran terminé évite l'essentiel des mauvaises surprises.

## L'écran est blanc

Par ordre de fréquence.

Une **exception à l'initialisation** : elle est dans le diagnostic, avec sa ligne. C'est
le cas facile.

Un **artefact React qui commence par un `import`** : il n'y a pas de bundler à
l'exécution, React est global. Un `import React` produit un écran blanc silencieux.

Un **composant jamais monté** : le code s'exécute, ne lève rien, mais rien n'est inséré
dans le document. Le diagnostic le signale — rendu vide sans exception.

Une **balise de fermeture de script écrite en clair** dans une chaîne JavaScript : le
parseur HTML termine l'élément à cet endroit et tout ce qui suit devient du texte.
Écrire cette séquence en deux morceaux concaténés.

## Les données n'arrivent pas

D'abord vérifier que la table existe et porte bien ce nom — la casse compte.

Ensuite, la faute la plus coûteuse : traiter le résultat du chargement comme une
enveloppe indexée par le nom de la table. Ce n'en est pas une, c'est directement le
tableau de lignes. Le champ vaut `undefined`, un `|| []` l'avale, l'écran affiche des
zéros. Aucune erreur, aucune trace — sauf le signalement que le pont émet maintenant
dans la console.

Puis : les colonnes formule masquées dans la vue ne remontent pas par l'événement de
sélection. Charger la table plutôt que se fier à l'enregistrement courant.

Enfin, si l'écriture échoue sans message clair, lire le **détail renvoyé par Grist** :
il nomme la cause — table inexistante, colonne invalide, erreur de sandbox. Une erreur
qui nomme sa cause ne se rejoue pas, elle se corrige.

## Regarder avant de conclure

Une capture de l'écran principal, avant de déclarer l'application terminée, révèle en
une fois ce qu'aucune relecture de code ne montre.

Ce qu'il faut y chercher : des compteurs à zéro alors que les tables ont des lignes ;
une liste vide ; un nombre à dix chiffres là où une date est attendue ; `[object Object]`
ou `NaN` dans une cellule ; un écran sans aucun style. Chacun de ces signes trahit une
faute précise, et chacun est invisible dans le code.

Le résumé final doit dire ce qui a été **vu**, pas ce qui a été voulu.
