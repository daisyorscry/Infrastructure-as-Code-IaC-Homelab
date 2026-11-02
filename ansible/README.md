# Homelab Ansible

| Playbook | Tugas | Parameter form |
|---|---|---|
| `create-vm.yml` | VM Factory Proxmox | Form factory yang sudah ada |
| `cluster-cilium.yml` | Buat cluster CRI-O + Cilium CNI/KPR + Envoy Gateway + LB IPAM/L2 bawaan | `worker_count`, `gateway_hostname` opsional |
| `cluster-istio.yml` | Buat cluster CRI-O + Calico VXLAN/iptables + kube-proxy + Istio Envoy Gateway + MetalLB | `worker_count`, `gateway_hostname` opsional |
| `add-workers.yml` | Tambah worker ke cluster yang dipilih | `master`, `workers` |

Playbook init dan worker terpisah. Hapus parameter lama `cluster_operation` dan `join_workers` dari template Semaphore. Task internal berada di `roles/homelab_cluster/`.

## Membuat cluster

Spawn VM melalui [VM Factory](../README.md#quick-start), lalu isi `inventory/cilium.yml` atau `inventory/istio.yml`: tepat satu control plane dan setidaknya sebanyak `worker_count` worker. Setiap host punya `ansible_host` dan `vm_id`; inventory memakai key persisten factory. Gunakan VM dan pool LB berbeda untuk kedua cluster.

`worker_count` default/minimum `4`. Playbook memilih sejumlah itu dari grup `k8s_workers` berdasarkan urutan nama. `gateway_hostname` boleh kosong agar Gateway menerima semua hostname. Tidak ada mode operasi atau parameter master/worker join pada form init.

```bash
ANSIBLE_CONFIG=ansible/ansible.cfg ansible-playbook \
  -i ansible/inventory/cilium.yml ansible/playbooks/cluster-cilium.yml \
  -e worker_count=4 -e gateway_hostname=app.example.com
```

Untuk Istio, ganti inventory dan playbook menjadi `istio.yml` / `cluster-istio.yml`. Flow menyiapkan guest, init master, join worker awal, memasang network stack/Gateway, lalu menunggu node Ready dan Gateway Programmed. Worker sementara NotReady sebelum CNI dipasang dalam run yang sama.

Konfigurasi LAN default `192.168.10.0/24` dengan pool LB `192.168.10.10–192.168.10.20`, di luar DHCP. Node boleh DHCP dengan reservasi MAC/IP agar `ansible_host` tetap. Konfigurasi lain dan pin versi ada di `inventory/group_vars/all.yml`; bisa dioverride lewat Extra vars template init.

## Interface L2 otomatis

Default `l2_interfaces: []` berarti otomatis. Ansible membaca `ip -j -4 route get <IP-master>` pada setiap worker, lalu memverifikasi interface tersebut ada dan bukan loopback. Nama berbeda seperti `ens18`, `ens19`, dan `eth0` didukung dalam cluster yang sama.

Setiap worker mendapat L2 policy/advertisement sendiri dengan selector hostname dan interface aktualnya. Saat tambah worker, policy worker baru ikut dibuat tanpa mengganti interface worker lama. Jika perlu manual override pada init, isi `l2_interfaces: [ens18]`; override wajib valid pada semua worker yang dituju dan dicatat dalam profil cluster.

## Menambah worker

Spawn VM baru, tambahkan host/IP/VMID ke inventory, lalu gunakan playbook worker terpisah:

```bash
ANSIBLE_CONFIG=ansible/ansible.cfg ansible-playbook \
  -i ansible/inventory/cilium.yml ansible/playbooks/add-workers.yml \
  -e master=master-1 -e workers=worker-5,worker-6
```

`master` adalah nama host inventory dalam grup `k8s_control_plane`, bukan IP bebas. `workers` adalah nama host dalam `k8s_workers`, dipisah koma atau berupa array YAML/JSON. Inventory boleh memuat beberapa master untuk pemilihan cluster; hanya master dan worker yang dipilih yang dihubungi. Tidak ada minimum empat worker tambahan.

Versi Kubernetes/CRI-O, jenis CNI, mode deteksi interface, dan identitas cluster dibaca dari `/etc/kubernetes/homelab/profile.json` di master. Tidak perlu membawa config Gateway, pool LB, versi chart, atau `worker_count` ke form worker. Playbook menyiapkan worker baru, membuat token singkat, join, mencabut token, membuat L2 advertisement worker baru, dan menunggu worker Ready. Control plane dan chart tidak di-install ulang.

Ini memilih cluster tujuan untuk worker baru; bukan memindahkan worker existing atau mengganti master cluster. Profil wajib berasal dari flow ini. Tidak ada reset, adopsi cluster tanpa profil, upgrade, atau migrasi CNI.

## Prasyarat dan validasi

Runner harus dapat SSH/sudo ke guest dan membaca key persisten. Guest: Debian >=12 atau Ubuntu >=24.04, kernel >=6.1, Python 3, Cloud-Init, dan iproute2. Siapkan known_hosts runner. VM Factory dan init/join tetap Run terpisah; IP belum otomatis ditulis ke inventory. Jangan pakai check mode atau `--limit`.

Gateway awal HTTP port 80; aplikasi memerlukan HTTPRoute. HTTPS/cert-manager/Cloudflare belum termasuk. Uji trafik dari LAN setelah memiliki route; status Ready tidak membuktikan reachability VIP dari klien.

Tes lokal tanpa VM:

```bash
ANSIBLE_CONFIG=ansible/ansible.cfg ansible-playbook -i ansible/inventory/cilium.yml ansible/playbooks/cluster-cilium.yml --syntax-check
ANSIBLE_CONFIG=ansible/ansible.cfg ansible-playbook -i ansible/inventory/istio.yml ansible/playbooks/cluster-istio.yml --syntax-check
ANSIBLE_CONFIG=ansible/ansible.cfg ansible-playbook -i ansible/inventory/cilium.yml ansible/playbooks/add-workers.yml --syntax-check
python -m unittest discover -s tests -v
```

Tes memakai Ansible Core, PyYAML, dan Jinja2 pada environment Linux yang sama. Lihat [tabel playbook dan parameter Semaphore](../README.md#playbooks) untuk konfigurasi form.
