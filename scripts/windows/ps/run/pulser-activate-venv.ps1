<#
Activates the Python virtual environment for the Pulser project.
#>

# Get script directory
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$repoRoot = Resolve-Path "$scriptDir\..\..\..\.." | ForEach-Object { $_.Path }

# Navigate to the Python source directory
$pythonDir = Join-Path $repoRoot "src\pulser\python"
Set-Location $pythonDir

# Activate the virtual environment
& "$pythonDir\.venv\Scripts\Activate.ps1"
