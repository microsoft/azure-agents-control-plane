// Prerequisites must be granted OUTSIDE this deployment. No tenant grants here.
// Configuration MI Graph APPLICATION roles:
// AgentIdentityBlueprint.Create, AgentIdentityBlueprint.Read.All,
// AgentIdentityBlueprint.AddRemoveCreds.All, AgentIdentityBlueprint.UpdateBranding.All
// (FIC PATCH), AgentIdentityBlueprint.UpdateAuthProperties.All,
// AgentIdentityBlueprintPrincipal.Create, AgentIdentityBlueprintPrincipal.Read.All.
// See https://learn.microsoft.com/entra/agent-id/create-blueprint and
// https://learn.microsoft.com/graph/api/federatedidentitycredential-update?view=graph-rest-beta
// Public cloud only. Graph agent FIC typed routes still use beta; lifecycle uses v1.0.
// Serialize deployments for a given deployment key; Graph creation is not transactional.

@description('Display name for the Entra Agent Identity Blueprint')
param blueprintDisplayName string

@minLength(1)
@description('Stable tenant-unique deployment key stored as a blueprint tag; do not change after creation')
param blueprintUniqueName string

@description('Full ARM resource ID of the user-assigned configuration/bootstrap MI, NOT its client ID or name')
param managedIdentityResourceId string

@description('Object/principal ID of the bootstrap MI trusted by the blueprint FIC')
param federatedIdentityPrincipalId string

@description('Existing blueprint client/app ID to adopt explicitly; empty creates/discovers by deployment tag')
param existingBlueprintAppId string = ''

@description('User object IDs for sponsors; at least one user or supported group is required for creation')
param sponsorPrincipalIds array = []

@description('Sponsor group IDs; only supported dynamic membership or Microsoft 365 groups, not security/role-assignable groups')
param sponsorGroupIds array = []

@description('User object IDs for optional owners; groups cannot be owners')
param ownerPrincipalIds array = []

@description('Service principal object IDs for optional owners')
param ownerServicePrincipalIds array = []

@minLength(1)
@description('OAuth2 scope exposed for incoming requests; existing unrelated scopes are preserved')
param agentScopeValue string = 'access_agent'

param tenantId string = tenant().tenantId
param location string = resourceGroup().location
param tags object = {}

@description('Change deliberately to rerun reconciliation after external drift')
param forceUpdateTag string = 'identity-v2'

var configuration = {
  displayName: blueprintDisplayName
  uniqueName: blueprintUniqueName
  tenantId: tenantId
  principalId: federatedIdentityPrincipalId
  existingAppId: existingBlueprintAppId
  sponsorUsers: sponsorPrincipalIds
  sponsorGroups: sponsorGroupIds
  ownerUsers: ownerPrincipalIds
  ownerServicePrincipals: ownerServicePrincipalIds
  scope: agentScopeValue
  cloud: environment().name
}

resource agentBlueprintScript 'Microsoft.Resources/deploymentScripts@2023-08-01' = {
  name: 'ds-agent-blueprint-${uniqueString(blueprintUniqueName)}'
  location: location
  tags: tags
  kind: 'AzurePowerShell'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${managedIdentityResourceId}': {}
    }
  }
  properties: {
    azPowerShellVersion: '12.0'
    timeout: 'PT15M'
    retentionInterval: 'P1D'
    cleanupPreference: 'OnSuccess'
    forceUpdateTag: forceUpdateTag
    environmentVariables: [
      {
        name: 'IDENTITY_CONFIG'
        value: string(configuration)
      }
    ]
    scriptContent: '''
      $ErrorActionPreference = 'Stop'
      $VerbosePreference = 'SilentlyContinue'
      $DebugPreference = 'SilentlyContinue'
      $c = $env:IDENTITY_CONFIG | ConvertFrom-Json
      if ($c.cloud -ne 'AzureCloud') { throw 'Agent ID provisioning currently supports Azure public cloud only.' }
      $tenant = ([guid]$c.tenantId).ToString()
      $principal = ([guid]$c.principalId).ToString()
      $graph = 'https://graph.microsoft.com/v1.0'
      # Deployment Scripts already authenticated Az as the attached user-assigned MI.
      # Do not use Connect-MgGraph -Identity, which may select a different identity.
      try {
        $access = (Get-AzAccessToken -ResourceUrl 'https://graph.microsoft.com' -TenantId $tenant -ErrorAction Stop).Token
        if ($access -is [securestring]) { $access = [pscredential]::new('token', $access).GetNetworkCredential().Password }
      } catch { throw 'Configuration MI Graph authentication failed; check its pregranted application roles.' }
      $headers = @{ Authorization = "Bearer $access"; 'OData-Version' = '4.0' }
      function Invoke-Graph([string]$Method, [string]$Uri, $Body = $null) {
        if (-not $Uri.StartsWith('https://graph.microsoft.com/')) { throw 'Unexpected Graph URL.' }
        $request = @{ Method = $Method; Uri = $Uri; Headers = $headers; TimeoutSec = 30; MaximumRedirection = 0; ErrorAction = 'Stop' }
        if ($null -ne $Body) { $request.Body = ConvertTo-Json -InputObject $Body -Depth 20 -Compress; $request.ContentType = 'application/json' }
        try { Invoke-RestMethod @request }
        catch { throw 'Blueprint Graph operation failed. Check configuration MI permissions and Entra logs; rerun to reconcile partial provisioning.' }
      }
      function Get-Collection([string]$Uri) {
        $pages = 0
        while ($Uri) {
          if (++$pages -gt 100) { throw 'Graph pagination limit exceeded.' }
          $page = Invoke-Graph GET $Uri
          if ($null -eq $page.value) { throw 'Invalid Graph collection response.' }
          $page.value
          $Uri = $page.'@odata.nextLink'
        }
      }
      function Get-Bindings($Ids, [string]$Kind) {
        foreach ($id in $Ids) { "$graph/$Kind/$(([guid]$id).ToString())" }
      }
      if ($c.existingAppId) {
        $appId = ([guid]$c.existingAppId).ToString()
        $app = Invoke-Graph GET "$graph/applications(appId='$appId')/microsoft.graph.agentIdentityBlueprint"
      } else {
        # Graph uniqueName is read-only except through the separate upsert API,
        # which requires broader ReadWrite.All. A deployment tag permits normal
        # typed POST creation with AgentIdentityBlueprint.Create instead.
        $deploymentTag = "azure-agents-control-plane:$($c.uniqueName)"
        $apps = @(Get-Collection "$graph/applications/microsoft.graph.agentIdentityBlueprint" | Where-Object { $_.tags -ccontains $deploymentTag })
        if ($apps.Count -gt 1) { throw 'Ambiguous blueprint deployment tag; supply an explicit existingBlueprintAppId.' }
        if ($apps.Count -eq 1) { $app = $apps[0] }
        else {
          # Do not duplicate a legacy deployment that omitted the deployment tag.
          $filter = [uri]::EscapeDataString("displayName eq '$($c.displayName.Replace("'", "''"))'")
          $legacy = @(Get-Collection "$graph/applications/microsoft.graph.agentIdentityBlueprint?`$filter=$filter")
          if ($legacy.Count -gt 0) { throw 'Blueprint name already exists. Supply existingBlueprintAppId to explicitly adopt it.' }
          $sponsors = @(Get-Bindings $c.sponsorUsers 'users') + @(Get-Bindings $c.sponsorGroups 'groups')
          if ($sponsors.Count -eq 0) { throw 'At least one valid blueprint sponsor is required.' }
          $body = @{
            '@odata.type' = '#microsoft.graph.agentIdentityBlueprint'
            displayName = $c.displayName
            tags = @($deploymentTag)
            'sponsors@odata.bind' = $sponsors
          }
          $owners = @(Get-Bindings $c.ownerUsers 'users') + @(Get-Bindings $c.ownerServicePrincipals 'servicePrincipals')
          if ($owners.Count -gt 0) { $body['owners@odata.bind'] = $owners }
          $app = Invoke-Graph POST "$graph/applications/microsoft.graph.agentIdentityBlueprint" $body
        }
      }
      if (-not $app.id -or -not $app.appId) { throw 'Blueprint response is missing required IDs.' }
      $objectId = ([guid]$app.id).ToString()
      $appId = ([guid]$app.appId).ToString()

      # Reconcile every step on BOTH create and reuse paths; never swallow errors.
      $filter = [uri]::EscapeDataString("appId eq '$appId'")
      $principals = @(Get-Collection "$graph/servicePrincipals/microsoft.graph.agentIdentityBlueprintPrincipal?`$filter=$filter")
      if ($principals.Count -gt 1) { throw 'Ambiguous blueprint principal.' }
      if ($principals.Count -eq 0) {
        $sp = Invoke-Graph POST "$graph/servicePrincipals/microsoft.graph.agentIdentityBlueprintPrincipal" @{ appId = $appId }
      } else { $sp = $principals[0] }
      if (-not $sp.id) { throw 'Blueprint principal response is missing its ID.' }

      $ficUri = "https://graph.microsoft.com/beta/applications/$objectId/microsoft.graph.agentIdentityBlueprint/federatedIdentityCredentials"
      $credentials = @(Get-Collection $ficUri)
      $existing = @($credentials | Where-Object { $_.name -ceq 'mcp-agent-msi' })
      if ($existing.Count -gt 1) { throw 'Ambiguous managed identity FIC.' }
      $desired = @{ issuer = "https://login.microsoftonline.com/$tenant/v2.0"; subject = $principal; audiences = @('api://AzureADTokenExchange') }
      if ($existing.Count -eq 0) {
        $desired.name = 'mcp-agent-msi'
        $null = Invoke-Graph POST $ficUri $desired
      } elseif ($existing[0].issuer -cne $desired.issuer -or $existing[0].subject -cne $desired.subject -or @($existing[0].audiences).Count -ne 1 -or $existing[0].audiences[0] -cne $desired.audiences[0]) {
        $null = Invoke-Graph PATCH "$ficUri/$($existing[0].id)" $desired
      }

      $app = Invoke-Graph GET "$graph/applications/$objectId/microsoft.graph.agentIdentityBlueprint"
      $scopes = @($app.api.oauth2PermissionScopes | Where-Object { $null -ne $_ })
      $matching = @($scopes | Where-Object { $_.value -ceq $c.scope })
      if ($matching.Count -gt 1) { throw 'Ambiguous blueprint OAuth scope.' }
      $changed = $false
      if ($matching.Count -eq 0) {
        $scopes += @{
          id = [guid]::NewGuid().ToString(); value = $c.scope; type = 'User'; isEnabled = $true
          adminConsentDisplayName = 'Access Agent'
          adminConsentDescription = 'Access the agent on behalf of the signed-in user.'
        }
        $changed = $true
      } elseif (-not $matching[0].isEnabled) { throw 'Existing agent OAuth scope is disabled; an administrator must review it.' }
      $identifierUri = "api://$appId"
      $uris = @($app.identifierUris)
      if ($uris -notcontains $identifierUri) { $uris += $identifierUri; $changed = $true }
      if ($changed) {
        $null = Invoke-Graph PATCH "$graph/applications/$objectId/microsoft.graph.agentIdentityBlueprint" @{
          identifierUris = $uris; api = @{ oauth2PermissionScopes = $scopes }
        }
      }
      # Only non-secret identifiers are exported. Ownership/sponsors of an adopted
      # blueprint are intentionally not overwritten by an infrastructure rerun.
      $DeploymentScriptOutputs = @{
        blueprintAppId = $appId; blueprintObjectId = $objectId
        blueprintPrincipalId = $sp.id; identifierUri = $identifierUri
      }
    '''
  }
}

output blueprintAppId string = agentBlueprintScript.properties.outputs.blueprintAppId
output blueprintObjectId string = agentBlueprintScript.properties.outputs.blueprintObjectId
output blueprintPrincipalId string = agentBlueprintScript.properties.outputs.blueprintPrincipalId
output identifierUri string = agentBlueprintScript.properties.outputs.identifierUri
