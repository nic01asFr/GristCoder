# Patterns transversaux

> Couche 2 : les solutions récurrentes, chacune avec **l'alternative écartée et
> pourquoi**. C'est ce qui aide à *choisir*, non à faire — et c'est la couche qu'un
> inventaire par répertoire ne peut pas produire, puisqu'elle vit à cheval sur des
> projets qui ne sont pas au même endroit.
>
> Un mot sur la confiance : la définition de cette couche demande une preuve sur au
> moins deux projets. Sur les huit ci-dessous, **deux** l'ont — le client dual-mode et
> les états propose/valide, chacun attesté sur un second projet non colocalisé. Les six
> autres viennent d'une seule source. Ils restent précieux, mais chaque fiche dit ce
> qu'elle vaut : un savoir qui tait son degré de preuve se fait croire.

## Client dual-mode — en ligne et hors-ligne

Un client unique bascule entre l'API plugin de Grist (en ligne) et REST + IndexedDB
(hors-ligne), avec cache marqué et persistant.

**Alternative écartée** : deux clients séparés, un widget Grist standard et une
application terrain distincte.
**Pourquoi** : cela duplique toute la logique métier, qui diverge vite. C'est
exactement le risque déjà matérialisé ailleurs — un widget écrasé par une réécriture
concurrente, faute d'un modèle unique.

*Confirmé sur **deux projets indépendants**, dont le second a repris le mécanisme du
premier plutôt que de le réinventer — primitives capteurs, file d'attente hors-ligne,
synchronisation par lots. L'un des deux seuls patterns de cette page à atteindre ce degré
de preuve.*

## Frontière invariant / configurable

On sépare explicitement ce qui ne bouge jamais — `colId`, formules, jointures — de ce
qui se paramètre : libellés, choix, valeurs par défaut, visibilité. Le paramétrable vit
dans une table Config versionnée, pas dans le code.

**Alternative écartée** : tout coder en dur dans chaque widget.
**Pourquoi** : chaque nouveau déploiement oblige alors à *forker* le code au lieu de
l'*instancier* par configuration.

*Principe explicitement nommé « pas assez fait jusqu'ici » avant d'être posé. Attesté sur
une seule source.*

## Miroir JS ↔ Python des formules métier

Toute formule métier qui doit aussi tourner côté client, donc hors ligne, est dupliquée
en JS pur dans un fichier dédié, documenté comme miroir à maintenir des deux côtés.

**Alternative écartée** : ne calculer que côté serveur Grist, en Python, et forcer une
lecture réseau.
**Pourquoi** : cela casse le hors-ligne, qui est l'usage terrain principal. Le coût de
la double maintenance est *accepté* contre cette capacité — et le piège, connu, est la
dérive silencieuse entre les deux calculs.

*Miroir vérifié identique à la formule Python sur un cas réel, en recette. Attesté sur une
seule source.*

## Registre de modules avec `formFactor`

Un `manifest.json` déclare `{widgetId, name, url, requiredAccess, formFactor}` où
`formFactor` vaut `desktop` ou `mobile`. Ajouter un module, c'est un dossier et une
entrée.

**Alternative écartée** : une application par plateforme, bureau et mobile séparées.
**Pourquoi** : même raison que le client dual-mode — duplication, puis divergence.

*Même famille que le manifeste d'application multi-écrans, avec `formFactor` en plus.
Attesté sur une seule source.*

## Ancrage générique `objet_id + niveau`

Une table `Entites` générique — `objet_id`, `niveau`, `parent` — porte l'arbre ; les
tables métier référencent leur ligne d'`Entites` plutôt que d'être visées directement.

**Alternative écartée** : lier chaque donnée à sa table métier, `Ref:Batiments` en dur.
**Pourquoi** : constaté noir sur blanc — ajouter plus tard un niveau plus fin (une zone
fonctionnelle sous le bâtiment) **n'est pas additif** avec un ancrage spécifique : il
faut reprendre le cœur, formules et jointures comprises. Le poser générique pendant que
le modèle est jeune coûte moins cher.

*Le problème a été constaté sur un modèle déjà en service, puis le document repris à vide
sur le schéma générique — ce qui donne la mesure du coût évité. Attesté sur une seule
source.*

## États `propose` / `valide`, dérivés de l'origine de la valeur

Le statut ne se choisit pas, il se **dérive de la source** : une valeur produite
automatiquement ou semi-automatiquement naît `propose` et demande une confirmation
humaine avant de compter ; une valeur saisie sur le terrain naît `valide`, parce que
l'agent sur place *est* le contrôle humain.

**Alternative écartée** : écrire la valeur finale dès qu'une source la produit, sans
distinction de statut.
**Pourquoi** : on ne distingue plus une donnée vérifiée d'une proposition automatique,
et l'erreur se propage silencieusement dans les calculs et les exports.

*Posé comme règle non négociable là où il a été implémenté. **Confirmé indépendamment sur
un second projet**, qui porte le même principe sous un autre nom — un niveau de source et
une modération pour arbitrer doublons et conflits. L'un des deux seuls patterns de cette
page à atteindre ce degré de preuve.*

## Registre de résolution multi-documents — sans `doc_id` en dur

Une table `Reg_Index` associe une ressource logique à un triplet — source, table Grist,
mode d'accès — et le service l'interroge **avant** de router un appel.

**Alternative écartée** : coder les identifiants des documents partenaires en dur — un
document par session, une clé par session.
**Pourquoi** : cela ne passe pas à l'échelle dès qu'il faut résoudre vers le document
d'un tiers dont l'identité n'est connue qu'au moment de la requête. Chaque nouveau
partenaire obligerait à modifier le code.

Le détail qui compte : l'index porte aussi un **niveau d'accès**, lu par le service avant
de servir. Changer la visibilité d'une ressource devient une modification de *donnée*,
pas de code. Un bug réel a été trouvé en chemin — l'index portait ce niveau et rien ne
l'appliquait encore.

*Structuré et durci en août 2026, audit de sécurité compris. Attesté sur une seule
source.*

## Moteur d'import — quatre contrôles, trois stratégies de fusion

Une configuration persistante par organisme, et un moteur qui vérifie à chaque
exécution : couverture des attributs obligatoires, **dérive de schéma** entre deux
exécutions, traduction des valeurs vers la nomenclature cible, journalisation.
L'écriture se fait sur une clé de fusion, avec trois stratégies au choix — tout
remplacer, remplacer si renseigné, compléter les vides.

**Alternative écartée** : s'appuyer sur l'import natif de Grist, dont le mapping et la
transformation existent dans l'interface.
**Pourquoi** : cette configuration n'est **ni persistée ni exposée en API REST**. Elle
sert un import manuel ponctuel, jamais un import récurrent ou programmé.

Le contrôle de dérive de schéma n'était demandé par personne : il s'est révélé nécessaire
en protégeant le rejeu automatique contre lui-même. Éprouvé — rejeu identique accepté,
rejeu après renommage d'une colonne source refusé.

*Attesté sur une seule source.*
