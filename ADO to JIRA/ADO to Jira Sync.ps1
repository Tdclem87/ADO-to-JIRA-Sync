$scriptName = "clean_ado_to_jira_sync.py"

Set-Location $PSScriptRoot
$updaterPath = Join-Path $PSScriptRoot "Update.ps1"
try {
    & $updaterPath
} catch {
    Write-Warning "Automatic update check failed: $($_.Exception.Message)"
    Write-Warning "Continuing with the currently available version."
}

$pythonPath = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"
if (-not (Test-Path $pythonPath)) {
    $pythonPath = "python"
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
