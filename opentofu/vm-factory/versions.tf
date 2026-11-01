terraform {
  required_version = ">= 1.13.0, < 1.14.0"

  required_providers {
    tls = {
      source  = "hashicorp/tls"
      version = "= 4.1.0"
    }

    proxmox = {
      source  = "bpg/proxmox"
      version = "= 0.119.0"
    }
  }

  # Persistent shared state, PostgreSQL with advisory locking.
  # Inject PG_CONN_STR through Semaphore Secrets.
  backend "pg" {}
}

provider "proxmox" {}
