{{- define "lchai.image" -}}
{{ .Values.image.registry }}/{{ .Values.image.prefix }}{{ .Values.image.separator | default "/" }}{{ .svc }}:{{ .Values.image.tag }}
{{- end -}}

{{/* Imagen de un servicio: usa image.tags.<servicio> si existe, si no image.tag */}}
{{- define "lchai.svcImage" -}}
{{- $tags := .root.Values.image.tags | default dict -}}
{{- $tag := index $tags .svc | default .root.Values.image.tag -}}
{{ .root.Values.image.registry }}/{{ .root.Values.image.prefix }}{{ .root.Values.image.separator | default "/" }}{{ .svc }}:{{ $tag }}
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

{{/*
Pod: sin token de Kubernetes montado, en todos los entornos (SonarQube S6865). Ningún servicio de lchai usa la
API de Kubernetes y el chart no usa roles IAM por cuenta de servicio (IRSA), así que en AWS no cambia nada.
*/}}
{{- define "lchai.podHardening" -}}
automountServiceAccountToken: false
{{- end -}}

{{/*
Dependencias de terceros (imágenes parcheadas en deps/): pod con fsGroup y seccomp, y contenedor sin root, sin
escalada, sin capabilities y con el sistema de archivos raíz de solo lectura (las rutas escribibles van en emptyDir).
Uso: include "lchai.depPodContext" (dict "gid" 70) / include "lchai.depContext" (dict "uid" 70 "gid" 70)
*/}}
{{- define "lchai.depPodContext" -}}
securityContext:
  {{- if .gid }}
  fsGroup: {{ .gid }}
  fsGroupChangePolicy: OnRootMismatch
  {{- end }}
  seccompProfile:
    type: RuntimeDefault
{{- end -}}

{{- define "lchai.depContext" -}}
securityContext:
  runAsNonRoot: true
  runAsUser: {{ .uid }}
  runAsGroup: {{ .gid }}
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities:
    drop: ["ALL"]
  seccompProfile:
    type: RuntimeDefault
{{- end -}}

{{/*
Discos Lustre (CSI): fsGroup no se aplica y el volumen llega con dueño root. initContainer mínimo que abre
los permisos del punto de montaje (solo con storage.fixPermissions=true). Uso: dict "image" .. "path" .. "volume" ..
*/}}
{{- define "lchai.fixPermsInit" -}}
- name: fix-permissions
  image: {{ .image }}
  command: ["sh", "-c", "chmod a+rwx {{ .path }}"]
  securityContext:
    runAsNonRoot: false
    runAsUser: 0
    allowPrivilegeEscalation: false
    readOnlyRootFilesystem: true
    capabilities:
      drop: ["ALL"]
    seccompProfile:
      type: RuntimeDefault
  resources:
    requests: { cpu: 10m, memory: 16Mi }
    limits: { cpu: 100m, memory: 64Mi }
  volumeMounts:
    - name: {{ .volume }}
      mountPath: {{ .path }}
{{- end -}}

{{/* Contenedores de las imágenes propias de lchai (corren sin root desde sec-20261029) */}}
{{- define "lchai.containerHardening" -}}
{{- if .Values.podSecurity.hardened }}
securityContext:
  runAsNonRoot: true
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true   # las rutas escribibles (/tmp, caché de nginx) van en emptyDir
  capabilities:
    drop: ["ALL"]
  seccompProfile:
    type: RuntimeDefault
{{- end }}
{{- end -}}

{{/* /tmp escribible (HOME de los servicios Python) cuando el contenedor corre con raíz de solo lectura */}}
{{- define "lchai.tmpMount" -}}
{{- if .Values.podSecurity.hardened }}
- name: tmp
  mountPath: /tmp
{{- end }}
{{- end -}}

{{- define "lchai.tmpVolume" -}}
{{- if .Values.podSecurity.hardened }}
- name: tmp
  emptyDir:
    sizeLimit: 1Gi
{{- end }}
{{- end -}}

{{/* imagePullSecrets adicionales (p.ej. harbor-pull-secret). Los de la ServiceAccount se suman. */}}
{{- define "lchai.imagePullSecrets" -}}
{{- with .Values.image.pullSecrets -}}
imagePullSecrets:
{{- range . }}
- name: {{ . }}
{{- end }}
{{- end }}
{{- end -}}

{{/*
Nombre de un workload (Deployment/StatefulSet/Job): <workloadPrefix>-<nombre>-<workloadSuffix>,
p. ej. txt-solca-lchai-postgres-pre. Prefijo y sufijo son opcionales; sin ninguno queda el nombre simple. Los Service no cambian (los servicios se siguen llamando api-gateway, postgres...).
Uso: include "lchai.wl" (dict "root" $ "name" "api-gateway")
*/}}
{{- define "lchai.wl" -}}
{{- $parts := list -}}
{{- with .root.Values.workloadPrefix }}{{ $parts = append $parts . }}{{ end -}}
{{- $parts = append $parts .name -}}
{{- with .root.Values.workloadSuffix }}{{ $parts = append $parts . }}{{ end -}}
{{- join "-" $parts -}}
{{- end -}}

{{/* Keycloak externo (keycloak.external.enabled): no se despliega Keycloak en el namespace; devuelve "true" o vacío */}}
{{- define "lchai.kcExternal" -}}
{{- if and .Values.keycloak.external .Values.keycloak.external.enabled -}}true{{- end -}}
{{- end -}}

{{/* URL base de Keycloak que usan los servicios y el webapp: la externa (sin "/" final) o el Service interno */}}
{{- define "lchai.kcBaseUrl" -}}
{{- if include "lchai.kcExternal" . -}}
{{ required "keycloak.external.url es obligatorio con keycloak.external.enabled" .Values.keycloak.external.url | trimSuffix "/" }}
{{- else -}}
http://keycloak:8080
{{- end -}}
{{- end -}}