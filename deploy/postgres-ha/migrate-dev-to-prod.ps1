<#
.SYNOPSIS
    Copy the *precious* Crimsonhaven tables from the dev database into a freshly
    bootstrapped production cluster, keeping the donors (Lumi's Loved Mortals)
    and the existing accounts.

.DESCRIPTION
    Only these tables are moved, in foreign-key-safe order:
        accounts, favorites, watch_progress, kofi_transactions
    The TMDB<->AniList mapping tables and api_cache are deliberately NOT copied --
    production rebuilds them from Fribb on first boot. sessions/challenges are
    skipped too (users simply re-login; challenges have a 5-minute TTL).

    pg_dump emits the rows as COPY plus the setval() for the accounts identity
    sequence, so production keeps the same user_ids and the next signup won't
    collide. The production tables must already exist and be EMPTY -- i.e. let the
    API boot once against the cluster first so its init_db() creates the schema.

    Requires the PostgreSQL client tools (pg_dump, psql) on PATH. Install via the
    EDB installer, `winget install PostgreSQL.PostgreSQL`, or run from a box that
    has them.

.PARAMETER DevUrl
    libpq URL of the dev database, e.g.
        postgresql://crimson:crimson@dev-host:5432/crimson

.PARAMETER ProdUrl
    libpq URL of the production cluster (multi-host is fine), e.g.
        postgresql://crimson:PASS@10.0.0.11,10.0.0.12,10.0.0.13:5432/crimson?target_session_attrs=read-write

.EXAMPLE
    .\migrate-dev-to-prod.ps1 -DevUrl $dev -ProdUrl $prod
#>
param(
    [Parameter(Mandatory = $true)] [string] $DevUrl,
    [Parameter(Mandatory = $true)] [string] $ProdUrl,
    [string] $DumpFile = "crimson_precious.sql"
)

$ErrorActionPreference = "Stop"
$tables = @("accounts", "favorites", "watch_progress", "kofi_transactions")

function Get-RowCounts([string]$url) {
    $result = [ordered]@{}
    foreach ($t in $tables) {
        $n = & psql $url -tAc "SELECT count(*) FROM $t" 2>$null
        $result[$t] = if ($LASTEXITCODE -eq 0) { ($n | Out-String).Trim() } else { "n/a" }
    }
    return $result
}

Write-Host "==> Source (dev) row counts:" -ForegroundColor Cyan
$before = Get-RowCounts $DevUrl
$before.GetEnumerator() | ForEach-Object { "    {0,-18} {1}" -f $_.Key, $_.Value }

Write-Host "==> Dumping precious tables from dev -> $DumpFile" -ForegroundColor Cyan
$tableArgs = $tables | ForEach-Object { "--table=$_" }
& pg_dump --data-only --no-owner --no-privileges @tableArgs $DevUrl |
    Out-File -FilePath $DumpFile -Encoding utf8
if ($LASTEXITCODE -ne 0) { throw "pg_dump failed (exit $LASTEXITCODE)" }

Write-Host "==> Loading into production" -ForegroundColor Cyan
Write-Host "    (production tables must already exist and be empty)" -ForegroundColor DarkGray
& psql $ProdUrl -v ON_ERROR_STOP=1 -f $DumpFile
if ($LASTEXITCODE -ne 0) { throw "psql restore failed (exit $LASTEXITCODE)" }

Write-Host "==> Verifying -- dev vs prod row counts:" -ForegroundColor Cyan
$after = Get-RowCounts $ProdUrl
$ok = $true
foreach ($t in $tables) {
    $match = ($before[$t] -eq $after[$t])
    if (-not $match) { $ok = $false }
    $flag = if ($match) { "OK " } else { "MISMATCH" }
    "    {0,-18} dev={1,-6} prod={2,-6} [{3}]" -f $t, $before[$t], $after[$t], $flag
}

if ($ok) {
    Write-Host "`nAll counts match. Donors and accounts are in production." -ForegroundColor Green
} else {
    Write-Warning "Counts differ. If prod wasn't empty, TRUNCATE the four tables (CASCADE) and re-run."
    exit 1
}
