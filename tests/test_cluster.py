"""Local Ansible validation. No task in these tests connects to a VM."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[1]
ANSIBLE = ROOT / 'ansible'
ROLE = ANSIBLE / 'roles/homelab_cluster'


class ClusterTests(unittest.TestCase):
    def setUp(self):
        self.workers = {f'worker-{i}': {'ansible_host': f'192.168.10.{20+i}'} for i in range(1, 5)}
        self.inventory = {'all': {'children': {
            'k8s_control_plane': {'hosts': {'master-1': {'ansible_host': '192.168.10.11'}}},
            'k8s_workers': {'hosts': self.workers},
        }}}
        self.config = {
            'node_lan_cidr': '192.168.10.0/24',
            'lb_start_ip': '192.168.10.200', 'lb_stop_ip': '192.168.10.220',
        }

    def run_preflight(self, stack='cilium', expected=True, message='', **overrides):
        with tempfile.TemporaryDirectory() as tmp:
            inv = Path(tmp) / 'inventory.yml'
            inv.write_text(yaml.safe_dump(self.inventory))
            env = dict(os.environ, ANSIBLE_CONFIG=str(ANSIBLE / 'ansible.cfg'),
                       ANSIBLE_INVENTORY_ENABLED='yaml,ini', ANSIBLE_NOCOLOR='1')
            result = subprocess.run([
                str(Path(sys.executable).with_name('ansible-playbook')) if Path(sys.executable).with_name('ansible-playbook').is_file() else shutil.which('ansible-playbook'), '-i', str(inv),
                str(ANSIBLE / f'playbooks/cluster-{stack}.yml'), '--tags', 'preflight',
                '-e', json.dumps(dict(self.config, **overrides)),
            ], cwd=ANSIBLE, env=env, capture_output=True, text=True, timeout=60)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode == 0, expected, output)
            if message:
                self.assertIn(message, output)
            # No remote-role tasks should execute even when host groups are populated.
            self.assertNotIn('TASK [Gather facts', output)
            self.assertNotIn('TASK [homelab_cluster : Read existing control plane files]', output)
            return output

    def test_both_stacks_accept_initial_four_workers(self):
        for stack in ('cilium', 'istio'):
            with self.subTest(stack=stack):
                output = self.run_preflight(stack=stack)
                self.assertIn('item=worker-4', output)

    def test_init_rejects_three_workers(self):
        del self.workers['worker-4']
        self.run_preflight(expected=False, message='>=4 workers')

    def test_join_selects_only_requested_worker(self):
        output = self.run_preflight(cluster_operation='join', workers='worker-4')
        section = output.split('Register only selected machines for guest access')[1]
        self.assertIn('item=master-1', section)
        self.assertIn('item=worker-4', section)
        self.assertNotIn('item=worker-1', section)

    def test_join_does_not_require_four_new_workers(self):
        self.inventory['all']['children']['k8s_workers']['hosts'] = {'worker-5': {'ansible_host': '192.168.10.25'}}
        self.run_preflight(cluster_operation='join', join_workers=['worker-5'])

    def test_join_requires_known_targets(self):
        for targets in ([], ['missing-worker'], 'missing-worker', 'worker-4,', 'worker-4,worker-4'):
            with self.subTest(targets=targets):
                self.run_preflight(expected=False, cluster_operation='join', workers=targets)

    def test_init_worker_count_from_string_survey(self):
        self.workers['worker-5'] = {'ansible_host': '192.168.10.25'}
        for count in ('4', '5'):
            with self.subTest(count=count):
                output = self.run_preflight(worker_count=count)
                section = output.split('Register only selected machines for guest access')[1]
                self.assertEqual('item=worker-5' in section, count == '5')

    def test_init_can_select_specific_workers(self):
        self.workers['worker-5'] = {'ansible_host': '192.168.10.25'}
        output = self.run_preflight(workers='worker-5, worker-4,worker-3,worker-2', worker_count=4)
        section = output.split('Register only selected machines for guest access')[1]
        self.assertIn('item=worker-5', section)
        self.assertNotIn('item=worker-1', section)

    def test_init_rejects_invalid_worker_counts(self):
        for count in (3, 5, 'all', 4.5, True):
            with self.subTest(count=count):
                self.run_preflight(expected=False, worker_count=count)

    def test_init_rejects_selection_count_mismatch(self):
        self.run_preflight(expected=False, workers='worker-1,worker-2', worker_count=4)

    def test_join_accepts_list_and_csv_on_both_stacks(self):
        for stack in ('cilium', 'istio'):
            for workers in (['worker-3', 'worker-4'], 'worker-3, worker-4'):
                with self.subTest(stack=stack, workers=workers):
                    output = self.run_preflight(stack=stack, cluster_operation='join', workers=workers)
                    section = output.split('Register only selected machines for guest access')[1]
                    self.assertIn('item=worker-3', section)
                    self.assertIn('item=worker-4', section)
                    self.assertNotIn('item=worker-1', section)

    def test_rejects_ambiguous_old_and_new_worker_parameters(self):
        self.run_preflight(expected=False, cluster_operation='join', workers='worker-4', join_workers=['worker-3'])

    def test_rejects_second_control_plane(self):
        self.inventory['all']['children']['k8s_control_plane']['hosts']['master-2'] = {'ansible_host': '192.168.10.12'}
        self.run_preflight(expected=False)

    def test_rejects_mismatched_runtime_minor(self):
        self.run_preflight(expected=False, crio_version='1.34.1')

    def test_rejects_network_overlap(self):
        self.run_preflight(expected=False, pod_cidr='192.168.0.0/16', message='Overlapping')

    def test_rejects_nodes_inside_lb_pool(self):
        self.run_preflight(expected=False, lb_start_ip='192.168.10.10', message='inside LB pool')

    def test_rejects_duplicate_node_addresses(self):
        self.workers['worker-4']['ansible_host'] = self.workers['worker-1']['ansible_host']
        self.run_preflight(expected=False, message='Duplicate inventory IPs')

    def test_rejects_missing_network_config(self):
        self.run_preflight(expected=False, node_lan_cidr='CHANGE_ME')

    def test_templates_render_valid_yaml_for_each_stack(self):
        from ansible.plugins.filter.core import FilterModule
        env = jinja2.Environment(loader=jinja2.FileSystemLoader(ROLE / 'templates'),
                                 undefined=jinja2.StrictUndefined)
        env.filters.update(FilterModule().filters())
        context = yaml.safe_load((ANSIBLE / 'inventory/group_vars/all.yml').read_text())
        context.update(self.config, cluster_api_ip='192.168.10.11', inventory_hostname='master-1',
                       cluster_cri_socket='unix:///var/run/crio/crio.sock')
        for stack in ('cilium', 'istio'):
            context['cluster_stack'] = stack
            for template in (ROLE / 'templates').glob('*.j2'):
                with self.subTest(stack=stack, template=template.name):
                    docs = list(yaml.safe_load_all(env.get_template(template.name).render(context)))
                    self.assertTrue(all(isinstance(doc, dict) for doc in docs))
                    if template.name == 'kubeadm-init.yml.j2':
                        self.assertEqual(len(docs), 3 if stack == 'cilium' else 4)
                        self.assertEqual(docs[0]['nodeRegistration']['criSocket'], context['cluster_cri_socket'])
                    if template.name == 'gateway.yml.j2':
                        self.assertEqual(docs[1]['spec']['gatewayClassName'], stack)
                    if template.name == 'cilium-values.yml.j2':
                        self.assertTrue(docs[0]['kubeProxyReplacement'])
                        self.assertEqual(docs[0]['ipam']['operator']['clusterPoolIPv4PodCIDRList'], [context['pod_cidr']])

    def test_two_cluster_entrypoints_and_factory_are_the_only_playbooks(self):
        self.assertEqual({p.name for p in (ANSIBLE / 'playbooks').glob('*.yml')},
                         {'create-vm.yml', 'cluster-cilium.yml', 'cluster-istio.yml'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
