# Interface d'un artefact

> Ce qui fait qu'un écran se laisse utiliser. Un plancher visuel est injecté — police,
> marges, tableau lisible, boutons corrects — mais il ne fait qu'éviter le pire. Tout ce
> qui suit reste à la charge de l'artefact.

## Par quoi commencer un artefact

Un artefact est une page complète et autonome : `<!DOCTYPE html>`, un `<style>` en tête,
un conteneur, et le script en fin de corps. Trois choses le distinguent d'une page
ordinaire.

`grist.ready({requiredAccess: 'full'})` en premier, sinon rien ne lit ni n'écrit. Le
chargement des données est **asynchrone** : l'écran doit exister avant elles. Et le
rendu se refait après chaque écriture, à partir des lignes que les helpers renvoient.

L'ordre qui marche : poser la structure, afficher un état d'attente, charger, rendre.
Jamais l'inverse — un `innerHTML` avant chargement écrase ce que le rendu vient de poser.

## Filtrer et rechercher dans une liste

Le filtrage est **côté client** : les lignes sont déjà en mémoire, les refiltrer ne coûte
rien et évite un aller-retour.

Un champ de recherche et un ou deux sélecteurs suffisent. La recherche compare en
minuscules sur les colonnes affichées ; les sélecteurs se remplissent depuis les valeurs
présentes, pas depuis une liste écrite en dur — sinon ils mentent dès que les données
changent.

Retenir le filtre entre deux ouvertures — dans `localStorage`, sous une clé propre à
l'artefact — évite de le reposer à chaque fois. Et afficher le nombre de lignes retenues
sur le total dit à l'utilisateur que le filtre agit, plutôt que de le laisser croire que
la table est vide.

## Éditer une ligne sans quitter la liste

Une modale : un fond assombri, un panneau centré, un formulaire pré-rempli, deux boutons.
Elle se ferme à l'échappement et au clic sur le fond, sinon on s'y sent enfermé.

L'enregistrement passe par `updateRow`, qui rend la liste à jour : on ferme la modale et
on rend, sans relire. En cas d'échec, la modale **reste ouverte** avec le message — la
fermer ferait perdre la saisie.

## Dire ce qui vient de se passer

Une écriture réussie sans retour visible laisse croire qu'il ne s'est rien passé. Un
bandeau bref, en haut ou en bas, qui disparaît de lui-même après quelques secondes,
suffit. Vert pour un succès, rouge pour un échec, et **le message d'erreur en clair** :
« Impossible d'enregistrer : … », jamais un simple « erreur ».

## Le chargement et le vide

Trois états à distinguer, et les confondre est la faute la plus courante.

**Pendant le chargement** : un mot, « Chargement… ». Un écran vide pendant deux secondes
ressemble à une panne.

**Aucune donnée** : un message qui dit quoi faire — « Aucune demande pour l'instant.
Créez la première avec le bouton ci-dessus. » Pas un tableau vide sans en-tête.

**Aucun résultat après filtrage** : un message *différent* — « Aucune ligne ne correspond
à votre recherche » — et de quoi effacer le filtre. Sans cette distinction, l'utilisateur
croit que ses données ont disparu.

## Un formulaire de saisie

Les champs portent un libellé au-dessus, jamais seulement un texte d'exemple à
l'intérieur — il disparaît dès qu'on tape et l'utilisateur ne sait plus ce qu'il
remplit.

Une date se saisit avec `<input type="date">` et se convertit avant écriture ; une valeur
parmi plusieurs avec un `<select>` alimenté par la table de référence, jamais par une
liste figée. La validation se fait avant l'envoi, avec le message à côté du champ fautif.

Après un enregistrement réussi, le formulaire se vide et la liste se rafraîchit — sinon
l'utilisateur renvoie la même ligne deux fois.
