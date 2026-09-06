output "resource_group_name" {
  description = "Name of the resource group managed by Terraform and populated by the Bicep deployment."
  value       = azurerm_resource_group.main.name
}

output "deployment_outputs" {
  description = "Outputs returned by the canonical Bicep deployment."
  value       = local.arm_outputs
  sensitive   = true
}

output "azd_environment" {
  description = "Bicep outputs plus AZURE_ENV_NAME, ready to export into the active azd environment."
  value = merge(local.arm_outputs, {
    AZURE_ENV_NAME = var.environment_name
  })
  sensitive = true
}