{{- define "lchai.image" -}}
{{ .Values.image.registry }}/{{ .Values.image.prefix }}/{{ .svc }}:{{ .Values.image.tag }}
{{- end -}}

{{/* Imagen de un servicio: usa image.tags.<servicio> si existe, si no image.tag */}}
{{- define "lchai.svcImage" -}}
{{- $tags := .root.Values.image.tags | default dict -}}
{{- $tag := index $tags .svc | default .root.Values.image.tag -}}
{{ .root.Values.image.registry }}/{{ .root.Values.image.prefix }}/{{ .svc }}:{{ $tag }}
{{- end -}}

{{/* nodeSelector por grupo de nodos (general | gpu); vacío = sin nodeSelector */}}
{{- define "lchai.nodeSelector" -}}
{{- $sel := index (.root.Values.nodeSelector | default dict) .pool -}}
{{- if $sel }}
nodeSelector:
{{- toYaml $sel | nindent 2 }}
{{- end }}
{{- end -}}

{{- define "lchai.envFrom" -}}
- configMapRef:
    name: lchai-config
- secretRef:
    name: lchai-secrets
{{- end -}}

{{/* URL pública con esquema: publicUrl o https://<domain> */}}
{{- define "lchai.publicUrl" -}}
{{ .Values.publicUrl | default (printf "https://%s" .Values.domain) }}
{{- end -}}

{{/*
Endpoint S3 que usan los servicios y que aparece en las URL firmadas (lo abre el navegador).
s3.endpoint si está; con localDeps y localDeps.s3.host, se arma con el esquema de la URL pública; vacío = AWS S3.
*/}}
{{- define "lchai.s3Endpoint" -}}
{{- if .Values.s3.endpoint -}}
{{ .Values.s3.endpoint }}
{{- else if and .Values.localDeps.enabled .Values.localDeps.s3.host -}}
{{- $scheme := ternary "http" "https" (hasPrefix "http://" (include "lchai.publicUrl" .)) -}}
{{ $scheme }}://{{ .Values.localDeps.s3.host }}{{ with .Values.localDeps.s3.publicPort }}:{{ . }}{{ end }}
{{- end -}}
{{- end -}}

{{/*
Anotaciones de los Ingress según ingress.provider. Con "nginx" se agregan solas las que lchai necesita
(subidas de láminas sin límite, búfer para las cabeceras grandes de Keycloak y tiempos largos);
ingress.annotations se suma y puede sobrescribirlas.
*/}}
{{- define "lchai.ingressAnnotations" -}}
{{- $a := dict -}}
{{- if eq .Values.ingress.provider "nginx" -}}
{{- $_ := set $a "nginx.ingress.kubernetes.io/proxy-body-size" "0" -}}
{{- $_ := set $a "nginx.ingress.kubernetes.io/proxy-buffer-size" "16k" -}}
{{- $_ := set $a "nginx.ingress.kubernetes.io/proxy-buffers-number" "8" -}}
{{- $_ := set $a "nginx.ingress.kubernetes.io/proxy-read-timeout" "600" -}}
{{- $_ := set $a "nginx.ingress.kubernetes.io/proxy-send-timeout" "600" -}}
{{- if .Values.ingress.tls.enabled -}}
{{- $_ := set $a "nginx.ingress.kubernetes.io/ssl-redirect" "true" -}}
{{- end -}}
{{- end -}}
{{- with .Values.ingress.tls.clusterIssuer -}}
{{- $_ := set $a "cert-manager.io/cluster-issuer" . -}}
{{- end -}}
{{- $a = merge (deepCopy (.Values.ingress.annotations | default dict)) $a -}}
{{- if $a -}}
{{ toYaml $a }}
{{- end -}}
{{- end -}}

{{/*
URL pública "efectiva" para Keycloak (KC_HOSTNAME_URL) y la validación del emisor del token:
publicUrl si está; fuera de AWS (provider != alb) se deduce del dominio; en AWS queda vacía como antes.
*/}}
{{- define "lchai.effectivePublicUrl" -}}
{{- if .Values.publicUrl -}}
{{ .Values.publicUrl }}
{{- else if ne .Values.ingress.provider "alb" -}}
https://{{ .Values.domain }}
{{- end -}}
{{- end -}}

{{/*
Checksum de lchai-config y lchai-secrets. Va como anotación del pod: si cambia la configuración
(dominio, contraseñas...), helm upgrade reinicia los pods. Sin esto seguían con los valores viejos.
*/}}
{{- define "lchai.configChecksum" -}}
checksum/config: {{ printf "%s%s" (include (print .Template.BasePath "/configmap.yaml") .) (include (print .Template.BasePath "/secret.yaml") .) | sha256sum | quote }}
{{- end -}}
