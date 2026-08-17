# Assembler une application

> Un artefact seul n'est pas une application. Ce qui suit concerne ce qui les relie :
> les pages, la communication entre écrans, la publication, et ce qui sort du document.

## Pages Grist et sections

Une page porte une ou plusieurs sections ; un artefact vit dans une section de type
`custom`. Trois règles, apprises en cassant des documents.

Les tables méta — `_grist_Views`, `_grist_Views_section`, `_grist_Pages` — s'écrivent
**uniquement** par UserAction (`UpdateRecord`), jamais par un PATCH REST : ce dernier
stocke le JSON comme objet et le frontend n'ouvre plus le document.

`customView` est une **chaîne** contenant du JSON, pas un objet. Même piège, mêmes
conséquences.

Pour retirer une section, `RemoveViewSection` — `DeleteViewSection` répond 500. Et après
une création, il faut relire les sections plutôt que se fier aux valeurs retournées.

Enfin : créer une table crée aussi sa vue brute, qui apparaît dans la navigation. Une
application propre les masque ou n'en tient pas compte.

## Faire communiquer deux écrans

Trois mécanismes, souvent confondus, qui ne servent pas au même usage.

**Entre deux widgets d'une même page** : un `BroadcastChannel` nommé par domaine, avec
des messages typés — sélection d'un objet, changement de contexte, effacement. Prévoir un
tampon pour les messages arrivés avant la fin du chargement, sinon les premiers se
perdent. C'est la voie éprouvée en production : elle fonctionne entre iframes de même
origine sans passer par Grist, donc sans la latence d'un aller-retour par le curseur.

**Entre un artefact et une iframe qu'il héberge** : `postMessage`, avec un identifiant de
requête pour rapprocher réponse et appel.

**À l'intérieur d'un seul artefact complexe** : un simple bus d'événements en mémoire.

*Arbitrage acté : pour le widget-à-widget, c'est `BroadcastChannel`. Les deux autres ne
sont pas des concurrents mais des briques d'un autre étage.*

## Publier un widget autonome

Un artefact publié est figé dans les options de sa section : le code vit dans le
document, et le serveur qui l'a produit peut disparaître.

D'où l'exigence : **un artefact publié ne doit référencer aucun service intermédiaire**.
Ni proxy, ni point d'entrée du serveur, ni son URL. Un widget qui appelle son fabricant
meurt avec lui.

Les dépendances npm sont assemblées au moment de la publication, ce qui supprime le
recours à un CDN de bibliothèque. Une dépendance demeure toutefois : la coquille de
galerie qui héberge le code. « Aucune dépendance au serveur » est exact ; « aucune
dépendance » ne l'est pas.

Écrire du JSX côté serveur échoue sur les instances protégées par un pare-feu applicatif
— les balises nues en sont la signature. L'écriture bascule alors sur le navigateur, ou
la source s'écrit en appels de fonction plutôt qu'en JSX.

## Sortir du document

**Réagir à un changement** : un webhook, déclenché par Grist sur création ou modification,
vers un service qui fait le reste. Il n'y a pas d'envoi de courriel natif : toute
notification passe par là.

**Appeler un modèle de langage** : jamais depuis l'artefact avec une clé en clair. Un
relais côté serveur porte la clé, et n'autorise que des hôtes déclarés — sans cette liste,
le relais devient un proxy ouvert.

**Enrichir depuis des données publiques** : le géocodage d'adresses françaises, les
données foncières, les référentiels d'entreprises sont accessibles en `fetch` direct
depuis un artefact, sans authentification. Le géocodeur rend une liste de candidats avec
un score : proposer, laisser choisir, ne jamais retenir le premier en silence.

## Un formulaire décrit plutôt que codé

Un manifeste déclare les champs, leurs types et leur table cible ; le service en dérive
le formulaire et **vérifie que le schéma du document correspond** — colonnes manquantes,
types divergents.

L'intérêt n'est pas d'éviter d'écrire un formulaire, c'est de rendre le contrat
vérifiable : la collecte et le document ne peuvent plus diverger en silence.
