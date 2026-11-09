{{- define "customer-profile-backend.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "customer-profile-backend.commonLabels" -}}
helm.sh/chart: {{ include "customer-profile-backend.chart" . }}
app.kubernetes.io/name: customer-profile-backend
app.kubernetes.io/instance: {{ .Release.Name | quote }}
app.kubernetes.io/part-of: customer-profile
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "customer-profile-backend.logsPvcName" -}}
{{- if .Values.persistence.logs.existingClaim -}}
{{- .Values.persistence.logs.existingClaim -}}
{{- else -}}
{{- .Values.names.logsPvc -}}
{{- end -}}
{{- end }}

{{- define "customer-profile-backend.taskUploadsPvcName" -}}
{{- if .Values.persistence.taskUploads.existingClaim -}}
{{- .Values.persistence.taskUploads.existingClaim -}}
{{- else -}}
{{- .Values.names.taskUploadsPvc -}}
{{- end -}}
{{- end }}

{{- define "customer-profile-backend.image" -}}
{{- printf "%s:%s" .Values.image.repository .Values.image.tag -}}
{{- end }}
