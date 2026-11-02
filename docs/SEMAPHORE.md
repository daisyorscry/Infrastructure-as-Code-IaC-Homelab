# Semaphore: init cluster dan tambah worker terpisah

Semua template memakai App **Ansible Playbook**, working directory `ansible` bila tersedia. Jangan aktifkan Check Mode atau Limit. Runner memerlukan akses SSH/sudo guest dan persistent mount key factory. VM Factory mengikuti [VM_FACTORY_ANSIBLE.md](VM_FACTORY_ANSIBLE.md).

| Template | Playbook dari root repo | Inventory |
|---|---|---|
| Create VM | `ansible/playbooks/create-vm.yml` | `ansible/inventory/runner.yml` |
| Init Cluster Cilium | `ansible/playbooks/cluster-cilium.yml` | `ansible/inventory/cilium.yml` |
| Init Cluster Istio | `ansible/playbooks/cluster-istio.yml` | `ansible/inventory/istio.yml` |
| Add Workers | `ansible/playbooks/add-workers.yml` | Inventory yang berisi master tujuan dan worker baru |

Hapus survey/Extra vars lama `cluster_operation` dan `join_workers`. Jangan campur variable group init ke template Add Workers. Runner cluster diuji dengan Ansible Core 2.20 / Python 3.13; Core 2.20 memerlukan Python controller >=3.12. Task cluster memakai modul builtin. Helm dipasang otomatis pada master saat init, dengan verifikasi checksum.

## Form init cluster

Kedua template init menggunakan parameter yang sama, dikirim sebagai **Extra variable**:

| Name | Type | Default | Required |
|---|---|---|---|
| `worker_count` | Integer | `4`, minimum `4` | Ya |
| `gateway_hostname` | String | kosong | Tidak |

Satu run membuat cluster dari VM yang sudah tersedia: menyiapkan CRI-O/Kubernetes, init master, join worker awal, memasang CNI/Envoy/Gateway/IP pool/L2, dan memeriksa readiness. Init inventory wajib berisi tepat satu control plane dan setidaknya `worker_count` worker. Jumlah worker tidak membuat VM baru. Worker dipilih berdasarkan urutan nama inventory.

Config dasar di `ansible/inventory/group_vars/all.yml`:

```yaml
cluster_name: homelab
node_lan_cidr: 192.168.10.0/24
lb_start_ip: 192.168.10.10
lb_stop_ip: 192.168.10.20
l2_interfaces: []
```

`l2_interfaces: []` mendeteksi interface tiap worker dari route ke master. Tidak perlu survey NIC dan tidak mengasumsikan nama ens18. Policy L2 dibuat per worker dengan selector hostname; NIC berbeda antarworker didukung. Manual override berupa array nama NIC tetap bisa dipasang pada template init bila dibutuhkan.

Untuk menjalankan Cilium dan Istio bersamaan, gunakan `cluster_name`, VM, dan pool LB yang berbeda. Range `.10–.20` harus di luar DHCP dan belum dipakai perangkat lain. Bisa dibagi, misalnya `.10–.14` dan `.15–.20`, setelah diverifikasi. Pin versi dan CIDR Pod/Service sudah ada di repo. Extra vars template init dapat mengubah config sebelum cluster dibuat.

## Form tambah worker

Template **Add Workers** punya form sendiri, semuanya **Extra variable**:

| Name | Type | Required | Contoh |
|---|---|---|---|
| `master` | String | Ya | `master-1` |
| `workers` | String | Ya | `worker-5,worker-6` |

```json
{"master":"master-1","workers":"worker-5,worker-6"}
```

`master` adalah nama inventory dari grup `k8s_control_plane`; IP dan SSH credential diambil dari inventory. `workers` berisi nama dalam `k8s_workers`. Spasi sekitar nama CSV dibersihkan; array JSON/YAML juga diterima. Nama kosong, duplikat, atau tidak dikenal ditolak. Tidak ada minimum empat worker tambahan.

Playbook membaca profil di `/etc/kubernetes/homelab/profile.json` pada master, memeriksa API/CNI, dan memakai versi runtime/Kubernetes serta mode interface cluster tersebut. Gateway hostname, pool IP, versi chart, dan jumlah worker init tidak perlu diisi ulang. Jika profil memakai auto-detection, interface worker baru juga dideteksi dan mendapat policy L2 sendiri. Tidak ada pemasangan ulang control plane, CNI, atau chart.

Untuk memilih cluster tujuan yang berbeda, ubah `master` ke nama master yang benar pada inventory Add Workers. Ini berlaku untuk worker baru, bukan mengganti master atau memindahkan node existing antarcluster. Tidak ada adopsi cluster tanpa profil, reset node, upgrade, atau migrasi CNI.

## Inventory dan SSH

Contoh init inventory (alamat/VMID hanya ilustrasi):

```yaml
all:
  vars:
    ansible_user: debian
    ansible_ssh_private_key_file: "/var/lib/homelab/vm-keys/vm-{{ vm_id }}"
  children:
    k8s_control_plane:
      hosts:
        master-1: {ansible_host: 192.168.10.101, vm_id: 93101}
    k8s_workers:
      hosts:
        worker-1: {ansible_host: 192.168.10.111, vm_id: 93111}
        worker-2: {ansible_host: 192.168.10.112, vm_id: 93112}
        worker-3: {ansible_host: 192.168.10.113, vm_id: 93113}
        worker-4: {ansible_host: 192.168.10.114, vm_id: 93114}
```

Node boleh DHCP dengan reservation MAC/IP agar `ansible_host` stabil. Semua node harus berada pada LAN yang sesuai dengan VIP LB. Guest memerlukan Python 3, Cloud-Init, iproute2, sudo, kernel >=6.1 pada Debian >=12 atau Ubuntu >=24.04. Siapkan known_hosts runner. Mount key harus terlihat pada runner yang menjalankan task; override path mount lewat environment `VM_SSH_KEY_DIR` jika berbeda. Jika memakai shared SSH key Semaphore, hapus `ansible_ssh_private_key_file` dan pasang credential guest yang sesuai.

Untuk Add Workers, tambahkan host baru ke inventory serta master tujuan, lalu Run template worker. Spawn VM dan join masih Run terpisah; IP hasil factory belum otomatis disinkronkan ke inventory. Hindari task paralel pada cluster yang sama.

## Hasil dan batas flow

Cilium: Cilium CNI, kube-proxy replacement, Cilium Envoy Gateway, native LB IPAM dan L2. Istio: Calico CNI VXLAN/iptables, kube-proxy iptables, Istio Envoy Gateway, MetalLB IPAddressPool/L2Advertisement. Istio tidak menggantikan CNI Calico dan tidak mengaktifkan injeksi sidecar aplikasi otomatis.

Satu control plane per cluster, belum HA. Kubeconfig admin tetap pada master. Kegagalan parsial kubeadm perlu pemeriksaan manual; node tidak dihapus otomatis. Gateway awal HTTP port 80 dan memerlukan HTTPRoute untuk aplikasi. HTTPS/cert-manager/Cloudflare belum termasuk; uji trafik dari LAN setelah memasang route.
