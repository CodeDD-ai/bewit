# Reset the ArchRev demo fixtures (docs/demo.md). Run from anywhere:
#   powershell -ExecutionPolicy Bypass -File .test/demo/reset.ps1
#
# Restores the files the demo prompts touch and removes the ones they
# create. It never touches .env files, .archrev/, or anything outside
# .test/. Session records of earlier demo runs stay (they are the audit
# trail); start a NEW agent conversation for a clean session.

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $root

function Write-Fixture([string]$Path, [string]$Content) {
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
    # UTF-8 without BOM, LF line endings, so diffs stay clean.
    [System.IO.File]::WriteAllText((Join-Path $root $Path), ($Content -replace "`r`n", "`n"))
}

# Baseline files the demo edits.
Write-Fixture ".test/free/app.py" @"
"""Demo app for the ArchRev live demo (docs/demo.md)."""


def add(a: int, b: int) -> int:
    return a + b
"@

Write-Fixture ".test/protected/settings.ini" @"
; Demo settings - protected by the test-path-block rule.
[server]
port = 8080
"@

Write-Fixture ".test/flagged/attempt.txt" @"
T03: this write should be allowed but flagged by test-path-flag.
"@

# Files the demo creates.
$created = @(
    ".test/free/test_app.py",
    ".test/free/notes.txt",
    ".test/forbidden/notes.txt",
    ".test/check/util.py"
)
foreach ($path in $created) {
    if (Test-Path $path) { Remove-Item -Force $path }
}

Write-Host "Demo fixtures reset in $root"
Write-Host "Next: commit or stash other changes, run 'archrev serve', and start a NEW agent conversation."
