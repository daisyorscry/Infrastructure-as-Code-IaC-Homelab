# Semaphore: dua flow cluster

Semua task memakai App **Ansible Playbook**. VM Factory tetap mengikuti [VM_FACTORY_ANSIBLE.md](VM_FACTORY_ANSIBLE.md).

## Template

| Template | Playbook dari root repo | Inventory dari repo |
|---|---|---|
| Create VM | `ansible/playbooks/create-vm.yml` | `ansible/inventory/runner.yml` |
| Cluster Cilium | `ansible/playbooks/cluster-cilium.yml` | `ansible/inventory/cilium.yml` |
| Cluster Istio | `ansible/playbooks/cluster-istio.yml` | `ansible/inventory/istio.yml` |

Gunakan working directory `ansible` jika tersedia. Jangan aktifkan Ansible Check Mode atau Limit. Setiap template cluster menangani init dan penambahan worker melalui mode operasi.

Cluster memerlukan runner Linux dengan Ansible Core (diuji 2.20 / Python 3.13; Core 2.20 memerlukan Python controller >=3.12), OpenSSH client, dan akses SSH/sudo ke VM. Dependency Proxmox/crypto tetap untuk factory. Task cluster hanya memakai modul builtin; Helm dipasang otomatis pada control plane dengan checksum terverifikasi.

## Stack yang dipilih

| Komponen | Cilium | Istio |
|---|---|---|
| Runtime | CRI-O | CRI-O |
| CNI Pod | Cilium native | Calico VXLAN, iptables dataplane |
| Service proxy | Cilium kube-proxy replacement | kube-proxy iptables |
| Gateway API controller / data plane | Cilium / Cilium Envoy | istiod / Istio Envoy |
| Alokasi IP LB | CiliumLoadBalancerIPPool | MetalLB IPAddressPool |
| Pengumuman L2 | CiliumL2AnnouncementPolicy | MetalLB L2Advertisement |

MetalLB tidak dipasang pada stack Cilium. Istio Gateway API membuat deployment/service Envoy otomatis dari resource Gateway; tidak perlu playbook ingress terpisah. Istio di sini memakai sidecar-mode control plane, bukan ambient/ztunnel dan bukan pengganti CNI Calico.

## Isi inventory

Gunakan VM berbeda untuk tiap stack. Pada inventory terkait, isi grup control plane dan worker. Nama host wajib lowercase dan sama dengan nama node yang diinginkan:

```yaml
all:
  vars:
    ansible_user: debian
    ansible_ssh_private_key_file: "/var/lib/homelab/vm-keys/vm-{{ vm_id }}"
  children:
    k8s_control_plane:
      hosts:
        master-1: {ansible_host: 192.168.10.11, vm_id: 93011}
    k8s_workers:
      hosts:
        worker-1: {ansible_host: 192.168.10.21, vm_id: 93021}
        worker-2: {ansible_host: 192.168.10.22, vm_id: 93022}
        worker-3: {ansible_host: 192.168.10.23, vm_id: 93023}
        worker-4: {ansible_host: 192.168.10.24, vm_id: 93024}
```

IP dan VMID ini ilustrasi. Gunakan hasil factory dan IP statis/reservasi DHCP. Mount key harus terlihat pada runner task cluster yang sama. Inventory bawaan mendukung override lokasi mount lewat environment `VM_SSH_KEY_DIR`. Jika memakai key bersama dari Key Store Semaphore, hapus variabel `ansible_ssh_private_key_file` dari inventory dan gunakan credential SSH tersebut. Deploy Key GitHub hanya untuk checkout repo.

Guest memerlukan Python 3, Cloud-Init, sudo yang berfungsi, dan kernel >=6.1 pada Debian >=12 atau Ubuntu >=24.04. Atur known_hosts runner sebelum eksekusi. Password sudo jika diperlukan dikirim lewat credential become Semaphore; jangan simpan di Git.

## Konfigurasi jaringan per template

Default ada pada `ansible/inventory/group_vars/all.yml`. Extra vars template lebih diutamakan. Pisahkan konfigurasi tiap cluster dan gunakan konfigurasi yang sama saat init maupun join. Contoh Extra vars template Cilium:

```json
{
  "cluster_name": "homelab-cilium",
  "node_lan_cidr": "192.168.10.0/24",
  "lb_start_ip": "192.168.10.200",
  "lb_stop_ip": "192.168.10.209",
  "l2_interfaces": ["ens18"]
}
```

Untuk Istio gunakan nama berbeda dan pool berbeda, misalnya `.210` sampai `.219`, setelah diverifikasi kosong dan di luar DHCP/router reservation. Interface memakai nama NIC fisik yang benar pada seluruh worker, bukan regex. Node dan VIP harus berada pada LAN/L2 yang sesuai; CIDR Pod/Service tidak boleh overlap LAN atau satu sama lain. Playbook mengecek overlap dan bentrok pool dengan IP inventory, tetapi tidak dapat memastikan alamat di luar inventory belum dipakai perangkat lain atau DHCP.

Kubernetes dan CRI-O harus memiliki minor yang sama. Versi chart/CRD dipin; periksa dukungan sebelum mengubahnya. Konfigurasi baru tidak diterapkan sebagai upgrade pada cluster existing karena profil disimpan dan dibandingkan sebelum menyiapkan guest.

## Survey operasi

| Name | Type | Pengiriman | Nilai |
|---|---|---|---|
| `cluster_operation` | Enum | Extra variable | `init` atau `join`; default `init` |
| `worker_count` | Integer | Extra variable | Default `4`, minimum `4`; hanya untuk init |
| `workers` | String | Extra variable | Nama host dipisah koma, contoh `worker-5,worker-6`; wajib untuk join |

Pada survey Semaphore, set `workers` **Required=No** agar bisa kosong saat init. Playbook tetap menolaknya jika kosong saat join. Set default `worker_count=4`; field ini boleh tetap tampil saat join karena nilainya diabaikan.

Untuk `init`, inventory wajib berisi tepat 1 control plane dan setidaknya sebanyak `worker_count` worker. Jika `workers` kosong, playbook memilih `worker_count` host pertama berdasarkan urutan nama inventory. Jika `workers` diisi, jumlah nama harus sama dengan `worker_count`. Minimum awal tetap 4. Ringkasan target ditampilkan sebelum akses guest.

Contoh form init: `cluster_operation=init`, `worker_count=6`, `workers` kosong untuk memilih 6 host dari inventory. Bisa juga isi `workers` dengan enam nama tertentu. Parameter jumlah tidak membuat VM; spawn dulu melalui factory dan tambahkan host/IP/VMID ke inventory.

Satu Run menyiapkan dan join worker terpilih, memasang network stack serta Gateway HTTP, lalu menunggu kesiapan cluster.

Untuk `join`, spawn VM lewat Create VM, tambahkan host baru ke inventory terkait, lalu isi form `cluster_operation=join`, `workers=worker-5,worker-6`. `worker_count` diabaikan pada mode join. Bentuk Extra vars yang setara:

```json
{"cluster_operation":"join","workers":"worker-5,worker-6"}
```

`workers` menerima String survey yang dipisah koma atau array YAML/JSON. Spasi di sekitar nama CSV dibersihkan. Nama yang kosong, duplikat, atau tidak ada di inventory ditolak. Parameter lama `join_workers` tetap diterima dalam bentuk array, tetapi tidak boleh diisi bersamaan dengan `workers`.

Tidak ada minimum 4 worker tambahan. Playbook memeriksa profil/API/CNI existing, bootstrap node baru, membuat token singkat, join, mencabut token, lalu mengecek kesiapan. Paket node lama dan add-on tidak dipasang ulang dalam mode ini.

Spawn dan join masih dua Run terpisah. IP dari factory belum disinkronkan otomatis ke inventory repository. Hindari task paralel untuk cluster yang sama.

## Batas eksekusi dan hasil

Satu control plane; belum HA. Tidak ada reset node, adopsi cluster existing tanpa profil, upgrade versi, atau migrasi antar-CNI. Jika kubeadm berhenti di tengah proses, periksa VM sebelum retry. Profil ada di `/etc/kubernetes/homelab/profile.json` dan kubeconfig admin tetap di control plane.

Hasil sukses menampilkan node dan alamat Gateway. Gateway menyediakan HTTP port 80; aplikasi memerlukan HTTPRoute. HTTPS/cert-manager/Cloudflare belum termasuk. Lakukan uji HTTP dari LAN setelah memasang route untuk membuktikan dataplane dan pengumuman L2 berfungsi.

Referensi implementasi: [CRI-O packaging](https://github.com/cri-o/packaging), [Cilium Gateway API](https://docs.cilium.io/en/stable/network/servicemesh/gateway-api/gateway-api/), [Cilium L2](https://docs.cilium.io/en/stable/network/l2-announcements/), [Calico Helm](https://docs.tigera.io/calico/latest/getting-started/kubernetes/helm), [Istio Gateway API](https://istio.io/latest/docs/tasks/traffic-management/ingress/gateway-api/), [MetalLB installation](https://metallb.io/installation/).
