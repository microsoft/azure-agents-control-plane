// https://learn.microsoft.com/entra/agent-id/create-delete-agent-identities
// https://learn.microsoft.com/graph/api/agentidentity-post?view=graph-rest-1.0
// Prerequisites: blueprint principal exists and trusts this bootstrap MI.
// Blueprint Graph APPLICATION roles must be preconsented by an administrator:
// AgentIdentity.CreateAsManager for parent lifecycle; AgentIdentity.Read.All for
// the collection discovery path. No broad AgentIdentity.Create.All is needed.
// This module NEVER assigns roles/permissions. The MI only obtains an exchange
// assertion; agent Graph operations authenticate as the BLUEPRINT, not the MI.
// Serialize deployment for a blueprint/name pair; Graph displayName is not unique.

@minLength(1)
param agentDisplayName string

@description('Client/app ID of the parent Agent Identity Blueprint')
param blueprintAppId string

@description('Full ARM resource ID of the bootstrap user-assigned managed identity')
param managedIdentityResourceId string

@description('Existing agent object ID (also its client/app ID) to reuse and validate')
param existingAgentIdentityId string = ''

@description('User sponsor object IDs; one valid user or supported group is required for creation')
param sponsorPrincipalIds array = []

@description('Supported dynamic membership or Microsoft 365 sponsor groups; not security/role-assignable groups')
param sponsorGroupIds array = []

param tenantId string = tenant().tenantId
param location string = resourceGroup().location
param tags object = {}
param forceUpdateTag string = 'identity-v2'

var configuration = {
  displayName: agentDisplayName
  blueprintAppId: blueprintAppId
  existingId: existingAgentIdentityId
  sponsorUsers: sponsorPrincipalIds
  sponsorGroups: sponsorGroupIds
  tenantId: tenantId
  cloud: environment().name
}

resource agentIdentityScript 'Microsoft.Resources/deploymentScripts@2023-08-01' = {
  name: 'ds-agent-identity-${uniqueString(blueprintAppId, agentDisplayName)}'
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
      $blueprint = ([guid]$c.blueprintAppId).ToString()
      $graph = 'https://graph.microsoft.com/v1.0'
      try {
        # Az expects a resource, not a /.default scope.
        $assertion = (Get-AzAccessToken -ResourceUrl 'api://AzureADTokenExchange' -TenantId $tenant -ErrorAction Stop).Token
        if ($assertion -is [securestring]) { $assertion = [pscredential]::new('token', $assertion).GetNetworkCredential().Password }
        $form = @{
          client_id = $blueprint; scope = 'https://graph.microsoft.com/.default'
          grant_type = 'client_credentials'
          client_assertion_type = 'urn:ietf:params:oauth:client-assertion-type:jwt-bearer'
          client_assertion = $assertion
        }
        # No fmi_path: provisioning authenticates AS the blueprint, not a child.
        $token = Invoke-RestMethod -Method POST -Uri "https://login.microsoftonline.com/$tenant/oauth2/v2.0/token" -Body $form -ContentType 'application/x-www-form-urlencoded' -TimeoutSec 30 -MaximumRedirection 0 -ErrorAction Stop
        if (-not $token.access_token -or $token.token_type -ine 'Bearer') { throw 'Invalid token response.' }
      } catch { throw 'Blueprint authentication failed. Verify MI federation, blueprint principal and preconsented Graph roles; rerun after propagation.' }
      $headers = @{ Authorization = "Bearer $($token.access_token)"; 'OData-Version' = '4.0' }
      $assertion = $null
      $form = $null
      $token = $null
      function Invoke-Graph([string]$Method, [string]$Uri, $Body = $null) {
        if (-not $Uri.StartsWith('https://graph.microsoft.com/v1.0/')) { throw 'Unexpected Graph URL.' }
        $request = @{ Method = $Method; Uri = $Uri; Headers = $headers; TimeoutSec = 30; MaximumRedirection = 0; ErrorAction = 'Stop' }
        if ($null -ne $Body) { $request.Body = ConvertTo-Json -InputObject $Body -Depth 10 -Compress; $request.ContentType = 'application/json' }
        try { Invoke-RestMethod @request }
        catch { throw 'Agent Graph operation failed. Check blueprint Graph application permissions and Entra logs; no MI Graph fallback is allowed.' }
      }
      if ($c.existingId) {
        $id = ([guid]$c.existingId).ToString()
        $agent = Invoke-Graph GET "$graph/servicePrincipals/$id/microsoft.graph.agentIdentity"
      } else {
        $filter = [uri]::EscapeDataString("displayName eq '$($c.displayName.Replace("'", "''"))'")
        $uri = "$graph/servicePrincipals/microsoft.graph.agentIdentity?`$filter=$filter"
        $matches = @()
        $pages = 0
        while ($uri) {
          if (++$pages -gt 100) { throw 'Graph pagination limit exceeded.' }
          $page = Invoke-Graph GET $uri
          if ($null -eq $page.value) { throw 'Invalid Graph collection response.' }
          $matches += @($page.value | Where-Object { $_.agentIdentityBlueprintId -eq $blueprint })
          $uri = $page.'@odata.nextLink'
        }
        if ($matches.Count -gt 1) { throw 'Multiple agents share this blueprint/name. Supply existingAgentIdentityId explicitly.' }
        if ($matches.Count -eq 1) { $agent = $matches[0] }
        else {
          $sponsors = @(
            foreach ($id in $c.sponsorUsers) { "$graph/users/$(([guid]$id).ToString())" }
            foreach ($id in $c.sponsorGroups) { "$graph/groups/$(([guid]$id).ToString())" }
          )
          if ($sponsors.Count -eq 0) { throw 'At least one valid agent sponsor is required.' }
          $agent = Invoke-Graph POST "$graph/servicePrincipals/microsoft.graph.agentIdentity" @{
            displayName = $c.displayName
            agentIdentityBlueprintId = $blueprint
            'sponsors@odata.bind' = $sponsors
          }
        }
      }
      if (-not $agent.id -or $agent.agentIdentityBlueprintId -ne $blueprint) { throw 'Agent is missing its ID or belongs to a different blueprint.' }
      $id = ([guid]$agent.id).ToString()
      # Agent identities are ServiceIdentity principals. Their id == appId;
      # Graph may omit appId entirely. Do not look for a child application.
      # https://learn.microsoft.com/entra/agent-id/agent-identities#authorizing-agent-identities
      if ($agent.appId -and $agent.appId -ne $id) { throw 'Unexpected agent appId/object ID mismatch.' }
      $DeploymentScriptOutputs = @{
        agentIdentityId = $id; agentIdentityAppId = $id
        agentIdentityPrincipalId = $id; agentDisplayName = $agent.displayName
      }
    '''
  }
}

output agentIdentityId string = agentIdentityScript.properties.outputs.agentIdentityId
output agentIdentityAppId string = agentIdentityScript.properties.outputs.agentIdentityAppId
output agentIdentityPrincipalId string = agentIdentityScript.properties.outputs.agentIdentityPrincipalId
output agentDisplayName string = agentIdentityScript.properties.outputs.agentDisplayName
