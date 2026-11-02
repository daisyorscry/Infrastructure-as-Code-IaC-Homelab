# Contributing

Small, focused improvements are welcome: clearer instructions, reproducible bug fixes, and better cluster validation.

1. Open an issue describing the playbook, expected behavior, and what happened.
2. Fork the repository and make a focused change.
3. Run the local checks below, then open a pull request with the result.

```bash
python -m pip install ansible-core==2.20.0
export ANSIBLE_CONFIG="$PWD/ansible/ansible.cfg"
ansible-playbook -i ansible/inventory/cilium.yml ansible/playbooks/cluster-cilium.yml --syntax-check
ansible-playbook -i ansible/inventory/istio.yml ansible/playbooks/cluster-istio.yml --syntax-check
ansible-playbook -i ansible/inventory/cilium.yml ansible/playbooks/add-workers.yml --syntax-check
python -m unittest discover -s tests -v
```

Checks require Linux and Python 3.12 or later. Tests use temporary inventories and do not contact Proxmox or Kubernetes nodes. Explain any live testing separately, including the guest OS and selected stack. Remove credentials and private keys from examples and logs.
