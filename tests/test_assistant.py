import importlib.util
import json
import os
from pathlib import Path
import tempfile
import contextlib
import io
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / 'migration_assistant.py'
spec = importlib.util.spec_from_file_location('assistant', MODULE)
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)

def obj(kind, name, spec=None, labels=None, ns='apps'):
    return {'apiVersion': 'monitoring.coreos.com/v1', 'kind': kind,
            'metadata': {'name': name, 'namespace': ns, 'labels': labels or {}, 'annotations': {}}, 'spec': spec or {}}

def baseline():
    r = {k: [] for k in a.KINDS}
    r.update({k: [] for k in ('dashboards', 'namespaces', 'persistentvolumes', 'persistentvolumeclaims', 'services', 'pods')})
    r['servicemonitors'] = [obj('ServiceMonitor', 'spring', {'endpoints': [{'port': 'http'}]})]
    r['prometheuses'] = [obj('Prometheus', 'main', {'serviceMonitorSelector': {}, 'serviceMonitorNamespaceSelector': {}}, ns='cattle-monitoring-system')]
    return {'schema': 1, 'context': 'test', 'cluster_uid': 'cluster-1', 'namespace': 'cattle-monitoring-system',
            'resources': r, 'releases': [], 'crds': [{'metadata': {'name': 'servicemonitors.monitoring.coreos.com'}}],
            'errors': [], 'runtime': {'targets': None, 'rules': None}}

class Tests(unittest.TestCase):
    def test_selector_operators(self):
        self.assertFalse(a.selector_matches(None, {}))
        self.assertTrue(a.selector_matches({}, {}))
        self.assertFalse(a.selector_matches({'matchLabels': {'release': 'new'}}, {'release': 'old'}))
        self.assertTrue(a.selector_matches({'matchExpressions': [{'key': 'x', 'operator': 'NotIn', 'values': ['a']}]}, {}))
        self.assertFalse(a.selector_matches({'matchExpressions': [{'key': 'x', 'operator': 'DoesNotExist'}]}, {'x': 'v'}))

    def test_namespace_scope_and_unknown(self):
        p = obj('Prometheus', 'main', {'serviceMonitorSelector': {}}, ns='monitoring')
        o = obj('ServiceMonitor', 'spring')
        self.assertFalse(a.selected(p, o, 'servicemonitors', {}))
        p['spec']['serviceMonitorNamespaceSelector'] = {}
        self.assertIsNone(a.selected(p, o, 'servicemonitors', {}))
        self.assertTrue(a.selected(p, o, 'servicemonitors', {'apps': {}}))

    def test_missing_resource_and_changed_spec(self):
        b = baseline(); c = baseline()
        c['resources']['servicemonitors'] = []
        self.assertTrue(any(level == 'BLOCKER' for level, _ in a.compare(b, c)))
        c = baseline(); c['resources']['servicemonitors'][0]['spec']['endpoints'][0]['port'] = 'metrics'
        self.assertTrue(any('Configuration changed' in msg for _, msg in a.compare(b, c)))

    def test_chart_owned_not_automatic_restore(self):
        b = baseline(); c = baseline()
        b['releases'] = [{'name': 'legacy'}]
        b['resources']['servicemonitors'][0]['metadata']['annotations']['meta.helm.sh/release-name'] = 'legacy'
        c['resources']['servicemonitors'] = []
        self.assertEqual(a.compare(b, c)[0][0], 'UNKNOWN')
        self.assertFalse(any('Missing independent' in msg for _, msg in a.compare(b, c)))

    def test_wrong_cluster_blocked(self):
        b = baseline(); c = baseline(); c['cluster_uid'] = 'other'
        with self.assertRaises(a.Failure): a.compare(b, c)

    def test_secret_refs_and_cleanup(self):
        o = obj('Alertmanager', 'main', {'configSecret': 'custom', 'foo': {'credentials': {'name': 'token', 'key': 'value'}}})
        self.assertEqual(a.refs(o), {'custom', 'token'})
        o['metadata'].update({'uid': 'u', 'ownerReferences': [{'uid': 'v'}], 'finalizers': ['x']})
        self.assertNotIn('uid', a.clean(o)['metadata'])
        self.assertNotIn('ownerReferences', a.clean(o)['metadata'])
        self.assertIn('uid', o['metadata'])

    def test_export_default_never_reads_secrets_or_values(self):
        with tempfile.TemporaryDirectory() as d:
            args = a.parser().parse_args(['export', '--output', d + '/backup'])
            with patch.object(a, 'run', side_effect=AssertionError('unexpected secret read')):
                a.export(args, baseline(), None)
            root = Path(d) / 'backup'
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            self.assertFalse((root / 'secrets.json').exists())
            self.assertEqual(a.load_baseline(root)['cluster_uid'], 'cluster-1')
            for f in root.iterdir(): self.assertEqual(f.stat().st_mode & 0o777, 0o600)
            (root / 'baseline.json').write_text('{}')
            with self.assertRaises(a.Failure): a.load_baseline(root)

    def test_prepare_incomplete_refused_and_candidate(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'backup'; root.mkdir()
            b = baseline(); a.dump(root / 'baseline.json', b); a.checksums(root)
            args = a.parser().parse_args(['prepare', '--baseline', str(root), '--output', d + '/plan', '--chart-version', 'test-version'])
            a.prepare(args)
            v = json.loads((Path(d) / 'plan/candidate-values.yaml').read_text())
            self.assertFalse(v['prometheus']['prometheusSpec']['ruleSelectorNilUsesHelmValues'])
            self.assertTrue(v['crds']['upgradeJob']['enabled'])
            b['errors'] = [{'collection': 'pods'}]; a.dump(root / 'baseline.json', b); a.checksums(root)
            args.output = d + '/bad'
            with self.assertRaises(a.Failure): a.prepare(args)

    def test_rbac_failure_is_not_empty_success(self):
        args = a.parser().parse_args(['assess', '--context', 'test'])
        with patch.object(a, 'run', side_effect=a.Failure('forbidden')):
            snap = a.Collector(args).collect()
        self.assertIsNone(snap['resources']['servicemonitors'])
        self.assertTrue(snap['errors'])
        self.assertTrue(any(level == 'UNKNOWN' for level, _ in a.report(snap)))

    def test_read_only_commands_and_context(self):
        args = a.parser().parse_args(['assess', '--context', 'test'])
        commands = []
        def fake(cmd):
            commands.append(cmd)
            if cmd[0] == 'helm': return '[]'
            if 'customresourcedefinitions' in cmd:
                return json.dumps({'items': [{'metadata': {'name': 'servicemonitors.monitoring.coreos.com'}}]})
            return '{"items": []}'
        with patch.object(a, 'run', side_effect=fake): a.Collector(args).collect()
        for cmd in commands:
            self.assertIn('test', cmd)
            self.assertIn('get' if cmd[0] == 'kubectl' else 'list', cmd)
            self.assertFalse(any(x in cmd for x in ('delete', 'apply', 'install', 'upgrade', 'patch')))

    def test_export_failure_is_reported_and_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as d:
            snap = baseline()
            snap['resources']['alertmanagers'] = [obj('Alertmanager', 'main', ns='cattle-monitoring-system')]
            collector = type('C', (), {'k': ['kubectl'], 'context': 'test'})()
            stream = io.StringIO()
            with patch.object(a, 'collect', return_value=(snap, collector)), patch.object(a, 'run', side_effect=a.Failure('forbidden')), contextlib.redirect_stdout(stream):
                code = a.main(['export', '--output', d + '/backup', '--include-secrets'])
            self.assertEqual(code, 3)
            self.assertIn('Collection failed: secret/', stream.getvalue())
            self.assertTrue(a.load_baseline(d + '/backup')['errors'])

    def test_cli_export_prepare_validate_workflow(self):
        with tempfile.TemporaryDirectory() as d:
            snap = baseline()
            collector = type('C', (), {'k': ['kubectl'], 'context': 'test'})()
            with patch.object(a, 'collect', return_value=(snap, collector)), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(a.main(['export', '--output', d + '/backup']), 0)
                self.assertEqual(a.main(['prepare', '--baseline', d + '/backup', '--output', d + '/plan', '--chart-version', 'review-version']), 0)
                changed = baseline(); changed['resources']['servicemonitors'] = []
                with patch.object(a, 'collect', return_value=(changed, collector)):
                    self.assertEqual(a.main(['validate', '--baseline', d + '/backup']), 2)

    def test_token_plain_http_rejected(self):
        args = a.parser().parse_args(['assess', '--token-file', '/does/not/exist'])
        with self.assertRaises(a.Failure): a.runtime_api('http://localhost:9090', 'targets', args)

if __name__ == '__main__': unittest.main()
