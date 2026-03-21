<# 
  Dealerly developer pipeline — runs ECC tests, Python compileall, Vite build;
  writes timestamped log + HTML summary and opens the HTML in the default browser.

  From repo root (d:\RHUL\Dealerly):
    powershell -ExecutionPolicy Bypass -File "Dealerly 1.0\scripts\dev_pipeline_report.ps1"

  Options:
    -RepoRoot <path>   Parent folder containing both "Dealerly 1.0" and package.json (default: inferred)
    -Loop               After each run, wait for Enter then repeat (Ctrl+C to exit)
    -NoBrowser          Do not open the HTML report

  MCP / Cursor (2026): Figma, Slack, GitLab, Chrome DevTools — see .cursorrules and prompts/GITLAB_PROJECT_TEMPLATE.md
#>
param(
  [string]$RepoRoot = "",
  [switch]$Loop,
  [switch]$NoBrowser
)

$ErrorActionPreference = "Continue"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$dealerly10 = Split-Path -Parent $here
if (-not $RepoRoot) {
  $RepoRoot = Split-Path -Parent $dealerly10
}

$logDir = Join-Path $dealerly10 "logs"
$reportDir = Join-Path $dealerly10 "reports"
foreach ($d in @($logDir, $reportDir)) {
  if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d | Out-Null }
}

function Html-Escape([string]$s) {
  if ($null -eq $s) { return "" }
  return [string]$s.Replace("&", "&amp;").Replace("<", "&lt;").Replace(">", "&gt;").Replace('"', "&quot;")
}

function Invoke-Step {
  param([string]$Name, [scriptblock]$Block)
  Write-Host "=== $Name ===" -ForegroundColor Cyan
  $sw = [System.Diagnostics.Stopwatch]::StartNew()
  $out = & $Block 2>&1 | ForEach-Object { "$_" }
  $sw.Stop()
  $ec = if ($null -ne $LASTEXITCODE) { $LASTEXITCODE } else { 0 }
  return @{
    Name     = $Name
    Ok       = ($ec -eq 0)
    ExitCode = $ec
    Ms       = [int]$sw.ElapsedMilliseconds
    Output   = $out
  }
}

function Run-PipelineOnce {
  param([string]$Stamp, [string]$LogPath, [string]$HtmlPath)

  $results = @()
  $results += Invoke-Step "npm test (ECC root)" {
    Set-Location $RepoRoot
    npm test
  }
  $results += Invoke-Step "python compileall (Dealerly 1.0\dealerly)" {
    Set-Location $dealerly10
    python -m compileall dealerly -q
  }
  $results += Invoke-Step "npm run build (Vite dashboard)" {
    Set-Location $RepoRoot
    npm run build
  }

  "--- $(Get-Date -Format o) ---" | Out-File -FilePath $LogPath -Encoding utf8
  foreach ($r in $results) {
    "" | Out-File -FilePath $LogPath -Append -Encoding utf8
    "[$($r.Name)] exit=$($r.ExitCode) ms=$($r.Ms) ok=$($r.Ok)" | Out-File -FilePath $LogPath -Append -Encoding utf8
    $r.Output | Out-File -FilePath $LogPath -Append -Encoding utf8
  }

  $rows = ""
  $allOk = $true
  foreach ($r in $results) {
    if (-not $r.Ok) { $allOk = $false }
    $cls = if ($r.Ok) { "ok" } else { "fail" }
    $snippet = ($r.Output | Select-Object -Last 30) -join "`n"
    $snippet = Html-Escape $snippet
    $rows += "<tr class='$cls'><td><strong>$(Html-Escape $r.Name)</strong></td><td>$($r.ExitCode)</td><td>$($r.Ms)</td><td>$(if ($r.Ok) { 'PASS' } else { 'FAIL' })</td></tr>"
    $rows += "<tr class='detail'><td colspan='4'><pre>$snippet</pre></td></tr>"
  }

  $rp = Html-Escape $RepoRoot
  $lp = Html-Escape $LogPath
  $html = @"
<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Dealerly dev pipeline — $Stamp</title>
<style>
body{font-family:system-ui,Segoe UI,sans-serif;margin:24px;background:#0f172a;color:#e2e8f0;}
h1{font-size:1.25rem;margin:0 0 8px;}
.meta{color:#94a3b8;font-size:0.9rem;margin-bottom:20px;}
table{border-collapse:collapse;width:100%;max-width:1100px;}
th,td{border:1px solid #334155;padding:8px 12px;text-align:left;vertical-align:top;}
th{background:#1e293b;}
tr.ok td:first-child{border-left:4px solid #22c55e;}
tr.fail td:first-child{border-left:4px solid #ef4444;}
tr.detail td{background:#020617;font-size:12px;}
pre{white-space:pre-wrap;word-break:break-word;margin:0;max-height:240px;overflow:auto;}
.banner{margin:16px 0;padding:12px 16px;border-radius:8px;background:#1e293b;border:1px solid #334155;}
.banner.fail{border-color:#ef4444;background:#450a0a;}
.banner.ok{border-color:#22c55e;background:#052e16;}
code{color:#a5f3fc;}
</style></head><body>
<h1>Dealerly developer pipeline</h1>
<div class="meta">v1.0.0-rc.1 · $Stamp · Repo: <code>$rp</code></div>
<div class="banner $(if ($allOk) { 'ok' } else { 'fail' })">
  <strong>Overall:</strong> $(if ($allOk) { 'All steps exited 0.' } else { 'One or more steps failed. Note: ECC npm test often reports Windows-only failures (chmod, paths); Python + Vite are the Dealerly app gates.' })
</div>
<p>Full log: <code>$lp</code></p>
<table>
<thead><tr><th>Step</th><th>Exit</th><th>ms</th><th>Result</th></tr></thead>
<tbody>$rows</tbody>
</table>
<p style="margin-top:24px;color:#94a3b8;font-size:0.85rem;">Speed targets: Phase 1 concurrency (<code>pipeline.py</code>), eBay parallel queries (<code>ingestion.py</code>), <code>DEALERLY_NO_FACEBOOK=1</code> for fast cycles. GitLab: <code>prompts/GITLAB_PROJECT_TEMPLATE.md</code>.</p>
</body></html>
"@
  $utf8 = New-Object System.Text.UTF8Encoding $false
  [System.IO.File]::WriteAllText($HtmlPath, $html, $utf8)

  if (-not $NoBrowser) {
    Start-Process $HtmlPath
  }
  Write-Host "Report: $HtmlPath" -ForegroundColor Green
  return $allOk
}

$exitCode = 0
do {
  $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
  $logPath = Join-Path $logDir "dev_pipeline_$stamp.log"
  $htmlPath = Join-Path $reportDir "dev_pipeline_report_$stamp.html"
  $ok = Run-PipelineOnce -Stamp $stamp -LogPath $logPath -HtmlPath $htmlPath
  if (-not $ok) { $exitCode = 1 }
  if ($Loop) {
    Write-Host "`nLoop: Enter = next run, Ctrl+C = exit" -ForegroundColor Yellow
    [void][System.Console]::ReadLine()
  }
} while ($Loop)

exit $exitCode
