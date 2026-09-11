# Shared gated build; no registry firewall updates in either platform wrapper.
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$python = $env:DEPLOYMENT_PYTHON
if (-not $python -and (Test-Path (Join-Path $root '.venv/Scripts/python.exe'))) {
    $python = Join-Path $root '.venv/Scripts/python.exe'
}
if (-not $python) { $python = 'python' }
$options = @('--from-azd')
if ($env:AZURE_ENV_NAME) { $options += "--environment=$($env:AZURE_ENV_NAME)" }
& $python (Join-Path $PSScriptRoot 'build_and_push.py') @options @args
if ($LASTEXITCODE -ne 0) { throw 'Gated build failed; no rollout is permitted.' }
