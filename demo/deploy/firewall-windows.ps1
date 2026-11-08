<#
.SYNOPSIS
    WMS 应用端口的 Windows 防火墙规则（本机 / Windows 演示机用）。

.DESCRIPTION
    对 WMS 应用端口（默认 8000 后端、8001 前端预览）设置：
      · 放行规则：仅"专用网络(Private)"且来源为"本地子网"，供本机演示/联调；
      · 阻断规则：显式阻断"公用网络(Public)"的入站访问，避免机器直接暴露。
    规则可重复执行（幂等：先删后建）。

    生产环境建议（对应文档 6.2）：
      · 后端只监听 127.0.0.1，由 Nginx 反代；
      · Linux 服务器请使用同目录 firewall-ufw.sh，仅放行 80/443/SSH。

.PARAMETER Ports
    要管理的 TCP 端口，默认 8000,8001。

.PARAMETER Profiles
    放行规则适用的网络配置文件，默认仅 Private。

.PARAMETER RemoteAddress
    放行规则的来源范围，默认 LocalSubnet（本地子网）。

.PARAMETER Remove
    移除全部 WMS-* 规则后退出。

.EXAMPLE
    # 应用规则（需管理员 PowerShell）
    powershell -ExecutionPolicy Bypass -File .\firewall-windows.ps1

.EXAMPLE
    # 查看规则
    Get-NetFirewallRule -DisplayName 'WMS-*' | Format-Table DisplayName,Enabled,Direction,Action

.EXAMPLE
    # 移除规则
    powershell -ExecutionPolicy Bypass -File .\firewall-windows.ps1 -Remove
#>
[CmdletBinding()]
param(
    [int[]]$Ports = @(8000, 8001),
    [string[]]$Profiles = @('Private'),
    [string]$RemoteAddress = 'LocalSubnet',
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'

$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw '需要管理员权限：请以管理员身份打开 PowerShell 后重新运行。'
}

$rulePrefix = 'WMS'

function Remove-WmsRules {
    Get-NetFirewallRule -DisplayName "$rulePrefix-*" -ErrorAction SilentlyContinue |
        Remove-NetFirewallRule -ErrorAction SilentlyContinue
}

if ($Remove) {
    Remove-WmsRules
    Write-Host "[WMS] 已移除全部 $rulePrefix-* 防火墙规则。"
    return
}

# 幂等：先清理旧规则再重建
Remove-WmsRules

foreach ($port in $Ports) {
    New-NetFirewallRule -DisplayName "$rulePrefix-Allow-$port" -Direction Inbound -Action Allow `
        -Protocol TCP -LocalPort $port -Profile $Profiles -RemoteAddress $RemoteAddress `
        -Description "WMS 应用端口 $port：仅 $($Profiles -join ',') / $RemoteAddress 可入站" | Out-Null
    Write-Host "[WMS] 已放行 TCP $port（$($Profiles -join ',')，来源 $RemoteAddress）"

    New-NetFirewallRule -DisplayName "$rulePrefix-Block-Public-$port" -Direction Inbound -Action Block `
        -Protocol TCP -LocalPort $port -Profile Public `
        -Description "WMS 应用端口 $port：阻断公网（Public）入站" | Out-Null
    Write-Host "[WMS] 已阻断 TCP $port 的公网（Public）入站"
}

Write-Host ''
Write-Host '当前规则：'
Get-NetFirewallRule -DisplayName "$rulePrefix-*" |
    Format-Table DisplayName, Enabled, Direction, Action -AutoSize
