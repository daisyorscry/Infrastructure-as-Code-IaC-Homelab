"""Run the local factory and stream its safe progress messages to Ansible."""
import json
from pathlib import Path
import subprocess
import sys

from ansible.plugins.action import ActionBase


class ActionModule(ActionBase):
    TRANSFERS_FILES = False
    _supports_check_mode = False
    _supports_async = False

    def run(self, tmp=None, task_vars=None):
        result = super().run(tmp, task_vars)
        script = self._task.args.get('script')
        request = self._task.args.get('request')
        if not script or not Path(script).is_file() or not isinstance(request, dict):
            return dict(result, failed=True, changed=False, msg='Factory requires a local script path and request mapping.')
        output = []
        try:
            with subprocess.Popen([sys.executable, '-u', script], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, encoding='utf-8', errors='replace') as process:
                process.stdin.write(json.dumps(request))
                process.stdin.close()
                for line in process.stdout:
                    line = line.rstrip('\n')
                    if line.startswith('[factory] '):
                        if not self._task.no_log:
                            self._display.display(line)
                    else:
                        output.append(line)
                rc = process.wait()
        except OSError:
            return dict(result, failed=True, changed=False, msg='Unable to start the local VM factory.')
        stdout = '\n'.join(output)
        if rc:
            return dict(result, failed=True, changed=False, rc=rc, stdout=stdout,
                        msg=stdout or f'VM factory exited with code {rc}; inspect retained state.')
        try:
            inventory = json.loads(stdout)
            if not isinstance(inventory, dict) or not isinstance(inventory.get('nodes'), list):
                raise ValueError('Expected factory inventory')
        except (ValueError, TypeError):
            return dict(result, failed=True, changed=False, rc=rc, msg='VM factory returned invalid inventory JSON.')
        return dict(result, changed=bool(inventory.get('changed')), rc=rc, stdout=stdout)
