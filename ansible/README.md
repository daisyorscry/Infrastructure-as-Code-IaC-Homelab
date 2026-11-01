# VM Factory

Gunakan **Ansible Playbook** `ansible/playbooks/create-vm.yml` pada Semaphore dengan inventory file `ansible/inventory/runner.yml`. Eksekusi memakai API Proxmox dari runner, tanpa SSH ke host Proxmox/guest, workspace, atau state PostgreSQL.

Form memakai field `vm_name`, `vm_id`, `cores`, `memory_mb`, `disk_gb`, `ipv4`, `gateway`, `vm_tags`, `on_boot`, dan Secret **`vm_password`**. Semua diteruskan sebagai Extra variable. Nama tag input Ansible adalah `vm_tags` karena `tags` merupakan nama khusus Ansible.

Lihat [panduan lengkap Semaphore](../docs/VM_FACTORY_ANSIBLE.md) untuk default, credential, dependency runner, dan perilaku retry. OpenTofu dipending dan tetap disimpan dalam repo; Ansible membuat key ED25519 per VMID ke bind mount persisten runner, yang perlu disiapkan sekali sesuai panduan. Private key bisa diambil melalui SCP, dan tidak dicetak di log.
