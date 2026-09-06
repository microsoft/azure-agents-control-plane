# Terraform deployment

Terraform is an optional deployment path. The Bicep files in `infra/` remain
the source of truth, and the root `azure.yaml` continues to make `azd up` use
Bicep by default.

This configuration compiles `infra/main.bicep` and applies the resulting ARM
template through an `azurerm_subscription_template_deployment`. Terraform also
owns the target resource group so that resource-group resources are removed by
`terraform destroy`. Keeping the resource definitions in Bicep avoids a second,
drifting implementation of the same infrastructure.

## Prerequisites

- Azure CLI, authenticated with `az login`
- Azure Developer CLI, with an active environment
- Terraform 1.5 or later
- Python 3.11 or later

Create or select an azd environment before applying. Terraform outputs are
written back to this environment for the existing post-provision hooks.

```bash
az login
azd auth login
azd env new dev
azd env set AZURE_LOCATION eastus2
```

## PowerShell

```powershell
# Review the infrastructure changes.
./infra/terraform/deploy.ps1 -Action plan

# Apply infrastructure, export outputs to azd, and run post-provision.
./infra/terraform/deploy.ps1 -Action apply

# Provision infrastructure without building and deploying the application.
./infra/terraform/deploy.ps1 -Action apply -SkipPostProvision
```

## Bash

```bash
# Review the infrastructure changes.
./infra/terraform/deploy.sh plan

# Apply infrastructure, export outputs to azd, and run post-provision.
./infra/terraform/deploy.sh apply

# Provision infrastructure without building and deploying the application.
./infra/terraform/deploy.sh apply --skip-postprovision
```

Both wrappers read `AZURE_ENV_NAME`, `AZURE_LOCATION`, `VNET_ENABLED`, and the
supported optional feature flags from the active azd environment. Use
`--environment` and `--location` in Bash, or `-EnvironmentName` and `-Location`
in PowerShell, to override the core values.

Copy `terraform.tfvars.example` to `terraform.tfvars` to configure additional
Bicep parameters. Parameters without a typed Terraform variable belong in the
`template_parameters` map using their original camel-case Bicep names. Paths
passed with `--var-file` or `-VarFile` are resolved from `infra/terraform`.

## Validation

The wrapper always recompiles Bicep before planning or applying. Local checks
can also be run directly:

```bash
az bicep build --file infra/main.bicep
terraform -chdir=infra/terraform init -backend=false
terraform -chdir=infra/terraform fmt -check
terraform -chdir=infra/terraform validate
```

Terraform uses local state unless a backend is added by the deployment
environment. Do not commit `terraform.tfstate`, generated templates, or local
`.tfvars` files. The provider lock file is committed for reproducible installs.

## Destruction

```powershell
./infra/terraform/deploy.ps1 -Action destroy
```

```bash
./infra/terraform/deploy.sh destroy
```

Destroying removes the Terraform-owned resource group. Subscription-scoped
settings and external Entra objects created by optional Bicep modules may need
separate cleanup.