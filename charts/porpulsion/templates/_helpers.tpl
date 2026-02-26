{{/*
Expand the name of the chart.
*/}}
{{- define "porpulsion.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "porpulsion.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Ollama URL — localhost when Ollama runs as a sidecar in the same pod,
or the user-supplied external URL when ollama.enabled=false.
*/}}
{{- define "porpulsion.ollamaUrl" -}}
{{- if .Values.ollamaUrl }}
{{- .Values.ollamaUrl }}
{{- else if .Values.ollama.enabled }}
{{- printf "http://localhost:%d" (.Values.ollama.service.port | int) }}
{{- else }}
{{- fail "Either set ollamaUrl or enable the bundled Ollama sidecar (ollama.enabled=true)" }}
{{- end }}
{{- end }}

{{/*
Chart label.
*/}}
{{- define "porpulsion.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "porpulsion.labels" -}}
helm.sh/chart: {{ include "porpulsion.chart" . }}
{{ include "porpulsion.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels.
*/}}
{{- define "porpulsion.selectorLabels" -}}
app.kubernetes.io/name: {{ include "porpulsion.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Service account name.
*/}}
{{- define "porpulsion.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "porpulsion.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Image tag — defaults to appVersion.
*/}}
{{- define "porpulsion.imageTag" -}}
{{- default "latest" .Values.image.tag }}
{{- end }}