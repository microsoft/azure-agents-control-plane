@description('Specifies the name of the virtual network.')
param vNetName string

@description('Specifies the location.')
param location string = resourceGroup().location

@description('Specifies the name of the subnet for the Service Bus private endpoint.')
param peSubnetName string = 'private-endpoints-subnet'

@description('Specifies the name of the subnet for Function App virtual network integration.')
param appSubnetName string = 'app'

param tags object = {}

// Network security group for the APIM StandardV2 VNet-integration subnet.
// Outbound access to Azure Storage and Key Vault is required by API Management.
resource apimNsg 'Microsoft.Network/networkSecurityGroups@2023-05-01' = {
  name: '${vNetName}-apim-nsg'
  location: location
  tags: tags
  properties: {
    securityRules: [
      {
        name: 'AllowStorageOutbound'
        properties: {
          direction: 'Outbound'
          priority: 100
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'VirtualNetwork'
          sourcePortRange: '*'
          destinationAddressPrefix: 'Storage'
          destinationPortRange: '443'
        }
      }
      {
        name: 'AllowKeyVaultOutbound'
        properties: {
          direction: 'Outbound'
          priority: 110
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'VirtualNetwork'
          sourcePortRange: '*'
          destinationAddressPrefix: 'AzureKeyVault'
          destinationPortRange: '443'
        }
      }
    ]
  }
}

resource virtualNetwork 'Microsoft.Network/virtualNetworks@2023-05-01' = {
  name: vNetName
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [
        '10.0.0.0/16'
      ]
    }
    encryption: {
      enabled: false
      enforcement: 'AllowUnencrypted'
    }
    subnets: [
      {
        name: peSubnetName
        id: resourceId('Microsoft.Network/virtualNetworks/subnets', vNetName, 'private-endpoints-subnet')
        properties: {
          addressPrefixes: [
            '10.0.1.0/24'
          ]
          delegations: []
          privateEndpointNetworkPolicies: 'Disabled'
          privateLinkServiceNetworkPolicies: 'Enabled'
        }
        type: 'Microsoft.Network/virtualNetworks/subnets'
      }
      {
        name: appSubnetName
        id: resourceId('Microsoft.Network/virtualNetworks/subnets', vNetName, 'app')
        properties: {
          addressPrefixes: [
            '10.0.2.0/24'
          ]
          delegations: []
          privateEndpointNetworkPolicies: 'Disabled'
          privateLinkServiceNetworkPolicies: 'Enabled'
        }
        type: 'Microsoft.Network/virtualNetworks/subnets'
      }
      {
        name: 'apim'
        id: resourceId('Microsoft.Network/virtualNetworks/subnets', vNetName, 'apim')
        properties: {
          addressPrefixes: [
            '10.0.3.0/24'
          ]
          delegations: [
            {
              name: 'apim-serverfarms-delegation'
              properties: {
                serviceName: 'Microsoft.Web/serverFarms'
              }
            }
          ]
          networkSecurityGroup: {
            id: apimNsg.id
          }
          privateEndpointNetworkPolicies: 'Disabled'
          privateLinkServiceNetworkPolicies: 'Enabled'
        }
        type: 'Microsoft.Network/virtualNetworks/subnets'
      }
      {
        name: 'svc-lb'
        id: resourceId('Microsoft.Network/virtualNetworks/subnets', vNetName, 'svc-lb')
        properties: {
          addressPrefixes: [
            '10.0.4.0/28'
          ]
          delegations: []
          privateEndpointNetworkPolicies: 'Disabled'
          privateLinkServiceNetworkPolicies: 'Enabled'
        }
        type: 'Microsoft.Network/virtualNetworks/subnets'
      }
    ]
    virtualNetworkPeerings: []
    enableDdosProtection: false
  }
}

output peSubnetName string = virtualNetwork.properties.subnets[0].name
output peSubnetID string = virtualNetwork.properties.subnets[0].id
output appSubnetName string = virtualNetwork.properties.subnets[1].name
output appSubnetID string = virtualNetwork.properties.subnets[1].id
output apimSubnetName string = virtualNetwork.properties.subnets[2].name
output apimSubnetID string = virtualNetwork.properties.subnets[2].id
output svcLbSubnetName string = virtualNetwork.properties.subnets[3].name
output svcLbSubnetID string = virtualNetwork.properties.subnets[3].id
