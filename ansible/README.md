# Homelab Ansible

| Playbook | Tugas | Parameter form |
|---|---|---|
| `cluster-cilium.yml` | Buat VM sampai cluster Cilium siap | `cluster_name`, `worker_count`, LAN/LB, `gateway_hostname` opsional |
| `cluster-istio.yml` | Buat VM sampai cluster Calico + Istio siap | `cluster_name`, `worker_count`, LAN/LB, `gateway_hostname` opsional |
| `add-workers.yml` | Join worker VM yang sudah tersedia | `master`, `workers` |

## Membuat VM dan cluster dalam satu Run

Gunakan inventory `inventory/runner-local.yml` untuk kedua template init. Di Semaphore, inventory static juga boleh berisi:

```yaml
all:
  hosts:
    localhost:
      ansible_connection: local
```

Form init mengirim Extra variables, contoh 10 VM untuk development:

```yaml
cluster_name: dev
worker_count: 9
node_lan_cidr: "192.168.1.0/24"
lb_start_ip: "192.168.1.10"
lb_stop_ip: "192.168.1.20"
gateway_hostname: "runvel.dev"
```

Pakai nama variabel persis `gateway_hostname`. Biarkan `l2_interfaces: []` di YAML, bukan string dari survey. `worker_count` default/minimum `4`; master selalu satu dan tidak dihitung sebagai worker. Cilium dan Istio memakai nama VM berbeda. Pool LB kedua stack harus dipisahkan jika aktif bersamaan.

```bash
ANSIBLE_CONFIG=ansible/ansible.cfg ansible-playbook \
  -i ansible/inventory/runner-local.yml ansible/playbooks/cluster-cilium.yml \
  -e cluster_name=dev -e worker_count=9 \
  -e node_lan_cidr=192.168.1.0/24 \
  -e lb_start_ip=192.168.1.10 -e lb_stop_ip=192.168.1.20
```

Untuk Istio ganti playbook menjadi `cluster-istio.yml`. Alurnya:

1. Validasi konfigurasi dan template sebelum membuat VM.
2. Alokasikan VMID kosong, simpan identitas, clone template, dan konfigurasi resource, SSH key, MAC/SMBIOS UUID baru, bridge, serta Cloud-Init DHCP. Tag VLAN/trunk dan tuning NIC dari template dipertahankan.
3. Tunggu Guest Agent dan poll `cloud-init status --format=json` melalui API Proxmox sampai `done`. Query pendek dapat diulang jika Guest Agent kehilangan PID setelah restart; command yang mengubah guest tidak diulang otomatis. Belum memakai IP DHCP untuk SSH.
4. Setelah Cloud-Init selesai, bersihkan cache Cloud-Init, machine-id/D-Bus ID lama, config jaringan yang dihasilkan Cloud-Init, dan lease DHCP pada VM baru. Reboot secara graceful, tunggu boot ID berubah, lalu tunggu Cloud-Init selesai kembali. Verifikasi machine-id hasil boot unik sebelum init Kubernetes. Template sumber tidak diubah.
5. Baca IP final dari Guest Agent. Pilih satu IPv4 dalam `node_lan_cidr`, di luar pool LB; tolak alamat ambigu atau duplikat.
6. Baca SSH host public keys dari Guest Agent, bentuk known_hosts dan inventory, lalu tunggu SSH.
7. Init Kubernetes, join worker, pasang network stack/Gateway, dan tunggu Ready/Programmed.

Bridge dan DHCP sudah harus tersedia. Template wajib punya QEMU Guest Agent yang aktif, Cloud-Init, Python 3, iproute2, SSH, dan sudo tanpa password untuk user Cloud-Init. Token API memerlukan izin clone/config/storage/power serta guest-agent exec dan audit (nama privilege mengikuti versi Proxmox). Endpoint/token memakai environment `PROXMOX_VE_ENDPOINT` dan secret `PROXMOX_VE_API_TOKEN`; `PROXMOX_VE_INSECURE=true` hanya jika memakai TLS self-signed tanpa CA tepercaya.

Default VM: node `pve`, template `9201`, storage `nvme`, bridge `vmbr1`, user `debian`, 4 core, RAM 8192 MiB, disk 30 GiB. Override lewat Extra vars; lihat `roles/homelab_factory/defaults/main.yml`. Factory menggunakan Python standard library dan `ssh-keygen`, tanpa dependensi collection tambahan.

Trunk/VLAN bukan mekanisme meminta IP baru. Jika template memiliki `tag`/`trunks`, factory mempertahankannya; tanpa itu NIC tetap untagged. Bridge/uplink Proxmox harus mendukung VLAN yang digunakan template. Identitas MAC dan machine-id baru membantu DHCP mengenali clone sebagai klien berbeda; server DHCP tetap menentukan alamat dan dapat memakai kembali IP yang sudah bebas. Template sebaiknya sudah dibersihkan dari identitas/lease sebelum dijadikan golden image untuk menghindari bentrok pada boot awal, sebelum factory mendapat akses Guest Agent.

## Fresh provisioning dan catatan persisten

Mount `VM_SSH_KEY_DIR` (default `/var/lib/homelab/vm-keys`), mode `0700`, berisi marker `.persistent-key-store`. Semua runner yang menjalankan cluster yang sama harus memakai volume yang sama. Matikan Allow parallel tasks di template Semaphore.

Key per VM tersimpan sebagai `vm-<VMID>-<run-id>`, sehingga Run baru mendapat key baru walaupun VMID dipakai kembali. Manifest, lock, known_hosts, dan inventory aktual tersimpan di `clusters/<cluster_name>--<stack>/`. Jangan gunakan direktori checkout atau `/tmp` untuk state ini. Path inventory hasil provisioning ditampilkan saat run.

Setiap Run factory membuat fleet baru dari awal. Manifest lama tidak dibaca untuk resume atau dibandingkan dengan form baru. Setelah VM sebelumnya dihapus oleh operator, subnet, pool LB, resource, atau jumlah worker dapat diubah lalu Run kembali. Catatan/inventory lama diarsipkan ke `history/<id>/`; key lama tetap disimpan.

Sebelum clone, factory memeriksa seluruh nama VM yang akan dibuat. Jika salah satu masih ada, Run berhenti dengan daftar nama yang bentrok. Operator membersihkan VM hasil Run sebelumnya terlebih dahulu, termasuk jika provisioning sebelumnya gagal sebagian. Factory tidak menghapus, me-reset, mengadopsi, atau melanjutkan VM existing. Manifest baru hanya mencatat hasil dan tahap Run saat ini. Proses pembuatan VM, Cloud-Init, restart, pembacaan IP, dan init Kubernetes tetap satu alur.

Log `[factory]` menampilkan nama VM, VMID, tahap lifecycle, dan heartbeat setiap 30 detik saat menunggu. Status terakhir juga disimpan sebagai `progress.json` di direktori cluster. `factory_timeout` default 900 detik berlaku per tahap tunggu, bukan total seluruh VM; clone dan provisioning dilakukan berurutan. Timeout menampilkan tahap yang macet dan error API terakhir. Perubahan kode hanya berlaku untuk Run berikutnya, bukan proses yang sudah berjalan.

IP node harus tetap stabil setelah Kubernetes di-init. Gunakan reservasi DHCP berdasarkan MAC VM yang dibuat factory sebelum pemakaian jangka panjang. Jika IP berubah sesudah cluster aktif, perbaiki lease/reservasi; factory bukan alat migrasi endpoint Kubernetes.

## Memakai VM yang sudah ada

Inventory `cilium.yml` dan `istio.yml` sekarang berisi `localhost` dengan default `worker_count: 9`, sehingga masing-masing memicu pembuatan 1 master + 9 worker. `runner-local.yml` juga dapat dipakai; jumlah worker mengikuti default role atau survey. Untuk memakai VM existing tanpa provisioning, sediakan inventory terpisah dengan grup `k8s_control_plane` dan `k8s_workers`, IP/VMID asli, serta konfigurasi SSH. Factory otomatis dilewati ketika grup node sudah terisi.

## Interface L2 otomatis

Default `l2_interfaces: []` berarti otomatis. Ansible membaca `ip -j -4 route get <IP-master>` pada setiap worker, lalu memverifikasi interface tersebut ada dan bukan loopback. Nama berbeda seperti `ens18`, `ens19`, dan `eth0` didukung dalam cluster yang sama.

Setiap worker mendapat L2 policy/advertisement sendiri dengan selector hostname dan interface aktualnya. Saat tambah worker, policy worker baru ikut dibuat tanpa mengganti interface worker lama. Jika perlu manual override pada init, isi `l2_interfaces: [ens18]`; override wajib valid pada semua worker yang dituju dan dicatat dalam profil cluster.

## Menambah worker

Siapkan VM worker baru, lalu gunakan inventory JSON yang diekspor factory sebagai dasar. Tambahkan host/IP/VMID worker baru dan jalankan playbook worker terpisah:

```bash
ANSIBLE_CONFIG=ansible/ansible.cfg ansible-playbook \
  -i /var/lib/homelab/vm-keys/clusters/dev--cilium/inventory.json ansible/playbooks/add-workers.yml \
  -e master=dev-cilium-master-1 -e workers=dev-cilium-worker-10,dev-cilium-worker-11
```

`master` adalah nama host inventory dalam grup `k8s_control_plane`, bukan IP bebas. `workers` adalah nama host dalam `k8s_workers`, dipisah koma atau berupa array YAML/JSON. Inventory boleh memuat beberapa master untuk pemilihan cluster; hanya master dan worker yang dipilih yang dihubungi. Tidak ada minimum empat worker tambahan.

Versi Kubernetes/CRI-O, jenis CNI, mode deteksi interface, dan identitas cluster dibaca dari `/etc/kubernetes/homelab/profile.json` di master. Tidak perlu membawa config Gateway, pool LB, versi chart, atau `worker_count` ke form worker. Playbook menyiapkan worker baru, membuat token singkat, join, mencabut token, membuat L2 advertisement worker baru, dan menunggu worker Ready. Control plane dan chart tidak di-install ulang.

Ini memilih cluster tujuan untuk worker baru; bukan memindahkan worker existing atau mengganti master cluster. Profil wajib berasal dari flow ini. Tidak ada reset, adopsi cluster tanpa profil, upgrade, atau migrasi CNI.

## Prasyarat dan validasi

Runner harus dapat SSH/sudo ke guest dan membaca key persisten. Guest: Debian >=12 atau Ubuntu >=24.04, kernel >=6.1, Python 3, Cloud-Init, dan iproute2. Siapkan known_hosts runner. Factory dan init sudah satu Run; add-workers masih memerlukan VM dan inventory yang tersedia. Jangan pakai check mode atau `--limit`.

Gateway awal HTTP port 80; aplikasi memerlukan HTTPRoute. HTTPS/cert-manager/Cloudflare belum termasuk. Uji trafik dari LAN setelah memiliki route; status Ready tidak membuktikan reachability VIP dari klien.

Tes lokal tanpa VM:

```bash
ANSIBLE_CONFIG=ansible/ansible.cfg ansible-playbook -i ansible/inventory/runner-local.yml ansible/playbooks/cluster-cilium.yml --syntax-check
ANSIBLE_CONFIG=ansible/ansible.cfg ansible-playbook -i ansible/inventory/runner-local.yml ansible/playbooks/cluster-istio.yml --syntax-check
ANSIBLE_CONFIG=ansible/ansible.cfg ansible-playbook -i ansible/inventory/cilium.yml ansible/playbooks/add-workers.yml --syntax-check
python -m unittest discover -s tests -v
```

Tes memakai Ansible Core, PyYAML, dan Jinja2 pada environment Linux yang sama. Lihat [tabel playbook dan parameter Semaphore](../README.md#playbooks) untuk konfigurasi form.
