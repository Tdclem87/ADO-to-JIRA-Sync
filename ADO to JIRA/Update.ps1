$ErrorActionPreference = "Stop"

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$gitCommand = Get-Command git.exe -ErrorAction SilentlyContinue
if (-not $gitCommand) {
    throw "Git is required to update this checkout. Install Git for Windows and try again."
}

$gitPath = $gitCommand.Source
& $gitPath -C $repoRoot rev-parse --is-inside-work-tree *> $null
if ($LASTEXITCODE -ne 0) {
    throw "This folder is not inside a Git checkout. Clone the repository before using the updater."
}

$localChanges = & $gitPath -C $repoRoot status --porcelain --untracked-files=no
if ($LASTEXITCODE -ne 0) {
    throw "Could not check the working tree status."
}
if ($localChanges) {
    throw "Tracked files have local edits. Commit or back them up before updating; the updater will not overwrite them."
}

$previousCommit = (& $gitPath -C $repoRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "Could not identify the current commit."
}

Write-Host "Checking for updates..."
& $gitPath -C $repoRoot pull --ff-only
if ($LASTEXITCODE -ne 0) {
    throw "Update failed. Check your network connection and branch tracking, then try again."
}

$currentCommit = (& $gitPath -C $repoRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "Could not identify the updated commit."
}
$requirementsPath = Join-Path $PSScriptRoot "requirements.txt"
$requirementsHash = (Get-FileHash -Path $requirementsPath -Algorithm SHA256).Hash
$dependencyMarkerPath = (& $gitPath -C $repoRoot rev-parse --git-path ado-to-jira-dependencies.sha256).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "Could not locate the Git metadata directory for the dependency marker."
}
if (-not [System.IO.Path]::IsPathRooted($dependencyMarkerPath)) {
    $dependencyMarkerPath = Join-Path $repoRoot $dependencyMarkerPath
}

$installedRequirementsHash = $null
if (Test-Path $dependencyMarkerPath) {
    $installedRequirementsHash = (Get-Content -Path $dependencyMarkerPath -Raw).Trim()
}
if ($installedRequirementsHash -ne $requirementsHash) {
    $pythonPath = Join-Path $repoRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path $pythonPath)) {
        $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
        if (-not $pythonCommand) {
            throw "Python is required because this update changed dependencies. Install Python and run Update.cmd again."
        }
        $pythonPath = $pythonCommand.Source
    }

    Write-Host "Installing or refreshing Python dependencies..."
    & $pythonPath -m pip install -r $requirementsPath
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency update failed. Resolve the pip error; the launcher will retry on its next start."
    }

    [System.IO.File]::WriteAllText($dependencyMarkerPath, $requirementsHash)
}

if ($currentCommit -eq $previousCommit) {
    Write-Host "The app is already up to date."
    return
}

Write-Host "App updated successfully."