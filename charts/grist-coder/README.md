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
| Garde d'accès | `APP_AUTH_TOKEN` Bearer auto-généré **+** owner-lock (1er `uid` Grist = propriétaire) |
| Clé LLM | **auto** depuis la config AI Assistant du datalab (rôle `edit`) ; **sinon** champ à renseigner |

## Sécurité — un tiers avec l'URL n'accède à rien

L'URL n'est pas un secret (ingress public). La protection est double :

- **`/mcp`, `/register`, SSE, `/llm-proxy`** exigent le Bearer `APP_AUTH_TOKEN`
  (dans ton `Secret`, que toi seul lis).
- **Owner-lock** (`app.ownerLock: on`) : le premier compte Grist qui s'enregistre
  devient propriétaire ; tout autre `uid` est rejeté (403), même avec une clé
  Grist valide. Empêche un tiers d'utiliser ton pod (ta clé LLM, ton compute).

On n'utilise **pas** l'OIDC Onyxia : le widget vit dans une iframe cross-origin
(Grist), où le cookie SSPCloud ne circule pas de façon fiable.

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
3. **Client MCP** (`.mcp.json` / Claude Desktop) :
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

La CI (`.gitlab-ci.yml`) publie les deux livrables à chaque push sur `master`
(latest) et sur tag `v*` (version figée) :

1. **Image** → registre conteneur GitLab (`$CI_REGISTRY_IMAGE`), via kaniko.
2. **Chart** → registre Helm GitLab (canal `stable`), après réécriture de
   `image.repository` avec `$CI_REGISTRY_IMAGE` (le chart pointe toujours sur
   l'image réellement poussée).

À valider au 1er déploiement réel : **la joignabilité SSPCloud → registre
conteneur CEREMA** (pull de l'image depuis le cluster datalab). Si le registre
n'est pas atteignable/anonyme depuis SSPCloud, basculer l'image sur un registre
public (ex. ghcr) — le reste du chart est inchangé.

## Câblage serveur attendu (grist_coder.py)

Le chart injecte ces variables ; le serveur doit les honorer (étape de câblage
suivante) : `APP_AUTH_TOKEN` (garde), `OWNER_LOCK`, `GRIST_SITE_URL`,
`GRIST_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`,
`LLM_AUTO_FROM_DATALAB`, `LLM_PROXY_ALLOWED_HOSTS`, `SSPCLOUD_NAMESPACE`,
`ONYXIA_USER`.
