# giga-coinwatch

giga-coinwatch is a self-hosted ancient-coin listing monitor for a personal collection. Its local dashboard shows observed fixed-price listings, scan history, source health and dealer discovery leads. The first full scan of a source records its existing stock; later observations can appear as new coins. A saved source is not necessarily an active scraper: check **Sources** for actual coverage and errors. giga-coinwatch does not search the entire web.

The five enabled dealer scopes are:

| Dealer | Monitored stock |
| --- | --- |
| Silbury Coins | Roman Republican |
| Historynumis | Ancient coins |
| RomanCoinShop | Roman Republic |
| MRB Coins | Roman Republic, $35 or less |
| Edward J. Waddell / Coin.com | Roman Republican silver |

Other sellers remain saved for review. Dealers with restrictions on automated collection stay disabled. Coverage is limited to the categories above, not each shop's full inventory; see [source validation](docs/source-validation.md) for evidence and limitations. Discovery checks public dealer directories and queues additional domains for review, with supporting shop links when it can verify fixed-price ancient stock. Accepting a dealer saves it to Sources; recurring monitoring needs a validated parser.

Choose **one** running mode for a collection database. Both modes serve the dashboard at <http://127.0.0.1:8000/> and run the daily schedule inside giga-coinwatch. Keep the PC awake for scheduled scans.

## Windows background app

Install Python 3.13 with the Windows `py` launcher. Open PowerShell in this project folder and run:

```powershell
.\scripts\install.ps1
```

This creates `.venv`, installs giga-coinwatch using the versions in `requirements.lock`, and starts a current-user **giga-coinwatch** logon task. The task launches `pythonw.exe` without a console. Open the dashboard in a browser; closing the browser leaves giga-coinwatch running. Data and diagnostic logs default to `%LOCALAPPDATA%\Coinwatch`. The existing data directory is retained when upgrading from the previous Coinwatch name. To choose a different absolute data path, set `COINWATCH_DATA_DIR` before installing and use that same path with manual commands.

```powershell
.\scripts\status.ps1
.\scripts\stop.ps1
.\scripts\start.ps1
.\scripts\remove-startup.ps1
```

`remove-startup.ps1` stops and removes the task, leaving the database and logs in place. Re-run `install.ps1` after updating the project to refresh its environment and task. The task starts after this user signs in and ends at sign-out; Windows must stay awake and signed in for scans. Task Scheduler restarts a failed process but does not create a separate daily scan job.

The installed `giga-coinwatch` command is also available; `python -m coinwatch` remains supported. For CLI status, use the installed interpreter after setting `$dataDir` to the installation's data path. For a one-off CLI scan, stop the background task first; while the app is running, use its **Scan now** button instead:

```powershell
$dataDir = Join-Path $env:LOCALAPPDATA 'Coinwatch'
.\.venv\Scripts\python.exe -m coinwatch --data-dir $dataDir status
.\scripts\stop.ps1
.\.venv\Scripts\python.exe -m coinwatch --data-dir $dataDir scan
.\scripts\start.ps1
```

Do not run a manual scan at the same time as another installation. giga-coinwatch uses a run lease to prevent overlapping scans within one database.

## Docker

With Docker Engine or Docker Desktop running, from this project folder:

```powershell
docker compose up -d --build
docker compose ps
docker compose logs -f giga-coinwatch
```

The container runs as a non-root user. Its database and settings live in the named `coinwatch_data` volume, which survives container replacement. The dashboard is published only to the host's `127.0.0.1:8000`. The container has a `/health` check and a restart policy. Docker and the PC must be running and awake; the restart policy cannot start Docker or wake the PC.

Docker packaging is included, but image startup and volume replacement have not been tested on this PC because Docker is not installed. The native Windows installation has been exercised; see [validation notes](docs/packaging-validation.md).

```powershell
docker compose stop
docker compose up -d --build
```

Do not run the native task and Docker service together on port 8000. Stop one before starting the other.

## Backup and migration

The `backup` and `restore` CLI commands preserve the collection database, including source baselines, observed identities, saved coins, settings and discovery decisions. Keep a copy of the backup outside the app data directory or Docker volume. Stop the old installation before restoring into and starting a different mode; never mount one live SQLite database in both modes.

For a native backup, stop the task and use the same data path as its installation:

```powershell
.\scripts\stop.ps1
$dataDir = Join-Path $env:LOCALAPPDATA 'Coinwatch'
$backup = Join-Path (Get-Location).Path 'coinwatch-backup.sqlite'
.\.venv\Scripts\python.exe -m coinwatch --data-dir $dataDir backup $backup
```

For a native restore, keep the task stopped, then run:

```powershell
$dataDir = Join-Path $env:LOCALAPPDATA 'Coinwatch'
$backup = (Resolve-Path .\coinwatch-backup.sqlite).Path
.\.venv\Scripts\python.exe -m coinwatch --data-dir $dataDir restore $backup
.\scripts\start.ps1
```

For a Docker backup, stop the service first and use a one-off container attached to its volume. This creates a backup in the volume; copy it to the host after starting the service again, then remove that temporary copy from the volume:

```powershell
docker compose stop
docker compose run --rm --no-deps giga-coinwatch python -m coinwatch backup /data/coinwatch-backup.sqlite
docker compose up -d
docker compose cp giga-coinwatch:/data/coinwatch-backup.sqlite .\coinwatch-backup.sqlite
docker compose exec -T giga-coinwatch rm /data/coinwatch-backup.sqlite
```

If migrating away from Docker, stop it again after copying the backup and keep it stopped while restoring and starting the native task. To restore into Docker, keep the service stopped and give a one-off container read-only access to the backup file:

```powershell
docker compose stop
$backup = (Resolve-Path .\coinwatch-backup.sqlite).Path
docker compose run --rm --no-deps -v "${backup}:/import.sqlite:ro" giga-coinwatch python -m coinwatch restore /import.sqlite
docker compose up -d
```

`docker compose down` leaves the named volume in place. Do not use `docker compose down --volumes` unless you intend to delete that data.

## Running limits

Listings link to sellers; giga-coinwatch does not purchase coins or authenticate them. New means **first found by giga-coinwatch**, not necessarily newly listed by a seller. Source pages can change, block automation or contain auction and sold stock, so check the dashboard's source health before relying on a scan. The app has no cloud hosting or paid search dependency.
