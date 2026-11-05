#Requires -RunAsAdministrator
# Opens this demo's server ports to the local subnet only (NFR-12). Run in an administrator PowerShell.
# -Remove deletes the NearPilot rules and adds nothing. Rerunning replaces the previous rules.
param([switch]$Remove)
$ErrorActionPreference = 'Stop'
$Group = 'NearPilot'
Get-NetFirewallRule -Group $Group -ErrorAction SilentlyContinue | Remove-NetFirewallRule
if ($Remove) {
    Write-Output "방화벽 규칙을 지웠다: $Group"
    exit 0
}
$Rules = @(
@RULES@
)
foreach ($Rule in $Rules) {
    New-NetFirewallRule -Group $Group -DisplayName "NearPilot $($Rule.Name)" -Direction Inbound -Action Allow `
        -Protocol $Rule.Protocol -LocalPort $Rule.Port -RemoteAddress LocalSubnet -Profile Any | Out-Null
    Write-Output "허용: $($Rule.Name) $($Rule.Protocol) $($Rule.Port) (로컬 서브넷)"
}
