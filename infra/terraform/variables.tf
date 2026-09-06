variable "subscription_id" {
  description = "Azure subscription ID. When null, the AzureRM provider uses ARM_SUBSCRIPTION_ID or the active Azure CLI subscription."
  type        = string
  default     = null
  nullable    = true
}

variable "environment_name" {
  description = "Deployment environment name. This is the same value as AZURE_ENV_NAME in azd."
  type        = string

  validation {
    condition     = length(var.environment_name) >= 1 && length(var.environment_name) <= 64
    error_message = "environment_name must contain between 1 and 64 characters."
  }
}

variable "location" {
  description = "Primary Azure location for the deployment."
  type        = string

  validation {
    condition = contains([
      "australiaeast",
      "eastasia",
      "eastus",
      "eastus2",
      "eastus2euap",
      "northeurope",
      "southcentralus",
      "southeastasia",
      "swedencentral",
      "uksouth",
      "westus2"
    ], var.location)
    error_message = "location must be one of the locations supported by infra/main.bicep."
  }
}

variable "vnet_enabled" {
  description = "Deploy the virtual network and private endpoints."
  type        = bool
  default     = true
}

variable "resource_group_name" {
  description = "Optional resource group name. An empty value uses the Bicep default of rg-<environment_name>."
  type        = string
  default     = ""
}

variable "deployment_name" {
  description = "Optional ARM subscription deployment name."
  type        = string
  default     = ""
}

variable "bicep_template_path" {
  description = "Path to the compiled main.json, relative to this Terraform directory."
  type        = string
  default     = "../main.json"
}

variable "mcp_entra_application_display_name" {
  description = "Display name for the MCP OAuth application."
  type        = string
  default     = "MCP-OAuth-App"
}

variable "mcp_entra_application_unique_name" {
  description = "Unique name for the MCP OAuth application. An empty value uses the Bicep-generated name."
  type        = string
  default     = ""
}

variable "existing_entra_app_id" {
  description = "Existing Entra application client ID used by MCP OAuth."
  type        = string
  default     = "6441e54f-8149-487b-aac4-3a55a049a362"
}

variable "fabric_enabled" {
  description = "Deploy Microsoft Fabric capacity resources."
  type        = bool
  default     = false
}

variable "fabric_sku_name" {
  description = "Microsoft Fabric capacity SKU."
  type        = string
  default     = "F2"

  validation {
    condition = contains([
      "F2", "F4", "F8", "F16", "F32", "F64",
      "F128", "F256", "F512", "F1024", "F2048"
    ], var.fabric_sku_name)
    error_message = "fabric_sku_name must be a supported Fabric capacity SKU."
  }
}

variable "fabric_admin_email" {
  description = "Optional Fabric capacity administrator email address."
  type        = string
  default     = ""
}

variable "fabric_private_link_service_id" {
  description = "Optional resource ID of the Fabric tenant private link service."
  type        = string
  default     = ""
}

variable "agent_identity_enabled" {
  description = "Deploy the preview Entra Agent Identity resources."
  type        = bool
  default     = false
}

variable "developer_principal_id" {
  description = "Optional developer principal ID for local Cosmos DB access."
  type        = string
  default     = ""
}

variable "developer_ip_address" {
  description = "Optional developer IP address for the Cosmos DB firewall."
  type        = string
  default     = ""
}

variable "fabric_data_agents_enabled" {
  description = "Enable Fabric Data Agents resources."
  type        = bool
  default     = false
}

variable "fabric_workspace_id" {
  description = "Optional Fabric workspace ID for data agents."
  type        = string
  default     = ""
}

variable "defender_enabled" {
  description = "Enable Microsoft Defender for Cloud."
  type        = bool
  default     = true
}

variable "defender_security_contact_email" {
  description = "Security contact email for Defender for Cloud. Defender plans are deployed only when this is set."
  type        = string
  default     = ""
}

variable "defender_security_contact_phone" {
  description = "Optional security contact phone number for Defender for Cloud."
  type        = string
  default     = ""
}

variable "purview_enabled" {
  description = "Deploy Microsoft Purview resources."
  type        = bool
  default     = false
}

variable "search_enabled" {
  description = "Deploy Azure AI Search resources."
  type        = bool
  default     = false
}

variable "template_parameters" {
  description = "Additional raw parameters for infra/main.bicep, keyed by the original Bicep parameter name. Typed variables take precedence."
  type        = any
  default     = {}
}