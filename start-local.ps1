#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ($env:OS -ne 'Windows_NT') {
    throw 'This launcher requires Windows Credential Manager.'
}

$configPath = Join-Path $PSScriptRoot 'local-demo.config.json'
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw 'Copy local-demo.config.example.json to local-demo.config.json and configure its public identifiers. Do not add credentials.'
}
$raw = Get-Content -LiteralPath $configPath -Raw
if ([string]::IsNullOrWhiteSpace($raw) -or -not $raw.TrimStart().StartsWith('{')) {
    throw 'local-demo.config.json must contain a JSON object.'
}
try {
    $config = $raw | ConvertFrom-Json
}
catch [System.ArgumentException] {
    throw 'local-demo.config.json must contain a valid JSON object.'
}
if ($null -eq $config -or $config.GetType() -ne [System.Management.Automation.PSCustomObject]) {
    throw 'local-demo.config.json must contain a JSON object.'
}

$required = @(
    'TENANT_ID', 'API_AUDIENCE', 'ALLOWED_USER_OID',
    'CONTOSO_ENTRA_TENANT_ID', 'CONTOSO_GATEWAY_API_CLIENT_ID',
    'CONTOSO_REQUESTER_CLIENT_ID', 'CONTOSO_GATEWAY_URL'
)
$allowed = $required + @('CONTOSO_APIM_HOST')
$values = @{ CONTOSO_REQUESTER_CREDENTIAL_SOURCE = 'windows' }
foreach ($property in $config.PSObject.Properties) {
    if ($allowed -cnotcontains $property.Name) {
        throw 'Unsupported configuration field. Only public identifiers and the gateway address are allowed.'
    }
    if ($property.Value -isnot [string] -or [string]::IsNullOrWhiteSpace($property.Value)) {
        throw "Configuration field $($property.Name) must be a nonempty string."
    }
    $values[$property.Name] = $property.Value
}
foreach ($name in $required) {
    if (-not $values.ContainsKey($name)) {
        throw "Missing configuration field: $name."
    }
}
if (Test-Path Env:CONTOSO_REQUESTER_CLIENT_SECRET) {
    throw 'Remove the conflicting process credential before using protected Windows storage.'
}

$python = Join-Path $PSScriptRoot '.venv-local\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    $python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
}
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Python environment is missing. Follow the Windows startup section in README.md.'
}
foreach ($asset in @('index.html', 'app.js', 'config.js')) {
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot "frontend\dist\$asset") -PathType Leaf)) {
        throw 'Frontend build is missing. Configure frontend\config.js and run npm run build.'
    }
}
if (-not $CheckOnly) {
    $occupied = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners() |
        Where-Object { $_.Port -eq 8000 }
    if ($occupied) {
        throw 'Port 8000 is already in use. The existing process has been preserved. Do not start a second server.'
    }
}

$previous = @{}
$names = @($values.Keys)
foreach ($name in $names) {
    $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
# An optional APIM hostname must not leak in from a different environment.
if (-not $values.ContainsKey('CONTOSO_APIM_HOST')) {
    $previous['CONTOSO_APIM_HOST'] = [Environment]::GetEnvironmentVariable('CONTOSO_APIM_HOST', 'Process')
    $values['CONTOSO_APIM_HOST'] = $null
}

Push-Location $PSScriptRoot
try {
    foreach ($name in $values.Keys) {
        $value = $values[$name]
        if ($null -eq $value) {
            $value = [NullString]::Value
        }
        [Environment]::SetEnvironmentVariable($name, $value, 'Process')
    }
    $preflight = @'
import sys

if sys.version_info < (3, 12):
    print("Python 3.12 or newer is required.", file=sys.stderr)
    raise SystemExit(1)
try:
    from contoso_backend.local_server import LocalSettings
    LocalSettings.from_env()
except (ImportError, ValueError):
    print("Local configuration, Python dependencies or the protected requester credential are invalid. See README.md.", file=sys.stderr)
    raise SystemExit(1) from None
print("Local configuration and protected Windows credential are ready.")
'@
    $preflight | & $python -
    if ($LASTEXITCODE -ne 0) {
        throw 'Local startup preflight failed. No server was started.'
    }
    if ($CheckOnly) {
        return
    }
    Write-Host "Python: $python"
    Write-Host 'Open http://localhost:8000/ in Edge or Chrome. Keep this terminal open. Press Ctrl+C to stop.'
    & $python -m contoso_backend.local_server
    if ($LASTEXITCODE -ne 0) {
        throw "The local server exited with code $LASTEXITCODE."
    }
}
finally {
    foreach ($name in $previous.Keys) {
        $value = $previous[$name]
        if ($null -eq $value) {
            $value = [NullString]::Value
        }
        [Environment]::SetEnvironmentVariable($name, $value, 'Process')
    }
    Pop-Location
}
