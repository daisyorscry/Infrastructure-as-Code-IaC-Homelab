"""Local Ansible validation. No task in these tests connects to a VM."""
import json
import base64
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
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
        if stack == 'workers':
            overrides.setdefault('master', 'master-1')
        playbook = 'add-workers.yml' if stack == 'workers' else f'cluster-{stack}.yml'
        with tempfile.TemporaryDirectory() as tmp:
            inv = Path(tmp) / 'inventory.yml'
            inv.write_text(yaml.safe_dump(self.inventory))
            env = dict(os.environ, ANSIBLE_CONFIG=str(ANSIBLE / 'ansible.cfg'),
                       ANSIBLE_INVENTORY_ENABLED='yaml,ini', ANSIBLE_NOCOLOR='1')
            result = subprocess.run([
                str(Path(sys.executable).with_name('ansible-playbook')) if Path(sys.executable).with_name('ansible-playbook').is_file() else shutil.which('ansible-playbook'), '-i', str(inv),
                str(ANSIBLE / 'playbooks' / playbook), '--tags', 'preflight',
                '-e', json.dumps(dict({} if stack == 'workers' else self.config, **overrides)),
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
        output = self.run_preflight(stack='workers', workers='worker-4')
        section = output.split('Register only selected machines for guest access')[1]
        self.assertIn('item=master-1', section)
        self.assertIn('item=worker-4', section)
        self.assertNotIn('item=worker-1', section)

    def test_join_does_not_require_four_new_workers(self):
        self.inventory['all']['children']['k8s_workers']['hosts'] = {'worker-5': {'ansible_host': '192.168.10.25'}}
        self.run_preflight(stack='workers', workers='worker-5')

    def test_join_requires_known_targets(self):
        for targets in ([], ['missing-worker'], 'missing-worker', 'worker-4,', 'worker-4,worker-4'):
            with self.subTest(targets=targets):
                self.run_preflight(stack='workers', expected=False, workers=targets)

    def test_init_worker_count_from_string_survey(self):
        self.workers['worker-5'] = {'ansible_host': '192.168.10.25'}
        for count in ('4', '5'):
            with self.subTest(count=count):
                output = self.run_preflight(worker_count=count)
                section = output.split('Register only selected machines for guest access')[1]
                self.assertEqual('item=worker-5' in section, count == '5')

    def test_init_rejects_worker_form_parameters(self):
        self.run_preflight(expected=False, workers='worker-1,worker-2,worker-3,worker-4')
        self.run_preflight(expected=False, master='master-1')

    def test_init_rejects_invalid_worker_counts(self):
        for count in (3, 5, 'all', 4.5, True):
            with self.subTest(count=count):
                self.run_preflight(expected=False, worker_count=count)

    def test_init_rejects_selection_count_mismatch(self):
        self.run_preflight(expected=False, workers='worker-1,worker-2', worker_count=4)

    def test_join_accepts_list_and_csv_without_init_config(self):
        for workers in (['worker-3', 'worker-4'], 'worker-3, worker-4'):
            with self.subTest(workers=workers):
                output = self.run_preflight(stack='workers', workers=workers)
                section = output.split('Register only selected machines for guest access')[1]
                self.assertIn('item=worker-3', section)
                self.assertIn('item=worker-4', section)
                self.assertNotIn('item=worker-1', section)

    def test_rejects_obsolete_operation_parameter(self):
        self.run_preflight(expected=False, cluster_operation='join')
        self.run_preflight(stack='workers', expected=False, workers='worker-4', cluster_operation='init')

    def test_worker_flow_requires_explicit_known_master(self):
        for master in ('', 'unknown-master', 'worker-1'):
            with self.subTest(master=master):
                self.run_preflight(stack='workers', expected=False, master=master, workers='worker-4')

    def test_worker_flow_selects_master_independently(self):
        self.inventory['all']['children']['k8s_control_plane']['hosts']['master-2'] = {'ansible_host': '192.168.10.12'}
        output = self.run_preflight(stack='workers', master='master-2', workers='worker-4')
        section = output.split('Register only selected machines for guest access')[1]
        self.assertIn('item=master-2', section)
        self.assertNotIn('item=master-1', section)

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
                       cluster_cri_socket='unix:///var/run/crio/crio.sock',
                       cluster_worker_targets=['worker-1', 'worker-2', 'worker-3'],
                       hostvars={'worker-1': {'node_l2_interfaces': ['ens18']},
                                 'worker-2': {'node_l2_interfaces': ['ens19']},
                                 'worker-3': {'node_l2_interfaces': ['eth0']}})
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
                    if template.name == 'worker-l2.yml.j2':
                        self.assertEqual(len(docs), 3)
                        for doc, worker, nic in zip(docs, context['cluster_worker_targets'], ['ens18', 'ens19', 'eth0']):
                            spec = doc['spec']
                            selector = spec['nodeSelector'] if stack == 'cilium' else spec['nodeSelectors'][0]
                            self.assertEqual(selector['matchLabels']['kubernetes.io/hostname'], worker)
                            self.assertEqual(spec['interfaces'], [f'^{nic}$' if stack == 'cilium' else nic])

    def test_workers_load_runtime_and_interface_mode_from_master(self):
        for stack in ('cilium', 'istio'):
            with self.subTest(stack=stack), tempfile.TemporaryDirectory() as tmp:
                profile = {
                    'name': f'existing-{stack}', 'stack': stack, 'api_ip': '192.168.10.11',
                    'kubernetes_version': '1.36.1', 'crio_version': '1.36.2', 'l2_interfaces': [],
                    'pod_cidr': '10.244.0.0/16', 'service_cidr': '10.96.0.0/12',
                    'node_lan_cidr': '192.168.10.0/24', 'lb_start_ip': '192.168.10.200',
                    'lb_stop_ip': '192.168.10.220',
                }
                inv = Path(tmp) / 'inventory.yml'
                inv.write_text(yaml.safe_dump(self.inventory))
                play = Path(tmp) / 'load-profile.yml'
                play.write_text(yaml.safe_dump([{
                    'hosts': 'localhost', 'gather_facts': False, 'connection': 'local',
                    'tasks': [{'ansible.builtin.set_fact': {
                        'selected_flow': 'worker', 'selected_master': 'master-1',
                        'selected_workers': ['worker-4'],
                    }}],
                }, {
                    'hosts': 'master-1', 'connection': 'local', 'gather_facts': False,
                    'vars': {'recorded_profile': {'content': base64.b64encode(json.dumps(profile).encode()).decode()}},
                    'tasks': [
                        {'ansible.builtin.import_role': {'name': str(ROLE), 'tasks_from': 'load_worker_profile'}},
                        {'ansible.builtin.assert': {'that': [
                            "hostvars['worker-4'].kubernetes_version == '1.36.1'",
                            "hostvars['worker-4'].crio_version == '1.36.2'",
                            "hostvars['worker-4'].l2_interfaces == []",
                            f"hostvars['localhost'].selected_stack == '{stack}'",
                        ]}},
                    ],
                }]))
                result = subprocess.run([
                    str(Path(sys.executable).with_name('ansible-playbook')), '-i', str(inv), str(play),
                ], cwd=ANSIBLE, env=dict(os.environ, ANSIBLE_CONFIG=str(ANSIBLE / 'ansible.cfg'),
                                        ANSIBLE_NOCOLOR='1'), capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_runner_factory_validates_network_before_api_access(self):
        for stack in ('cilium', 'istio'):
            with self.subTest(stack=stack):
                env = dict(os.environ, ANSIBLE_CONFIG=str(ANSIBLE / 'ansible.cfg'),
                           PROXMOX_VE_ENDPOINT='', PROXMOX_VE_API_TOKEN='', ANSIBLE_NOCOLOR='1')
                result = subprocess.run([
                    str(Path(sys.executable).with_name('ansible-playbook')),
                    '-i', str(ANSIBLE / 'inventory/runner-local.yml'),
                    str(ANSIBLE / 'playbooks' / f'cluster-{stack}.yml'), '--tags', 'factory',
                    '-e', json.dumps(dict(self.config, pod_cidr='192.168.0.0/16')),
                ], cwd=ANSIBLE, env=env, capture_output=True, text=True, timeout=60)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Overlapping Pod/Service/LAN CIDRs', result.stdout + result.stderr)
                self.assertNotIn('Clone, finish Cloud-Init', result.stdout)

    def test_static_inventory_skips_entire_factory_without_evaluating_delegate(self):
        for stack in ('cilium', 'istio'):
            with self.subTest(stack=stack), tempfile.TemporaryDirectory() as tmp:
                inventory = Path(tmp) / 'existing.yml'
                inventory.write_text(yaml.safe_dump(self.inventory))
                result = subprocess.run([
                    str(Path(sys.executable).with_name('ansible-playbook')), '-i', str(inventory),
                    str(ANSIBLE / 'playbooks' / f'cluster-{stack}.yml'), '--tags', 'factory',
                ], cwd=ANSIBLE, env=dict(os.environ, ANSIBLE_CONFIG=str(ANSIBLE / 'ansible.cfg'),
                                        ANSIBLE_NOCOLOR='1'), capture_output=True, text=True, timeout=60)
                output = result.stdout + result.stderr
                self.assertEqual(result.returncode, 0, output)
                self.assertIn('VM provisioning is disabled', output)
                self.assertNotIn('Wait for SSH on the final post-restart addresses', output)
                self.assertNotIn('Read generated inventory', output)

    def test_explicit_false_override_skips_factory_with_empty_runner(self):
        result = subprocess.run([
            str(Path(sys.executable).with_name('ansible-playbook')),
            '-i', str(ANSIBLE / 'inventory/runner-local.yml'),
            str(ANSIBLE / 'playbooks/cluster-cilium.yml'), '--tags', 'factory',
            '-e', json.dumps({'cluster_provision_vms': False}),
        ], cwd=ANSIBLE, env=dict(os.environ, ANSIBLE_CONFIG=str(ANSIBLE / 'ansible.cfg'),
                                ANSIBLE_NOCOLOR='1'), capture_output=True, text=True, timeout=60)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn('VM provisioning is disabled', output)
        self.assertNotIn('Wait for SSH on the final post-restart addresses', output)

    def test_runner_factory_invokes_lifecycle_before_node_access(self):
        env = dict(os.environ, ANSIBLE_CONFIG=str(ANSIBLE / 'ansible.cfg'),
                   PROXMOX_VE_ENDPOINT='', PROXMOX_VE_API_TOKEN='', ANSIBLE_NOCOLOR='1')
        result = subprocess.run([
            str(Path(sys.executable).with_name('ansible-playbook')),
            '-i', str(ANSIBLE / 'inventory/runner-local.yml'),
            str(ANSIBLE / 'playbooks/cluster-cilium.yml'),
            '-e', json.dumps(self.config),
        ], cwd=ANSIBLE, env=env, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('PROXMOX_VE_ENDPOINT must be', result.stdout + result.stderr)
        self.assertNotIn('TASK [homelab_cluster : Read existing control plane files]', result.stdout)

    def test_generated_hosts_are_waited_for_and_consumed_by_preflight(self):
        # Replace only the PVE helper in an isolated copy. All Ansible tasks run,
        # including delegated wait_for_connection, using local test connections.
        with tempfile.TemporaryDirectory(prefix='homelab-flow-test-') as tmp:
            isolated = Path(tmp) / 'ansible'
            shutil.copytree(ANSIBLE, isolated)
            names = ['dev-cilium-master-1'] + [f'dev-cilium-worker-{i}' for i in range(1, 5)]
            result_data = {'changed': True, 'known_hosts_path': str(Path(tmp) / 'known_hosts'),
                           'inventory_path': str(Path(tmp) / 'inventory.json'), 'nodes': [
                               {'name': name, 'group': 'k8s_control_plane' if i == 0 else 'k8s_workers',
                                'ip': f'192.168.10.{101+i}', 'vmid': 101+i,
                                'key_path': str(Path(tmp) / f'key-{i}')} for i, name in enumerate(names)]}
            release = Path(tmp) / 'release-helper'
            (isolated / 'files/provision_cluster.py').write_text(
                "import sys, time\nfrom pathlib import Path\n"
                "print('[factory] test VM: Cloud-Init complete; restarting', file=sys.stderr, flush=True)\n"
                + f'while not Path({str(release)!r}).exists():\n    time.sleep(0.05)\n'
                + 'print(' + repr(json.dumps(result_data)) + ')\n')
            inventory = Path(tmp) / 'runner.yml'
            inventory.write_text(yaml.safe_dump({'all': {'vars': {
                'ansible_connection': 'local', 'ansible_python_interpreter': sys.executable,
            }, 'hosts': {'localhost': {}}}}))
            env = dict(os.environ, ANSIBLE_CONFIG=str(isolated / 'ansible.cfg'), ANSIBLE_NOCOLOR='1',
                       PYTHONUNBUFFERED='1')
            command = [
                str(Path(sys.executable).with_name('ansible-playbook')), '-i', str(inventory),
                str(isolated / 'playbooks/cluster-cilium.yml'), '--tags', 'factory,preflight',
                '-e', json.dumps(self.config),
            ]
            # The helper cannot finish until its progress reaches this test.
            # A fallback releases it if streaming regresses, so the test can fail cleanly.
            fallback = threading.Timer(15, release.touch)
            streamed_before_completion = False
            lines = []
            try:
                with subprocess.Popen(command, cwd=isolated, env=env, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, text=True) as process:
                    fallback.start()
                    for line in process.stdout:
                        lines.append(line)
                        if '[factory] test VM:' in line and not release.exists():
                            streamed_before_completion = process.poll() is None
                            release.touch()
                    rc = process.wait(timeout=60)
            finally:
                fallback.cancel()
            output = ''.join(lines)
            self.assertEqual(rc, 0, output)
            self.assertTrue(streamed_before_completion, 'Factory output was buffered until completion: ' + output)
            self.assertIn('[factory] test VM: Cloud-Init complete; restarting', output)
            self.assertLess(output.index('[factory] test VM:'), output.index('Read generated inventory]'))
            waits = output.split('Wait for SSH on the final post-restart addresses]')[1].split('TASK [')[0]
            self.assertNotIn('skipping:', waits)
            for name in names:
                self.assertIn(name, waits)
            self.assertIn('All assertions passed', output)


if __name__ == '__main__':
    unittest.main(verbosity=2)
