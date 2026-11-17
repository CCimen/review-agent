{{/*
Hermes and the review workers mount the same volume. With ReadWriteOnce they
must share a node, also when only one of them is replaced: each pod requires a
node that already runs a pod with the volume label. The first pod matches its
own rule, which Kubernetes allows when no other pod matches.
*/}}
{{- define "review-agent.sharedVolumeAffinity" -}}
{{- if eq .Values.persistence.accessMode "ReadWriteOnce" }}
affinity:
  podAffinity:
    requiredDuringSchedulingIgnoredDuringExecution:
    - labelSelector:
        matchLabels:
          review-agent/volume: hermes-data
      topologyKey: kubernetes.io/hostname
{{- end }}
{{- end }}
