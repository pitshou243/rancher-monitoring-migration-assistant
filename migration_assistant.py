#!/usr/bin/env python3
"""Read-only monitoring migration assistant. No third-party Python dependencies."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

VERSION = '0.1.0'
KINDS = ('servicemonitors', 'podmonitors', 'prometheusrules', 'alertmanagerconfigs',
         'prometheuses', 'alertmanagers', 'probes', 'scrapeconfigs', 'prometheusagents', 'thanosrulers')
CUSTOM = ('servicemonitors', 'podmonitors', 'prometheusrules', 'alertmanagerconfigs', 'probes', 'scrapeconfigs')
PAIRS = {'servicemonitors': ('serviceMonitorSelector', 'serviceMonitorNamespaceSelector'),
         'podmonitors': ('podMonitorSelector', 'podMonitorNamespaceSelector'),
         'prometheusrules': ('ruleSelector', 'ruleNamespaceSelector'),
         'probes': ('probeSelector', 'probeNamespaceSelector'),
         'scrapeconfigs': ('scrapeConfigSelector', 'scrapeConfigNamespaceSelector')}

class Failure(Exception):
    pass

def dump(path, obj):
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + '\n')
    path.chmod(0o600)

def run(argv, timeout=60):
    try:
        p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Failure(f'{argv[0]} unavailable or timed out') from e
    if p.returncode:
        # Do not echo stderr: hooks/clients may include credentials.
        raise Failure(f'{argv[0]} {argv[1]} failed (exit {p.returncode}); check connectivity/RBAC separately')
    return p.stdout

def identity(obj):
    m = obj.get('metadata', {})
    return f"{obj.get('kind', '')}/{m.get('namespace', '')}/{m.get('name', '')}"

def owned(obj, releases):
    a = obj.get('metadata', {}).get('annotations', {})
    return a.get('meta.helm.sh/release-name') in releases

def clean(obj):
    obj = copy.deepcopy(obj)
    obj.pop('status', None)
    m = obj.get('metadata', {})
    for key in ('uid', 'resourceVersion', 'generation', 'creationTimestamp', 'managedFields', 'ownerReferences', 'finalizers'):
        m.pop(key, None)
    a = m.get('annotations', {})
    for key in ('kubectl.kubernetes.io/last-applied-configuration', 'meta.helm.sh/release-name', 'meta.helm.sh/release-namespace'):
        a.pop(key, None)
    m.get('labels', {}).pop('app.kubernetes.io/managed-by', None)
    return obj

def selector_matches(selector, labels):
    if selector is None:
        return False
    for k, v in selector.get('matchLabels', {}).items():
        if labels.get(k) != v:
            return False
    for e in selector.get('matchExpressions', []):
        k, op, vals = e['key'], e['operator'], e.get('values', [])
        if op == 'In' and labels.get(k) not in vals:
            return False
        if op == 'NotIn' and labels.get(k) in vals:
            return False
        if op == 'Exists' and k not in labels:
            return False
        if op == 'DoesNotExist' and k in labels:
            return False
        if op not in ('In', 'NotIn', 'Exists', 'DoesNotExist'):
            raise Failure(f'Unsupported selector operator: {op}')
    return True

def selected(prom, obj, kind, namespaces):
    rs, ns = PAIRS[kind]
    spec = prom.get('spec', {})
    if not selector_matches(spec.get(rs), obj.get('metadata', {}).get('labels', {})):
        return False
    objns = obj['metadata']['namespace']
    nsel = spec.get(ns)
    if nsel is None:
        return objns == prom['metadata']['namespace']
    if objns not in namespaces:
        return None
    return selector_matches(nsel, namespaces[objns])

def refs(obj):
    """Find explicit Secret selectors/references; never guess a Grafana credential name."""
    result = set()
    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k in ('secret', 'secretKeyRef', 'secretRef', 'secretKeySelector') and isinstance(v, dict) and v.get('name'):
                    result.add(v['name'])
                if k in ('imagePullSecrets', 'secrets') and isinstance(v, list):
                    result.update(z['name'] for z in v if isinstance(z, dict) and z.get('name'))
                if k in ('secretName', 'configSecret') and isinstance(v, str) and v:
                    result.add(v)
                if k in ('credentials', 'password', 'username', 'clientSecret', 'bearerTokenSecret', 'keySecret') and isinstance(v, dict) and v.get('name') and v.get('key'):
                    result.add(v['name'])
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(obj.get('spec', {}))
    if obj.get('kind') == 'Alertmanager' and not obj.get('spec', {}).get('configSecret'):
        result.add('alertmanager-' + obj['metadata']['name'])
    return result

class Collector:
    def __init__(self, args):
        self.args = args
        self.context = args.context or run(['kubectl', 'config', 'current-context']).strip()
        self.k = ['kubectl', '--context', self.context, '--request-timeout=30s']
        self.errors = []
        self.absent = []

    def get(self, resource, all_ns=False, ns=None):
        cmd = self.k + ['get', resource, '-o', 'json']
        if all_ns:
            cmd += ['-A']
        elif ns:
            cmd += ['-n', ns]
        try:
            data = json.loads(run(cmd))
            if not isinstance(data.get('items'), list):
                raise Failure('Unexpected API response')
            return data['items']
        except (Failure, ValueError) as e:
            self.errors.append({'collection': resource, 'error': str(e)})
            return None

    def collect(self):
        crds = self.get('customresourcedefinitions')
        names = {c['metadata']['name'] for c in crds or []}
        resources = {}
        for kind in KINDS:
            full = kind + '.monitoring.coreos.com'
            if crds is not None and full not in names:
                resources[kind] = []
                self.absent.append(kind)
            else:
                resources[kind] = self.get(full, all_ns=True)
        for key in ('namespaces', 'persistentvolumes'):
            resources[key] = self.get(key)
        for key in ('persistentvolumeclaims', 'services', 'pods', 'deployments', 'statefulsets'):
            resources[key] = self.get(key, ns=self.args.namespace)
        cms = self.get('configmaps', all_ns=True)
        resources['dashboards'] = [x for x in cms or [] if x.get('metadata', {}).get('labels', {}).get('grafana_dashboard')] if cms is not None else None
        releases = None
        try:
            releases = json.loads(run(['helm', '--kube-context', self.context, 'list', '-n', self.args.namespace, '-a', '-o', 'json']))
        except (Failure, ValueError) as e:
            self.errors.append({'collection': 'helm', 'error': str(e)})
        # Cluster UID prevents accidental comparison with another cluster.
        uid = next((x['metadata'].get('uid') for x in resources['namespaces'] or [] if x['metadata']['name'] == 'kube-system'), None)
        moncrds = [c for c in crds or [] if c['metadata']['name'].endswith('.monitoring.coreos.com')]
        return {'schema': 1, 'tool_version': VERSION, 'captured_at': datetime.now(timezone.utc).isoformat(),
                'context': self.context, 'cluster_uid': uid, 'namespace': self.args.namespace,
                'resources': resources, 'crds': moncrds, 'releases': releases,
                'absent_apis': self.absent, 'errors': self.errors}

def runtime_api(url, endpoint, args):
    if not url:
        return None
    if not url.startswith(('http://', 'https://')):
        raise Failure('Prometheus URL must use http or https')
    ctx = ssl.create_default_context(cafile=args.ca_file)
    headers = {}
    if args.token_file:
        if not url.startswith('https://'):
            raise Failure('Bearer token requires HTTPS')
        headers['Authorization'] = 'Bearer ' + Path(args.token_file).read_text().strip()
    try:
        request = urllib.request.Request(url.rstrip('/') + '/api/v1/' + endpoint, headers=headers)
        # Never follow redirects with credentials.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **kw):
                return None
        opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=ctx))
        with opener.open(request, timeout=30) as r:
            data = json.load(r)
        if data.get('status') != 'success':
            raise Failure('Prometheus API returned unsuccessful status')
        return data['data']
    except Exception as e:
        raise Failure(f'Prometheus {endpoint} query failed; verify endpoint/authentication/TLS') from e

def collect(args):
    for binary in ('kubectl', 'helm'):
        if not shutil.which(binary):
            raise Failure(f'Missing dependency: {binary}')
    c = Collector(args)
    snapshot = c.collect()
    snapshot['runtime'] = {}
    for endpoint in ('targets', 'rules'):
        try:
            snapshot['runtime'][endpoint] = runtime_api(args.prometheus_url, endpoint, args)
        except Failure as e:
            snapshot['errors'].append({'collection': 'runtime/' + endpoint, 'error': str(e)})
    return snapshot, c

def report(snapshot):
    findings = []
    def add(level, msg):
        findings.append((level, msg))
    for err in snapshot['errors']:
        add('UNKNOWN', f"Collection failed: {err['collection']}")
    r = snapshot['resources']
    releases = snapshot['releases'] or []
    legacy = [x['name'] for x in releases if x.get('chart', '').startswith('rancher-monitoring-') and not re.match(r'rancher-monitoring-(crd|dashboard)', x.get('chart', ''))]
    for x in releases:
        if 'monitoring' in x.get('chart', '') or 'prometheus' in x.get('chart', ''):
            add('DETAIL', f"Release {x['name']}: {x['chart']} ({x.get('status', 'unknown')})")
    if not r.get('prometheuses'):
        add('BLOCKER', 'No Prometheus CR discovered; monitoring readiness is unverified')
    chart_owned = [identity(o) for kind in CUSTOM for o in r.get(kind) or [] if owned(o, legacy)]
    if chart_owned:
        add('WARN', f'{len(chart_owned)} monitor/rule/config resources belong to legacy Helm releases; uninstall may remove them')
    nslabels = {o['metadata']['name']: o['metadata'].get('labels', {}) for o in r.get('namespaces') or []}
    for p in r.get('prometheuses') or []:
        for kind in PAIRS:
            objects = r.get(kind)
            if objects is None:
                continue
            excluded = [identity(o) for o in objects if selected(p, o, kind, nslabels) is False]
            unknown = [o for o in objects if selected(p, o, kind, nslabels) is None]
            if unknown:
                add('UNKNOWN', f"{identity(p)} namespace selection unknown for {len(unknown)} {kind}")
            if excluded:
                add('WARN', f"{identity(p)} excludes {len(excluded)} {kind}; inspect namespace/label selectors")
    if snapshot['crds']:
        add('WARN', 'Preserve monitoring CRDs during runtime uninstall; CRD deletion removes associated CRs')
    bound = {x['spec'].get('volumeName') for x in r.get('persistentvolumeclaims') or []}
    for pv in r.get('persistentvolumes') or []:
        if pv['metadata']['name'] in bound and pv['spec'].get('persistentVolumeReclaimPolicy') != 'Retain':
            add('WARN', f"PV {pv['metadata']['name']} does not use Retain; review storage backup/recovery")
    for pvc in r.get('persistentvolumeclaims') or []:
        if pvc.get('status', {}).get('phase') != 'Bound':
            add('BLOCKER', f'{identity(pvc)} is not Bound')
    images = sorted({c.get('image', '') for pod in r.get('pods') or [] for c in pod.get('spec', {}).get('containers', [])})
    for image in images:
        add('DETAIL', f'Container image: {image}')
    for pod in r.get('pods') or []:
        if pod.get('status', {}).get('phase') in ('Succeeded',):
            continue
        if not any(x.get('type') == 'Ready' and x.get('status') == 'True' for x in pod.get('status', {}).get('conditions', [])):
            add('WARN', f'{identity(pod)} is not Ready')
    targets = snapshot.get('runtime', {}).get('targets')
    if targets is None:
        add('UNKNOWN', 'Prometheus targets/rule loading not fully verified; provide --prometheus-url for runtime checks')
    else:
        bad = sum(x.get('health') != 'up' for x in targets.get('activeTargets', []))
        if bad:
            add('WARN', f'{bad} active scrape targets are not up')
    rules = snapshot.get('runtime', {}).get('rules')
    if rules is not None:
        bad = sum(x.get('health') == 'err' or bool(x.get('lastError')) for g in rules.get('groups', []) for x in g.get('rules', []))
        if bad:
            add('BLOCKER', f'{bad} loaded rules report evaluation errors')
    add('UNKNOWN', 'Control-plane metrics, notification delivery, data recovery, HPA and Project Monitoring require explicit validation')
    return findings

def directory(path):
    p = Path(path).absolute()
    p.mkdir(mode=0o700, parents=True, exist_ok=False)
    p.chmod(0o700)
    return p

def checksums(path):
    sums = {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(path.rglob('*')) if p.is_file() and p.name != 'checksums.json'}
    dump(path / 'checksums.json', sums)

def load_baseline(path):
    root = Path(path)
    try:
        sums = json.loads((root / 'checksums.json').read_text())
        if not sums or 'baseline.json' not in sums:
            raise Failure('Baseline checksum manifest is incomplete')
        for name, expected in sums.items():
            p = root / name
            if not p.resolve().is_relative_to(root.resolve()):
                raise Failure('Unsafe baseline path')
            if hashlib.sha256(p.read_bytes()).hexdigest() != expected:
                raise Failure(f'Baseline checksum mismatch: {name}')
        data = json.loads((root / 'baseline.json').read_text())
        if data.get('schema') != 1:
            raise Failure('Unsupported baseline schema')
        return data
    except (OSError, ValueError) as e:
        raise Failure('Baseline is missing, unreadable, or invalid') from e

def export(args, snap, collector):
    out = directory(args.output)
    dump(out / 'baseline.json', snap)
    releases = [x['name'] for x in snap['releases'] or []]
    restore, review = [], []
    for kind in CUSTOM + ('dashboards',):
        for obj in snap['resources'].get(kind) or []:
            (review if owned(obj, releases) else restore).append(clean(obj))
    dump(out / 'independent-resources.json', {'apiVersion': 'v1', 'kind': 'List', 'items': restore})
    dump(out / 'helm-owned-review.json', {'apiVersion': 'v1', 'kind': 'List', 'items': review})
    referenced = {(o['metadata']['namespace'], name) for kind in KINDS + ('pods', 'deployments', 'statefulsets') for o in snap['resources'].get(kind) or [] for name in refs(o)}
    referenced.update(tuple(x.split('/', 1)) for x in args.secret)
    dump(out / 'secret-references.json', [{'namespace': ns, 'name': name} for ns, name in sorted(referenced)])
    if args.include_secrets:
        secrets = []
        for ns, name in sorted(referenced):
            try:
                obj = json.loads(run(collector.k + ['get', 'secret', name, '-n', ns, '-o', 'json']))
                secrets.append(clean(obj))
            except (Failure, ValueError) as e:
                snap['errors'].append({'collection': f'secret/{ns}/{name}', 'error': str(e)})
        dump(out / 'secrets.json', {'apiVersion': 'v1', 'kind': 'List', 'items': secrets})
        for release in snap['releases'] or []:
            name = release['name']
            for action in ('values', 'manifest'):
                try:
                    cmd = ['helm', '--kube-context', collector.context, 'get', action, name, '-n', args.namespace]
                    if action == 'values':
                        cmd += ['-a', '-o', 'json']
                    (out / f'helm-{name}-{action}.txt').write_text(run(cmd))
                except Failure as e:
                    snap['errors'].append({'collection': f'helm/{name}/{action}', 'error': str(e)})
    # Refs are best effort; values/manifests may contain inline credentials.
    dump(out / 'baseline.json', snap)
    (out / 'BACKUP-NOTES.txt').write_text('Configuration only; no TSDB, Grafana database, silences or volume contents backed up.\nThis entire directory is sensitive, even without --include-secrets.\nSecret discovery is best effort; check Grafana and additional scrape/template/image pull Secrets manually.\nHelm values/manifests require --include-secrets because they may contain credentials.\nReview helm-owned-review.json before restoring; do not duplicate target-chart resources.\n')
    for p in out.iterdir():
        if p.is_file():
            p.chmod(0o600)
    checksums(out)
    print(f'Export: {out}')

def prepare(args):
    baseline = load_baseline(args.baseline)
    if baseline['errors']:
        raise Failure('Incomplete baseline; resolve collection errors before preparation')
    if baseline['namespace'] != args.namespace:
        raise Failure('Baseline namespace differs from --namespace')
    out = directory(args.output)
    # JSON is valid YAML and preserves exact string values without a YAML dependency.
    values = {'prometheus': {'prometheusSpec': {k: False for k in
              ('serviceMonitorSelectorNilUsesHelmValues', 'podMonitorSelectorNilUsesHelmValues',
               'ruleSelectorNilUsesHelmValues', 'probeSelectorNilUsesHelmValues', 'scrapeConfigSelectorNilUsesHelmValues')}},
              'kubeEtcd': {'enabled': False}, 'kubeControllerManager': {'enabled': False},
              'kubeScheduler': {'enabled': False}, 'kubeProxy': {'enabled': False}}
    warnings = ['Candidate values only. No chart version is QA-certified by this tool.',
                'Review discovery scope: disabling Helm-label defaults can include unrelated CRs.',
                'Set explicit resource/namespace selectors; preserve necessary original discovery scope.',
                'Complete persistence, image pull, credentials, control-plane collection and Grafana embedding settings.',
                'No storage cloning, credentials transfer or automatic historical-data restoration is configured.']
    if baseline['crds']:
        values['crds'] = {'upgradeJob': {'enabled': True}}
        warnings.append('Dev-proposed preserved-CRD path: --skip-crds plus upgradeJob. Confirm exact target chart support and rendered hook behavior before using.')
    ams = [o for o in baseline['resources'].get('alertmanagers') or [] if o['metadata']['namespace'] == args.namespace]
    if len(ams) == 1:
        name = ams[0].get('spec', {}).get('configSecret') or 'alertmanager-' + ams[0]['metadata']['name']
        values['alertmanager'] = {'alertmanagerSpec': {'useExistingSecret': True, 'configSecret': name}}
        warnings.append(f'Ensure Alertmanager Secret {name} exists in target namespace and export referenced receiver/template Secrets.')
    else:
        warnings.append('Select Alertmanager configuration Secret manually; zero or multiple source instances discovered.')
    if args.grafana_secret:
        values['grafana'] = {'admin': {'existingSecret': args.grafana_secret}}
        warnings.append('Verify Grafana username/password key names; admin credentials do not restore its database.')
    dump(out / 'candidate-values.yaml', values)
    chart = 'prometheus-community/kube-prometheus-stack' if args.target == 'community' else 'oci://dp.apps.rancher.io/charts/prometheus-operator'
    dump(out / 'plan.json', {'target': args.target, 'chart': chart, 'version': args.chart_version,
                            'namespace': args.namespace, 'release': args.target_release,
                            'source_context': baseline['context'], 'skip_crds': bool(baseline['crds']),
                            'warnings': warnings})
    (out / 'REVIEW.txt').write_text('\n'.join(warnings) + '\n\nUse helm show values and helm template for the exact target version.\nMatch dashboard proxies to actual rendered services; service names are not guessed.\nSee RUNBOOK.md for the manual migration sequence.\n')
    checksums(out)
    print(f'Candidate plan: {out}; review required')

def compare(baseline, current):
    findings = []
    if not baseline.get('cluster_uid') or not current.get('cluster_uid'):
        findings.append(('UNKNOWN', 'Cluster identity unavailable'))
    elif baseline['cluster_uid'] != current['cluster_uid']:
        raise Failure('Baseline belongs to a different cluster')
    source_releases = [x['name'] for x in baseline.get('releases') or []]
    for kind in CUSTOM + ('dashboards',):
        before = baseline['resources'].get(kind)
        after = current['resources'].get(kind)
        if before is None or after is None:
            findings.append(('UNKNOWN', f'Cannot compare {kind}: collection unavailable'))
            continue
        lookup = {identity(o): o for o in after}
        for obj in before:
            # Target charts regenerate built-ins; compare independent resources only.
            if owned(obj, source_releases):
                continue
            key = identity(obj)
            if key not in lookup:
                findings.append(('BLOCKER', f'Missing independent resource: {key}'))
            else:
                field = 'data' if kind == 'dashboards' else 'spec'
                if obj.get(field) != lookup[key].get(field):
                    findings.append(('WARN', f'Configuration changed: {key}'))
                if obj.get('metadata', {}).get('labels', {}) != lookup[key].get('metadata', {}).get('labels', {}):
                    findings.append(('WARN', f'Labels changed: {key}; review selection'))
    for endpoint in ('targets', 'rules'):
        old = baseline.get('runtime', {}).get(endpoint)
        new = current.get('runtime', {}).get(endpoint)
        if old is None or new is None:
            findings.append(('UNKNOWN', f'No complete runtime baseline/comparison for {endpoint}'))
            continue
        if endpoint == 'rules':
            keyset = lambda d: {(g.get('name'), x.get('name'), x.get('query'), json.dumps(x.get('labels', {}), sort_keys=True)) for g in d.get('groups', []) for x in g.get('rules', [])}
        else:
            keyset = lambda d: {json.dumps(x.get('labels', {}), sort_keys=True) for x in d.get('activeTargets', [])}
        missing = keyset(old) - keyset(new)
        if missing:
            findings.append(('WARN', f'{len(missing)} baseline {endpoint} identities absent or changed; inspect release-label/name changes'))
    return findings

def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=('assess', 'export', 'prepare', 'validate'))
    p.add_argument('--context', help='Explicit kubectl/Helm context; otherwise capture current context once')
    p.add_argument('--namespace', default='cattle-monitoring-system')
    p.add_argument('--output')
    p.add_argument('--baseline')
    p.add_argument('--include-secrets', action='store_true', help='Export referenced Secrets and potentially sensitive Helm values/manifests')
    p.add_argument('--secret', action='append', default=[], metavar='NAMESPACE/NAME', help='Additional Secret to export; repeatable')
    p.add_argument('--target', choices=('community', 'application-collection'), default='community')
    p.add_argument('--chart-version')
    p.add_argument('--target-release', default='kube-prometheus-stack')
    p.add_argument('--grafana-secret')
    p.add_argument('--prometheus-url', help='Reachable Prometheus base URL for optional read-only targets/rules APIs')
    p.add_argument('--ca-file')
    p.add_argument('--token-file', help='Bearer token file; HTTPS required')
    p.add_argument('--verbose', action='store_true')
    p.add_argument('--version', action='version', version=VERSION)
    return p

def main(argv=None):
    args = parser().parse_args(argv)
    os.umask(0o077)
    if args.mode in ('export', 'prepare') and not args.output:
        raise Failure('--output is required')
    if args.mode in ('prepare', 'validate') and not args.baseline:
        raise Failure('--baseline is required')
    if args.mode == 'prepare' and (not args.chart_version or args.target_release == 'rancher-monitoring'):
        raise Failure('Specify --chart-version and a target release other than rancher-monitoring')
    for ref in args.secret:
        if not re.fullmatch(r'[a-z0-9][a-z0-9.-]*/[a-z0-9][a-z0-9.-]*', ref):
            raise Failure('--secret must be NAMESPACE/NAME')
    if args.mode == 'prepare':
        prepare(args)
        return 0
    baseline = load_baseline(args.baseline) if args.mode == 'validate' else None
    snap, collector = collect(args)
    findings = report(snap)
    if baseline:
        if baseline['namespace'] != args.namespace:
            raise Failure('Baseline namespace differs from --namespace')
        if baseline['errors']:
            findings.append(('UNKNOWN', 'Baseline collection was incomplete'))
        findings += compare(baseline, snap)
    if args.mode == 'export':
        export(args, snap, collector)
        findings = report(snap)
    for level, msg in findings:
        if level != 'DETAIL' or args.verbose:
            print(f'{level}: {msg}')
    counts = {k: sum(l == k for l, _ in findings) for k in ('BLOCKER', 'WARN', 'UNKNOWN')}
    print('Summary: ' + ', '.join(f'{v} {k.lower()}' for k, v in counts.items()))
    # Collection errors can be added during export.
    return 2 if counts['BLOCKER'] else 3 if snap['errors'] or (baseline and baseline['errors']) else 0

if __name__ == '__main__':
    try:
        sys.exit(main())
    except (Failure, FileExistsError, OSError) as e:
        print(f'ERROR: {e}', file=sys.stderr)
        sys.exit(3)
