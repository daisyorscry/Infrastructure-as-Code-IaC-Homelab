
#!/usr/bin/env bash
set -Eeuo pipefail

section() {
    printf '\n=== %s ===\n' "$1"
}

require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "[FAIL] $1 not found"
        exit 1
    fi

    echo "[OK] $1: $(command -v "$1")"
}

section "Semaphore Runner Information"

echo "Task ID    : ${SEMAPHORE_TASK_ID:-local}"
echo "Project ID : ${SEMAPHORE_PROJECT_ID:-local}"
echo "Hostname   : $(hostname)"
echo "User       : $(id -un)"
echo "Directory  : $PWD"

section "Checking Dependencies"

for app in bash ansible ansible-playbook git python3 ssh-keygen; do
    require_command "$app"
done

section "Checking Versions"

ansible-playbook --version | head -n 1
git --version
python3 --version

section "Testing Ansible"

ANSIBLE_HOST_KEY_CHECKING=True ansible \
    -i 'localhost,' \
    all \
    --connection local \
    -m ansible.builtin.ping

section "Final Result"

echo "[PASS] Semaphore Runner is working."
echo "[PASS] Ansible local connection successful."
echo "[PASS] OpenSSH key generator is available."
echo "[PASS] Git is available."
echo "No infrastructure resources were modified."
