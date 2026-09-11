#!/usr/bin/env bash
#
# install.sh — Deploie GristCoder MCP dans le namespace SSPCloud de l'utilisateur.
#
# Usage (depuis un terminal de service SSPCloud lance avec kubernetes.role: edit) :
#   curl -sL https://gitlab.cerema.fr/mcp/gristcoder_mcp/-/raw/master/charts/grist-coder/scripts/install.sh | bash
#
# Variables d'env optionnelles :
#   NAMESPACE        : namespace cible. Sinon $KUBERNETES_NAMESPACE ou contexte courant.
#   VERSION          : version du chart (sinon latest).
#   HELM_REPO_URL    : surcharger l'URL du Helm repo (debug).
#   LLM_API_KEY      : cle LLM explicite (sinon recuperee du datalab si role edit).
#   GRIST_API_KEY    : cle API Grist (optionnelle, pour clients MCP / webhooks).
#   GRIST_SITE_URL   : instance Grist (defaut https://grist.numerique.gouv.fr).
#   OWNER_UID        : identifiant numerique de ton compte Grist (recommande : sans
#                      lui, le premier compte verifie apres chaque demarrage devient
#                      proprietaire du pod).
#
set -euo pipefail

if [[ -t 1 ]]; then
  BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; CYAN=$'\033[36m'; RESET=$'\033[0m'
else
  BOLD=""; GREEN=""; YELLOW=""; RED=""; CYAN=""; RESET=""
fi
log()  { echo "${CYAN}[--]${RESET} $*"; }
ok()   { echo "${GREEN}[OK]${RESET} $*"; }
warn() { echo "${YELLOW}[!!]${RESET} $*"; }
die()  { echo "${RED}[KO]${RESET} $*" >&2; exit 1; }

# --- 1. Prerequis --------------------------------------------------------------
log "Verification des prerequis..."
command -v kubectl >/dev/null || die "kubectl introuvable. Lance ce script dans un service SSPCloud."
command -v helm    >/dev/null || die "helm introuvable. Lance ce script dans un service SSPCloud."
kubectl auth can-i get pods >/dev/null 2>&1 || die "Pas d'acces au cluster K8s (kubeconfig SSPCloud ?)."

# --- 2. Namespace --------------------------------------------------------------
NS="${NAMESPACE:-${KUBERNETES_NAMESPACE:-}}"
[[ -z "$NS" ]] && NS=$(kubectl config view --minify -o jsonpath='{..namespace}' 2>/dev/null || echo "")
[[ -z "$NS" ]] && die "Impossible de determiner le namespace. Definir NAMESPACE=user-XXX."
[[ "$NS" != user-* ]] && warn "Namespace '$NS' ne commence pas par 'user-'."
IDEP="${NS#user-}"
ok "Namespace cible : ${BOLD}$NS${RESET} (idep: $IDEP)"

# Verifie le role edit (necessaire a la recuperation auto de la cle LLM du datalab).
if ! kubectl -n "$NS" auth can-i get secrets >/dev/null 2>&1; then
  warn "Le service n'a pas le role 'edit' (lecture des Secrets). La recuperation"
  warn "auto de la cle LLM depuis le datalab echouera : fournir LLM_API_KEY."
fi

# Repertoires Helm inscriptibles : sur les pods SSPCloud, ~/.config n'est pas
# inscriptible ("mkdir /home/onyxia/.config/helm: permission denied"). On force
# les dossiers cache/config/data de Helm vers un emplacement sur (writable).
_HELM_BASE="${TMPDIR:-/tmp}/gristcoder-helm"
export HELM_CACHE_HOME="${HELM_CACHE_HOME:-$_HELM_BASE/cache}"
export HELM_CONFIG_HOME="${HELM_CONFIG_HOME:-$_HELM_BASE/config}"
export HELM_DATA_HOME="${HELM_DATA_HOME:-$_HELM_BASE/data}"
mkdir -p "$HELM_CACHE_HOME" "$HELM_CONFIG_HOME" "$HELM_DATA_HOME"

# --- 3. Helm repo (registre Helm GitLab CEREMA, pull anonyme) -------------------
HELM_REPO_URL="${HELM_REPO_URL:-https://gitlab.cerema.fr/api/v4/projects/3099/packages/helm/stable}"
log "Ajout du Helm repo : $HELM_REPO_URL"
helm repo add gristcoder "$HELM_REPO_URL" --force-update >/dev/null
helm repo update gristcoder >/dev/null
ok "Helm repo OK"

# --- 4. Install ----------------------------------------------------------------
HOST="user-${IDEP}-grist-coder.user.lab.sspcloud.fr"
RELEASE="grist-coder"
log "Deploiement de GristCoder sur https://$HOST ..."
ARGS=(
  --namespace "$NS"
  # Hors catalogue, le contexte Onyxia ne remplit rien : l'hote, la classe
  # d'ingress et l'emetteur de certificats sont donc fixes ici, a l'identique de
  # ce que le chart codait en dur avant d'adopter le library-chart.
  --set "ingress.hostname=$HOST"
  --set "ingress.ingressClassName=nginx"
  --set "ingress.useCertManager=true"
  --set "ingress.certManagerClusterIssuer=letsencrypt"
  --set "onyxia_user=$IDEP"
  --set "grist.siteUrl=${GRIST_SITE_URL:-https://grist.numerique.gouv.fr}"
)
[[ -n "${LLM_API_KEY:-}"   ]] && ARGS+=(--set-string "llm.apiKey=$LLM_API_KEY" --set "llm.autoFromDatalab=false")
[[ -n "${GRIST_API_KEY:-}" ]] && ARGS+=(--set-string "grist.apiKey=$GRIST_API_KEY")
[[ -n "${OWNER_UID:-}"     ]] && ARGS+=(--set-string "security.ownerUid=$OWNER_UID")
[[ -n "${VERSION:-}"       ]] && ARGS+=(--version "$VERSION")

helm upgrade --install "$RELEASE" gristcoder/grist-coder "${ARGS[@]}" --wait --timeout 5m
ok "GristCoder deploye"

# --- 5. Recap ------------------------------------------------------------------
APP_TOKEN=$(kubectl -n "$NS" get secret grist-coder -o jsonpath='{.data.APP_AUTH_TOKEN}' 2>/dev/null | base64 -d || echo "")
cat <<EOF

${BOLD}Deploiement termine${RESET}

  Serveur   : ${CYAN}https://$HOST${RESET}
  Widget    : ${CYAN}https://$HOST/${RESET}   (a ajouter dans ton doc Grist)
  MCP        : ${CYAN}https://$HOST/mcp${RESET}
  App token  : ${YELLOW}$APP_TOKEN${RESET}

${BOLD}Connecteur OAuth (Claude Desktop / claude.ai, recommande) :${RESET}
  Ajoute un connecteur personnalise avec l'URL ${CYAN}https://$HOST/mcp${RESET}
  puis colle ta cle API Grist sur la page de consentement du pod.
  (Aucun header a configurer ; le connecteur ne recoit qu'un jeton revocable.)

${BOLD}Client MCP par headers (Claude Code) :${RESET}
  ${GREEN}claude mcp add grist-coder --transport http https://$HOST/mcp \\
    --header "Authorization: Bearer <ta_cle_api_grist>" \\
    --header "X-App-Token: $APP_TOKEN"${RESET}

Owner-lock actif : seul le 1er compte Grist enregistre pourra utiliser ce pod.
EOF
