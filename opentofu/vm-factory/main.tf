# Keep resource addresses keyed by name, as in the original fleet configuration.
# The public interface is now scalar survey fields, with one VM per workspace.
locals {
  vms = {
    (var.vm_name) = {
      vm_id     = var.vm_id
      cores     = var.cores
      memory_mb = var.memory_mb
      disk_gb   = var.disk_gb
      ipv4      = var.ipv4
      gateway   = var.ipv4 == "dhcp" ? null : var.gateway
      tags      = compact([for tag in split(",", var.tags) : trimspace(tag)])
      on_boot   = var.on_boot
    }
  }
}

resource "tls_private_key" "guest" {
  for_each = local.vms

  algorithm = "ED25519"
}

resource "proxmox_virtual_environment_vm" "managed" {
  for_each = local.vms

  name        = each.key
  vm_id       = each.value.vm_id
  node_name   = var.node_name
  description = "Homelab IaC managed VM - ${each.key}"
  tags        = distinct(concat(["iac"], each.value.tags))

  clone {
    vm_id        = var.template_id
    datastore_id = var.datastore_id
    full         = true
  }

  cpu {
    cores = each.value.cores
    type  = "host"
  }

  memory {
    dedicated = each.value.memory_mb
    floating  = 0
  }

  scsi_hardware = "virtio-scsi-single"

  disk {
    interface    = "scsi0"
    datastore_id = var.datastore_id
    size         = each.value.disk_gb
    discard      = "on"
    iothread     = true
  }

  network_device {
    bridge = var.bridge
    model  = "virtio"
  }

  initialization {
    datastore_id = var.datastore_id

    ip_config {
      ipv4 {
        address = each.value.ipv4
        gateway = each.value.ipv4 == "dhcp" ? null : each.value.gateway
      }
    }

    user_account {
      username = var.cloud_init_user
      keys = compact([
        trimspace(tls_private_key.guest[each.key].public_key_openssh),
        trimspace(var.ssh_public_key),
      ])
    }
  }

  agent {
    enabled = true
    wait_for_ip {
      ipv4 = true
    }
  }

  started = true
  on_boot = each.value.on_boot

  # Avoid unplanned reboots on updates; review changes manually.
  reboot_after_update = false

  lifecycle {
    prevent_destroy = true

    precondition {
      condition     = terraform.workspace == "vm-${var.vm_id}"
      error_message = "Select workspace vm-${var.vm_id} in Semaphore. Each workspace manages exactly one VM; default and mismatched workspaces are refused."
    }
  }
}
