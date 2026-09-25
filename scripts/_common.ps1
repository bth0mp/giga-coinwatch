#requires -Version 5.1

$ErrorActionPreference = 'Stop'

function Get-CoinwatchRoot {
    return [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
}

function Get-CoinwatchDataDir {
    if (-not [string]::IsNullOrWhiteSpace($env:COINWATCH_DATA_DIR)) {
        if (-not [IO.Path]::IsPathRooted($env:COINWATCH_DATA_DIR)) {
            throw 'COINWATCH_DATA_DIR must be an absolute path.'
        }
        return [IO.Path]::GetFullPath($env:COINWATCH_DATA_DIR)
    }
    return Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'Coinwatch'
}

function Get-CoinwatchPython {
    $python = Join-Path (Get-CoinwatchRoot) '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "giga-coinwatch is not installed. Run .\scripts\install.ps1 first."
    }
    return $python
}

function Get-CoinwatchTask {
    param([string]$Name = 'giga-coinwatch')
    $folder = Get-CoinwatchFolder
    try {
        return $folder.GetTask($Name)
    } catch [System.IO.FileNotFoundException] {
        return $null
    }
}

function Get-CoinwatchFolder {
    $service = New-Object -ComObject Schedule.Service
    $service.Connect()
    return $service.GetFolder('\')
}
