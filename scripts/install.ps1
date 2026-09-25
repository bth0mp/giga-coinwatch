#requires -Version 5.1

. "$PSScriptRoot\_common.ps1"

$root = Get-CoinwatchRoot
$venv = Join-Path $root '.venv'
$python = Join-Path $venv 'Scripts\python.exe'
$pythonw = Join-Path $venv 'Scripts\pythonw.exe'
$constraints = Join-Path $root 'requirements.lock'
$dataDir = Get-CoinwatchDataDir

if (-not (Test-Path -LiteralPath $constraints -PathType Leaf)) {
    throw "Missing dependency lock file: $constraints"
}

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
        throw 'Python launcher (py.exe) is required. Install Python 3.13 with the launcher, then retry.'
    }
    & py -3.13 -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python 3.13 environment.' }
}

& $python -m pip install -c $constraints -e $root
if ($LASTEXITCODE -ne 0) { throw 'giga-coinwatch installation failed.' }
if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf)) {
    throw 'The Python environment does not contain pythonw.exe.'
}

New-Item -ItemType Directory -Path $dataDir -Force | Out-Null

$existing = Get-CoinwatchTask
if ($existing -and $existing.State -eq 4) {
    $existing.Stop(0)
}
$legacy = Get-CoinwatchTask -Name 'Coinwatch'
if ($legacy -and $legacy.State -eq 4) {
    $legacy.Stop(0)
}

# Task Scheduler stops asynchronously; wait for the old process's database lock.
$waitForStop = @'
import sys, time
from pathlib import Path
from coinwatch.__main__ import InstanceLock
deadline = time.monotonic() + 10
while True:
    try:
        with InstanceLock(Path(sys.argv[1])):
            break
    except ValueError:
        if time.monotonic() >= deadline:
            raise SystemExit('The previous app is still stopping. Retry installation shortly.')
        time.sleep(0.1)
'@
& $python -c $waitForStop $dataDir
if ($LASTEXITCODE -ne 0) { throw 'The previous app has not stopped yet.' }

$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$arguments = '-m coinwatch --data-dir "{0}" run --host 127.0.0.1 --port 8000' -f $dataDir
$service = New-Object -ComObject Schedule.Service
$service.Connect()
$definition = $service.NewTask(0)
$definition.RegistrationInfo.Description = 'giga-coinwatch private ancient coin monitor'
$definition.Principal.UserId = $identity
$definition.Principal.LogonType = 3 # Interactive token; no stored password
$definition.Principal.RunLevel = 0 # Current user's normal privileges
$trigger = $definition.Triggers.Create(9) # At logon
$trigger.UserId = $identity
$action = $definition.Actions.Create(0) # Execute
$action.Path = $pythonw
$action.Arguments = $arguments
$action.WorkingDirectory = $root
$definition.Settings.Enabled = $true
$definition.Settings.StartWhenAvailable = $true
$definition.Settings.DisallowStartIfOnBatteries = $false
$definition.Settings.StopIfGoingOnBatteries = $false
$definition.Settings.ExecutionTimeLimit = 'PT0S'
$definition.Settings.RestartInterval = 'PT1M'
$definition.Settings.RestartCount = 999
$definition.Settings.MultipleInstances = 2 # Ignore a duplicate start

$task = $service.GetFolder('\').RegisterTaskDefinition('giga-coinwatch', $definition, 6, $identity, $null, 3, $null)
if ($legacy) { $service.GetFolder('\').DeleteTask('Coinwatch', 0) }
$task.Run($null) | Out-Null

Write-Output "giga-coinwatch installed and started for $identity."
Write-Output 'Dashboard: http://127.0.0.1:8000/'
Write-Output "Data: $dataDir"
