<#
This script compiles the pulser examples for Windows.
Operating systems supported: Windows
#>

# Get the script directory.
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$repoRoot = Resolve-Path "$scriptDir\..\..\..\.." | ForEach-Object { $_.Path }
$protoDir = Join-Path $repoRoot "src\pulser\proto"

# Check for Python.
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Error "Please install Python"
    exit 1
}

# Check Python version >= 3.12.
Write-Host "Building pulser python..."
python -c "import sys; exit(0 if sys.version_info >= (3,12) else 1)"
if ($LASTEXITCODE -ne 0) {
    $pyVersion = (python --version) -replace ".*?(\d+\.\d+\.\d+).*", '$1'
    Write-Error "Python version installed is $pyVersion. Version 3.12 or newer is required!"
    exit 1
}

# Set up Python venv and install dependencies.
$pythonDstDir = Join-Path $repoRoot "src\pulser\python"
Set-Location $pythonDstDir

python -m venv .venv
& "$pythonDstDir\.venv\Scripts\Activate.ps1"
python -m pip install --upgrade pip
python -m pip install -r requirements_windows.txt

# Create the output dir.
$outputDir = Join-Path $pythonDstDir "generated"
New-Item -ItemType Directory -Path $outputDir -Force | Out-Null

# Compile proto files.
Get-ChildItem -Path $protoDir -Filter *.proto | ForEach-Object {
    $protoFile = $_.FullName
    Write-Host "Compiling $protoFile"
    python -m grpc_tools.protoc `
        -Igenerated="$protoDir" `
        -I"$protoDir" `
        --python_out="$pythonDstDir" `
        --pyi_out="$pythonDstDir" `
        --grpc_python_out="$pythonDstDir" `
        "$protoFile"
}

Write-Host "Completed building"
Write-Host "** To interact with the python getting started notebook run: $repoRoot\scripts\windows\run\pulser-run-grpc-example-pynb.cmd **"

# Return to original directory.
Set-Location $scriptDir
