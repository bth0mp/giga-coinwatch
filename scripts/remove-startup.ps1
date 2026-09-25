#requires -Version 5.1

. "$PSScriptRoot\_common.ps1"

$task = Get-CoinwatchTask
if (-not $task) {
    Write-Output 'giga-coinwatch startup is not installed.'
    return
}
if ($task.State -eq 4) { $task.Stop(0) }
(Get-CoinwatchFolder).DeleteTask('giga-coinwatch', 0)
Write-Output "giga-coinwatch startup removed. Data remains at $(Get-CoinwatchDataDir)."
