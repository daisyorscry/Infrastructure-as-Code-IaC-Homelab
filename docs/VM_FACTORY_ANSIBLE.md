# VM Factory Ansible di Semaphore

Flow aktif: **isi form → cek input/template/VMID → full clone → Cloud-Init → resize disk → start → tampilkan IP jika Guest Agent siap**. Satu Run membuat satu VM baru. Tidak memerlukan workspace, JSON daftar VM, atau PostgreSQL state. OpenTofu dipending; jangan jalankan task OpenTofu untuk VM yang sama.

## Task Template

Sebaiknya buat template baru agar task OpenTofu lama tetap tersimpan sebagai arsip.

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

Tidak perlu memilih workspace. Jangan mengaktifkan Ansible Check Mode/Dry Run: modul clone/resize tidak menyediakan Plan yang setara OpenTofu, dan playbook akan menolaknya. Review isian sebelum menjalankan task.

Runner memerlukan Python >=3.11 dan Ansible Core >=2.17 untuk collection 2.0.0. Pasang dependency `ansible/requirements.txt` (`proxmoxer`, `requests`) pada Python yang dipakai Ansible di image runner. Requirements collection disediakan pada `ansible/collections/requirements.yml` dan `collections/requirements.yml`; pastikan Semaphore memasang collection atau sudah tersedia di image. File Python requirements tidak otomatis dipasang hanya karena berada di repository.

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
| `VM_SSH_PUBLIC_KEY` | Environment, opsional | Public key operator yang dimasukkan ke Cloud-Init |

Jika `VM_SSH_PUBLIC_KEY` kosong, public key tambahan lama dari `TF_VAR_ssh_public_key` juga diterima. Tanpa keduanya, playbook tidak menambahkan key; konfigurasi key yang diwarisi dari template harus diperiksa. Template sebaiknya dibersihkan dari credential/key lama.

`PG_CONN_STR` dan semua `TF_VAR_*` spesifikasi VM tidak dibutuhkan task ini. Jangan menghapus database state lama jika sudah ada state/private key OpenTofu yang perlu disimpan.

## Survey Variables

Untuk semua field, **Pass variable as: Extra variable**, nama tanpa `TF_VAR_`.

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

Default infrastructure ada di `ansible/vars/vm-factory.yml`: node `pve`, template `9201`, storage `nvme`, bridge `vmbr0`, user `debian`. Jika perlu override lewat form, tambahkan survey String `node_name`, Integer `template_id`, String `datastore_id`, String `bridge`, atau String `cloud_init_user`. Environment `TF_VAR_node_name` dan sejenisnya hanya untuk OpenTofu, tidak mengubah konfigurasi Ansible ini.

## Password dan SSH

Password diisi setiap Run melalui Secret survey dan diterapkan menggunakan Cloud-Init `cipassword` pada user `cloud_init_user`. Task password memakai `no_log: true` dan tidak menampilkan credential pada ringkasan. Secret masih diproses oleh Semaphore dan Proxmox/Cloud-Init, sehingga akses sistem tersebut tetap perlu dibatasi. Jangan memasukkan password ke default survey, command line, Git, atau log.

Password akun untuk console tidak menjamin SSH password login diizinkan. Kebijakan SSH/Cloud-Init di golden template harus disesuaikan jika ingin login SSH dengan password. Public key operator opsional bisa dipakai untuk SSH tanpa password.

Flow Ansible ini **belum generate SSH key per VM**: fitur tersebut tetap ada pada OpenTofu yang dipending. Tidak ada private key baru yang disimpan dalam folder sementara runner. Password juga belum digenerate atau dirotasi otomatis.

## Prasyarat dan hasil

Template harus berupa QEMU template pada node tujuan, memiliki disk `scsi0`, drive Cloud-Init, tanpa `cicustom` override, dan QEMU Guest Agent terpasang. Bersihkan machine-id/host SSH key/lease DHCP sebelum membuat golden template. Disk diminta minimal 30 GiB dan tidak boleh lebih kecil dari disk template yang terbaca. Cloud-Init guest harus mendukung pembesaran filesystem saat disk diperbesar.

Token memerlukan visibility inventory cluster (termasuk VM/container yang sudah ada), baca konfigurasi template, clone, konfigurasi guest/disk/network, resize, dan start pada storage/node tujuan. Playbook mengecek bentrok nama/VMID pada inventory yang terlihat oleh token; Proxmox tetap menentukan konflik VMID saat clone. Jika ada task lain yang mengambil VMID setelah preflight, hasil clone yang tidak membuat VM baru ditolak sebelum konfigurasi.

Sesudah VM start, playbook mencoba membaca IPv4 melalui Guest Agent sekitar satu menit. Jika IP belum ada, provisioning tetap selesai dengan pesan untuk memeriksa Proxmox; hasil itu bukan bukti Cloud-Init selesai atau SSH sudah siap.

Jika ada kegagalan setelah clone, VM dapat tertinggal dalam keadaan stopped atau sebagian terkonfigurasi. Playbook tidak menghapus/reset otomatis. Periksa VM di Proxmox sebelum retry; task create menolak VMID/nama existing, termasuk VM dari run yang terputus. Untuk VM selanjutnya cukup Run lagi dengan nama/VMID baru. Update VM existing perlu task terpisah yang belum disediakan.

Referensi: [modul Proxmox KVM](https://docs.ansible.com/projects/ansible/latest/collections/community/proxmox/proxmox_kvm_module.html), [Survey Variables](https://semaphoreui.com/docs/user-guide/task-templates/survey-vars).
