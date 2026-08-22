# Concevoir les données

> Le modèle se paie longtemps. Une table mal posée se rattrape ; un ancrage mal posé
> oblige à reprendre les formules et les jointures.

## Créer les tables et les colonnes

Une table se crée avec ses colonnes en une fois : un identifiant, un type, et selon le
type des options. Les types courants — texte, entier, nombre, date, booléen, choix,
choix multiple, référence, liste de références, pièce jointe.

Trois choses à savoir avant d'écrire.

**La casse compte.** Un identifiant de colonne mal casé produit une erreur opaque à
l'écriture, pas à la création. Relire le schéma plutôt que se fier à sa mémoire.

**Le préfixe souligné disparaît.** Un identifiant commençant par `_` est créé sans lui.
Un code qui l'attend cherchera une colonne inexistante.

**Une référence vers une table créée plus tard dans le même lot casse.** L'ordre compte :
créer d'abord la table cible, ou ajouter la colonne de référence dans un second temps.

## Les références

Une référence pointe **une table fixe** — c'est un choix de modèle, pas un détail. Pour
qu'une donnée puisse pointer plusieurs tables selon le cas, il faut passer par un
identifiant textuel résolu par le code, pas par une référence.

Une référence affiche par défaut un identifiant de ligne. Déclarer la colonne à afficher
la rend lisible partout — dans la table comme dans les widgets.

Une liste de références se stocke sous une forme préfixée : la traiter comme un tableau
ordinaire écrit une valeur invalide.

## Les formules

Une colonne formule se calcule côté serveur, en Python, à chaque changement. Elle voit
la ligne courante, les tables du document, et les fonctions d'agrégation.

Ce qu'il faut en retenir pour concevoir : **tout ce qui se déduit doit être une formule**,
jamais une valeur saisie qu'on maintient à la main. Un solde, un total, un statut dérivé,
un décompte — s'ils sont saisis, ils divergeront.

Une colonne formule jamais écrite reste marquée comme telle avec une formule vide : c'est
un état normal, pas un schéma cassé.

Et une formule d'agrégat en cascade n'est pas un risque de performance : mesuré, une
lecture de plus de huit cents entités avec agrégats récursifs se compte en centaines de
millisecondes.

## Typer les colonnes, ou le payer ailleurs

C'est le défaut le plus coûteux du modèle, et le plus discret : tout laisser en texte.
Rien ne casse à la création. Rien ne casse à la saisie. Ça casse plus tard, ailleurs,
dans un écran qu'on croyait fini.

Un montant en texte se lit `"5000"`. Une addition qui part de zéro donne alors `"05000"`,
puis `"050002000"` : la somme **concatène**. Et le piège se referme parce que la mise en
forme ne proteste pas — appliquée à une chaîne, elle la rend telle quelle, sans erreur.
On croit avoir formaté. Le tableau de bord affiche un total de quinze chiffres, et le code
a l'air juste.

Une date en texte ne se trie pas dans l'ordre du temps, ne se filtre pas par période, et
n'ouvre pas de sélecteur de date. Un statut en texte n'a ni liste fermée, ni pastille
colorée, et accueille toutes les fautes de frappe. Dans les trois cas, les vues de synthèse
natives deviennent inutilisables sur la colonne.

Deux gestes, et le premier dispense du second.

**À la création, donner le bon type.** Montant, total, quantité, taux : numérique ou
entier. Date, échéance : date. Statut, catégorie, priorité : liste de choix. Case à
cocher : booléen. Le coût est nul à ce moment-là ; il devient une reprise ensuite.

**À la lecture, convertir avant de calculer.** Même sur une colonne bien typée, une valeur
passée par une saisie peut revenir en chaîne. Toute arithmétique commence donc par une
conversion explicite, avec un repli sur zéro pour les valeurs absentes.

Le signe qui doit alerter, à l'écran : un total anormalement long, ou qui commence par un
zéro. Ce n'est jamais un grand nombre, c'est une concaténation.

## Ce qui coûte cher plus tard

Ancrer les données directement sur une table métier — une référence vers « Bâtiments » —
paraît naturel et se paie au premier niveau supplémentaire. Ajouter ensuite un niveau
plus fin n'est pas additif : il faut reprendre le cœur du modèle.

Une table d'entités générique, portant un identifiant d'objet, un niveau et un parent,
coûte un peu plus à poser et évite cette reprise. Le choix se fait au début, quand le
modèle est encore jeune — après, il est trop tard pour qu'il soit économique.
