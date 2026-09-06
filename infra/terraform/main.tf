locals {
  resource_group_name = var.resource_group_name != "" ? var.resource_group_name : "rg-${var.environment_name}"
  deployment_name = var.deployment_name != "" ? var.deployment_name : "terraform-${substr(
    replace(lower(var.environment_name), "/[^0-9a-z-]/", "-"),
    0,
    min(54, length(var.environment_name))
  )}"

  reserved_parameter_names = toset([
    "environmentName",
    "location",
    "vnetEnabled",
    "resourceGroupName",
    "mcpEntraApplicationDisplayName",
    "mcpEntraApplicationUniqueName",
    "existingEntraAppId",
    "fabricEnabled",
    "fabricSkuName",
    "fabricAdminEmail",
    "fabricPrivateLinkServiceId",
    "agentIdentityEnabled",
    "developerPrincipalId",
    "developerIpAddress",
    "fabricDataAgentsEnabled",
    "fabricWorkspaceId",
    "defenderEnabled",
    "defenderSecurityContactEmail",
    "defenderSecurityContactPhone",
    "purviewEnabled",
    "searchEnabled"
  ])

  additional_parameters = {
    for name, value in var.template_parameters : name => value
    if !contains(local.reserved_parameter_names, name)
  }

  standard_parameters = {
    environmentName                = var.environment_name
    location                       = var.location
    vnetEnabled                    = var.vnet_enabled
    resourceGroupName              = local.resource_group_name
    mcpEntraApplicationDisplayName = var.mcp_entra_application_display_name
    mcpEntraApplicationUniqueName  = var.mcp_entra_application_unique_name != "" ? var.mcp_entra_application_unique_name : "mcp-oauth-app-${var.environment_name}"
    existingEntraAppId             = var.existing_entra_app_id
    fabricEnabled                  = var.fabric_enabled
    fabricSkuName                  = var.fabric_sku_name
    fabricAdminEmail               = var.fabric_admin_email
    fabricPrivateLinkServiceId     = var.fabric_private_link_service_id
    agentIdentityEnabled           = var.agent_identity_enabled
    developerPrincipalId           = var.developer_principal_id
    developerIpAddress             = var.developer_ip_address
    fabricDataAgentsEnabled        = var.fabric_data_agents_enabled
    fabricWorkspaceId              = var.fabric_workspace_id
    defenderEnabled                = var.defender_enabled
    defenderSecurityContactEmail   = var.defender_security_contact_email
    defenderSecurityContactPhone   = var.defender_security_contact_phone
    purviewEnabled                 = var.purview_enabled
    searchEnabled                  = var.search_enabled
  }

  template_parameters = merge(local.additional_parameters, local.standard_parameters)
  arm_outputs = {
    for name, output in try(jsondecode(azurerm_subscription_template_deployment.main.output_content), {}) :
    name => output.value
  }
}

resource "azurerm_resource_group" "main" {
  name     = local.resource_group_name
  location = var.location

  tags = {
    azd-env-name = var.environment_name
  }
}

resource "azurerm_subscription_template_deployment" "main" {
  name     = local.deployment_name
  location = var.location

  template_content = file("${path.module}/${var.bicep_template_path}")
  parameters_content = jsonencode({
    for name, value in local.template_parameters : name => {
      value = value
    }
  })

  depends_on = [azurerm_resource_group.main]
}