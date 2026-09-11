{{/* Helpers du chart grist-coder */}}

{{/*
  Noms et libelles : ceux du library-chart, pour que l'ingress, la route et les
  politiques reseau qu'il genere designent bien nos objets. Sa logique de nommage
  est identique a l'ancienne : Service, Deployment et Secret gardent leurs noms.
*/}}
{{- define "grist-coder.fullname" -}}
{{- include "library-chart.fullname" . -}}
{{- end -}}

{{- define "grist-coder.labels" -}}
{{ include "library-chart.labels" . }}
app.kubernetes.io/part-of: onyxia
{{- end -}}

{{- define "grist-coder.selectorLabels" -}}
{{- include "library-chart.selectorLabels" . -}}
{{- end -}}

{{/*
  Hote public : la route quand elle est active (OpenShift), l'ingress sinon.
  C'est lui que le serveur annonce comme PUBLIC_URL (metadonnees OAuth, URL du
  widget) : il doit etre celui par lequel on l'atteint vraiment.
*/}}
{{- define "grist-coder.hostname" -}}
{{- if .Values.route.enabled -}}
{{- include "library-chart.route.hostname" . -}}
{{- else -}}
{{- include "library-chart.ingress.hostname" . -}}
{{- end -}}
{{- end -}}

{{/*
  Configuration LLM : premiere valeur non vide entre le profil historique
  (llm.apiKey / baseUrl / model, rempli depuis user.profile.aiAssistant.*), le
  format recent (llm.provider.*, rempli depuis ai.activeProvider.*) et le defaut.
*/}}
{{- define "grist-coder.llmKey" -}}
{{- .Values.llm.apiKey | default .Values.llm.provider.apiKey | default "" -}}
{{- end -}}
{{- define "grist-coder.llmBase" -}}
{{- .Values.llm.baseUrl | default .Values.llm.provider.apiBase | default "https://llm.lab.sspcloud.fr/api" -}}
{{- end -}}
{{- define "grist-coder.llmModel" -}}
{{- .Values.llm.model | default .Values.llm.provider.selectedModel | default "qwen3-6-35b-moe" -}}
{{- end -}}

{{/*
  APP_AUTH_TOKEN — garde du pod, un FILTRE et non un secret (elle voyage dans l'URL
  du widget), stable entre upgrades :
    1. valeur fournie (Onyxia : {{service.oneTimePassword}}) -> utilisee telle quelle ;
    2. sinon, Secret existant -> reutilise, jamais regenere ;
    3. sinon -> tiree au sort (48 caracteres).
  Seul le cas 1 permet aux notes d'afficher l'URL complete du widget : dans les cas
  2 et 3, les notes et le Secret seraient rendus separement, et un tirage au sort
  donnerait deux valeurs differentes.
*/}}
{{- define "grist-coder.appAuthToken" -}}
{{- if .Values.appAuth.token -}}
{{- .Values.appAuth.token -}}
{{- else -}}
{{- $existing := (lookup "v1" "Secret" .Release.Namespace (include "grist-coder.fullname" .)) -}}
{{- if and $existing $existing.data (index $existing.data "APP_AUTH_TOKEN") -}}
{{- index $existing.data "APP_AUTH_TOKEN" | b64dec -}}
{{- else -}}
{{- randAlphaNum 48 -}}
{{- end -}}
{{- end -}}
{{- end -}}
