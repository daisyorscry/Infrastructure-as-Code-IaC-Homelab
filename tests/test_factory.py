"""VM lifecycle tests against a fake PVE API. Never contacts infrastructure."""
import copy
import importlib.util
import ipaddress
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('provision_cluster', ROOT / 'ansible/files/provision_cluster.py')
factory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(factory)


class FakeProxmox:
    def __init__(self):
        self.vms = {}
        self.events = []
        self.commands = {}
        self.next_pid = 1
        self.fail_cloud_init = False
        self.fail_clone_wait = False
        self.template = {'template': 1, 'scsi0': 'nvme:base-9201,size=20G',
                         'ide2': 'nvme:cloudinit,media=cdrom', 'net0': 'virtio,bridge=vmbr1'}

    def call(self, method, path, **params):
        self.events.append((method, path, params))
        if path == '/cluster/resources':
            return [{'vmid': vmid, 'name': vm['name'], 'node': 'pve', 'type': 'qemu'} for vmid, vm in self.vms.items()]
        if path == '/cluster/nextid':
            return max([100] + list(self.vms)) + 1
        if '/tasks/' in path:
            if self.fail_clone_wait and 'clone' in path:
                self.fail_clone_wait = False
                raise factory.FactoryError('simulated clone timeout')
            return {'status': 'stopped', 'exitstatus': 'OK'}
        parts = path.split('/')
        vmid = int(parts[4])
        action = '/'.join(parts[5:])
        if vmid == 9201 and action == 'config':
            return copy.deepcopy(self.template)
        if action == 'clone':
            newid = params['newid']
            assert newid not in self.vms
            self.vms[newid] = dict(self.template, name=params['name'], description=params['description'],
                                    status='stopped', boot_id='11111111-1111-1111-1111-111111111111',
                                    ip=f'192.168.1.{newid}')
            return 'UPID:pve:clone'
        vm = self.vms[vmid]
        if action == 'config':
            if method == 'GET':
                return copy.deepcopy(vm)
            vm.update(params)
            return 'UPID:pve:configure'
        if action == 'resize':
            vm['scsi0'] = 'nvme:vm,size=' + params['size']
            return 'UPID:pve:resize'
        if action == 'status/current':
            return {'status': vm['status']}
        if action == 'status/start':
            vm['status'] = 'running'
            return 'UPID:pve:start'
        if action == 'status/reboot':
            vm['boot_id'] = '22222222-2222-2222-2222-222222222222'
            return 'UPID:pve:reboot'
        if action == 'agent/exec':
            command = params['command']
            if command[0] == '/bin/cat':
                output, rc = vm['boot_id'], 0
            elif command[0] == '/usr/bin/cloud-init':
                output, rc = 'status: done', 1 if self.fail_cloud_init else 0
            elif command[0] == '/bin/sh':
                output, rc = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeHostKey root@vm', 0
            else:
                raise AssertionError(command)
            pid = self.next_pid
            self.next_pid += 1
            self.commands[pid] = {'exited': 1, 'exitcode': rc, 'out-data': output}
            return {'pid': pid}
        if action == 'agent/exec-status':
            return self.commands[params['pid']]
        if action == 'agent/network-get-interfaces':
            # Fail the test if addresses are queried before the mandatory restart.
            assert vm['boot_id'].startswith('2222')
            return {'result': [{'name': 'ens18', 'ip-addresses': [
                {'ip-address-type': 'ipv4', 'ip-address': vm['ip']}]}]}
        raise AssertionError((method, path, params))


class FactoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='homelab-factory-test-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        root.chmod(0o700)
        (root / '.persistent-key-store').touch()
        self.cfg = dict(cluster_name='dev', stack='cilium', worker_count=4,
                        node_name='pve', template_id=9201, datastore_id='nvme', bridge='vmbr1',
                        cloud_init_user='debian', cores=4, memory_mb=8192, disk_gb=30,
                        node_lan_cidr='192.168.1.0/24', lb_start_ip='192.168.1.10',
                        lb_stop_ip='192.168.1.20', key_directory=str(root), timeout=1, poll_interval=1)
        self.api = FakeProxmox()

    def run_factory(self):
        return factory.Factory(self.cfg, self.api).run()

    def test_clone_cloud_init_reboot_then_discover_and_export(self):
        result = self.run_factory()
        self.assertTrue(result['changed'])
        self.assertEqual(len(result['nodes']), 5)
        self.assertEqual(result['nodes'][0]['name'], 'dev-cilium-master-1')
        self.assertEqual(sum(node['group'] == 'k8s_workers' for node in result['nodes']), 4)
        events = self.api.events
        for node in result['nodes']:
            prefix = f"/nodes/pve/qemu/{node['vmid']}/"
            cloud = next(i for i, (_, p, args) in enumerate(events) if p == prefix + 'agent/exec' and args['command'][0] == '/usr/bin/cloud-init')
            reboot = next(i for i, (_, p, _) in enumerate(events) if p == prefix + 'status/reboot')
            network = next(i for i, (_, p, _) in enumerate(events) if p == prefix + 'agent/network-get-interfaces')
            self.assertLess(cloud, reboot)
            self.assertLess(reboot, network)
            self.assertTrue(Path(node['key_path']).is_file())
        inventory = json.loads(Path(result['inventory_path']).read_text())
        self.assertEqual(inventory['all']['vars']['worker_count'], 4)
        self.assertIn('192.168.1.101 ssh-ed25519 ', Path(result['known_hosts_path']).read_text())
        for vm in self.api.vms.values():
            self.assertEqual(vm['ipconfig0'], 'ip=dhcp')

    def test_retry_refreshes_dhcp_without_reclone_reconfigure_or_reboot(self):
        first = self.run_factory()
        retained_keys = [Path(node['key_path']).read_bytes() for node in first['nodes']]
        self.api.events.clear()
        self.api.vms[101]['ip'] = '192.168.1.150'
        second = self.run_factory()
        self.assertFalse(second['changed'])
        self.assertEqual(second['nodes'][0]['ip'], '192.168.1.150')
        self.assertFalse(any(method != 'GET' and '/agent/exec' not in path for method, path, _ in self.api.events))
        self.assertEqual(retained_keys, [Path(node['key_path']).read_bytes() for node in second['nodes']])

    def test_resume_interrupted_clone_wait(self):
        self.api.fail_clone_wait = True
        with self.assertRaisesRegex(factory.FactoryError, 'clone timeout'):
            self.run_factory()
        result = self.run_factory()
        self.assertEqual(len(result['nodes']), 5)
        clones = [event for event in self.api.events if event[1].endswith('/clone')]
        self.assertEqual(len(clones), 5)

    def test_cloud_init_failure_stops_before_reboot_or_network(self):
        self.api.fail_cloud_init = True
        with self.assertRaisesRegex(factory.FactoryError, 'Guest command'):
            self.run_factory()
        self.assertFalse(any(path.endswith(('/status/reboot', '/agent/network-get-interfaces')) for _, path, _ in self.api.events))
        self.api.fail_cloud_init = False
        self.assertEqual(len(self.run_factory()['nodes']), 5)

    def test_foreign_name_is_rejected_before_clone(self):
        self.api.vms[500] = {'name': 'dev-cilium-master-1'}
        with self.assertRaisesRegex(factory.FactoryError, 'refusing adoption'):
            self.run_factory()
        self.assertFalse(any(path.endswith('/clone') for _, path, _ in self.api.events))

    def test_ownership_mismatch_and_missing_key_never_modify_existing_vm(self):
        result = self.run_factory()
        original = self.api.vms[101]['description']
        self.api.vms[101]['description'] = 'foreign'
        self.api.events.clear()
        with self.assertRaisesRegex(factory.FactoryError, 'Ownership mismatch'):
            self.run_factory()
        self.assertTrue(all(method == 'GET' for method, _, _ in self.api.events))
        self.api.vms[101]['description'] = original
        Path(result['nodes'][0]['key_path']).unlink()
        with self.assertRaisesRegex(factory.FactoryError, 'Missing retained SSH key'):
            self.run_factory()

    def test_refuses_changed_manifest_settings(self):
        self.run_factory()
        self.cfg['worker_count'] = 9
        with self.assertRaisesRegex(factory.FactoryError, 'settings differ'):
            self.run_factory()

    def test_lost_vm_is_not_replaced(self):
        self.run_factory()
        del self.api.vms[101]
        with self.assertRaisesRegex(factory.FactoryError, 'no automatic replacement'):
            self.run_factory()

    def test_duplicate_final_addresses_are_rejected(self):
        self.run_factory()
        self.api.vms[102]['ip'] = self.api.vms[101]['ip']
        with self.assertRaisesRegex(factory.FactoryError, 'Duplicate DHCP'):
            self.run_factory()

    def test_invalid_template_fails_before_creating_vm(self):
        self.api.template['cicustom'] = 'user=local:snippets/custom.yaml'
        with self.assertRaisesRegex(factory.FactoryError, 'Source must be'):
            self.run_factory()
        self.assertEqual(self.api.vms, {})

    def test_address_selection_excludes_other_networks_and_lb_pool(self):
        lan = ipaddress.IPv4Network('192.168.1.0/24')
        start, stop = map(ipaddress.IPv4Address, ('192.168.1.10', '192.168.1.20'))
        interfaces = [{'ip-addresses': [{'ip-address-type': 'ipv4', 'ip-address': ip} for ip in
                      ('127.0.0.1', '192.168.10.101', '192.168.1.10', '192.168.1.101')]}]
        self.assertEqual(factory.select_ip(interfaces, lan, start, stop), '192.168.1.101')
        interfaces[0]['ip-addresses'].append({'ip-address-type': 'ipv4', 'ip-address': '192.168.1.102'})
        with self.assertRaisesRegex(factory.FactoryError, 'Multiple guest addresses'):
            factory.select_ip(interfaces, lan, start, stop)


if __name__ == '__main__':
    unittest.main()
