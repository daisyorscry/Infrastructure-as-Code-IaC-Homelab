# Homelab Ansible

Folder `playbooks/` hanya berisi VM Factory dan **dua entry point cluster**:

| Playbook | Stack |
|---|---|
| `create-vm.yml` | VM Factory Proxmox yang sudah ada |
| `cluster-cilium.yml` | CRI-O + Cilium CNI + kube-proxy replacement + Cilium Envoy Gateway + LB IPAM/L2 bawaan |
| `cluster-istio.yml` | CRI-O + Calico CNI (VXLAN/iptables) + kube-proxy iptables + Istio Envoy Gateway + MetalLB L2 |

Task internal dan manifest berada di `roles/homelab_cluster/`. Tidak ada playbook cluster per tahap lagi. Istio dipakai untuk controller dan gateway Envoy; Calico menyediakan jaringan Pod. Injeksi sidecar aplikasi bersifat opsional melalui label namespace, dan tidak diaktifkan otomatis.

## Init

Spawn VM melalui [VM Factory](../docs/VM_FACTORY_ANSIBLE.md), lalu isi inventory `inventory/cilium.yml` atau `inventory/istio.yml`: **1 control plane dan minimal 4 worker**. Isi `ansible_host` dan `vm_id` tiap host. Inventory memakai key factory pada persistent mount runner. Kedua stack harus memakai VM dan pool IP berbeda.

Isi LAN, range LB di luar DHCP, dan interface fisik di `inventory/group_vars/all.yml` atau Extra vars khusus template Semaphore. VM harus memakai IP statis atau reservasi DHCP, Debian >=12 / Ubuntu >=24.04, kernel >=6.1, Python 3, Cloud-Init, SSH dan sudo. Pastikan SSH host keys sudah dipercaya runner (`host_key_checking=True`).

```bash
# Dari root repo pada runner Linux. Isi inventory dan config lebih dulu.
ANSIBLE_CONFIG=ansible/ansible.cfg ansible-playbook \
  -i ansible/inventory/cilium.yml ansible/playbooks/cluster-cilium.yml \
  -e '{"cluster_operation":"init","worker_count":4}'
```

Untuk Istio, ganti inventory dan playbook menjadi `istio.yml` / `cluster-istio.yml`.

`worker_count` menentukan jumlah worker init (default `4`, minimum `4`). Jika `workers` kosong, dipilih sebanyak itu dari inventory berdasarkan urutan nama. Untuk memilih host sendiri, isi `workers=worker-1,worker-2,worker-3,worker-4`; jumlah namanya harus sama dengan `worker_count`. Parameter ini memilih VM yang sudah ada di inventory, bukan membuat VM baru.

Satu run memeriksa target → menyiapkan node baru dengan CRI-O/Kubernetes → init control plane → join worker terpilih → memasang CNI/controller/pool/L2/Gateway → menunggu node Ready, DNS, daemonset, dan Gateway Programmed. Worker join sebelum CNI dipasang dan sementara berstatus NotReady; playbook melanjutkan pemasangan CNI dalam run yang sama.

## Tambah worker

Spawn VM baru lewat factory, tambahkan ke inventory cluster yang benar, lalu jalankan **playbook yang sama**:

```bash
ANSIBLE_CONFIG=ansible/ansible.cfg ansible-playbook \
  -i ansible/inventory/cilium.yml ansible/playbooks/cluster-cilium.yml \
  -e cluster_operation=join -e workers=worker-5
```

Mode `join` hanya menghubungi control plane dan worker yang dipilih. Worker baru disiapkan dan di-join; paket node lama dan network stack tidak dipasang ulang. Tidak perlu menambah 4 worker lagi. VM Factory dan join adalah dua Run terpisah; belum ada workflow Semaphore yang men-chain spawn dan join otomatis.

Untuk beberapa worker, isi `workers=worker-5,worker-6`. YAML/JSON list juga diterima. `worker_count` hanya berlaku saat init; join memakai semua nama dalam `workers`. Parameter lama `join_workers` berupa list tetap didukung, tetapi tidak boleh digabung dengan `workers`.

Jangan gunakan `--limit` atau check mode; gunakan `workers` untuk pemilihan host. Token join tidak dicetak dan dicabut setelah percobaan join selesai.

## Konfigurasi dan validasi

Versi dipin di `inventory/group_vars/all.yml`. Profil cluster dicatat pada `/etc/kubernetes/homelab/profile.json`; perubahan stack, versi, atau jaringan pada cluster existing akan ditolak. Init ulang dapat melanjutkan pemasangan add-on setelah join, tetapi bukan reset, migrasi CNI, atau upgrade. Kegagalan di tengah kubeadm init atau join dapat memerlukan pemeriksaan manual; VM/node tidak dihapus otomatis.

Gateway awal menyediakan **HTTP port 80**. Tambahkan `HTTPRoute` untuk aplikasi; tanpa route, respons 404 dari gateway adalah wajar. HTTPS, cert-manager, Cloudflare, dan deployment aplikasi belum termasuk kedua flow ini. Pemeriksaan kesiapan Kubernetes tidak membuktikan reachability IP LB dari LAN; uji dari klien LAN setelah memiliki route.

Syntax dan pengujian preflight/manifest lokal, tanpa akses VM:

```bash
ANSIBLE_CONFIG=ansible/ansible.cfg ansible-playbook -i ansible/inventory/cilium.yml ansible/playbooks/cluster-cilium.yml --syntax-check
ANSIBLE_CONFIG=ansible/ansible.cfg ansible-playbook -i ansible/inventory/istio.yml ansible/playbooks/cluster-istio.yml --syntax-check
python -m unittest discover -s tests -v
```

Tes memerlukan Ansible Core, PyYAML, dan Jinja2 pada environment Linux yang sama; diuji dengan Ansible Core 2.20. Lihat [panduan Semaphore](../docs/SEMAPHORE.md) untuk form dan contoh inventory.
