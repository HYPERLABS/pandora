<#
Activates the MTDR Python virtual environment and starts Jupyter Lab.
#>

# Get script directory
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$repoRoot = Resolve-Path "$scriptDir\..\..\..\.." | ForEach-Object { $_.Path }

# Navigate to the Python directory
$pythonDir = Join-Path $repoRoot "src\mtdr\python"
Set-Location $pythonDir

# Activate the virtual environment
& "$pythonDir\.venv\Scripts\Activate.ps1"

# Launch Jupyter Lab
jupyter lab --ip='*' --no-browser --port=9999
