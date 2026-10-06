# Rancher Monitoring Migration Assistant

Version 0.1.0 - reviewable MVP for monitoring changes in Rancher 2.15.x.

A Bash launcher and Python standard-library backend inventory monitoring, export configuration, prepare candidate values, and compare a replacement deployment with a baseline. Cluster operations are read-only. This is not a SUSE-certified migration mechanism.

## Requirements

- Bash, Python **3.9+**, `kubectl`, and Helm **3** on the administration workstation.
- A kubeconfig for the affected downstream cluster. Use `--context` explicitly.
- Read permissions for namespaces, monitoring CRDs/CRs across namespaces, dashboard ConfigMaps, PVs, and runtime workloads/services/PVCs in `cattle-monitoring-system`.
- Helm release metadata read access. Secret payload access is needed only when using `--include-secrets`.
- No Python packages or jq required. Suitable for administration hosts on SLES/openSUSE, RHEL/Rocky Linux and Ubuntu when these dependencies are installed. These OS combinations have not been integration-tested.

The tool does not change host services or SELinux configuration.

## Usage

```bash
chmod +x monitoring-migration-assistant.sh
./monitoring-migration-assistant.sh assess --context downstream-prod

./monitoring-migration-assistant.sh export \
  --context downstream-prod --output ./monitoring-baseline

# Explicitly include credentials and Helm values/manifests when needed.
./monitoring-migration-assistant.sh export \
  --context downstream-prod --output ./monitoring-private-backup \
  --include-secrets \
  --secret cattle-monitoring-system/my-grafana-admin-secret

./monitoring-migration-assistant.sh prepare \
  --baseline ./monitoring-private-backup \
  --output ./monitoring-plan \
  --target application-collection \
  --chart-version '<reviewed-target-version>' \
  --grafana-secret my-grafana-admin-secret

# Execute the reviewed migration outside this tool, then compare.
./monitoring-migration-assistant.sh validate \
  --context downstream-prod --baseline ./monitoring-private-backup
```

`--target community` selects the community chart in the generated plan. `--target-release` defaults to `kube-prometheus-stack`; `rancher-monitoring` is rejected. The namespace defaults to `cattle-monitoring-system`. The internal integration guide only supports that namespace; overriding it is for assessment, not a claim of supported UI integration elsewhere.

### Runtime evidence

Add `--prometheus-url` to both `export` and `validate` to capture and compare active scrape target labels and loaded rule identities/queries. The URL must be reachable from the workstation. You can establish a port-forward separately:

```bash
# Substitute the actual source or target Prometheus service.
kubectl --context downstream-prod -n cattle-monitoring-system \
  port-forward service/ACTUAL-PROMETHEUS-SERVICE 9090:9090

# Run in another terminal.
./monitoring-migration-assistant.sh export --context downstream-prod \
  --output ./baseline-with-runtime --prometheus-url http://127.0.0.1:9090
```

For a TLS/authenticated endpoint, use `--ca-file /path/ca.pem` and `--token-file /path/token`. TLS verification remains enabled. Bearer tokens require HTTPS and are not forwarded across redirects. Grafana and Alertmanager APIs are not queried.

## Modes

| Mode | Behavior |
|---|---|
| `assess` | Reads inventories; prints blockers, warnings, unknowns and a short summary. Use `--verbose` for discovered release versions and container images. |
| `export` | Creates a new protected directory, baseline, CR exports, dashboard artifacts, Secret-reference inventory and SHA-256 checksums. Does not overwrite existing directories. |
| `prepare` | Offline generation of candidate runtime values and a JSON plan with a pinned user-supplied chart version. Rejects incomplete baselines. Does not download, render or validate a chart. |
| `validate` | Reads the live cluster; compares independently managed CR/dashboard specs and labels, checks current selection and optional runtime evidence. Rejects a different cluster UID. |

## Export contents and credentials

All exports use directory mode `0700` and file mode `0600`. Treat the entire bundle as sensitive: inline credentials can exist in CR specs, dashboards and Prometheus runtime labels even when Kubernetes Secrets are not exported.

- `baseline.json`: captured inventory, context, cluster UID, collection status, optional runtime responses.
- `independent-resources.json`: cleaned Kubernetes List for review; does not include Helm-owned resources.
- `helm-owned-review.json`: cleaned resources annotated as belonging to a discovered Helm release. Review these manually; target charts may regenerate built-ins. This includes custom resources deployed through another Helm release.
- `secret-references.json`: best-effort references found in monitoring CRs and runtime workloads.
- `secrets.json`: only with `--include-secrets`, referenced and explicitly requested Secrets.
- `helm-*-values.txt`, `helm-*-manifest.txt`: only with `--include-secrets`, because Helm values/manifests may contain credentials. Captures all releases in the runtime namespace.
- `checksums.json`: local integrity checks, not a signature or guarantee of authenticity.

Secret discovery is not exhaustive. Review receiver templates, extra scrape jobs, image pull Secrets and custom mounts. Non-Secret ConfigMaps used for additional configuration require separate backup. Resource cleaning removes status, owner references, finalizers and Helm ownership metadata; review restoration intent before applying. Original metadata remains in the baseline.

No TSDB, Grafana database, Alertmanager silences, PVC data, Helm history or backup snapshots are exported. This is not a complete disaster-recovery backup. Protect and transfer actual data separately.

## Candidate values

`candidate-values.yaml` uses JSON syntax, which is valid YAML accepted by Helm. It contains:

- Relevant Prometheus `*SelectorNilUsesHelmValues: false` settings.
- Control-plane collection disabled pending an explicit distribution-specific collection design.
- Dev-proposed `crds.upgradeJob.enabled: true` when monitoring CRDs exist; the plan records `--skip-crds` intent.
- Existing Alertmanager config Secret when a single runtime-namespace instance is identified.
- Optional Grafana admin Secret reference supplied by the user.

It deliberately leaves resource/namespace selection policy, persistence, credentials transfer, image configuration, Grafana embedding/authentication and dashboard service proxies for review. Turning off Helm-label defaults can broaden discovery. Compare original Prometheus selectors and explicitly restrict the target scope.

The upstream CRD upgrade job is described as preview functionality. Its exact behavior/options and Application Collection equivalents must be checked against the chosen chart version. No target version is marked QA-validated in this MVP.

## Interpretation and exit codes

```text
WARN: 4 monitor/rule/config resources belong to legacy Helm releases; uninstall may remove them
WARN: Prometheus/cattle-monitoring-system/main excludes 2 servicemonitors; inspect namespace/label selectors
UNKNOWN: Prometheus targets/rule loading not fully verified; provide --prometheus-url for runtime checks
Summary: 0 blocker, 2 warn, 2 unknown
```

Example only; actual findings depend on the cluster.

| Exit | Meaning |
|---|---|
| `0` | Command completed with no blockers or collection errors. Warnings/unknowns can remain; this is not migration approval. |
| `2` | Blocker identified, such as missing independent resources, unbound PVCs or rule evaluation errors. |
| `3` | Invalid input, failed/incomplete collection, checksum/identity failure or a command error. |

`assess` counts selection across all discovered Prometheus instances. Resources intentionally excluded by a particular instance may be expected; review its namespace and role. A selected ServiceMonitor does not establish that its service exists or that its endpoint is healthy. Optional targets/rules APIs supply additional evidence, but they do not validate notification delivery, historical data or business-critical metrics.

## Development and validation

```bash
python3 -m unittest discover -s tests -v
bash -n monitoring-migration-assistant.sh
```

Fixture tests cover selectors, namespace discovery, RBAC failures, missing/changed CRs, Helm ownership, protected exports, integrity checks, incomplete preparation, context propagation, cluster mismatch and credential transport restrictions.

Live Rancher/RKE2/K3s migration QA and storage recovery tests have not been performed. See [RUNBOOK.md](RUNBOOK.md) and [VALIDATION.md](VALIDATION.md) before a customer pilot.

## Sources

- [Rancher monitoring documentation](https://documentation.suse.com/en-us/cloudnative/rancher-manager/v2.15/en/observability/monitoring-and-dashboards/monitoring-and-dashboards.html)
- [Upstream chart values](https://github.com/prometheus-community/helm-charts/blob/main/charts/kube-prometheus-stack/values.yaml)
- [Helm uninstall options](https://helm.sh/docs/helm/helm_uninstall/)

Design also reflects the supplied Dev/Product installation guide and migration discussion. Those are guidance, not evidence of a completed QA matrix.
