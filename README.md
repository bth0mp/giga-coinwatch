# giga-coinwatch

giga-coinwatch is a self-hosted ancient-coin listing monitor for a personal collection. Its local dashboard shows observed fixed-price listings, wanted coin searches, scan history, source health and dealer discovery leads. The first full scan of a source records its existing stock; later observations can appear as new coins. A saved source is not necessarily an active scraper: check **Sources** for actual coverage and errors. Optional wider-web search adds unverified leads from a search provider; coverage is not exhaustive.

The five dealer scopes enabled by default are:

| Dealer | Monitored stock |
| --- | --- |
| Silbury Coins | Roman Republican |
| Historynumis | Ancient coins |
| RomanCoinShop | Roman Republic |
| MRB Coins | Roman Republic, $35 or less |
| Edward J. Waddell / Coin.com | Roman Republican silver |

The bundled registry contains 81 dealer/storefront entries, including additional ancient and mixed-period sellers saved for review. Dealers with restrictions on automated collection stay disabled. Coverage is limited to the categories above, not each shop's full inventory; see [source validation](docs/source-validation.md) for evidence and limitations. Discovery checks a public dealer directory and, when Tavily is configured, searches the wider web for additional domains. Leads enter a review queue, with supporting shop links when fixed-price ancient stock can be confirmed. Accepting a dealer saves it to Sources; recurring monitoring needs a validated parser.

Choose **one** running mode for a collection database. Both modes serve the dashboard at <http://127.0.0.1:8000/> and run the daily schedule inside giga-coinwatch. Keep the PC awake for scheduled scans.

## Wanted coins and scan controls

Open **Wanted coins** to save a search by coin type, mint, ruler, keywords, exclusions, category, currency and maximum price. For example, enter `denarius` as the type, `Rome` as the mint, and `Hadrian` as the ruler. All entered words must occur in the seller's title or category. Put a phrase in double quotes to keep its words together; any excluded word or phrase rejects a local match. Matching ignores case and accents, but does not infer attributes, translate mint names, or expand synonyms. A price limit requires a currency; no exchange-rate conversion is performed.

Matches include available coins from the initial inventory and subsequent scans, with a link to the seller and the last observed price. These are seller-text matches, not verified coin attributions. Pausing a wanted search stops its automatic web queries; the saved local match view stays usable. Editing the criteria clears old web leads for that search. Deleting a search leaves catalog and saved coins intact.

- **Scan coins** refreshes enabled, supported dealer stock and checks enabled wanted searches on the web when configured.
- **Find dealers** searches Tavily when configured and checks the supported public directory for additional dealer candidates; it does not rescan coin inventory. Review leads under **Discoveries**. A newly accepted dealer still needs a validated parser before monitoring can be enabled.
- **Scan both** does both jobs. The daily schedule uses this mode.
- **Scan for this search** refreshes dealer stock and runs the selected wanted search on the web if opted in. Choose any whole number from **1–50 web queries** beside the button; the credit/result estimate updates before you start. It also works for a paused search as an explicit one-off check.

Only one scan runs at a time. A coin-only, dealer-only or targeted scan does not replace the next daily combined check.

### Optional wider-web setup

Monitored-dealer searches and directory discovery work without an API key. Each wanted search also has an **Open web search** link for manual use. To enable automatic web leads later, create a [Tavily account](https://app.tavily.com/) and enter its API key in **Settings → Wider-web search**. As checked on 25 September 2026, Tavily offers 1,000 free API credits per month without a payment card; check its [current allowance and pricing](https://docs.tavily.com/documentation/api-credits) before enabling paid usage.

giga-coinwatch uses basic searches (one credit per query). Ordinary daily and global coin scans use one query for each of up to five enabled wanted searches, prioritizing those least recently attempted. More than five enabled searches rotate across scans. A manual search for one saved search can instead use your chosen budget of 1–50 queries, with up to twenty results per query before duplicates. There are no automatic API retries, and the app does not enable billing or purchase credits. Searches send the coin criteria and exclusions to Tavily; price and currency limits apply to catalog matches, not unverified web leads.

Broader scans vary shopping phrases in English, German, French, Spanish, Italian and Portuguese, preserving your entered coin criteria and exclusions. They search the indexed public web without a country filter or restriction to your registered dealers. Later queries can exclude domains already returned to reach additional shops. This increases coverage but cannot guarantee every site or coin will be found. The query count is a maximum: the run may stop early on a provider error, edited/deleted search, cancellation, or because fewer distinct queries fit the provider's length limit.

Dealer discovery uses up to two additional basic queries per dealer/combined scan, rotating among ancient-coin categories. Each requests up to twenty search results; at most ten new domains from web search are inspected, with at most three public pages per domain. Up to 150 known domains are excluded at the provider, and all known/dismissed domains are filtered locally. Ordinary combined scans therefore use at most seven basic queries; a CLI combined scan for one wanted search adds up to two dealer queries to its chosen coin-search budget. Search snippets alone never confirm a dealer's stock: inaccessible or unclear pages remain labelled for review. IAPN and Artemis interactive directories, and the access-blocked VCoins directory, are not automatic static-directory inputs.

Web results are clearly labelled **unverified** and can include auctions, sold coins or pages without a price. They never enter the monitored-dealer catalog automatically. Results accumulate per wanted search, with duplicate URLs combined and a limit of 1,000 retained leads. A small daily scan does not erase an earlier broad search. Each lead's **Last found** date shows when it was last returned; the latest scan date does not reverify older leads. Successful results survive partial provider failures, with the failure displayed alongside them. Changing the search criteria clears the previous criteria's leads.

The key is stored separately from the collection database in `web-search.json` in the app data directory (inside `/data` for Docker). It is never shown back in the dashboard. You can remove it in Settings. An optional `TAVILY_API_KEY` environment variable on the running process takes precedence over the local file; remove that variable and restart to stop using an environment key. For a normal setup, use Settings so no environment configuration is needed.

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

The installed `giga-coinwatch` command is also available; `python -m coinwatch` remains supported. For CLI status, use the installed interpreter after setting `$dataDir` to the installation's data path. For a one-off CLI scan, stop the background task first; while the app is running, use its dashboard scan buttons instead:

```powershell
$dataDir = Join-Path $env:LOCALAPPDATA 'Coinwatch'
.\.venv\Scripts\python.exe -m coinwatch --data-dir $dataDir status
.\scripts\stop.ps1
.\.venv\Scripts\python.exe -m coinwatch --data-dir $dataDir scan
.\scripts\start.ps1
```

Do not run a manual scan at the same time as another installation. giga-coinwatch uses a run lease to prevent overlapping scans within one database.

CLI scans accept `scan --mode coins`, `scan --mode dealers`, or `scan --mode both` (the default). To refresh a wanted search, use `scan --mode coins --search-id 1 --web-queries 12`, replacing `1` with the ID in that search's dashboard URL and `12` with your chosen query budget. Omitting `--web-queries` uses one query. Budgets above one require a specific wanted search with wider-web checking enabled.

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

The `backup` and `restore` CLI commands preserve the collection database, including source baselines, observed identities, saved coins, wanted searches and web leads, schedule settings and discovery decisions. API keys are excluded: re-enter the key in Settings after moving to another installation. Keep a copy of the backup outside the app data directory or Docker volume. Stop the old installation before restoring into and starting a different mode; never mount one live SQLite database in both modes.

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

Listings link to sellers; giga-coinwatch does not purchase coins or authenticate them. New means **first found by giga-coinwatch**, not necessarily newly listed by a seller. Source pages can change, block automation or contain auction and sold stock, so check the dashboard's source health before relying on a scan. The app runs locally; wider-web search is an optional external service.
