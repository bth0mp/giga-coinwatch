#requires -Version 5.1

. "$PSScriptRoot\_common.ps1"

$task = Get-CoinwatchTask
if (-not $task) { throw 'giga-coinwatch startup is not installed. Run .\scripts\install.ps1 first.' }
if ($task.State -ne 4) { $task.Run($null) | Out-Null }
Write-Output 'giga-coinwatch start requested. Dashboard: http://127.0.0.1:8000/'
