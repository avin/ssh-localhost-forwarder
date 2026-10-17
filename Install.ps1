[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$SshHost,
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'SshLocalhostForwarder'),
    [string]$Python = 'python',
    [ValidateRange(1, 300)][int]$PollIntervalSeconds = 5,
    [int[]]$IncludePorts = @(),
    [int[]]$ExcludePorts = @(),
    [switch]$NoDocker,
    [switch]$NoAutostart,
    [switch]$NoStart
)
$ErrorActionPreference = 'Stop'
$InstallDir = [IO.Path]::GetFullPath($InstallDir)
$pythonExecutable = (& $Python -c 'import sys; print(sys.executable)')
if ($LASTEXITCODE -ne 0 -or -not $pythonExecutable) { throw 'Python 3.10+ is required' }
$pythonExecutable = $pythonExecutable.Trim()
& $pythonExecutable -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'
if ($LASTEXITCODE -ne 0) { throw 'Python 3.10+ is required' }
$pythonWindowless = Join-Path (Split-Path $pythonExecutable) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonWindowless)) { throw 'pythonw.exe not found next to python.exe' }
foreach ($port in @($IncludePorts) + @($ExcludePorts)) {
    if ($port -lt 1 -or $port -gt 65535) { throw 'Ports must be between 1 and 65535' }
}
if ([string]::IsNullOrWhiteSpace($SshHost) -or $SshHost.StartsWith('-') -or $SshHost -match '\s') {
    throw 'SshHost must be an SSH alias or user@host'
}
$configPath = Join-Path $InstallDir 'config.json'
if (Test-Path -LiteralPath $configPath) {
    throw 'Installation already has config.json. Stop the forwarder and edit that file, or select another InstallDir.'
}
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
foreach ($name in @('forwarder.py', 'discovery.py', 'Manage.ps1', 'README.md')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination (Join-Path $InstallDir $name)
}
$config = [ordered]@{
    ssh_host = $SshHost
    poll_interval_seconds = $PollIntervalSeconds
    connect_timeout_seconds = 5
    remote_python = 'python3'
    ssh_path = 'ssh'
    docker = (-not $NoDocker.IsPresent)
    listen_ipv6 = $true
    include_ports = @($IncludePorts)
    exclude_ports = @($ExcludePorts)
}
$config | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $configPath -Encoding UTF8
@{ python = $pythonExecutable; pythonw = $pythonWindowless } |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $InstallDir 'installation.json') -Encoding UTF8
# Check the SSH connection and remote commands before enabling autostart.
& $pythonExecutable (Join-Path $InstallDir 'forwarder.py') --config $configPath check
if ($LASTEXITCODE -ne 0) { throw 'SSH discovery failed. Fix SSH/config.json, then run Manage.ps1 -Action Check.' }
if (-not $NoAutostart) {
    $shortcutPath = Join-Path ([Environment]::GetFolderPath('Startup')) 'SshLocalhostForwarder.lnk'
    if (Test-Path -LiteralPath $shortcutPath) { throw 'Startup shortcut already exists; use Manage.ps1 -Action EnableAutostart after checking its target.' }
    $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $pythonWindowless
    $shortcut.Arguments = '"' + (Join-Path $InstallDir 'forwarder.py') + '" --config "' + $configPath + '" run'
    $shortcut.WorkingDirectory = $InstallDir
    $shortcut.WindowStyle = 7
    $shortcut.Description = 'Automatic localhost TCP forwarding over SSH'
    $shortcut.Save()
}
if (-not $NoStart) { & (Join-Path $InstallDir 'Manage.ps1') -Action Start }
Write-Output "Installed in $InstallDir"
