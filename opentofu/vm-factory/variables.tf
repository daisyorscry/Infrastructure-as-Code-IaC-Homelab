variable "node_name" {
  type        = string
  description = "Proxmox node hostname"
  default     = "pve"
}

variable "template_id" {
  type        = number
  description = "Golden cloud-init template VM ID; unique machine-id per clone is required"
  default     = 9201
}

variable "datastore_id" {
  type    = string
  default = "nvme"
}

variable "bridge" {
  type    = string
  default = "vmbr0"
}

variable "cloud_init_user" {
  type    = string
  default = "debian"
}

variable "ssh_public_key" {
  type        = string
  sensitive   = true
  default     = ""
  nullable    = false
  description = "Optional additional public SSH key authorized on all guests alongside the generated per-VM key"
}

variable "vm_name" {
  type        = string
  description = "Guest hostname for the single VM managed by this workspace"

  validation {
    condition     = length(var.vm_name) <= 63 && can(regex("^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?$", var.vm_name))
    error_message = "Use a hostname of 1-63 letters, digits or hyphens, starting and ending with a letter or digit."
  }
}

variable "vm_id" {
  type        = number
  description = "Unused Proxmox VMID; select the matching workspace vm-<vm_id> in Semaphore"

  validation {
    condition     = var.vm_id >= 100 && var.vm_id <= 999999999 && floor(var.vm_id) == var.vm_id
    error_message = "VMID must be an integer between 100 and 999999999."
  }
}

variable "cores" {
  type    = number
  default = 4

  validation {
    condition     = var.cores >= 1 && floor(var.cores) == var.cores
    error_message = "CPU cores must be a positive integer."
  }
}

variable "memory_mb" {
  type    = number
  default = 8192

  validation {
    condition     = var.memory_mb >= 1024 && floor(var.memory_mb) == var.memory_mb
    error_message = "Memory must be an integer of at least 1024 MB."
  }
}

variable "disk_gb" {
  type    = number
  default = 30

  validation {
    condition     = var.disk_gb >= 30 && floor(var.disk_gb) == var.disk_gb
    error_message = "Disk must be an integer of at least 30 GB; never shrink below the template disk size."
  }
}

variable "ipv4" {
  type    = string
  default = "dhcp"

  validation {
    condition     = var.ipv4 == "dhcp" || (can(cidrnetmask(var.ipv4)) && can(cidrhost(var.ipv4, 0)))
    error_message = "Use dhcp or an IPv4 address with CIDR, e.g. 192.168.10.116/24."
  }
}

variable "gateway" {
  type        = string
  default     = ""
  description = "Leave blank for DHCP; an IPv4 gateway is required for a static address"

  validation {
    condition     = var.ipv4 == "dhcp" ? trimspace(var.gateway) == "" : can(cidrnetmask("${var.gateway}/32"))
    error_message = "Leave gateway blank for DHCP; for static IPv4 supply a valid gateway address without CIDR."
  }
}

variable "tags" {
  type        = string
  default     = ""
  description = "Optional comma-separated tags for the form, e.g. test,k8s_worker; iac is always added"
}

variable "on_boot" {
  type    = bool
  default = true
}
