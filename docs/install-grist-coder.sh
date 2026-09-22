#!/usr/bin/env bash
#
# install.sh — Deploie Grist Coder MCP dans ton namespace SSPCloud (modele QGIS-sspcloud).
#
# Usage :
#   curl -fsSL https://nic01asfr.github.io/GristCoder/install-grist-coder.sh | bash
#   curl -fsSL https://raw.githubusercontent.com/nic01asFr/GristCoder/master/install.sh | bash
#
# Pre-requis : terminal dans un service Onyxia (Jupyter, VS Code, …) lance avec
#   Kubernetes > acces depuis le service : oui
#   Kubernetes > role                         : admin (recommande ; edit seul peut bloquer helm)
#
set -euo pipefail

RELEASE="${RELEASE:-grist-coder}"
REPO_NAME="gristcoder"
HELM_REPO_PAGES="https://nic01asfr.github.io/GristCoder/helm"
HELM_REPO_RAW="https://raw.githubusercontent.com/nic01asFr/GristCoder/master/docs/helm"
HELM_REPO_GITLAB="https://gitlab.cerema.fr/api/v4/projects/3099/packages/helm/stable"
IMAGE_REPO_DEFAULT="ghcr.io/nic01asfr/grist-coder"

SA_NS_FILE="/var/run/secrets/kubernetes.io/serviceaccount/namespace"
NS="${NAMESPACE:-${KUBERNETES_NAMESPACE:-}}"
if [[ -z "$NS" && -f "$SA_NS_FILE" ]]; then
  NS=$(cat "$SA_NS_FILE")
fi
USERNAME="${ONYXIA_USER:-${NS#user-}}"
if [[ -z "$NS" || -z "$USERNAME" ]]; then
  echo "ERREUR : impossible de detecter le namespace SSPCloud."
  echo "Lance ce script depuis un terminal d'un service Onyxia."
  exit 1
fi

K8S_DOMAIN="${K8S_DOMAIN:-${ONYXIA_DOMAIN:-user.lab.sspcloud.fr}}"
HOST="${NS}-grist-coder.${K8S_DOMAIN}"

if [[ -t 1 ]]; then
  BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; CYAN=$'\033[36m'; RESET=$'\033[0m'
else
  BOLD=""; GREEN=""; YELLOW=""; RED=""; CYAN=""; RESET=""
fi
log()  { echo "${CYAN}[--]${RESET} $*"; }
ok()   { echo "${GREEN}[OK]${RESET} $*"; }
warn() { echo "${YELLOW}[!!]${RESET} $*"; }
die()  { echo "${RED}[KO]${RESET} $*" >&2; exit 1; }

log "Verification des prerequis..."
command -v kubectl >/dev/null || die "kubectl introuvable."
command -v helm    >/dev/null || die "helm introuvable."

if ! kubectl auth can-i get secrets -n "$NS" >/dev/null 2>&1 \
   || ! kubectl auth can-i create deployments -n "$NS" >/dev/null 2>&1; then
  echo ""
  die "Droits Kubernetes insuffisants dans $NS.

  Relance le pod terminal avec :
    Kubernetes > Enable access from within the service : oui
    Kubernetes > Kubernetes role                       : admin

  (Le role par defaut 'view' ne suffit pas ; 'edit' peut echouer sur helm list.)"
fi

# Profil IA (Vault), comme QGIS — les placeholders du chart ne sont resolus que via l'UI Onyxia.
LLM_API_KEY_VALUE="${LLM_API_KEY:-}"
LLM_BASE_URL_VALUE="${LLM_BASE_URL:-}"
LLM_MODEL_VALUE="${LLM_MODEL:-}"
if [[ -z "$LLM_API_KEY_VALUE" && -n "${VAULT_TOKEN:-}" && -n "${VAULT_ADDR:-}" ]]; then
  log "Lecture profil Assistant IA (Vault)..."
  _vault_mount="${VAULT_MOUNT:-onyxia-kv}"
  _profile_json=$(curl -s --max-time 15 \
    -H "X-Vault-Token: $VAULT_TOKEN" \
    "$VAULT_ADDR/v1/${_vault_mount}/data/${USERNAME}/.onyxia/userProfileStr" \
    2>/dev/null || echo "")
  if [[ -n "$_profile_json" ]]; then
    _parsed=$(printf '%s' "$_profile_json" | python3 -c '
import json, sys
try:
    outer = json.load(sys.stdin)
    raw = outer["data"]["data"]["value"]
    ai = json.loads(raw)["userProfileValues"]["aiAssistant"]
    print("\t".join([
        (ai.get("apiKey") or "").strip(),
        (ai.get("apiBase") or "").strip(),
        (ai.get("model") or "").strip(),
    ]))
except Exception:
    print("\t\t")
' 2>/dev/null || printf '\t\t')
    LLM_API_KEY_VALUE=$(printf '%s' "$_parsed" | cut -f1)
    LLM_BASE_URL_VALUE=$(printf '%s' "$_parsed" | cut -f2)
    LLM_MODEL_VALUE=$(printf '%s' "$_parsed" | cut -f3)
  fi
  [[ -n "$LLM_API_KEY_VALUE" ]] && ok "Cle IA trouvee dans le profil${LLM_MODEL_VALUE:+ ($LLM_MODEL_VALUE)}"
fi

# Garde du pod : value Helm (visible dans Mes services + NOTES), stable entre reinstalls.
SECRET_NAME="${RELEASE}"
APP_GUARD=$(kubectl get secret "$SECRET_NAME" -n "$NS" \
  -o jsonpath='{.data.APP_AUTH_TOKEN}' 2>/dev/null | base64 -d 2>/dev/null || echo "")
if [[ -z "$APP_GUARD" ]]; then
  APP_GUARD=$(head -c 48 /dev/urandom | base64 | tr -d '/+=' | cut -c1-48)
  log "Garde du pod : nouvelle valeur generee"
else
  ok "Garde du pod : valeur existante conservee"
fi

export HELM_CONFIG_HOME="${HELM_CONFIG_HOME:-/home/onyxia/work/.helm-config}"
export HELM_CACHE_HOME="${HELM_CACHE_HOME:-/home/onyxia/work/.helm-cache}"
export HELM_DATA_HOME="${HELM_DATA_HOME:-/home/onyxia/work/.helm-data}"
mkdir -p "$HELM_CONFIG_HOME" "$HELM_CACHE_HOME" "$HELM_DATA_HOME" 2>/dev/null || {
  export HELM_CONFIG_HOME="/tmp/gristcoder-helm/config"
  export HELM_CACHE_HOME="/tmp/gristcoder-helm/cache"
  export HELM_DATA_HOME="/tmp/gristcoder-helm/data"
  mkdir -p "$HELM_CONFIG_HOME" "$HELM_CACHE_HOME" "$HELM_DATA_HOME"
}

echo ""
echo "+==============================================================+"
echo "|  Installation Grist Coder MCP — $USERNAME"
echo "|  Namespace : $NS"
echo "|  URL       : https://$HOST"
echo "|  Image     : $IMAGE_REPO_DEFAULT"
echo "+==============================================================+"
echo ""

HELM_REPO_URL="${HELM_REPO_URL:-$HELM_REPO_PAGES}"
log "[1/4] Depot Helm : $HELM_REPO_URL"
if ! helm repo add "$REPO_NAME" "$HELM_REPO_URL" --force-update >/dev/null 2>&1; then
  warn "Pages indisponible — repli raw GitHub."
  HELM_REPO_URL="$HELM_REPO_RAW"
  if ! helm repo add "$REPO_NAME" "$HELM_REPO_URL" --force-update >/dev/null 2>&1; then
    warn "Raw GitHub indisponible — repli GitLab CI."
    HELM_REPO_URL="$HELM_REPO_GITLAB"
    helm repo add "$REPO_NAME" "$HELM_REPO_URL" --force-update >/dev/null
  fi
fi
helm repo update "$REPO_NAME" >/dev/null
ok "Helm repo OK ($HELM_REPO_URL)"

log "[2/4] Deploiement Helm..."
ARGS=(
  --namespace "$NS"
  --set "ingress.hostname=$HOST"
  --set "ingress.ingressClassName=onyxia"
  --set "onyxia_user=$USERNAME"
  --set-string "appAuth.token=$APP_GUARD"
  --set "grist.siteUrl=${GRIST_SITE_URL:-https://grist.numerique.gouv.fr}"
)
[[ -n "$LLM_API_KEY_VALUE" ]] && ARGS+=(--set-string "llm.apiKey=$LLM_API_KEY_VALUE" --set "llm.autoFromDatalab=false")
[[ -n "$LLM_BASE_URL_VALUE"  ]] && ARGS+=(--set-string "llm.baseUrl=$LLM_BASE_URL_VALUE")
[[ -n "$LLM_MODEL_VALUE"     ]] && ARGS+=(--set-string "llm.model=$LLM_MODEL_VALUE")
[[ -n "${GRIST_API_KEY:-}"   ]] && ARGS+=(--set-string "grist.apiKey=$GRIST_API_KEY")
[[ -n "${OWNER_UID:-}"       ]] && ARGS+=(--set-string "security.ownerUid=$OWNER_UID")
[[ -n "${VERSION:-}"         ]] && ARGS+=(--version "$VERSION")

helm upgrade --install "$RELEASE" "$REPO_NAME/grist-coder" "${ARGS[@]}" --wait --timeout 10m
ok "Release $RELEASE a jour"

log "[3/4] Enregistrement Mes services (Onyxia)..."
ONYXIA_SECRET="sh.onyxia.release.v1.${RELEASE}"
if [[ "$NS" == user-* ]]; then
  OWNER_ID="$USERNAME"; SHARE="false"
else
  OWNER_ID="${ONYXIA_IDEP:-$USERNAME}"; SHARE="false"
fi
if kubectl create secret generic "$ONYXIA_SECRET" -n "$NS" --type=onyxia.sh/release.v1 \
    --from-literal=owner="$OWNER_ID" \
    --from-literal=friendlyName="Grist Coder MCP" \
    --from-literal=catalog="GristCoder" \
    --from-literal=share="$SHARE" \
    --dry-run=client -o yaml 2>/dev/null | kubectl apply -f - >/dev/null 2>&1; then
  ok "Visible dans datalab > Mes services"
else
  warn "Secret Onyxia non enregistre (le pod reste accessible par son URL)"
fi

log "[4/4] Notes d'installation"
echo ""
helm get notes "$RELEASE" -n "$NS" 2>/dev/null | sed '1d' || true
echo ""
BASE="https://$HOST"
echo "+==============================================================+"
echo "|  Acces rapides"
echo "+==============================================================+"
echo "|  Widget Grist (URL complete) :"
echo "|    ${BASE}/?app_token=${APP_GUARD}"
echo "|"
echo "|  Config MCP (JSON, garde incluse) :"
echo "|    curl -sH \"X-App-Token: ${APP_GUARD}\" ${BASE}/setup.json"
echo "|"
echo "|  Ou : Mes services > Ouvrir (memes blocs copier-coller)"
echo "+==============================================================+"
echo ""
echo "Desinstaller : helm uninstall $RELEASE -n $NS"
echo "              kubectl delete secret $ONYXIA_SECRET -n $NS"
