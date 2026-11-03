#!/usr/bin/env python3
"""Proxmox VM lifecycle for cluster init. Standard library only; JSON in/out.

Tokens/passwords come only from the runner environment. No VM is deleted or
adopted. A durable manifest and clone-time ownership marker gate every mutation.
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
    def __init__(self, method, path, status):
        self.status = status
        super().__init__(f'Proxmox {method} {path}: HTTP {status}; check runner connectivity and token permissions.')


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
            raise APIError(method, path, error.code) from None
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

    def poll(self, label, operation):
        deadline = time.monotonic() + self.cfg['timeout']
        while True:
            try:
                result = operation()
                if result:
                    return result
            except APIError as error:
                if error.status not in (500, 503, 595, 596):
                    raise
            if time.monotonic() >= deadline:
                raise FactoryError(f'Timed out waiting for {label}; state retained at {self.manifest_path}.')
            time.sleep(self.cfg['poll_interval'])

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

    def guest_exec(self, record, command):
        path = self.vm_path(record) + '/agent'
        pid = self.api.call('POST', path + '/exec', command=command)['pid']
        result = self.poll('guest command on ' + record['name'],
                           lambda: self._exec_status(path, pid))
        if result.get('exitcode') != 0 or result.get('out-truncated'):
            raise FactoryError(f"Guest command {command[0]} failed on {record['name']}; inspect Cloud-Init/guest logs.")
        return result.get('out-data', '')

    def _exec_status(self, path, pid):
        status = self.api.call('GET', path + '/exec-status', pid=pid)
        return status if status.get('exited') else None

    def boot_id(self, record):
        value = self.guest_exec(record, ['/bin/cat', '/proc/sys/kernel/random/boot_id']).strip()
        if not re.fullmatch(r'[0-9a-f-]{36}', value):
            raise FactoryError('Guest Agent returned an invalid boot ID.')
        return value

    def owned(self, record):
        config = self.api.call('GET', self.vm_path(record) + '/config')
        if config.get('description') != record['owner'] or config.get('name') != record['name']:
            raise FactoryError(f"Ownership mismatch for VMID {record['vmid']}; refusing to modify it.")
        return config

    def key(self, record, create=False):
        path = self.root / f"vm-{record['vmid']}"
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
        if record['phase'] not in ('planned', 'cloning', 'cloned', 'configured', 'rebooting', 'ready'):
            raise FactoryError('Unknown VM lifecycle phase; inspect the manifest before retrying.')
        path = self.vm_path(record)
        existing = [vm for vm in self.inventory_vms() if int(vm['vmid']) == record['vmid']]
        if existing:
            if existing[0].get('node') != self.cfg['node_name'] or existing[0].get('type') != 'qemu':
                raise FactoryError('Recorded VMID belongs to a different node or resource type.')
            self.owned(record)
            if record['phase'] == 'planned':
                raise FactoryError('VM exists before recorded clone submission; refusing adoption.')
            if record['phase'] == 'cloning':
                if not record.get('clone_task'):
                    raise FactoryError('Clone submission was interrupted; inspect VM/task before retrying. No VM adopted.')
                self.wait_task(record['clone_task'])
                record['phase'] = 'cloned'
                self.save()
        elif record['phase'] == 'planned':
            self.key(record, create=True)
            # Persist intent first. An interrupted API response must never cause an unsafe clone retry.
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
        else:
            raise FactoryError(f"Recorded VMID {record['vmid']} is missing; no automatic replacement. Inspect {self.manifest_path}.")
        config = self.owned(record)
        public_key = self.key(record)
        if record['phase'] == 'cloned':
            if self.api.call('GET', path + '/status/current')['status'] != 'stopped':
                raise FactoryError('Unconfigured clone is already running; inspect it before retrying.')
            task = self.api.call('POST', path + '/config', cores=self.cfg['cores'], sockets=1, cpu='host',
                          memory=self.cfg['memory_mb'], balloon=0, onboot=1, agent='enabled=1',
                          net0='virtio,bridge=' + self.cfg['bridge'],
                          ciuser=self.cfg['cloud_init_user'],
                          sshkeys=quote(public_key, safe=''), ipconfig0='ip=dhcp',
                          tags=f"iac;homelab-{self.cfg['stack']}")
            if task:
                self.wait_task(task)
            # Absolute size makes a retry after a successful resize safe.
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
            self.wait_task(self.api.call('POST', path + '/status/start'))
            self.changed = True
        if record['phase'] == 'configured':
            self.poll('Guest Agent on ' + record['name'], lambda: self.boot_id(record))
            # No DHCP address is needed yet: Cloud-Init is awaited over authenticated QGA.
            self.guest_exec(record, ['/usr/bin/cloud-init', 'status', '--wait'])
            record['boot_id_before'] = self.boot_id(record)
            record['phase'] = 'rebooting'
            self.save()
        if record['phase'] == 'rebooting':
            current_boot = self.poll('Guest Agent before reboot', lambda: self.boot_id(record))
            if current_boot == record['boot_id_before']:
                # Graceful reboot; never reset/power-cycle an existing guest.
                self.wait_task(self.api.call('POST', path + '/status/reboot'))
                self.changed = True
            self.poll('a new boot ID on ' + record['name'],
                      lambda: self.boot_id(record) != record['boot_id_before'])
            self.guest_exec(record, ['/usr/bin/cloud-init', 'status', '--wait'])
            record['phase'] = 'ready'
            self.save()
        if record['phase'] != 'ready':
            raise FactoryError('Unknown VM lifecycle phase; inspect the manifest.')
        # Always refresh after reboot and on retry; never trust an old DHCP lease.
        record['ip'] = self.poll('final LAN address on ' + record['name'], lambda: select_ip(
            self.api.call('GET', path + '/agent/network-get-interfaces')['result'],
            self.lan, self.lb_start, self.lb_stop))
        keys = self.guest_exec(record, ['/bin/sh', '-c', 'cat /etc/ssh/ssh_host_*_key.pub']).splitlines()
        record['host_keys'] = [key.strip() for key in keys if re.match(r'^(ssh-ed25519|ssh-rsa|ecdsa-sha2-\S+) [A-Za-z0-9+/=]+(?: |$)', key)]
        if not record['host_keys']:
            raise FactoryError('Guest has no readable SSH host public keys.')
        self.save()

    def run(self):
        with (self.state_dir / '.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise FactoryError('Another factory run is active for this cluster.') from None
            return self._run_locked()

    def _run_locked(self):
        settings = {key: value for key, value in self.cfg.items() if key not in ('timeout', 'poll_interval')}
        # Bind identity to the API endpoint too; state must never target another PVE cluster.
        settings['endpoint'] = os.environ.get('PROXMOX_VE_ENDPOINT', '').rstrip('/')
        if self.manifest_path.exists():
            self.manifest = json.loads(self.manifest_path.read_text())
            if self.manifest.get('schema') != 1 or self.manifest.get('settings') != settings:
                raise FactoryError('Factory settings differ from retained state; no implicit resize/rebuild/expansion. Use add-workers for expansion.')
        else:
            self.manifest = {'schema': 1, 'settings': settings, 'owner_id': uuid.uuid4().hex, 'nodes': {}}
            self.save()
        vms = self.inventory_vms()
        for name, _ in self.desired:
            matches = [vm for vm in vms if vm.get('name') == name]
            recorded = self.manifest['nodes'].get(name)
            if matches and (not recorded or len(matches) != 1 or int(matches[0]['vmid']) != recorded['vmid']):
                raise FactoryError(f'VM name {name} already exists outside this manifest; refusing adoption.')
        template = self.api.call('GET', f"{self.base}/{self.cfg['template_id']}/config")
        if not template.get('template') or template.get('cicustom') or not template.get('net0') or not any('cloudinit' in str(v) for k, v in template.items() if re.fullmatch(r'(ide|scsi|sata)\d+', k)):
            raise FactoryError('Source must be a Cloud-Init template with net0, scsi0, and no cicustom override.')
        if self.cfg['disk_gb'] < disk_size_gib(template):
            raise FactoryError('Requested disk is smaller than the template.')
        for name, group in self.desired:
            if name not in self.manifest['nodes']:
                vmid = int(self.api.call('GET', '/cluster/nextid'))
                if vmid == self.cfg['template_id'] or any(vm['vmid'] == vmid for vm in self.manifest['nodes'].values()):
                    raise FactoryError('Allocated VMID conflicts with retained state; inspect Proxmox.')
                self.manifest['nodes'][name] = {'name': name, 'group': group, 'vmid': vmid,
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
