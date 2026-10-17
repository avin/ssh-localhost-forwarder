[CmdletBinding()]
param(
    [ValidateSet('Start', 'Stop', 'Status', 'Check', 'EnableAutostart', 'DisableAutostart')]
    [string]$Action = 'Status'
)
$ErrorActionPreference = 'Stop'
$configPath = Join-Path $PSScriptRoot 'config.json'
$scriptPath = Join-Path $PSScriptRoot 'forwarder.py'
$runtimeDir = Join-Path $PSScriptRoot 'runtime'
$installation = Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot 'installation.json') | ConvertFrom-Json
. (Join-Path $PSScriptRoot 'Autostart.ps1')
switch ($Action) {
    'Start' {
        Remove-Item -LiteralPath (Join-Path $runtimeDir 'paused') -ErrorAction SilentlyContinue
        $task = Get-ForwarderTask -ScriptPath $scriptPath -ConfigPath $configPath
        if ($task -and $task.State -ne 'Disabled') {
            Start-ScheduledTask -TaskName $task.TaskName
        } else {
            Start-Process -FilePath $installation.pythonw -ArgumentList ('"{0}" --config "{1}" run' -f $scriptPath, $configPath) -WindowStyle Hidden
        }
    }
    'Stop' {
        New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
        New-Item -ItemType File -Path (Join-Path $runtimeDir 'paused') -Force | Out-Null
        # A dead instance has no lock. Treat Stop as successful in that case too.
        & $installation.python -c 'import pathlib,sys; sys.path.insert(0,sys.argv[1]); import forwarder; h=forwarder.acquire_lock(pathlib.Path(sys.argv[2])/"instance.lock"); sys.exit(0 if h else 1)' $PSScriptRoot $runtimeDir
        if ($LASTEXITCODE -eq 0) { Write-Output 'Stopped (automatic restart paused until Start)'; return }
        & $installation.python $scriptPath --config $configPath stop
        if ($LASTEXITCODE -ne 0) { throw 'Could not request stop' }
        $deadline = [DateTime]::UtcNow.AddSeconds(60)
        do {
            Start-Sleep -Milliseconds 300
            $status = Get-Content -Raw -LiteralPath (Join-Path $runtimeDir 'status.json') | ConvertFrom-Json
            if ($status.stopped) { Write-Output 'Stopped (automatic restart paused until Start)'; return }
        } while ([DateTime]::UtcNow -lt $deadline)
        throw 'Stop not confirmed. Inspect runtime/forward.log.'
    }
    'Status' {
        & $installation.python $scriptPath --config $configPath status
        if ($LASTEXITCODE -ne 0) { throw 'Could not read status' }
    }
    'Check' {
        & $installation.python $scriptPath --config $configPath check
        if ($LASTEXITCODE -ne 0) { throw 'Discovery failed' }
    }
    'EnableAutostart' {
        Enable-ForwarderAutostart -InstallDir $PSScriptRoot -PythonWindowless $installation.pythonw
    }
    'DisableAutostart' {
        $task = Get-ForwarderTask -ScriptPath $scriptPath -ConfigPath $configPath
        if ($task) { Disable-ScheduledTask -TaskName $task.TaskName | Out-Null }
        Write-Output 'Autostart and automatic restart disabled; running tunnels are unaffected'
    }
}
