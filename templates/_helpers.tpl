{{/*
Selector labels. Deliberately plain, and deliberately not the operator's
`agent.datadoghq.com/component: agent` + `app.kubernetes.io/part-of` pair —
the existing datadog-agent Service selects on that and fronts every node agent.
*/}}
{{- define "otel-trace-gateway.selectorLabels" -}}
app: {{ .Release.Name }}
{{- end }}

{{- define "otel-trace-gateway.labels" -}}
{{ include "otel-trace-gateway.selectorLabels" . }}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}
