# Cartographie dans Grist

> Convention établie et éprouvée sur plusieurs applications territoriales. La suivre
> rend une carte interopérable avec l'écosystème ; s'en écarter produit une carte qui
> marche seule et ne se réutilise pas.

## Une couche est une vue sur une source

Deux natures de source, et le choix se fait au début.

Une couche **autoportante** contient son GeoJSON : dessins, imports rapides. Elle
fonctionne sans document Grist derrière.

Une couche **liée** pointe une table « une ligne = un objet ». La collection est
construite à la volée depuis la table, l'édition se fait ligne par ligne, et un cache
peut être conservé pour l'affichage autonome ou la performance.

La géométrie stockée est **toujours en WGS84**. Convertir à la lecture si la source est
dans une autre projection, jamais stocker autre chose.

## La table de données

Une table par couche, une ligne par objet. Une colonne porte la géométrie, les autres
sont les attributs — donc utilisables comme n'importe quelle donnée Grist : filtres,
formules, jointures, vues liées.

C'est ce qui fait l'intérêt de la carte dans Grist : la géométrie n'est pas un monde à
part, c'est une colonne de plus.

## Le registre de couches

Une table déclare **ce qui est une couche et comment l'afficher** : nom, nature, table
source, colonne de géométrie, type géométrique, style.

Seules les couches déclarées ici sont affichées ; les autres tables géographiques du
document sont simplement *proposées à lier*. Sans ce registre, il faut deviner quelles
tables sont cartographiables — et se tromper.

Le lien vers la table source est stocké en **texte**, pas en référence Grist. Raison
technique, pas préférence : une colonne `Ref` cible une table fixe, or une couche doit
pouvoir pointer n'importe laquelle.

## Un style qui voyage

Le style est décrit en JSON plutôt qu'en code : un mode — uniforme, par catégories, par
tranches — un champ porteur, des couleurs, des seuils.

L'intérêt est la portabilité : la même description se lit dans un SIG bureautique et
dans un widget. Un style écrit en dur dans le JavaScript d'un artefact ne sort jamais de
cet artefact.

## Trois contraintes Grist à respecter

La réactivité automatique ne vaut que pour la table à laquelle le widget est rattaché.
Les autres tables se lisent en une fois, et se rafraîchissent explicitement après
écriture.

Une carte qui lit plusieurs tables et écrit demande l'accès complet.

Et le lien couche → table étant du texte, c'est au code de le résoudre — donc de traiter
le cas où la table a été renommée ou supprimée, plutôt que de rendre une carte vide sans
explication.
