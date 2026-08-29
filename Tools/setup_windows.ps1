[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
    throw "This setup script supports Windows only."
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$venvDirectory = Join-Path $projectRoot ".venv"
$venvPython = Join-Path $venvDirectory "Scripts\python.exe"
$python = Get-Command "python.exe" -ErrorAction Stop

function Invoke-SetupStep {
    param(
        [Parameter(Mandatory)]
        [string]$Description,
        [Parameter(Mandatory)]
        [scriptblock]$Command
    )

    Write-Host $Description
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

Invoke-SetupStep "Creating the Windows virtual environment..." {
    & $python.Source -m venv $venvDirectory
}

Invoke-SetupStep "Upgrading pip..." {
    & $venvPython -m pip install --upgrade pip
}

Invoke-SetupStep "Installing PyTorch 2.13.0 with CUDA 13.0 support..." {
    & $venvPython -m pip install torch==2.13.0 --index-url https://download.pytorch.org/whl/cu130
}

Invoke-SetupStep "Installing the Windows MVP audio dependencies..." {
    & $venvPython -m pip install demucs==4.1.0 soundfile==0.13.1 sounddevice==0.5.6
}

Write-Host "Windows MVP environment is ready: $venvPython"
