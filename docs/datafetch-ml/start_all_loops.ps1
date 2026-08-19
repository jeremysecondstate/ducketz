$ErrorActionPreference = 'Stop'

# This launcher starts only the seven recurring production owners. Empty-store
# ALFRED and included Standard-plan Databento bootstraps remain explicit,
# one-time maintenance commands documented in current_start_command.

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Ducketz virtual-environment Python was not found at $python"
}

$owners = @(
    [pscustomobject]@{
        Title = '1 - Ducketz CME-L2'
        Arguments = @('-m', 'datafetching.cme_runtime', '--datastore-target', 'pc', '--max-concurrency', '1')
    },
    [pscustomobject]@{
        Title = '2 - Ducketz Loop A'
        Arguments = @('-m', 'datafetching.orchestrate', '--datastore-target', 'pc', '--watchlist', 'datafetching\watchlist.txt', '--providers', 'databento', 'fmp', 'fred', 'schwab', 'sec', '--cme-mode', 'external', '--options-mode', 'external', '--interval-minutes', '15', '--bar-readiness-recovery-timeout-seconds', '420', '--bar-readiness-recovery-poll-seconds', '10')
    },
    [pscustomobject]@{
        Title = '3 - Ducketz Daily ALFRED'
        Arguments = @('-m', 'datafetching.fred_alfred_runtime', '--datastore-target', 'pc', '--utc-hour', '7')
    },
    [pscustomobject]@{
        Title = '4 - Ducketz Active Pricing - logical Loop 3'
        Arguments = @('-m', 'ml.option_pricing_runtime', '--datastore-target', 'pc', '--watchlist', 'datafetching\watchlist.txt', '--interval-minutes', '15', '--phase-offset-minutes', '1', '--bar-readiness-mode', 'required', '--bar-readiness-timeout-seconds', '480')
    },
    [pscustomobject]@{
        Title = '5 - Ducketz Options Capture - logical Loop 4'
        Arguments = @('-m', 'datafetching.options_runtime', '--datastore-target', 'pc', '--watchlist', 'datafetching\watchlist.txt', '--provider-mode', 'opra-canonical', '--interval-minutes', '15', '--phase-offset-minutes', '6', '--pricing-barrier-timeout-seconds', '45', '--bar-readiness-mode', 'required')
    },
    [pscustomobject]@{
        Title = '6 - Ducketz Directional Loop B'
        Arguments = @('-m', 'ml.prediction_runtime', '--datastore-target', 'pc', '--watchlist', 'datafetching\watchlist.txt', '--provider', 'databento', '--horizons', '1h', '4h', '1d', '1w', '--feature-profile', 'loop-a-all-bsgp-active-v3', '--model-family', 'logistic', '--calibration', 'platt', '--round-trip-cost', '0.001', '--interval-minutes', '15', '--phase-offset-minutes', '5')
    },
    [pscustomobject]@{
        Title = '7 - Ducketz Strategy'
        Arguments = @('-m', 'ml.strategy_runtime', '--datastore-target', 'pc', '--interval-minutes', '15', '--phase-offset-minutes', '10', '--pricing-mode', 'active')
    }
)

$escapedRoot = $repoRoot.Replace("'", "''")
$escapedPython = $python.Replace("'", "''")
foreach ($owner in $owners) {
    $escapedTitle = $owner.Title.Replace("'", "''")
    $quotedArguments = $owner.Arguments | ForEach-Object {
        "'" + ([string]$_).Replace("'", "''") + "'"
    }
    $command = (
        "Set-Location -LiteralPath '$escapedRoot'; " +
        "`$Host.UI.RawUI.WindowTitle = '$escapedTitle'; " +
        "& '$escapedPython' " +
        ($quotedArguments -join ' ')
    )
    Start-Process -FilePath 'powershell.exe' -ArgumentList (
        "-NoExit -ExecutionPolicy Bypass -Command `"$command`""
    )
}
