# VM Factory Ansible di Semaphore

Flow aktif: **isi form → cek input/template/VMID → generate/simpan SSH key → full clone → Cloud-Init → resize disk → start → tampilkan IP jika Guest Agent siap**. Satu Run membuat satu VM baru. Tidak memerlukan workspace, JSON daftar VM, atau PostgreSQL state. Konfigurasi OpenTofu sudah dihapus dari repo; gunakan task Ansible untuk provisioning berikutnya.

## Task Template

Gunakan template Ansible di bawah. Task OpenTofu lama di Semaphore tidak lagi memakai konfigurasi dari repo ini.

| Field | Value |
|---|---|
| Name | Create VM |
| App | Ansible Playbook |
| Repository / branch | Repo ini / `main` |
| Playbook | `ansible/playbooks/create-vm.yml` (path dari root repo) |
| Working directory jika tersedia | `ansible` |
| Inventory | File `ansible/inventory/runner.yml` |
| Variable Group | Group koneksi Proxmox yang sudah ada |
| Runner | Runner pada server ansible-opentofu |
| SSH guest credential | Tidak diperlukan untuk create; playbook memakai API dari localhost runner |

### Membuat inventory di UI

Buat Inventory bernama `runner-local`. Jika tersedia opsi inventory dari repository, pilih repo ini dan path `ansible/inventory/runner.yml`. Alternatif yang tidak memerlukan path checkout adalah **Static inventory** dengan format YAML, lalu isi:

```yaml
all:
  hosts:
    localhost:
      ansible_connection: local
      ansible_python_interpreter: "{{ ansible_playbook_python }}"
```

Pilih inventory `runner-local` pada template Create VM. Tidak diperlukan SSH guest credential untuk eksekusi localhost ini; Deploy Key GitHub tetap dipakai pada konfigurasi Repository.

Tidak perlu memilih workspace. Jangan mengaktifkan Ansible Check Mode/Dry Run: modul clone/resize tidak menyediakan Plan yang setara OpenTofu, dan playbook akan menolaknya. Review isian sebelum menjalankan task.

Runner memerlukan Python >=3.11, Ansible Core >=2.17, dan binary `ssh-keygen` (paket OpenSSH client). Pasang dependency `ansible/requirements.txt` (`proxmoxer`, `requests`) pada Python yang dipakai Ansible di image runner. Requirements collection disediakan pada `ansible/collections/requirements.yml` dan `collections/requirements.yml`; pastikan Semaphore memasang collection atau sudah tersedia di image. File Python requirements tidak otomatis dipasang hanya karena berada di repository.

Contoh instalasi saat membangun/menyiapkan environment Python runner:

```bash
python -m pip install -r ansible/requirements.txt
ansible-galaxy collection install -r ansible/collections/requirements.yml
```

Gunakan interpreter/environment Ansible runner yang sebenarnya, bukan Python host jika runner ada di container. Dependency sebaiknya masuk ke image agar tetap tersedia saat container dibuat ulang.

## Variable Group

Nama koneksi OpenTofu yang sudah dibuat tetap digunakan, sehingga tidak perlu membuat token baru:

| Name | Type | Value |
|---|---|---|
| `PROXMOX_VE_ENDPOINT` | Environment | `https://IP_PROXMOX:8006/` |
| `PROXMOX_VE_API_TOKEN` | Secret environment | `user@realm!token=secret` |
| `PROXMOX_VE_INSECURE` | Environment | `true` hanya untuk sertifikat lab yang belum valid |
| `VM_SSH_PUBLIC_KEY` | Environment, opsional | Public key operator tambahan selain key per VM |
| `VM_SSH_KEY_DIR` | Environment, opsional | Path key store di dalam runner; default `/var/lib/homelab/vm-keys` |

Jika `VM_SSH_PUBLIC_KEY` kosong, public key tambahan lama dari `TF_VAR_ssh_public_key` juga diterima. Tanpa keduanya, guest tetap mendapat public key yang digenerate otomatis per VM. Template sebaiknya dibersihkan dari credential/key lama.

`PG_CONN_STR` dan semua `TF_VAR_*` spesifikasi VM tidak dibutuhkan task ini. Jangan menghapus database state lama jika sudah ada state/private key OpenTofu yang perlu disimpan.

## Survey Variables

Untuk field biasa, **Pass variable as: Extra variable**, nama tanpa `TF_VAR_`. Khusus Secret `vm_password`, pilih **Environment variable** untuk melewati masalah pengiriman Secret extra variable pada setup runner ini. Nama tetap `vm_password`; playbook membaca environment tersebut.

| Name | Title | Type | Default | Required |
|---|---|---|---|---|
| `vm_name` | Nama VM | String | kosong | Ya |
| `vm_id` | VM ID | Integer | kosong | Ya |
| `cores` | CPU cores | Integer | `4` | Ya |
| `memory_mb` | RAM MB | Integer | `8192` | Ya |
| `disk_gb` | Disk GB | Integer | `30` | Ya |
| `ipv4` | IPv4 / DHCP | String | `dhcp` | Ya |
| `gateway` | Gateway IPv4 | String | kosong | Tidak |
| `vm_tags` | Tags, pisahkan koma | String | kosong | Tidak |
| `on_boot` | Start saat Proxmox boot | Enum | `true` | Ya |
| `vm_password` | Password guest | **Secret** | kosong | **Ya** |

Enum memakai Key `true` / label `Ya` dan Key `false` / label `Tidak`. Untuk DHCP, gateway kosong. Untuk statis, contoh `ipv4=192.168.10.116/24` dan `gateway=192.168.10.1`. Tags berupa `test,debian` dan tag `iac` ditambahkan otomatis.

Default infrastructure ada di `ansible/vars/vm-factory.yml`: node `pve`, template `9201`, storage `nvme`, bridge `vmbr1`, user `debian`. Jika perlu override lewat form, tambahkan survey String `node_name`, Integer `template_id`, String `datastore_id`, String `bridge`, atau String `cloud_init_user`. Environment `TF_VAR_node_name` dan sejenisnya hanya untuk OpenTofu, tidak mengubah konfigurasi Ansible ini.

## Password dan SSH

Password diisi setiap Run melalui Secret survey `vm_password` dengan **Pass variable as: Environment variable** dan diterapkan menggunakan Cloud-Init `cipassword` pada user `cloud_init_user`. Task password memakai `no_log: true` dan tidak menampilkan credential pada ringkasan. Secret masih diproses oleh Semaphore dan Proxmox/Cloud-Init, sehingga akses sistem tersebut tetap perlu dibatasi. Jangan memasukkan password ke default survey, command line, Git, atau log.

Jika field sudah diisi tetapi password tidak sampai ke runner, task menampilkan **boolean saja** pada `password_input_present`: `extra_variable`, `survey_environment`, dan `secret_environment`. Nilai/password maupun panjangnya tidak dicetak. `survey_environment: true` berarti Secret survey environment berhasil diterima. Extra variable lama tetap didukung untuk kompatibilitas.

Alternatif untuk menguji jalur Secret yang sama dengan token Proxmox: tambahkan **Secret environment** `VM_GUEST_PASSWORD` pada Variable Group task ini. Isi melalui UI Semaphore, tanpa mengetiknya dalam command line/Git. Nonaktifkan requirement survey `vm_password` jika sengaja memakai alternatif Variable Group. Password group berlaku untuk run yang menggunakan group itu sampai diubah; untuk password berbeda per Run gunakan survey environment. Jika semua boolean tetap false, periksa Secret/group yang terpasang serta kecocokan versi server Semaphore dan remote runner; ini bukan error credential Proxmox.

Password akun untuk console tidak menjamin SSH password login diizinkan. Kebijakan SSH/Cloud-Init di golden template harus disesuaikan jika ingin login SSH dengan password. Public key operator opsional bisa dipakai untuk SSH tanpa password.

### SSH key otomatis dan penyimpanan persisten

Playbook membuat key **ED25519 per VMID** di key store runner. Private key tidak dicetak ke log, Git, atau state PostgreSQL. Ringkasan hanya menampilkan path dan fingerprint public key. Key tidak memiliki passphrase; batasi akses dan backup folder tersebut. Password guest tetap diisi lewat form dan tidak disimpan sebagai file oleh playbook.

#### Cari Compose runner Semaphore

Container `ansible` di `/opt/automation/ansible/docker-compose.yaml` berbeda dari container runner Semaphore. Mount penyimpanan key wajib dipasang pada runner **`semaphore-runner-runner-1`** yang menjalankan task.

Jalankan di server `ansible-opentofu` untuk melihat lokasi Compose, direktori, project, dan service runner:

```bash
docker inspect semaphore-runner-runner-1 --format '{{ index .Config.Labels "com.docker.compose.project.config_files" }}'
docker inspect semaphore-runner-runner-1 --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}'
docker inspect semaphore-runner-runner-1 --format '{{ index .Config.Labels "com.docker.compose.project" }}'
docker inspect semaphore-runner-runner-1 --format '{{ index .Config.Labels "com.docker.compose.service" }}'
```

Supaya tidak perlu menyalin path dari output terminal, simpan hasilnya sebagai variabel. Gunakan sesi terminal yang sama untuk langkah berikutnya:

```bash
runner_compose_file=$(docker inspect semaphore-runner-runner-1 --format '{{ index .Config.Labels "com.docker.compose.project.config_files" }}')
runner_compose_dir=$(docker inspect semaphore-runner-runner-1 --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}')
runner_project=$(docker inspect semaphore-runner-runner-1 --format '{{ index .Config.Labels "com.docker.compose.project" }}')
runner_service=$(docker inspect semaphore-runner-runner-1 --format '{{ index .Config.Labels "com.docker.compose.service" }}')
```

Perintah edit/recreate di bawah untuk deployment dengan satu file Compose. Jika lokasi file berisi beberapa path dipisahkan koma, gunakan setiap file sebagai opsi `-f` terpisah sesuai urutannya. Jika metadata kosong atau `<no value>`, temukan konfigurasi deployment runner dahulu.

#### Siapkan folder key

**Setup sekali pada host Docker runner**, di luar repository:

```bash
sudo install -d -m 700 /opt/automation/vm-keys
sudo touch /opt/automation/vm-keys/.persistent-key-store
sudo chmod 600 /opt/automation/vm-keys/.persistent-key-store
```

Pastikan direktori dimiliki UID/GID user yang menjalankan proses Ansible di runner. Jika runner tidak menjalankan task sebagai root, sesuaikan ownership folder dan marker dengan `chown UID:GID` menggunakan UID/GID proses runner tersebut.

#### Edit Compose runner

Buka file yang ditemukan tadi (untuk deployment satu file Compose):

```bash
nano "$runner_compose_file"
```

Tambahkan bind mount berikut pada **service runner dalam Compose runner yang sudah ada**, gabungkan dengan volume lain tanpa menghapusnya:

```yaml
services:
  runner:
    volumes:
      - /opt/automation/vm-keys:/var/lib/homelab/vm-keys
```

#### Terapkan perubahan runner

Sesuaikan nama service pada YAML jika bukan `runner`, lalu simpan file. Jalankan saat tidak ada task Semaphore yang aktif karena runner dapat dibuat ulang:

```bash
cd "$runner_compose_dir"
docker compose -p "$runner_project" -f "$runner_compose_file" config --quiet && docker compose -p "$runner_project" -f "$runner_compose_file" up -d "$runner_service"
```

Perintah ini memakai project/file/service runner yang sama. Jangan jalankan pada Compose container `ansible` yang terpisah.

#### Cek mount dan permission

Sebelum Run, pastikan folder host dan marker terlihat di dalam runner:

```bash
docker exec semaphore-runner-runner-1 ls -ld /var/lib/homelab/vm-keys
docker exec semaphore-runner-runner-1 ls -l /var/lib/homelab/vm-keys/.persistent-key-store
```

Lihat mount dan UID/GID proses runner:

```bash
docker inspect semaphore-runner-runner-1 --format '{{ range .Mounts }}{{ println .Source "->" .Destination }}{{ end }}'
docker top semaphore-runner-runner-1 -eo pid,uid,gid,args
```

Pastikan ada `/opt/automation/vm-keys -> /var/lib/homelab/vm-keys`. Jika proses yang menjalankan Ansible bukan root, ganti `UID_RUNNER:GID_RUNNER` berikut dengan angka UID/GID proses tersebut:

```bash
sudo chown UID_RUNNER:GID_RUNNER /opt/automation/vm-keys
sudo chown UID_RUNNER:GID_RUNNER /opt/automation/vm-keys/.persistent-key-store
sudo chmod 700 /opt/automation/vm-keys
sudo chmod 600 /opt/automation/vm-keys/.persistent-key-store
```

Jika ingin menjalankan playbook manual dari container `ansible` terpisah, mount yang sama juga bisa ditambahkan pada service itu. Untuk task Semaphore, mount runner tetap wajib.

Playbook memeriksa direktori mode `0700` dan marker sebelum melakukan provisioning. Marker merupakan penanda bahwa admin sudah menyiapkan mount; playbook tidak dapat menjamin durabilitas storage hanya dari adanya marker. Jangan membuat direktori/marker hanya di filesystem sementara container. User runner perlu dapat menulis ke folder itu.

Untuk VMID `100000`, hasil pada host Docker:

```text
/opt/automation/vm-keys/vm-100000       # private key
/opt/automation/vm-keys/vm-100000.pub   # public key
```

Dari komputer lu, ambil private key tanpa mencetak isinya:

```bash
mkdir -p ~/.ssh
chmod 700 ~/.ssh
umask 077
scp root@IP_SERVER_RUNNER:/opt/automation/vm-keys/vm-100000 ~/.ssh/vm-100000
chmod 600 ~/.ssh/vm-100000
ssh -i ~/.ssh/vm-100000 debian@IP_VM
```

Gunakan akun SSH server yang berhak membaca file tersebut; contoh memakai root. Ganti user guest jika mengubah `cloud_init_user`. Public key `.pub` dimasukkan otomatis melalui Cloud-Init; private key tetap berada di folder persisten host. Menyalin ke komputer tidak menghapus salinan server. Kalau nanti Ansible perlu SSH ke guest, private key bisa ditambahkan terpisah ke Key Store Semaphore; tidak ada sinkronisasi otomatis.

Key disimpan berdasarkan VMID dan **tidak diregenerate otomatis**. Jika clone gagal sebelum VM dibuat, key yang sudah ada akan dipakai ulang saat retry. Jika VMID lama digunakan ulang untuk VM baru setelah VM lama dihapus manual, key lama juga digunakan ulang sampai admin mengarsipkan/mengganti key secara sengaja. Jangan hapus key VM yang masih aktif. Folder ini perlu backup di luar host Docker; bind mount hanya melindungi dari penggantian container, bukan kehilangan disk/server.

## Prasyarat dan hasil

Template harus berupa QEMU template pada node tujuan, memiliki disk `scsi0`, drive Cloud-Init, tanpa `cicustom` override, dan QEMU Guest Agent terpasang. Bersihkan machine-id/host SSH key/lease DHCP sebelum membuat golden template. Disk diminta minimal 30 GiB dan tidak boleh lebih kecil dari disk template yang terbaca. Cloud-Init guest harus mendukung pembesaran filesystem saat disk diperbesar.

Token memerlukan visibility inventory cluster (termasuk VM/container yang sudah ada), baca konfigurasi template, clone, konfigurasi guest/disk/network, resize, dan start pada storage/node tujuan. Playbook mengecek bentrok nama/VMID pada inventory yang terlihat oleh token; Proxmox tetap menentukan konflik VMID saat clone. Jika ada task lain yang mengambil VMID setelah preflight, hasil clone yang tidak membuat VM baru ditolak sebelum konfigurasi.

Sesudah VM start, playbook mencoba membaca IPv4 melalui Guest Agent sekitar satu menit. Jika IP belum ada, provisioning tetap selesai dengan pesan untuk memeriksa Proxmox; hasil itu bukan bukti Cloud-Init selesai atau SSH sudah siap.

Jika ada kegagalan setelah clone, VM dapat tertinggal dalam keadaan stopped atau sebagian terkonfigurasi. Playbook tidak menghapus/reset otomatis. Periksa VM di Proxmox sebelum retry; task create menolak VMID/nama existing, termasuk VM dari run yang terputus. Untuk VM selanjutnya cukup Run lagi dengan nama/VMID baru. Update VM existing perlu task terpisah yang belum disediakan.

Referensi: [modul Proxmox KVM](https://docs.ansible.com/projects/ansible/latest/collections/community/proxmox/proxmox_kvm_module.html), [Survey Variables](https://semaphoreui.com/docs/user-guide/task-templates/survey-vars).
