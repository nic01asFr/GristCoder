{{/* Helpers du chart grist-coder-onyxia */}}

{{- define "grist-coder.fullname" -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "grist-coder.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "grist-coder.labels" -}}
helm.sh/chart: {{ include "grist-coder.chart" . }}
{{ include "grist-coder.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: onyxia
{{- end -}}

{{- define "grist-coder.selectorLabels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
  APP_AUTH_TOKEN — garde Bearer du pod, stable entre upgrades :
    1. Si valeur explicite fournie -> on l'utilise.
    2. Sinon, si Secret existe deja -> on reutilise (jamais regenere).
    3. Sinon -> on genere aleatoire 48 char.
  L'auteur du chart n'a jamais connaissance de ce token : il nait dans le
  namespace du user et n'en sort pas.
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
