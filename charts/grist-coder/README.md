# grist-coder — chart Onyxia SSPCloud (self-hosted par-utilisateur)

Déploie **ton** serveur GristCoder MCP dans **ton** namespace SSPCloud. Chaque
utilisateur héberge son propre pod et garde ses propres secrets : **l'auteur du
chart n'est dépositaire d'aucun secret**. C'est le pendant du modèle
`qgis-sspcloud` / `n8n-onyxia`.

## Principe

| Aspect | Mécanisme |
|---|---|
| Déploiement | catalogue Onyxia (`values.schema.json`) **ou** `install.sh` one-liner |
| URL par-user | `user-<idep>-grist-coder.user.lab.sspcloud.fr` (ingress TLS auto) |
| Secrets | naissent dans le namespace du user (`Secret` `helm.sh/resource-policy: keep`) ; jamais commités |
| Garde d'accès | identité vérifiée auprès de Grist **+** propriétaire désigné (`security.ownerUid`) **+** filtre `APP_AUTH_TOKEN` |
| Clé LLM | **catalogue Onyxia** → auto depuis l'AI Assistant du datalab (RBAC + Secret injecté) ; **`helm install`** → renseigner `llm.apiKey` (déterministe) |

## Clé LLM — deux chemins

La récupération auto depuis le datalab suppose **deux conditions** réunies :
1. un **Secret `*-secretassistant`** dans le namespace — injecté par Onyxia quand le
   service est lancé **depuis le catalogue** avec l'option AI Assistant ;
2. un **RBAC** autorisant le pod à lire ce Secret (`rbac.create: true` → RoleBinding
   vers la ClusterRole `edit`, inclus dans ce chart).

Sur un simple **`helm install` depuis un terminal**, le Secret n'existe pas (Onyxia
ne l'injecte que via le catalogue) → renseigner **`llm.apiKey`** (voie déterministe,
recommandée hors catalogue). Le serveur tombe alors sur cette clé et affiche un
bandeau non bloquant si aucune clé n'est disponible.

## Sécurité — un tiers avec l'URL n'accède à rien

L'URL n'est pas un secret (ingress public). La protection est double :

- **L'identité vient de Grist, jamais du client.** Un widget s'enregistre avec son
  jeton d'accès ; le pod le présente à Grist, sur un site en liste blanche
  (`grist.siteUrl`), et n'en lit l'identifiant qu'après une réponse 200.
- **Propriétaire désigné** : renseigne `security.ownerUid` (onglet **Sécurité** du
  formulaire) avec l'identifiant numérique de ton compte Grist (en ligne de commande :
  `--set-string security.ownerUid=<id>`, ou `OWNER_UID=<id>` pour `install.sh`). À
  défaut, le premier compte *vérifié* après chaque démarrage devient propriétaire —
  un collaborateur qui ouvrirait le widget le premier après un redémarrage
  prendrait le pod.
- **`APP_AUTH_TOKEN` est un filtre, pas un secret.** Il voyage dans l'URL du widget,
  stockée dans les options de section de chaque document qui l'intègre : tout
  collaborateur peut le lire. Rien ne repose sur lui seul — la clé LLM du pod n'est
  prêtée qu'à une session de widget vérifiée.
- Un client MCP présente soit une clé API Grist, soit un **access token OAuth** émis
  par le pod (voir ci-dessous).

**Connecteur OAuth 2.1 embarqué** (actif en ligne, dès que `PUBLIC_URL` est injecté) :
le pod est son propre Authorization Server. Le « login » du consentement valide
ta **clé API Grist** (même identité `uid:{id}` + owner-lock) et le pod émet un jeton
opaque révocable. La clé Grist ne repart jamais vers le client (pas de token
passthrough) ; le jeton remplace le couple `Bearer` + `X-App-Token`. Le Dynamic
Client Registration (RFC 7591) est activé → le connecteur s'enregistre seul, sans
`client_id` manuel.

On n'utilise **pas** l'OIDC Onyxia (le widget vit dans une iframe cross-origin Grist
où le cookie SSPCloud ne circule pas de façon fiable) : l'AS OAuth du pod est
autonome et adossé à la clé Grist, pas au SSO SSPCloud.

## Déploiement

### Où sont quoi (organisation du projet)

| Ressource | Emplacement | Rôle |
|-----------|-------------|------|
| **Code & doc publique** | [GitHub — GristCoder](https://github.com/nic01asFr/GristCoder) | Miroir lisible, Pages, script `install.sh` |
| **Image conteneur** | `ghcr.io/nic01asfr/grist-coder` | Ce que Kubernetes exécute (tags `latest` + version app, ex. `5.14`) |
| **Chart Helm** | [GitHub Pages — helm/](https://nic01asfr.github.io/GristCoder/helm) | Ajouté par `install.sh` (repli interne si indisponible) |
| **CI** | Pipeline privée (miroir → GitHub) | Build image → ghcr.io, package chart |

### Option A — terminal Onyxia (recommandé grand public)

1. **Profil IA** — datalab : *Mon compte → Assistant IA* (clé, base `https://llm.lab.sspcloud.fr/api`, modèle outillé).
2. **Pod avec Kubernetes** — lance VS Code, Jupyter ou équivalent avec **`kubernetes.role: edit`** (accès admin au namespace ; sans ça : `LLM_API_KEY=…`).
3. **Terminal du pod** :

```bash
curl -sL https://raw.githubusercontent.com/nic01asFr/GristCoder/master/charts/grist-coder/scripts/install.sh | bash
```

Optionnel : `OWNER_UID=<id Grist>` · `GRIST_API_KEY=…` · `GRIST_SITE_URL=…`

Le script ajoute le registre Helm GitHub Pages et déploie l'image **ghcr.io** (tag = `Chart.AppVersion`).

**Mettre à jour** après une nouvelle image :

```bash
curl -sL https://raw.githubusercontent.com/nic01asFr/GristCoder/master/charts/grist-coder/scripts/install.sh | bash
# ou
kubectl rollout restart deployment/grist-coder -n "$KUBERNETES_NAMESPACE"
```

### Option B — catalogue Onyxia

Si *GristCoder MCP* est déjà dans ton catalogue : lance-le avec le rôle **`edit`**
et renseigne *Identifiant Grist du propriétaire* (onglet Sécurité). Sinon, ajoute
une source de service Helm pointant sur le dépôt GitHub Pages ci-dessus (usage avancé).

## Après déploiement

Dans **Mes services** → **Ouvrir** (ou `helm get notes grist-coder -n <ns>`), les
notes Helm affichent deux blocs **prêts à copier** :

1. **URL widget Grist** (`?app_token=…`) — page personnalisée, accès complet au document.
2. **Fragment `.mcp.json`** — URL `/mcp`, `X-Grist-Site`, `X-App-Token` et placeholder
   `VOTRE_CLE_API_GRIST` (à remplacer une seule fois).

**Claude Desktop / claude.ai** : connecteur personnalisé sur `https://<host>/mcp` +
consentement OAuth (clé Grist une fois, sans `.mcp.json`).

Repli CLI si les notes ne s'affichent pas :

```bash
kubectl get secret grist-coder -o jsonpath='{.data.APP_AUTH_TOKEN}' | base64 -d ; echo
```

## Distribution (CI)

La CI (`.gitlab-ci.yml`) publie à chaque push sur `master` les tags **`latest`**
et **`<appVersion>`** (version de la docstring `grist_coder.py`, identique à
`server.json` et au tag OCI attendu par Antigravity / le registre MCP). Un tag
Git `v*` ajoute aussi ce numéro s'il diffère de l'appVersion :

1. **Image** → registre public (ghcr.io), via kaniko. Registre configuré par variables CI/CD.
2. **Chart** → registre Helm (canal `stable`) et miroir GitHub Pages, après réécriture de
   `image.repository` avec `$IMAGE_REPO` (le chart pointe toujours sur l'image
   réellement poussée).

### Variables CI/CD requises (Settings → CI/CD → Variables)

| Variable | Exemple | Note |
|---|---|---|
| `IMAGE_REGISTRY` | `ghcr.io` | hôte du registre |
| `IMAGE_REPO` | `ghcr.io/nic01asfr/grist-coder` | chemin complet |
| `REGISTRY_USER` | `nic01asfr` | identifiant de push |
| `REGISTRY_TOKEN` | *(masked)* | token de push (PAT GitHub `write:packages`) |

ghcr.io est recommandé : pull anonyme, déjà validé sur SSPCloud (qgis/n8n).

## Câblage serveur attendu (grist_coder.py)

Le chart injecte ces variables ; le serveur doit les honorer (étape de câblage
suivante) : `APP_AUTH_TOKEN` (garde), `OWNER_LOCK`, `GRIST_SITE_URL`,
`GRIST_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`,
`LLM_AUTO_FROM_DATALAB`, `LLM_PROXY_ALLOWED_HOSTS`, `SSPCLOUD_NAMESPACE`,
`ONYXIA_USER`.
