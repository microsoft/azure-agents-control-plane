#!/usr/bin/env bash
set -euo pipefail

ACTION="apply"
ENVIRONMENT_NAME="${AZURE_ENV_NAME:-}"
LOCATION="${AZURE_LOCATION:-}"
VNET_ENABLED=""
VAR_FILE=""
AUTO_APPROVE="false"
SKIP_POSTPROVISION="false"

usage() {
  cat <<'EOF'
Usage: ./infra/terraform/deploy.sh [validate|plan|apply|destroy] [options]

Options:
  --environment NAME       Override AZURE_ENV_NAME.
  --location LOCATION      Override AZURE_LOCATION.
  --vnet-enabled BOOL      Set VNet deployment to true or false.
  --var-file PATH          Pass a Terraform variable file.
  --auto-approve           Skip approval for apply or destroy.
  --skip-postprovision     Do not run the existing azd post-provision hook.
EOF
}

if [[ $# -gt 0 && "$1" != --* ]]; then
  ACTION="$1"
  shift
fi

case "$ACTION" in
  validate|plan|apply|destroy) ;;
  *)
    usage
    exit 2
    ;;
esac

while [[ $# -gt 0 ]]; do
  case "$1" in
    --environment)
      ENVIRONMENT_NAME="$2"
      shift 2
      ;;
    --location)
      LOCATION="$2"
      shift 2
      ;;
    --vnet-enabled)
      VNET_ENABLED="$2"
      shift 2
      ;;
    --var-file)
      VAR_FILE="$2"
      shift 2
      ;;
    --auto-approve)
      AUTO_APPROVE="true"
      shift
      ;;
    --skip-postprovision)
      SKIP_POSTPROVISION="true"
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 2
      ;;
  esac
done

for command in az terraform; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "Required command '$command' was not found." >&2
    exit 1
  fi
done

SCRIPT_DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIRECTORY="$(cd "$SCRIPT_DIRECTORY/.." && pwd)"
REPOSITORY_ROOT="$(cd "$INFRA_DIRECTORY/.." && pwd)"
GENERATED_DIRECTORY="$SCRIPT_DIRECTORY/generated"
GENERATED_TEMPLATE="$GENERATED_DIRECTORY/main.json"

get_azd_value() {
  azd env get-value "$1" 2>/dev/null | tr -d '"' || true
}

mkdir -p "$GENERATED_DIRECTORY"
az bicep build --file "$INFRA_DIRECTORY/main.bicep" --outfile "$GENERATED_TEMPLATE"
terraform -chdir="$SCRIPT_DIRECTORY" init

if [[ "$ACTION" == "validate" ]]; then
  terraform -chdir="$SCRIPT_DIRECTORY" validate
  exit 0
fi

if ! command -v azd >/dev/null 2>&1; then
  echo "Required command 'azd' was not found." >&2
  exit 1
fi

if [[ -z "$ENVIRONMENT_NAME" ]]; then
  ENVIRONMENT_NAME="$(get_azd_value AZURE_ENV_NAME)"
fi
if [[ -z "$LOCATION" ]]; then
  LOCATION="$(get_azd_value AZURE_LOCATION)"
fi
if [[ -z "$VNET_ENABLED" ]]; then
  VNET_ENABLED="$(get_azd_value VNET_ENABLED)"
fi
VNET_ENABLED="${VNET_ENABLED:-true}"

if [[ -z "$ENVIRONMENT_NAME" || -z "$LOCATION" ]]; then
  echo "Environment and location are required. Pass them explicitly or select an azd environment." >&2
  exit 1
fi
if [[ "$VNET_ENABLED" != "true" && "$VNET_ENABLED" != "false" ]]; then
  echo "--vnet-enabled must be 'true' or 'false'." >&2
  exit 1
fi

SUBSCRIPTION_ID="${ARM_SUBSCRIPTION_ID:-$(az account show --query id --output tsv)}"
if [[ -z "$SUBSCRIPTION_ID" ]]; then
  echo "Unable to determine the Azure subscription. Run 'az login' or set ARM_SUBSCRIPTION_ID." >&2
  exit 1
fi

terraform_arguments=(
  "-var=subscription_id=$SUBSCRIPTION_ID"
  "-var=environment_name=$ENVIRONMENT_NAME"
  "-var=location=$LOCATION"
  "-var=vnet_enabled=$VNET_ENABLED"
  "-var=bicep_template_path=generated/main.json"
)

add_azd_variable() {
  local azd_name="$1"
  local terraform_name="$2"
  local value
  value="$(get_azd_value "$azd_name")"
  if [[ -n "$value" ]]; then
    terraform_arguments+=("-var=$terraform_name=$value")
  fi
}

add_azd_variable FABRIC_ENABLED fabric_enabled
add_azd_variable FABRIC_SKU fabric_sku_name
add_azd_variable FABRIC_ADMIN_EMAIL fabric_admin_email
add_azd_variable FABRIC_PRIVATE_LINK_SERVICE_ID fabric_private_link_service_id
add_azd_variable AGENT_IDENTITY_ENABLED agent_identity_enabled
add_azd_variable DEVELOPER_PRINCIPAL_ID developer_principal_id
add_azd_variable DEVELOPER_IP_ADDRESS developer_ip_address
add_azd_variable FABRIC_DATA_AGENTS_ENABLED fabric_data_agents_enabled
add_azd_variable FABRIC_WORKSPACE_ID fabric_workspace_id
add_azd_variable DEFENDER_ENABLED defender_enabled
add_azd_variable DEFENDER_SECURITY_CONTACT_EMAIL defender_security_contact_email
add_azd_variable DEFENDER_SECURITY_CONTACT_PHONE defender_security_contact_phone
add_azd_variable PURVIEW_ENABLED purview_enabled
add_azd_variable SEARCH_ENABLED search_enabled

if [[ -n "$VAR_FILE" ]]; then
  terraform_arguments+=("-var-file=$VAR_FILE")
fi
if [[ "$AUTO_APPROVE" == "true" && ( "$ACTION" == "apply" || "$ACTION" == "destroy" ) ]]; then
  terraform_arguments+=("-auto-approve")
fi

terraform -chdir="$SCRIPT_DIRECTORY" "$ACTION" "${terraform_arguments[@]}"

if [[ "$ACTION" != "apply" ]]; then
  exit 0
fi

if command -v python3 >/dev/null 2>&1; then
  PYTHON_COMMAND="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_COMMAND="python"
else
  echo "Required command 'python3' or 'python' was not found." >&2
  exit 1
fi

terraform -chdir="$SCRIPT_DIRECTORY" output -json azd_environment \
  | "$PYTHON_COMMAND" "$SCRIPT_DIRECTORY/export_azd_env.py"

if [[ "$SKIP_POSTPROVISION" != "true" ]]; then
  (
    cd "$REPOSITORY_ROOT"
    "$INFRA_DIRECTORY/hooks/postprovision.sh"
  )
fi