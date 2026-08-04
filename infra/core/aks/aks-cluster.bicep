@description('Name of the AKS cluster')
param aksClusterName string

@description('Location for the AKS cluster')
param location string = resourceGroup().location

@description('Tags for the AKS cluster')
param tags object = {}

@description('Kubernetes version')
param kubernetesVersion string = '1.34'

@description('System node pool VM size')
param systemNodePoolVmSize string = 'Standard_DS2_v2'

@description('System node pool count')
param systemNodePoolCount int = 2

@description('User assigned managed identity ID for AKS')
param userAssignedIdentityId string

@description('Log Analytics workspace ID')
param logAnalyticsWorkspaceId string

@description('Virtual Network Subnet ID')
param subnetId string = ''

@description('Enable Azure AD RBAC')
param enableAzureRbac bool = true

@description('Enable monitoring')
param enableMonitoring bool = true

@description('AKS SKU tier - Free or Standard')
@allowed(['Free', 'Standard'])
param skuTier string = 'Standard'

@description('Azure Monitor Workspace ID for Prometheus metrics')
param azureMonitorWorkspaceId string = ''

@description('Enable Prometheus metrics collection')
param enablePrometheus bool = true

// AKS Cluster with system node pool
resource aksCluster 'Microsoft.ContainerService/managedClusters@2024-02-01' = {
  name: aksClusterName
  location: location
  tags: tags
  sku: {
    name: 'Base'
    tier: skuTier
  }
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${userAssignedIdentityId}': {}
    }
  }
  properties: {
    kubernetesVersion: kubernetesVersion
    dnsPrefix: aksClusterName
    enableRBAC: true
    aadProfile: {
      managed: true
      enableAzureRBAC: enableAzureRbac
    }
    agentPoolProfiles: [
      {
        name: 'system'
        count: systemNodePoolCount
        vmSize: systemNodePoolVmSize
        osType: 'Linux'
        mode: 'System'
        type: 'VirtualMachineScaleSets'
        enableAutoScaling: true
        minCount: systemNodePoolCount
        maxCount: systemNodePoolCount + 2
        vnetSubnetID: !empty(subnetId) ? subnetId : null
        tags: tags
      }
    ]
    networkProfile: {
      networkPlugin: 'azure'
      networkPolicy: 'azure'
      serviceCidr: '10.240.0.0/16'
      dnsServiceIP: '10.240.0.10'
      loadBalancerSku: 'standard'
    }
    addonProfiles: enableMonitoring ? {
      omsagent: {
        enabled: true
        config: {
          logAnalyticsWorkspaceResourceID: logAnalyticsWorkspaceId
        }
      }
    } : {}
    oidcIssuerProfile: {
      enabled: true
    }
    securityProfile: {
      workloadIdentity: {
        enabled: true
      }
    }
    azureMonitorProfile: enablePrometheus && !empty(azureMonitorWorkspaceId) ? {
      metrics: {
        enabled: true
        kubeStateMetrics: {
          metricLabelsAllowlist: '*'
          metricAnnotationsAllowList: '*'
        }
      }
    } : null
  }
}

// Output AKS cluster details
output aksClusterId string = aksCluster.id
output aksClusterName string = aksCluster.name
output aksClusterFqdn string = aksCluster.properties.fqdn
output aksClusterOidcIssuerUrl string = aksCluster.properties.oidcIssuerProfile.issuerURL
