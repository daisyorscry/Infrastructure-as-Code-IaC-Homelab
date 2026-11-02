# Homelab IaC dengan Ansible

VM Factory dan flow cluster Kubernetes berada dalam [`ansible/`](ansible/README.md). Folder v4 sudah digabung dan konfigurasi OpenTofu dihapus dari repository.

- [VM Factory aktif di Semaphore](docs/VM_FACTORY_ANSIBLE.md)
- [Inventory dan task cluster di Semaphore](docs/SEMAPHORE.md)

VM Factory tetap memakai flow yang sudah ada. Dua playbook khusus membuat cluster dari awal (1 control plane + minimal 4 worker):

- `cluster-cilium.yml`: CRI-O, Cilium CNI, kube-proxy replacement, Cilium Envoy Gateway, native LB IPAM dan L2 announcement.
- `cluster-istio.yml`: CRI-O, Calico CNI, kube-proxy iptables, Istio Envoy Gateway, MetalLB IPAddressPool dan L2Advertisement.

Tambahkan worker melalui `add-workers.yml` dengan parameter `master` dan `workers`. Konfigurasi runtime/CNI dibaca dari master; tidak perlu mengisi ulang variabel init. Interface L2 dideteksi otomatis per worker dari route menuju master.

Isi inventory dan konfigurasi jaringan sebelum menjalankan. Kedua stack memakai fleet VM terpisah.
