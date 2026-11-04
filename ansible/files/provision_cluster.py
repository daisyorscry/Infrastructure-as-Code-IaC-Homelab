#!/usr/bin/env python3
"""Proxmox VM lifecycle for cluster init. Standard library only; JSON in/out.

Tokens/passwords come only from the runner environment. No VM is deleted or
adopted. Each run creates a fresh fleet; manifests are audit records, not resume state.
"""
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import ssl
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen
import uuid


class FactoryError(Exception):
    pass


class APIError(FactoryError):
    def __init__(self, method, path, status, reason=''):
        self.status = status
        detail = f' ({reason})' if reason else ''
        super().__init__(f'Proxmox {method} {path}: HTTP {status}{detail}; check runner connectivity and token permissions.')


class GuestProcessLost(FactoryError):
    pass


class GuestCommandError(FactoryError):
    def __init__(self, record, command, result):
        self.result = result
        super().__init__(f"Guest command {command[0]} failed on {record['name']} "
                         f"(exit code {result.get('exitcode', 'unknown')}); inspect guest logs.")


def fresh_nic(template_net0, bridge, mac):
    # Keep the template's VLAN/trunk and NIC tuning, but replace its identity.
    properties = dict(part.split('=', 1) for part in template_net0.split(',') if '=' in part)
    for key in ('virtio', 'e1000', 'e1000e', 'rtl8139', 'vmxnet3', 'ne2k_pci', 'pcnet', 'macaddr', 'model'):
        properties.pop(key, None)
    properties['bridge'] = bridge
    properties.pop('link_down', None)
    return ','.join([f'virtio={mac}'] + [f'{key}={value}' for key, value in properties.items()])


class Proxmox:
    def __init__(self, endpoint, token, insecure=False):
        parsed = urlsplit(endpoint)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise FactoryError('PROXMOX_VE_ENDPOINT must be https://host:8006 without a path or credentials.')
        if not re.fullmatch(r'[^!\s]+![^=\s]+=\S+', token):
            raise FactoryError('Set secret PROXMOX_VE_API_TOKEN=user@realm!token=secret.')
        self.base = endpoint.rstrip('/') + '/api2/json'
        self.token = token
        self.context = ssl._create_unverified_context() if insecure else ssl.create_default_context()

    def call(self, method, path, **params):
        encoded = urlencode(params, doseq=True).encode()
        url = self.base + path
        if method == 'GET' and params:
            url += '?' + encoded.decode()
        request = Request(url, data=encoded if method != 'GET' else None, method=method,
                          headers={'Authorization': 'PVEAPIToken=' + self.token,
                                   'Content-Type': 'application/x-www-form-urlencoded'})
        try:
            with urlopen(request, context=self.context, timeout=30) as response:
                return json.load(response)['data']
        except HTTPError as error:
            # Never include the response body or submitted parameters (Cloud-Init secrets).
            reason = str(error.reason).replace(self.token, '[redacted]')[:300]
            error.close()
            raise APIError(method, path, error.code, reason) from None
        except (URLError, TimeoutError, OSError):
            raise FactoryError(f'Cannot reach Proxmox {method} {path}; check endpoint, TLS, and runner routing.') from None


def atomic_json(path, data):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.factory-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def select_ip(interfaces, lan, start, stop):
    candidates = set()
    for interface in interfaces:
        for address in interface.get('ip-addresses', []):
            if address.get('ip-address-type') != 'ipv4':
                continue
            ip = ipaddress.IPv4Address(address['ip-address'])
            if ip in lan and ip not in (lan.network_address, lan.broadcast_address) and not start <= ip <= stop:
                candidates.add(str(ip))
    if len(candidates) > 1:
        raise FactoryError('Multiple guest addresses in node_lan_cidr; use a template with one node-LAN address.')
    return next(iter(candidates), None)


def disk_size_gib(config):
    match = re.search(r'(?:^|,)size=([0-9.]+)([KMGT])(?:,|$)', config.get('scsi0', ''))
    if not match:
        raise FactoryError('Template/VM requires scsi0 with a readable disk size.')
    return float(match[1]) * {'K': 1 / 1024**2, 'M': 1 / 1024, 'G': 1, 'T': 1024}[match[2]]


class Factory:
    def __init__(self, cfg, api):
        self.cfg, self.api = cfg, api
        self.validate()
        self.root = Path(cfg['key_directory'])
        if not self.root.is_absolute() or not self.root.is_dir() or self.root.is_symlink():
            raise FactoryError('Mount the persistent VM_SSH_KEY_DIR before running the factory.')
        if self.root.stat().st_mode & 0o777 != 0o700 or not (self.root / '.persistent-key-store').is_file():
            raise FactoryError('Persistent key directory requires mode 0700 and .persistent-key-store marker.')
        self.state_dir = self.root / 'clusters' / f"{cfg['cluster_name']}--{cfg['stack']}"
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.manifest_path = self.state_dir / 'manifest.json'
        self.prefix = cfg['cluster_name']
        if not self.prefix.endswith('-' + cfg['stack']):
            self.prefix += '-' + cfg['stack']
        self.desired = [(f'{self.prefix}-master-1', 'k8s_control_plane')]
        self.desired += [(f'{self.prefix}-worker-{i}', 'k8s_workers') for i in range(1, cfg['worker_count'] + 1)]
        if any(len(name) > 63 for name, _ in self.desired):
            raise FactoryError('cluster_name is too long for generated node hostnames.')
        self.base = f"/nodes/{quote(cfg['node_name'], safe='')}/qemu"
        self.lan = ipaddress.IPv4Network(cfg['node_lan_cidr'])
        self.lb_start = ipaddress.IPv4Address(cfg['lb_start_ip'])
        self.lb_stop = ipaddress.IPv4Address(cfg['lb_stop_ip'])
        self.changed = False
        self._poll_depth = 0
        self._active_node = None

    def validate(self):
        cfg = self.cfg
        if cfg['stack'] not in ('cilium', 'istio') or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?', cfg['cluster_name']):
            raise FactoryError('Use a lowercase DNS cluster_name and cilium/istio stack.')
        if type(cfg['worker_count']) is not int or not 4 <= cfg['worker_count'] <= 999:
            raise FactoryError('worker_count must be an integer between 4 and 999.')
        for key, minimum in [('cores', 1), ('memory_mb', 1024), ('disk_gb', 30), ('template_id', 100), ('timeout', 1), ('poll_interval', 1)]:
            if type(cfg[key]) is not int or cfg[key] < minimum:
                raise FactoryError(f'{key} must be an integer >= {minimum}.')
        for key in ('node_name', 'datastore_id', 'bridge', 'cloud_init_user'):
            if not re.fullmatch(r'[a-zA-Z0-9_.-]+', cfg[key]):
                raise FactoryError(f'Invalid {key}.')
        lan = ipaddress.IPv4Network(cfg['node_lan_cidr'])
        start, stop = [ipaddress.IPv4Address(cfg[key]) for key in ('lb_start_ip', 'lb_stop_ip')]
        if not (start in lan and stop in lan and start <= stop) or any(ip in (lan.network_address, lan.broadcast_address) for ip in (start, stop)):
            raise FactoryError('LB pool must be a usable range inside node_lan_cidr.')

    def save(self):
        atomic_json(self.manifest_path, self.manifest)

    def progress(self, message):
        node = self._active_node
        prefix = f"{node['name']} (VMID {node['vmid']}): " if node else ''
        status = {'updated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                  'message': prefix + message}
        if node:
            status.update(name=node['name'], vmid=node['vmid'], phase=node['phase'])
        atomic_json(self.state_dir / 'progress.json', status)
        print('[factory] ' + status['message'], file=sys.stderr, flush=True)

    def poll(self, label, operation):
        started = time.monotonic()
        deadline = started + self.cfg['timeout']
        last_report = started
        self._poll_depth += 1
        last_error = None
        try:
            if self._poll_depth == 1:
                self.progress('Waiting for ' + label)
            while True:
                try:
                    result = operation()
                    if result:
                        return result
                except APIError as error:
                    if error.status not in (500, 503, 595, 596):
                        raise
                    last_error = str(error)
                now = time.monotonic()
                if now >= deadline:
                    detail = f' Last API error: {last_error}' if last_error else ''
                    raise FactoryError(f'Timed out waiting for {label}; state retained at {self.manifest_path}.{detail}')
                if self._poll_depth == 1 and now - last_report >= 30:
                    detail = f'; last API error: {last_error}' if last_error else ''
                    self.progress(f'Still waiting for {label} ({int(now - started)}s elapsed){detail}')
                    last_report = now
                time.sleep(self.cfg['poll_interval'])
        finally:
            self._poll_depth -= 1

    def wait_task(self, upid):
        node = upid.split(':')[1]
        path = f"/nodes/{quote(node, safe='')}/tasks/{quote(upid, safe='')}/status"

        def finished():
            status = self.api.call('GET', path)
            if status['status'] != 'stopped':
                return None
            if status.get('exitstatus') != 'OK':
                raise FactoryError(f'Proxmox task failed ({upid}); inspect its task log. No VM deleted.')
            return status
        return self.poll('Proxmox task ' + upid, finished)

    def vm_path(self, record):
        return f"{self.base}/{record['vmid']}"

    def guest_exec(self, record, command, retry_lost=False):
        path = self.vm_path(record) + '/agent'
        pid = None

        def execute():
            nonlocal pid
            if pid is None:
                pid = self.api.call('POST', path + '/exec', command=command)['pid']
            try:
                return self._exec_status(path, pid)
            except GuestProcessLost:
                if not retry_lost:
                    raise
                self.progress('Guest Agent lost a read-only command PID; submitting a new status query')
                pid = None
                return None

        result = self.poll('guest command ' + ' '.join(command[:3]) + ' on ' + record['name'],
                           execute)
        if result.get('exitcode') != 0 or result.get('out-truncated'):
            raise GuestCommandError(record, command, result)
        return result.get('out-data', '')

    def _exec_status(self, path, pid):
        try:
            status = self.api.call('GET', path + '/exec-status', pid=pid)
        except APIError as error:
            if error.status == 500 and re.search(r"invalid parameter ['\"]pid['\"]", str(error), re.I):
                raise GuestProcessLost('Guest Agent no longer tracks the command PID (agent/VM may have restarted).') from None
            raise
        return status if status.get('exited') else None

    def wait_cloud_init(self, record):
        def completed():
            exitcode = 0
            try:
                output = self.guest_exec(record, ['/usr/bin/cloud-init', 'status', '--format=json'], retry_lost=True)
            except GuestCommandError as error:
                if error.result.get('out-truncated') or error.result.get('exitcode') not in (1, 2):
                    raise
                output = error.result.get('out-data', '')
                exitcode = error.result.get('exitcode', 'unknown')
            try:
                details = json.loads(output)
                status = details['status']
            except (ValueError, KeyError, TypeError):
                raise FactoryError(f"Cloud-Init returned invalid status JSON on {record['name']} (exit code {exitcode}).") from None
            if status == 'done' and exitcode == 0:
                return True
            if status == 'running' and exitcode == 0:
                return None
            # Report module names, not raw user-data errors that could contain secrets.
            modules = sorted(set(re.findall(
                r'package-update-upgrade-install|apt-configure|scripts-user|runcmd|set-passwords|users-groups',
                json.dumps(details))))
            module_hint = ' Failed modules: ' + ', '.join(modules) + '.' if modules else ''
            raise FactoryError(f'Cloud-Init status is {status} on {record["name"]} (exit code {exitcode}).'
                               + module_hint + ' Inspect /var/log/cloud-init-output.log in this VM.')
        self.poll('Cloud-Init completion on ' + record['name'], completed)

    def clean_network_identity(self, record):
        self.progress('Resetting cloned machine-id, Cloud-Init network state, and DHCP leases before reboot')
        # --configs is unavailable in some supported Debian 12 cloud-init versions.
        self.guest_exec(record, ['/usr/bin/cloud-init', 'clean', '--machine-id'])
        # All paths are inside this run's fresh guest; the source template is untouched.
        cleanup = """from pathlib import Path
dbus = Path('/var/lib/dbus/machine-id')
if not dbus.is_symlink():
    dbus.unlink(missing_ok=True)
for pattern in ('/var/lib/dhcp/*.leases', '/var/lib/dhcp/*.leases~',
                '/var/lib/NetworkManager/*lease*', '/run/systemd/netif/leases/*',
                '/etc/netplan/50-cloud-init.yaml', '/etc/network/interfaces.d/50-cloud-init',
                '/etc/systemd/network/*cloud-init*.network',
                '/etc/NetworkManager/system-connections/cloud-init-*.nmconnection'):
    import glob
    for name in glob.glob(pattern):
        path = Path(name)
        if path.is_file() or path.is_symlink():
            path.unlink()
Path('/var/lib/NetworkManager/secret_key').unlink(missing_ok=True)
"""
        self.guest_exec(record, ['/usr/bin/python3', '-c', cleanup])

    def boot_id(self, record):
        value = self.guest_exec(record, ['/bin/cat', '/proc/sys/kernel/random/boot_id'], retry_lost=True).strip()
        if not re.fullmatch(r'[0-9a-f-]{36}', value):
            raise FactoryError('Guest Agent returned an invalid boot ID.')
        return value

    def owned(self, record):
        config = self.api.call('GET', self.vm_path(record) + '/config')
        if config.get('description') != record['owner'] or config.get('name') != record['name']:
            raise FactoryError(f"Ownership mismatch for VMID {record['vmid']}; refusing to modify it.")
        return config

    def key(self, record, create=False):
        path = self.root / f"vm-{record['vmid']}-{self.manifest['owner_id']}"
        if not path.exists():
            if not create:
                raise FactoryError(f'Missing retained SSH key {path}; refusing to replace an existing VM identity.')
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(path),
                            '-C', record['name']], check=True, capture_output=True)
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise FactoryError(f'Private key {path} must be a regular private file (0600).')
        # Derive the public key from the retained private key, never regenerate it.
        public = subprocess.run(['ssh-keygen', '-y', '-f', str(path)], check=True, capture_output=True, text=True).stdout.strip()
        if not public.startswith('ssh-ed25519 '):
            raise FactoryError('Retained factory key must be ED25519.')
        record['key_path'] = str(path)
        return public

    def inventory_vms(self):
        return self.api.call('GET', '/cluster/resources', type='vm')

    def provision(self, record):
        self._active_node = record
        self.progress('Creating fresh VM')
        path = self.vm_path(record)
        if any(int(vm['vmid']) == record['vmid'] for vm in self.inventory_vms()):
            raise FactoryError(f"VMID {record['vmid']} was allocated by another operation; no existing VM modified.")
        self.key(record, create=True)
        self.progress('Cloning template ' + str(self.cfg['template_id']))
        record['phase'] = 'cloning'
        self.save()
        record['clone_task'] = self.api.call('POST', f"{self.base}/{self.cfg['template_id']}/clone",
                                           newid=record['vmid'], name=record['name'],
                                           description=record['owner'], full=1,
                                           storage=self.cfg['datastore_id'])
        self.save()
        self.wait_task(record['clone_task'])
        record['phase'] = 'cloned'
        self.save()
        self.changed = True
        config = self.owned(record)
        public_key = self.key(record)
        if record['phase'] == 'cloned':
            self.progress('Configuring resources and Cloud-Init DHCP')
            if self.api.call('GET', path + '/status/current')['status'] != 'stopped':
                raise FactoryError('Unconfigured clone is already running; inspect it before retrying.')
            task = self.api.call('POST', path + '/config', cores=self.cfg['cores'], sockets=1, cpu='host',
                          memory=self.cfg['memory_mb'], balloon=0, onboot=1, agent='enabled=1',
                          net0=fresh_nic(config['net0'], self.cfg['bridge'], record['mac']),
                          smbios1='uuid=' + record['firmware_uuid'],
                          ciuser=self.cfg['cloud_init_user'], ciupgrade=0,
                          sshkeys=quote(public_key, safe=''), ipconfig0='ip=dhcp',
                          tags=f"iac;homelab-{self.cfg['stack']}")
            if task:
                self.wait_task(task)
            # Grow the fresh clone to the requested absolute size.
            current_size = disk_size_gib(config)
            if self.cfg['disk_gb'] < current_size:
                raise FactoryError('Requested disk is smaller than the clone; shrinking is never attempted.')
            if self.cfg['disk_gb'] > current_size:
                task = self.api.call('PUT', path + '/resize', disk='scsi0', size=f"{self.cfg['disk_gb']}G")
                if task:
                    self.wait_task(task)
            record['phase'] = 'configured'
            self.save()
            self.changed = True
        if self.api.call('GET', path + '/status/current')['status'] != 'running':
            self.progress('Starting VM')
            self.wait_task(self.api.call('POST', path + '/status/start'))
            self.changed = True
        if record['phase'] == 'configured':
            self.poll('Guest Agent on ' + record['name'], lambda: self.boot_id(record))
            # No DHCP address is needed yet: Cloud-Init is awaited over authenticated QGA.
            self.progress('Waiting for Cloud-Init before reboot')
            self.wait_cloud_init(record)
            self.clean_network_identity(record)
            record['boot_id_before'] = self.boot_id(record)
            record['phase'] = 'rebooting'
            self.save()
        if record['phase'] == 'rebooting':
            current_boot = self.poll('Guest Agent before reboot', lambda: self.boot_id(record))
            if current_boot == record['boot_id_before']:
                # Graceful reboot; never reset/power-cycle an existing guest.
                self.progress('Requesting graceful reboot')
                self.wait_task(self.api.call('POST', path + '/status/reboot'))
                self.changed = True
            self.poll('a new boot ID on ' + record['name'],
                      lambda: self.boot_id(record) != record['boot_id_before'])
            self.wait_cloud_init(record)
            record['phase'] = 'ready'
            self.save()
        if record['phase'] != 'ready':
            raise FactoryError('Unknown VM lifecycle phase; inspect the manifest.')
        # Discover the final address only after the fresh VM has restarted.
        self.progress('Discovering final DHCP address in ' + str(self.lan))
        record['ip'] = self.poll('final LAN address on ' + record['name'], lambda: select_ip(
            self.api.call('GET', path + '/agent/network-get-interfaces')['result'],
            self.lan, self.lb_start, self.lb_stop))
        record['machine_id'] = self.guest_exec(record, ['/bin/cat', '/etc/machine-id'], retry_lost=True).strip()
        if not re.fullmatch(r'[0-9a-f]{32}', record['machine_id']):
            raise FactoryError('Guest machine-id was not regenerated correctly after reboot.')
        if any(node is not record and node.get('machine_id') == record['machine_id'] for node in self.manifest['nodes'].values()):
            raise FactoryError('Duplicate machine-id after reboot; inspect the template identity settings.')
        keys = self.guest_exec(record, ['/bin/sh', '-c', 'cat /etc/ssh/ssh_host_*_key.pub'], retry_lost=True).splitlines()
        record['host_keys'] = [key.strip() for key in keys if re.match(r'^(ssh-ed25519|ssh-rsa|ecdsa-sha2-\S+) [A-Za-z0-9+/=]+(?: |$)', key)]
        if not record['host_keys']:
            raise FactoryError('Guest has no readable SSH host public keys.')
        self.save()
        self.progress('Ready at ' + record['ip'])

    def run(self):
        with (self.state_dir / '.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise FactoryError('Another factory run is active for this cluster.') from None
            return self._run_locked()

    def _run_locked(self):
        self.progress('Checking fresh factory request and Proxmox template')
        desired_names = {name for name, _ in self.desired}
        conflicts = sorted(vm['name'] for vm in self.inventory_vms() if vm.get('name') in desired_names)
        if conflicts:
            raise FactoryError('Fresh factory requires unused VM names. Existing: ' + ', '.join(conflicts)
                               + '. Remove the previous VMs yourself before running again; no VM deleted or adopted.')
        template = self.api.call('GET', f"{self.base}/{self.cfg['template_id']}/config")
        if not template.get('template') or template.get('cicustom') or not template.get('net0') or not any('cloudinit' in str(v) for k, v in template.items() if re.fullmatch(r'(ide|scsi|sata)\d+', k)):
            raise FactoryError('Source must be a Cloud-Init template with net0, scsi0, and no cicustom override.')
        if self.cfg['disk_gb'] < disk_size_gib(template):
            raise FactoryError('Requested disk is smaller than the template.')
        # Prior records never constrain a fresh request. Keep them as audit history.
        old_artifacts = [self.state_dir / name for name in ('manifest.json', 'inventory.json', 'known_hosts')]
        if any(path.exists() for path in old_artifacts):
            history = self.state_dir / 'history' / uuid.uuid4().hex
            history.mkdir(parents=True, mode=0o700)
            for path in old_artifacts:
                if path.exists():
                    os.replace(path, history / path.name)
            self.progress('Archived previous factory records at ' + str(history))
        self.manifest = {'schema': 1, 'mode': 'fresh', 'settings': dict(self.cfg),
                         'owner_id': uuid.uuid4().hex, 'nodes': {}}
        self.save()
        for name, group in self.desired:
            vmid = int(self.api.call('GET', '/cluster/nextid'))
            if vmid == self.cfg['template_id'] or any(vm['vmid'] == vmid for vm in self.manifest['nodes'].values()):
                raise FactoryError('Allocated VMID conflicts with this run; inspect Proxmox.')
            self.manifest['nodes'][name] = {'name': name, 'group': group, 'vmid': vmid,
                'mac': '02:' + ':'.join(f'{part:02x}' for part in uuid.uuid4().bytes[:5]),
                'firmware_uuid': str(uuid.uuid4()),
                'owner': f"homelab-factory:{self.manifest['owner_id']}:{name}", 'phase': 'planned'}
            self.save()
            self.provision(self.manifest['nodes'][name])
        nodes = [self.manifest['nodes'][name] for name, _ in self.desired]
        if len({node['ip'] for node in nodes}) != len(nodes):
            raise FactoryError('Duplicate DHCP addresses across factory nodes.')
        known_hosts = self.state_dir / 'known_hosts'
        content = ''.join(f"{node['ip']} {' '.join(key.split()[:2])}\n" for node in nodes for key in node['host_keys'])
        known_hosts.write_text(content)
        known_hosts.chmod(0o600)
        inventory = {'all': {'children': {group: {'hosts': {node['name']: {
            'ansible_host': node['ip'], 'vm_id': node['vmid'], 'ansible_user': self.cfg['cloud_init_user'],
            'ansible_ssh_private_key_file': node['key_path'],
            'ansible_ssh_common_args': '-o StrictHostKeyChecking=yes -o UserKnownHostsFile=' + shlex.quote(str(known_hosts)),
        } for node in nodes if node['group'] == group}} for group in ('k8s_control_plane', 'k8s_workers')}}}
        inventory['all']['vars'] = {'worker_count': self.cfg['worker_count']}
        inventory_path = self.state_dir / 'inventory.json'
        atomic_json(inventory_path, inventory)
        self._active_node = None
        self.progress(f'All {len(nodes)} VMs ready; generated inventory: {inventory_path}')
        return {'changed': self.changed, 'nodes': [{k: node[k] for k in ('name', 'group', 'vmid', 'ip', 'key_path')} for node in nodes],
                'known_hosts_path': str(known_hosts), 'inventory_path': str(inventory_path)}


def main():
    try:
        cfg = json.load(sys.stdin)
        api = Proxmox(os.environ.get('PROXMOX_VE_ENDPOINT', ''), os.environ.get('PROXMOX_VE_API_TOKEN', ''),
                      os.environ.get('PROXMOX_VE_INSECURE', 'false').lower() in ('true', '1', 'yes'))
        print(json.dumps(Factory(cfg, api).run()))
    except (FactoryError, ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        # subprocess output and API payloads are intentionally excluded.
        message = str(error) if isinstance(error, FactoryError) else f'Factory failed ({type(error).__name__}); inspect retained manifest and VM tasks.'
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
