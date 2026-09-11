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
| Garde d'accès | identité vérifiée auprès de Grist **+** propriétaire désigné (`app.ownerUid`) **+** filtre `APP_AUTH_TOKEN` |
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
- **Propriétaire désigné** : renseigne `app.ownerUid` avec l'identifiant numérique de
  ton compte Grist (en ligne de commande : `--set-string app.ownerUid=<id>`). À
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

### Option A — one-liner (service SSPCloud lancé avec `kubernetes.role: edit`)

```bash
curl -sL https://gitlab.cerema.fr/mcp/gristcoder_mcp/-/raw/master/charts/grist-coder/scripts/install.sh | bash
```

Variables optionnelles : `LLM_API_KEY`, `GRIST_API_KEY`, `GRIST_SITE_URL`.

Le chart est tiré du **registre Helm GitLab CEREMA** (pull anonyme, projet public) :
`https://gitlab.cerema.fr/api/v4/projects/3099/packages/helm/stable`.

### Option B — catalogue Onyxia

Ajouter la source de service pointant sur le registre Helm ci-dessus, puis lancer
le service depuis le catalogue. Le formulaire (généré depuis `values.schema.json`)
pré-remplit l'hôte et l'idep. **Lancer avec le rôle `edit`** pour la récupération
auto de la clé LLM.

## Après déploiement

1. Récupérer le token du pod :
   ```bash
   kubectl get secret grist-coder -o jsonpath='{.data.APP_AUTH_TOKEN}' | base64 -d ; echo
   ```
2. **Widget Grist** : ajouter un widget personnalisé pointant sur `https://<host>/`,
   renseigner le token dans les options de section (`appToken`).
3. **Connecteur OAuth** (recommandé, Claude Desktop / claude.ai) : ajouter un
   connecteur personnalisé avec l'URL `https://<host>/mcp`, puis coller sa clé API
   Grist sur la page de consentement du pod. Aucun header à configurer.
4. **Client MCP par headers** (`.mcp.json` / Claude Code) :
   ```json
   {
     "mcpServers": {
       "grist-coder": {
         "type": "http",
         "url": "https://<host>/mcp",
         "headers": {
           "Authorization": "Bearer <clé_api_grist>",
           "X-App-Token": "<APP_AUTH_TOKEN>"
         }
       }
     }
   }
   ```

## Distribution (CI GitLab CEREMA)

La CI (`.gitlab-ci.yml`) publie à chaque push sur `master` (latest) et tag `v*` :

1. **Image** → registre externe public (le GitLab CEREMA n'a pas de registre
   conteneur), via kaniko. Registre configuré par variables CI/CD.
2. **Chart** → registre Helm GitLab (canal `stable`), après réécriture de
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
