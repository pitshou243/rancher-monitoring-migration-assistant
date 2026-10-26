# Rancher Monitoring Migration Assistant

Version 0.1.0 - reviewable MVP for monitoring changes in Rancher 2.15.x.

## What does this tool do?

The Monitoring Migration Assistant helps you understand your existing Rancher monitoring
setup, save its configuration, prepare a starting configuration for a replacement stack,
and check what changed after you migrate.

It is intended for administrators planning a transition from the existing
`rancher-monitoring` deployment to a separately installed monitoring stack and
`rancher-monitoring-dashboards`, in the context of Rancher 2.15.x monitoring changes.
The replacement runtime can use the SUSE Application Collection `prometheus-operator`
chart or the community `kube-prometheus-stack` chart. You choose the exact target version.

**The tool assists with the migration; you perform the actual migration yourself.**
It reads the cluster and writes local reports/configuration files. It does not install,
uninstall, upgrade, restore, or modify anything in the cluster. It is a reviewable MVP,
not a SUSE-certified migration mechanism.

## How it fits into a migration

Run the tool from an administration workstation that has access to the affected cluster.
Use the same cluster for the before-and-after comparison.

| Stage | What you run | What the tool does | What you do with the result |
|---|---|---|---|
| Before migration | `assess` | Inventories monitoring releases, monitors, rules, dashboards, workloads and storage metadata. Reports collection failures, selection warnings and other risks. | Investigate findings and identify configuration/data that needs protection. |
| Before migration | `export` | Saves a baseline of the current setup, reviewable resource manifests and dashboard ConfigMaps. With `--include-secrets`, also saves discovered/requested Secrets and Helm values/manifests. | Protect the export and review whether it contains all required custom configuration. Back up persistent data separately. |
| Planning | `prepare` | Reads the exported baseline offline and generates candidate Helm values plus a plan recording your chosen chart and version. | Complete and review the values against the exact target chart; rehearse the migration. |
| Migration | Manual work outside this tool | No migration commands are executed. | Follow the reviewed [runbook](RUNBOOK.md) to transition the runtime, restore configuration/data as needed, and configure dashboards and control-plane collection. |
| After migration | `validate` | Compares the live cluster with the baseline. Reports missing/changed resources and previously selected monitors/rules that are no longer selected. Optional checks compare Prometheus runtime evidence and exported Secrets. | Investigate differences and test application metrics, dashboards and alert delivery. |

### Example: protecting application monitoring

Suppose your current stack scrapes Spring Boot applications through ServiceMonitors,
uses custom PrometheusRules, and sends notifications through a customized Alertmanager.

1. Run `assess` to inspect the current monitoring setup and selection warnings.
2. Run `export` to capture monitors, rules and dashboard ConfigMaps. Add
   `--include-secrets` to capture discovered credential/configuration Secrets and Helm
   values/manifests; use `--secret namespace/name` for additional known Secrets.
3. Run `prepare` to generate starting values for the chosen replacement chart.
   These values are not a complete translation of your old Helm configuration.
4. Perform the reviewed migration manually, including persistence, credentials,
   Alertmanager configuration, discovery selectors and dashboard integration.
5. Run `validate` against the same baseline. A missing ServiceMonitor or rule is a
   blocker, including when it was Helm-owned. A previously selected resource that no
   current Prometheus selects is also a blocker when selection can be determined.
6. Confirm that representative Spring Boot metrics are present and send a controlled
   test notification. The tool does not perform these application/notification tests.

For stronger before-and-after evidence, provide `--prometheus-url` to both `export` and
`validate`. To compare captured Secret contents, also use `validate --include-secrets`.
Neither option proves that the entire migration succeeded.

## What remains manual?

- Choosing and verifying supported source/target versions and rendering the target chart.
- Installing/uninstalling Helm releases, upgrading/cleaning up CRDs, and restoring resources.
- Completing target Helm values, service proxy mappings, authentication and control-plane collection.
- Backing up and restoring Prometheus history, the Grafana database, Alertmanager state and volume data.
- Checking uncaptured dependencies, dashboard behavior, alert delivery, HPA and Project Monitoring.

**An exit code of `0` means no blockers or collection errors were detected by the checks
performed. Warnings and unknowns can remain; it is not a migration approval.**

The implementation is a Bash launcher with a Python standard-library backend.

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
| `validate` | Reads the live cluster; compares all captured CR/dashboard specs and labels, including Helm-owned resources, checks current selection and optional runtime evidence. Rejects a different cluster UID. |

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

### Validation safeguards

Validation requires matching, nonempty cluster UIDs. Helm ownership uses both release
name and namespace. Missing Helm-owned resources are blockers requiring manual review
of replacement coverage; they are still excluded from automatic restoration exports.
Previously selected monitors/rules that no current Prometheus selects are blockers.
Selection is evaluated across instances; this does not prove per-instance routing parity.
Unknown namespace selection remains UNKNOWN.

To compare all exported Secret payloads (including Alertmanager configuration and captured
dependencies), explicitly opt in on validation:

```bash
./monitoring-migration-assistant.sh validate --context downstream-prod \
  --baseline ./monitoring-private-backup --include-secrets
```

A changed payload/type blocks validation; an unreadable/missing Secret fails collection.
Secret values are never printed. Without this option, Secret comparison is UNKNOWN.
This compares captured Secrets only; notification delivery and uncaptured dependencies
still require manual checks. Built-in resource renames can produce blockers: review their
replacement coverage rather than treating this tool as automatic migration approval.
