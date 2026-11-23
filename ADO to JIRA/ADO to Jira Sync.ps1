$scriptName = "clean_ado_to_jira_sync.py"

Set-Location $PSScriptRoot
$updaterPath = Join-Path $PSScriptRoot "Update.ps1"
try {
    & $updaterPath
} catch {
    Write-Warning "Automatic update check failed: $($_.Exception.Message)"
    Write-Warning "Continuing with the currently available version."
}

$venvPath = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.venv"))
$pythonPath = Join-Path $venvPath "Scripts\python.exe"
if (-not (Test-Path $pythonPath)) {
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        & $pythonCommand.Source -m venv $venvPath
    } else {
        $pythonLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
        if (-not $pythonLauncher) {
            throw "Python 3 is required to run this app. Install Python; the launcher will install its packages automatically."
        }
        & $pythonLauncher.Source -3 -m venv $venvPath
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create the app's Python environment."
    }
}

Write-Host "Checking for pip updates..."
& $pythonPath -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    Write-Warning "pip could not be updated; continuing with the installed version."
}

$requirementsPath = Join-Path $PSScriptRoot "requirements.txt"
$requirementsHash = (Get-FileHash -Path $requirementsPath -Algorithm SHA256).Hash
$dependencyMarkerPath = Join-Path $venvPath "requirements.sha256"
$installedRequirementsHash = $null
if (Test-Path $dependencyMarkerPath) {
    $installedRequirementsHash = (Get-Content -Path $dependencyMarkerPath -Raw).Trim()
}
if ($installedRequirementsHash -ne $requirementsHash) {
    Write-Host "Downloading and installing Python dependencies..."
    & $pythonPath -m pip install -r $requirementsPath
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency installation failed. The launcher will retry when started again."
    }
    [System.IO.File]::WriteAllText($dependencyMarkerPath, $requirementsHash)
}

while ($true) {
    Write-Host ""
    Write-Host "===== Azure DevOps to Jira Sync =====" -ForegroundColor Green
    Write-Host "Starting a new run with $scriptName..."
    Write-Host "Using Python: $pythonPath"
    Write-Host ""

    & $pythonPath $scriptName

    Write-Host ""
    Write-Host "Run completed."
    Write-Host "Restarting from the first prompt..."
}
