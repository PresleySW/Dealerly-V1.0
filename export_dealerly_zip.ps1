param(
    [string]$OutputDir = "."
)

$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot ".")).Path
$outDir = (Resolve-Path $OutputDir).Path
$ts = Get-Date -Format "yyyyMMdd_HHmmss"
$zipPath = Join-Path $outDir ("Dealerly_0.9_" + $ts + ".zip")
$tmpDir = Join-Path $env:TEMP ("dealerly_zip_" + [guid]::NewGuid().ToString("N"))

New-Item -ItemType Directory -Path $tmpDir | Out-Null

try {
    robocopy $root $tmpDir /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NP `
      /XD ".git" ".venv" "__pycache__" ".pytest_cache" "node_modules" ".mypy_cache" `
      /XF "dealerly.db" "dealerly.db-wal" "dealerly.db-shm" "dealerly_log.csv" "*.pyc" "*.pyo" "*.zip" > $null

    if (Test-Path $zipPath) {
        Remove-Item $zipPath -Force
    }
    Compress-Archive -Path (Join-Path $tmpDir "*") -DestinationPath $zipPath -CompressionLevel Optimal
    Write-Output ("Created: " + $zipPath)
}
finally {
    if (Test-Path $tmpDir) {
        Remove-Item $tmpDir -Recurse -Force
    }
}
