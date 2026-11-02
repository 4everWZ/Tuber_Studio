param(
    [Parameter(Mandatory=$true)][string]$Data,
    [string]$Source = (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent),
    [string]$Python,
    [string]$Config,
    [string]$BindAddress = '127.0.0.1',
    [ValidateRange(1,65535)][int]$Port = 9020,
    [switch]$Background
)
$ErrorActionPreference = 'Stop'
$Source = (Resolve-Path -LiteralPath $Source).Path
$Data = (Resolve-Path -LiteralPath $Data).Path
if (-not $Python) { $Python = Join-Path $Source '.venv/Scripts/python.exe' }
$Python = (Resolve-Path -LiteralPath $Python).Path
if (-not $Config) {
    if ($env:TUBER_CONFIG) { $Config = $env:TUBER_CONFIG }
    else { $Config = Join-Path $Data 'studio.json' }
}
$Config = (Resolve-Path -LiteralPath $Config).Path
if (-not (Test-Path -LiteralPath (Join-Path $Source 'src/tuber_studio/app.py'))) {
    throw 'Source must be the Tuber Studio program directory'
}
if (-not (Test-Path -LiteralPath (Join-Path $Data 'studio.sqlite3'))) {
    throw 'Restore or initialize the business data directory first'
}
$env:TUBER_RUNTIME = $Data
$env:TUBER_CONFIG = $Config
$env:PYTHONPATH = Join-Path $Source 'src'
$businessVisionCheck = @'
import importlib.util, json, sys
with open(sys.argv[1], encoding="utf-8") as source:
    settings = json.load(source)
profiles = [settings.get("vision", {}), settings.get("video_vision", {})]
profiles.extend(value.get("vision", {}) for value in settings.get("work_mode_profiles", {}).values())
needs_pillow = any(value.get("image_profile", "server_native") != "server_native" for value in profiles)
sys.exit(42 if needs_pillow and importlib.util.find_spec("PIL") is None else 0)
'@
& $Python -c $businessVisionCheck $Config
if ($LASTEXITCODE -eq 42) {
    throw "Gemma image preprocessing requires the optional business-host dependencies in tools/deployment/business-requirements.txt. Install them into the selected project Python environment before starting."
}
if ($LASTEXITCODE -ne 0) { throw 'Business configuration or selected Python environment could not be checked' }
if ($Background) {
    # The user does not need an interactive terminal to keep the B/S server up.
    $businessArgs = @('-m', 'tuber_studio', '--host', $BindAddress, '--port', [string]$Port)
    $process = Start-Process -FilePath $Python -ArgumentList $businessArgs -WorkingDirectory $Source `
        -WindowStyle Hidden -RedirectStandardOutput (Join-Path $Data 'business-stdout.log') `
        -RedirectStandardError (Join-Path $Data 'business-stderr.log') -PassThru
    Write-Output "Business process: $($process.Id)"
    Write-Output "Workbench: http://${BindAddress}:${Port}"
} else {
    & $Python -m tuber_studio --host $BindAddress --port $Port
    exit $LASTEXITCODE
}
