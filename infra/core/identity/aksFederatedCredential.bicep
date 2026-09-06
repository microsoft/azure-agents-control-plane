// Optional DIRECT AKS -> blueprint federation. This is NOT the AKS -> bootstrap
// MI federation required by get_agent_credential(); that ARM MI FIC must remain.
// Do not annotate the bootstrap service account with the agent's client ID.
// This module only configures trust; runtime direct-assertion mode is not provided.
// Configuration MI requires pregranted Graph APPLICATION roles:
// AgentIdentityBlueprint.Read.All, AgentIdentityBlueprint.AddRemoveCreds.All,
// AgentIdentityBlueprint.UpdateBranding.All (for FIC PATCH).
// https://learn.microsoft.com/graph/api/federatedidentitycredential-update?view=graph-rest-beta
// Never resolves a child service principal to an application, and never skips.

param aksClusterName string
param serviceAccountNamespace string
param serviceAccountName string

@description('Application OBJECT ID of the blueprint (not blueprint principal ID or agent ID)')
param blueprintObjectId string

@description('Blueprint client/app ID; checked against the object ID before modifying trust')
param blueprintAppId string

@minLength(1)
param federatedCredentialName string

param subjectIdentifier string = 'system:serviceaccount:${serviceAccountNamespace}:${serviceAccountName}'
param location string = resourceGroup().location
param tags object = {}

@description('Full ARM resource ID of a user-assigned MI with pregranted Graph FIC permissions')
param configurationIdentityResourceId string

param tenantId string = tenant().tenantId
param forceUpdateTag string = 'identity-v2'

resource aksCluster 'Microsoft.ContainerService/managedClusters@2024-02-01' existing = {
  name: aksClusterName
}

var configuration = {
  objectId: blueprintObjectId
  appId: blueprintAppId
  name: federatedCredentialName
  issuer: aksCluster.properties.oidcIssuerProfile.issuerURL
  subject: subjectIdentifier
  tenantId: tenantId
  cloud: environment().name
}

resource federatedCredentialScript 'Microsoft.Resources/deploymentScripts@2023-08-01' = {
  name: 'ds-fed-cred-${uniqueString(blueprintObjectId, serviceAccountNamespace, serviceAccountName)}'
  location: location
  tags: tags
  kind: 'AzurePowerShell'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${configurationIdentityResourceId}': {}
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
      $objectId = ([guid]$c.objectId).ToString()
      $appId = ([guid]$c.appId).ToString()
      $tenant = ([guid]$c.tenantId).ToString()
      if (-not $c.issuer -or -not $c.issuer.StartsWith('https://')) { throw 'AKS OIDC issuer is missing or invalid; enable OIDC before provisioning federation.' }
      if ($c.subject -notmatch '^system:serviceaccount:[^:]+:[^:]+$') { throw 'Invalid Kubernetes service account subject.' }
      try {
        $access = (Get-AzAccessToken -ResourceUrl 'https://graph.microsoft.com' -TenantId $tenant -ErrorAction Stop).Token
        if ($access -is [securestring]) { $access = [pscredential]::new('token', $access).GetNetworkCredential().Password }
      } catch { throw 'Configuration MI Graph authentication failed.' }
      $headers = @{ Authorization = "Bearer $access"; 'OData-Version' = '4.0' }
      function Invoke-Graph([string]$Method, [string]$Uri, $Body = $null) {
        if (-not $Uri.StartsWith('https://graph.microsoft.com/')) { throw 'Unexpected Graph URL.' }
        $request = @{ Method = $Method; Uri = $Uri; Headers = $headers; TimeoutSec = 30; MaximumRedirection = 0; ErrorAction = 'Stop' }
        if ($null -ne $Body) { $request.Body = ConvertTo-Json -InputObject $Body -Depth 10 -Compress; $request.ContentType = 'application/json' }
        try { Invoke-RestMethod @request }
        catch { throw 'Blueprint AKS federation operation failed. Check Graph permissions, blueprint IDs and Entra logs; no skip/fallback is allowed.' }
      }
      $app = Invoke-Graph GET "https://graph.microsoft.com/v1.0/applications/$objectId/microsoft.graph.agentIdentityBlueprint"
      if ($app.appId -ne $appId) { throw 'Blueprint application object/client ID mismatch.' }
      $ficUri = "https://graph.microsoft.com/beta/applications/$objectId/microsoft.graph.agentIdentityBlueprint/federatedIdentityCredentials"
      $uri = $ficUri
      $existing = @()
      $pages = 0
      while ($uri) {
        if (++$pages -gt 100) { throw 'Graph pagination limit exceeded.' }
        $page = Invoke-Graph GET $uri
        if ($null -eq $page.value) { throw 'Invalid Graph collection response.' }
        $existing += @($page.value | Where-Object { $_.name -ceq $c.name })
        $uri = $page.'@odata.nextLink'
      }
      if ($existing.Count -gt 1) { throw 'Ambiguous AKS federated credential name.' }
      $desired = @{ issuer = $c.issuer; subject = $c.subject; audiences = @('api://AzureADTokenExchange') }
      if ($existing.Count -eq 0) {
        $desired.name = $c.name
        $credential = Invoke-Graph POST $ficUri $desired
        $status = 'created'
      } else {
        $credential = $existing[0]
        $status = 'exists'
        if ($credential.issuer -cne $desired.issuer -or $credential.subject -cne $desired.subject -or @($credential.audiences).Count -ne 1 -or $credential.audiences[0] -cne $desired.audiences[0]) {
          $null = Invoke-Graph PATCH "$ficUri/$($credential.id)" $desired
          $status = 'updated'
        }
      }
      if (-not $credential.id) { throw 'Federated credential response is missing its ID.' }
      $DeploymentScriptOutputs = @{ status = $status; credentialId = $credential.id }
    '''
  }
}

output oidcIssuerUrl string = aksCluster.properties.oidcIssuerProfile.issuerURL
output subjectIdentifier string = subjectIdentifier
output credentialId string = federatedCredentialScript.properties.outputs.credentialId
output status string = federatedCredentialScript.properties.outputs.status
