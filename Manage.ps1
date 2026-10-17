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
$shortcutPath = Join-Path ([Environment]::GetFolderPath('Startup')) 'SshLocalhostForwarder.lnk'
switch ($Action) {
    'Start' {
        Start-Process -FilePath $installation.pythonw -ArgumentList ('"{0}" --config "{1}" run' -f $scriptPath, $configPath) -WindowStyle Hidden
    }
    'Stop' {
        & $installation.python $scriptPath --config $configPath stop
        if ($LASTEXITCODE -ne 0) { throw 'Could not request stop' }
        $deadline = [DateTime]::UtcNow.AddSeconds(60)
        do {
            Start-Sleep -Milliseconds 300
            $status = Get-Content -Raw -LiteralPath (Join-Path $runtimeDir 'status.json') | ConvertFrom-Json
            if ($status.stopped) { Write-Output 'Stopped'; return }
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
        $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcutPath)
        if ((Test-Path -LiteralPath $shortcutPath) -and $shortcut.Arguments -notlike ('*"' + $scriptPath + '"*')) {
            throw 'Shortcut belongs to another installation'
        }
        $shortcut.TargetPath = $installation.pythonw
        $shortcut.Arguments = '"' + $scriptPath + '" --config "' + $configPath + '" run'
        $shortcut.WorkingDirectory = $PSScriptRoot
        $shortcut.WindowStyle = 7
        $shortcut.Description = 'Automatic localhost TCP forwarding over SSH'
        $shortcut.Save()
        Write-Output 'Autostart enabled at Windows sign-in'
    }
    'DisableAutostart' {
        if (Test-Path -LiteralPath $shortcutPath) {
            $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcutPath)
            if ($shortcut.Arguments -notlike ('*"' + $scriptPath + '"*')) { throw 'Shortcut belongs to another installation' }
            Remove-Item -LiteralPath $shortcutPath
        }
        Write-Output 'Autostart disabled; running tunnels are unaffected'
    }
}
