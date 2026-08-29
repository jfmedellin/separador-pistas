[CmdletBinding()]
param(
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = "1.1.2",
    [ValidateSet("cpu", "cuda")]
    [string]$Variant = "cpu",
    [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"

if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
    throw "The portable Windows build must run on Windows."
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$buildDirectory = Join-Path $projectRoot "build"
$distDirectory = Join-Path $projectRoot "dist"
$specPath = Join-Path $projectRoot "Tools\stemslayer_portable.spec"

function Resolve-BuildPython {
    if ($PythonPath) {
        return (Resolve-Path -LiteralPath $PythonPath -ErrorAction Stop).Path
    }

    $candidates = @(
        (Join-Path $projectRoot ".venv-portable-$Variant\Scripts\python.exe"),
        (Join-Path $projectRoot ".venv-portable\Scripts\python.exe"),
        (Join-Path $projectRoot ".venv\Scripts\python.exe")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    $python = Get-Command "python.exe" -ErrorAction Stop
    return $python.Source
}

function Invoke-BuildStep {
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

if (-not (Test-Path -LiteralPath $specPath -PathType Leaf)) {
    throw "PyInstaller spec not found: $specPath"
}

$python = Resolve-BuildPython
foreach ($directory in @($buildDirectory, $distDirectory)) {
    if (Test-Path -LiteralPath $directory) {
        $resolvedDirectory = (Resolve-Path -LiteralPath $directory).Path
        if (-not $resolvedDirectory.StartsWith($projectRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to clean a path outside the project: $resolvedDirectory"
        }
        Remove-Item -LiteralPath $resolvedDirectory -Recurse -Force
    }
}
New-Item -ItemType Directory -Path $buildDirectory, $distDirectory | Out-Null

@"
import importlib.metadata as metadata
import sys
import torch

variant = "$Variant"
torch_version = metadata.version("torch")
cuda_version = torch.version.cuda

if torch_version.split("+")[0] != "2.13.0":
    raise SystemExit("PyTorch 2.13.0 is required; found " + torch_version)
if variant == "cpu" and cuda_version is not None:
    raise SystemExit("CPU-only PyTorch is required; this environment reports CUDA " + str(cuda_version))
if variant == "cuda" and cuda_version != "13.0":
    raise SystemExit("The CUDA variant requires PyTorch cu130; this environment reports CUDA " + str(cuda_version))
if metadata.version("demucs") != "4.1.0":
    raise SystemExit("Demucs 4.1.0 is required")
print(sys.executable)
print("variant=" + variant)
print("torch=" + torch.__version__)
print("cuda=" + str(cuda_version))
print("demucs=" + metadata.version("demucs"))
"@ | Set-Content -LiteralPath (Join-Path $buildDirectory "check_portable_environment.py") -Encoding utf8
$pythonDetails = & $python (Join-Path $buildDirectory "check_portable_environment.py")
$environmentExitCode = $LASTEXITCODE
if ($environmentExitCode -ne 0) {
    throw "The build Python does not satisfy the $Variant portable runtime contract. Install the matching PyTorch 2.13.0 wheel and Tools\requirements-portable.txt first."
}
$pythonDetails | ForEach-Object { Write-Host "  $_" }

Invoke-BuildStep "Building the Stemslayer and StemslayerWorker one-folder executables..." {
    & $python -m PyInstaller `
        --clean `
        --noconfirm `
        --distpath $distDirectory `
        --workpath $buildDirectory `
        $specPath
}

$bundleDirectory = Join-Path $distDirectory "Stemslayer"
$guiExecutable = Join-Path $bundleDirectory "Stemslayer.exe"
$workerExecutable = Join-Path $bundleDirectory "StemslayerWorker.exe"
foreach ($executable in @($guiExecutable, $workerExecutable)) {
    if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
        throw "PyInstaller did not produce the expected executable: $executable"
    }
}

Invoke-BuildStep "Running frozen entrypoint smoke tests..." {
    $guiSmoke = Start-Process -FilePath $guiExecutable -ArgumentList @("--self-test") -WindowStyle Hidden -Wait -PassThru
    if ($guiSmoke.ExitCode -ne 0) {
        throw "Stemslayer.exe --self-test failed with exit code $($guiSmoke.ExitCode)."
    }
    $workerSmoke = Start-Process -FilePath $workerExecutable -ArgumentList @("--help") -WindowStyle Hidden -Wait -PassThru
    if ($workerSmoke.ExitCode -ne 0) {
        throw "StemslayerWorker.exe --help failed with exit code $($workerSmoke.ExitCode)."
    }
}

$archiveName = "Stemslayer-v$Version-windows-x64-$Variant-portable.zip"
$archivePath = Join-Path $distDirectory $archiveName
$checksumPath = "$archivePath.sha256"
if (Test-Path -LiteralPath $archivePath) {
    Remove-Item -LiteralPath $archivePath -Force
}
if (Test-Path -LiteralPath $checksumPath) {
    Remove-Item -LiteralPath $checksumPath -Force
}

Invoke-BuildStep "Creating $archiveName..." {
    Compress-Archive -Path (Join-Path $bundleDirectory "*") -DestinationPath $archivePath -CompressionLevel Optimal
}

$checksum = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
"$checksum  $archiveName" | Set-Content -LiteralPath $checksumPath -Encoding ascii -NoNewline

Write-Host "Portable release created: $archivePath"
Write-Host "SHA-256: $checksum"
