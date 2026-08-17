# Restreindre ce que chaque utilisateur voit

> Vérifié : ce qui suit décrit ce que l'API permet réellement, éprouvé sur une
> application en service, et non ce qu'on suppose depuis un widget.

## La règle est appliquée par Grist, pas par l'écran

C'est le point qui change tout, et qu'on prend souvent à l'envers.

Les règles d'accès sont évaluées **côté serveur**, là où l'adresse et le niveau d'accès
de l'utilisateur sont disponibles. Un widget n'a donc **pas besoin de connaître
l'identité** de qui le regarde pour que la restriction s'applique : il ne reçoit déjà
que les lignes auxquelles l'utilisateur a droit.

Corollaire : filtrer dans l'artefact est un **confort d'affichage**, jamais une sécurité.
Un filtre en JavaScript se contourne en ouvrant la console.

## Ce à quoi ressemble une règle

Une règle porte sur une ressource — une table, un jeu de colonnes — et s'écrit comme une
condition qui **interdit** quand elle est vraie. Deux formes couvrent l'essentiel des
besoins.

L'appartenance directe : chacun voit ses propres lignes, sauf les propriétaires du
document. La condition compare l'adresse de l'utilisateur à une colonne de la ligne.

L'appartenance par liste : un responsable voit les lignes de son équipe. La condition
teste la présence de l'adresse dans une colonne qui en contient plusieurs — en encadrant
la valeur cherchée par des séparateurs, faute de quoi une adresse contenue dans une autre
passerait.

## Ce que l'artefact peut savoir

L'identité de l'utilisateur connecté est mise à disposition dans chaque artefact —
identifiant, et selon le contexte adresse et nom.

Elle sert à **présenter** : afficher « mes demandes » plutôt que toutes, pré-remplir un
champ auteur, masquer un bouton hors de portée. Elle ne sert pas à protéger.

Et il ne faut jamais désigner l'utilisateur courant par un identifiant écrit en dur :
l'écran montrerait la même ligne à tout le monde. Le rapprochement se fait avec une
colonne de la table des personnes, en prévoyant le cas où il échoue — un message clair
plutôt qu'une ligne au hasard.

## L'ordre dans lequel s'y prendre

Poser d'abord le modèle avec la colonne qui porte l'appartenance — une adresse, ou une
liste d'adresses. Sans elle, aucune règle n'est exprimable.

Écrire ensuite les règles, en gardant les propriétaires du document toujours autorisés :
une règle qui exclut tout le monde verrouille le document pour son auteur aussi.

Vérifier enfin depuis un compte qui n'est pas propriétaire. Une règle se juge à ce qu'elle
refuse, et cela ne se voit pas depuis le compte qui l'a écrite.
