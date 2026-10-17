# Shared by the installer and management commands. No host-specific settings.
function Get-ForwarderTaskName {
    'SshLocalhostForwarder-' + [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
}

function Get-ForwarderTask {
    param([string]$ScriptPath, [string]$ConfigPath)
    $task = Get-ScheduledTask -TaskName (Get-ForwarderTaskName) -ErrorAction SilentlyContinue
    if ($task) {
        $expected = '"' + $ScriptPath + '" --config "' + $ConfigPath + '" run'
        if (@($task.Actions).Count -ne 1 -or $task.Actions[0].Arguments -ne $expected) {
            throw 'Scheduled task belongs to another installation'
        }
    }
    return $task
}

function Enable-ForwarderAutostart {
    param([string]$InstallDir, [string]$PythonWindowless)
    $scriptPath = Join-Path $InstallDir 'forwarder.py'
    $configPath = Join-Path $InstallDir 'config.json'
    $null = Get-ForwarderTask -ScriptPath $scriptPath -ConfigPath $configPath
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $action = New-ScheduledTaskAction -Execute $PythonWindowless -Argument ('"{0}" --config "{1}" run' -f $scriptPath, $configPath) -WorkingDirectory $InstallDir
    $principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Limited
    $triggers = @(
        (New-ScheduledTaskTrigger -AtLogOn -User $identity.Name),
        (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1))
    )
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName (Get-ForwarderTaskName) -Action $action -Trigger $triggers -Principal $principal -Settings $settings -Description 'Automatic localhost TCP forwarding over SSH; restart every minute if stopped' -Force | Out-Null
    # Migrate the earlier shortcut after the scheduled task is registered.
    $shortcutPath = Join-Path ([Environment]::GetFolderPath('Startup')) 'SshLocalhostForwarder.lnk'
    if (Test-Path -LiteralPath $shortcutPath) {
        $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcutPath)
        if ($shortcut.Arguments -eq ('"{0}" --config "{1}" run' -f $scriptPath, $configPath)) {
            Remove-Item -LiteralPath $shortcutPath
        }
    }
    Write-Output 'Autostart and automatic restart enabled (one-minute interval)'
}
