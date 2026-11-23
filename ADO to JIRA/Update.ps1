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
if ($currentCommit -eq $previousCommit) {
    Write-Host "The app is already up to date."
    return
}

Write-Host "App updated successfully."