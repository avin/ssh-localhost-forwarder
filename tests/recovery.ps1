# Fault injection against an existing Windows installation. Briefly interrupts tunnels.
[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'SshLocalhostForwarder'),
    [int]$ProbePort = 0
)
$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path $InstallDir 'forwarder.py'
$configPath = Join-Path $InstallDir 'config.json'
$statusPath = Join-Path $InstallDir 'runtime\status.json'
. (Join-Path $InstallDir 'Autostart.ps1')
function Get-TestForwarder {
    @(Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" | Where-Object {
        $_.CommandLine -and $_.CommandLine.Contains($scriptPath) -and $_.CommandLine.Contains($configPath)
    })
}
function Assert-ProbePort {
    if ($ProbePort -eq 0) { return }
    $client = New-Object Net.Sockets.TcpClient
    try {
        $connect = $client.ConnectAsync('127.0.0.1', $ProbePort)
        if (-not $connect.Wait(2000) -or -not $client.Connected) { throw "localhost:$ProbePort is unavailable" }
    } finally { $client.Dispose() }
}
$task = Get-ForwarderTask -ScriptPath $scriptPath -ConfigPath $configPath
if (-not $task -or $task.State -eq 'Disabled') { throw 'Enable scheduled autostart before this test' }
$owners = Get-TestForwarder
if ($owners.Count -ne 1) { throw 'Exactly one running forwarder is required' }
Assert-ProbePort
$oldProcessId = $owners[0].ProcessId
$oldState = Get-Content -Raw -LiteralPath $statusPath | ConvertFrom-Json
$oldTunnelIds = @($oldState.tunnels | ForEach-Object { $_.pid })
Stop-Process -Id $oldProcessId -Force
Start-Sleep -Milliseconds 500
$orphans = @($oldTunnelIds | ForEach-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue })
if ($orphans.Count) { throw 'Hard kill left tunnel processes behind' }
Write-Output 'PASS: hard kill removed owned SSH processes'
$deadline = [DateTime]::UtcNow.AddSeconds(100)
$recovered = $false
do {
    Start-Sleep -Seconds 2
    $newOwners = Get-TestForwarder
    if ($newOwners.Count -eq 1 -and $newOwners[0].ProcessId -ne $oldProcessId) {
        $newState = Get-Content -Raw -LiteralPath $statusPath | ConvertFrom-Json
        if (-not $newState.stopped -and -not $newState.error -and $newState.tunnels.Count) {
            try { Assert-ProbePort; $recovered = $true; break } catch { }
        }
    }
} while ([DateTime]::UtcNow -lt $deadline)
if (-not $recovered) { throw 'Scheduled task did not restore the forwarder and tunnels within 100 seconds' }
Write-Output 'PASS: scheduled task restarted the forwarder and restored TCP access'
& (Join-Path $InstallDir 'Manage.ps1') -Action Stop
Start-ScheduledTask -TaskName $task.TaskName
Start-Sleep -Seconds 3
if ((Get-TestForwarder).Count) { throw 'Scheduled retry ignored the manual pause' }
Write-Output 'PASS: manual Stop survives a scheduled retry'
& (Join-Path $InstallDir 'Manage.ps1') -Action Start
$deadline = [DateTime]::UtcNow.AddSeconds(20)
do {
    Start-Sleep -Seconds 1
    if ((Get-TestForwarder).Count -eq 1) {
        try { Assert-ProbePort; Write-Output 'PASS: manual Start restores forwarding'; return } catch { }
    }
} while ([DateTime]::UtcNow -lt $deadline)
throw 'Manual Start did not restore forwarding'
