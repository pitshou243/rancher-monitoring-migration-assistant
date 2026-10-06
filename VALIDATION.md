# Validation status and customer pilot gates

## Completed locally

- Python fixture tests and Bash syntax validation.
- Mocked Kubernetes/Helm collection behavior, including forbidden API reads.
- Protected export and checksum round-trip checks.
- Candidate generation and baseline comparison checks.

No live cluster migration has been performed. No source or target chart is QA-certified by this package.

## Required integration matrix

| Scenario | Expected evidence |
|---|---|
| Rancher 2.15.x + RKE2/K3s | Confirm version inventory, context consistency, collector compatibility and actual metrics collection. |
| Community / Application Collection runtime | Validate generated keys, rendered templates, CRD hook behavior, image pull credentials and proxy service mappings. |
| Independent / Helm-owned custom resources | Confirm backup completeness and uninstall behavior; restore custom monitors and rules without duplicating built-ins. |
| Spring Boot monitoring | Expected targets up, representative metrics present and authentication/TLS working. |
| Alertmanager customization | Preserved routes, receivers, templates, inhibition and controlled notification delivery. |
| Prometheus / Grafana persistence | Recover historical data, dashboards and credentials using the selected infrastructure. |
| CRD upgrade and old-release cleanup | Validate CR survival, schema compatibility, hook behavior and rollback limitations. |
| Restricted permissions / network outage | Incomplete collection reported; preparation blocked; no false readiness result. |
| Project Monitoring / HPA | Validate federation, project access and custom metric consumers. |

## Known MVP limits

- All monitoring CRs are inventoried cluster-wide; multiple instances need interpretation.
- Helm values/manifests are omitted unless credential export is explicitly enabled.
- Supporting ConfigMaps beyond dashboard artifacts need manual export.
- ServiceMonitor selection checks cannot prove endpoint health; optional runtime APIs improve evidence.
- Runtime rule comparison uses group/name/query/labels; target comparison uses target labels. Release label/name changes require review.
- No API-server server-side dry-run, Helm render, chart schema validation, Grafana API validation, notification send, sample-value comparison, cloning or data restoration.
- Secrets referenced outside supported fields or inline Helm values require review.
- No Helm uninstall, install, scale, patch, apply, CRD cleanup or other cluster mutation is executed.

## Release criteria

Dev/QA review the pilot results, specify exact versions, approve the documented procedure and support boundary, and document recovery/rollback evidence before recommending the utility for production migrations.
