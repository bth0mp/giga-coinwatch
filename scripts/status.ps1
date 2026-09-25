#requires -Version 5.1

. "$PSScriptRoot\_common.ps1"

$task = Get-CoinwatchTask
if (-not $task) {
    Write-Output 'giga-coinwatch startup is not installed.'
    return
}
$states = @('Unknown', 'Disabled', 'Queued', 'Ready', 'Running')
[pscustomobject]@{
    State = $states[[int]$task.State]
    LastRunTime = $task.LastRunTime
    LastTaskResult = $task.LastTaskResult
    Startup = 'At sign-in; daily scan timing is managed inside giga-coinwatch'
    Dashboard = 'http://127.0.0.1:8000/'
}
