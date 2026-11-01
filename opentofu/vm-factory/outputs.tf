output "managed_vms" {
  description = "Single VM managed in this workspace (never import an existing VM by declaration alone)"
  value = {
    for name, vm in proxmox_virtual_environment_vm.managed : name => {
      vm_id = vm.vm_id
      name  = vm.name
      ipv4  = try(vm.ipv4_addresses, [])
    }
  }
}

output "vm_ssh_public_keys" {
  description = "Generated OpenSSH public key per VM"
  value = {
    for name, key in tls_private_key.guest : name => trimspace(key.public_key_openssh)
  }
}

output "vm_ssh_private_keys" {
  description = "Generated OpenSSH private key per VM; retrieve securely from the same backend/workspace, never in task logs"
  sensitive   = true
  value = {
    for name, key in tls_private_key.guest : name => key.private_key_openssh
  }
}

output "vm_workspace" {
  description = "Workspace to select again for subsequent Plan/Apply or key retrieval"
  value       = terraform.workspace
}
