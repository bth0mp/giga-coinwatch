# giga-coinwatch

giga-coinwatch is a self-hosted ancient-coin listing monitor for a personal collection. Its local dashboard shows observed fixed-price listings, wanted coin searches, scan history, source health and dealer discovery leads. The first full scan of a source records its existing stock; later observations can appear as new coins. A saved source is not necessarily an active scraper: check **Sources** for actual coverage and errors. Optional wider-web search shows individual coin listings only after the seller's page provides evidence of a fixed price and purchase availability; coverage is not exhaustive.

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

## Search and scan controls

Open the **Search** tab, select a saved search, enter **1–50 Web queries** and **1–120 Web check time (minutes)**, then click **Search now**. The time limit defaults to 10 minutes and covers web queries plus sale checks; monitored-dealer scanning has its own budget. The estimated Tavily credit usage updates beside the query control. Increasing the time limit does not increase your selected query or credit limit. Results open on that search's detail page. The older `/wanted` address still works.

Use **New saved search** on the same page to enter a coin type, mint, ruler, keywords, exclusions, category, currency and maximum price. For example, enter `denarius` as the type, `Rome` as the mint, and `Hadrian` as the ruler. All entered words must match the seller's title or category. Matching ignores case and accents and recognizes a limited set of Boeotia spelling variants, including `Boiotian` and `Boetia`, as the same term. Unrelated words remain separate requirements. Put a phrase in double quotes for literal phrase matching; any excluded word or phrase removes a match. Web results also enforce your terms, exclusions, currency and maximum price using the verified seller title and offer, not the search snippet. A price limit requires a currency; no exchange-rate conversion is performed.

Matches include available coins from the initial inventory and subsequent scans, with a link to the seller and the last observed price. These are seller-text matches, not verified coin attributions. Pausing a wanted search stops its automatic web queries; the saved local match view stays usable. Editing the criteria clears old web leads for that search. Deleting a search leaves catalog and saved coins intact.

To remove a saved search, choose **Delete** beside it on the **Search** page, or **Delete search** on its results page. The confirmation names the search being removed. Confirming deletes its criteria and web results, stops its daily checks, and returns to Search; **Cancel** keeps it.

- **Scan coins** refreshes enabled, supported dealer stock and checks enabled wanted searches on the web when configured.
- **Find dealers** searches Tavily when configured and checks the supported public directory for additional dealer candidates; it does not rescan coin inventory. Review leads under **Discoveries**. A newly accepted dealer still needs a validated parser before monitoring can be enabled.
- **Scan both** does both jobs. The daily schedule uses this mode.
- **Scan for this search** refreshes dealer stock and runs the selected wanted search on the web if opted in. Choose **1–50 web queries** and **1–120 minutes** beside the button; the credit/result estimate updates before you start. It also works for a paused search as an explicit one-off check. These per-run controls do not change daily scan limits.

Only one scan runs at a time. A coin-only, dealer-only or targeted scan does not replace the next daily combined check.

### Optional wider-web setup

Monitored-dealer searches and directory discovery work without an API key. Each wanted search also has an **Open web search** link for manual use. To enable automatic web leads later, create a [Tavily account](https://app.tavily.com/) and enter its API key in **Settings → Wider-web search**. As checked on 25 September 2026, Tavily offers 1,000 free API credits per month without a payment card; check its [current allowance and pricing](https://docs.tavily.com/documentation/api-credits) before enabling paid usage.

giga-coinwatch uses basic searches (one credit per query). Ordinary daily and global coin scans use one query for each of up to five enabled wanted searches, prioritizing those least recently attempted. More than five enabled searches rotate across scans. A manual search for one saved search can instead use your chosen budget of 1–50 queries, with up to twenty candidate links per query before duplicates and sale checks. There are no automatic API retries, and the app does not enable billing or purchase credits. Searches send the coin criteria and exclusions to Tavily; price and currency filters apply locally to both monitored-catalog and verified web listings without currency conversion.

Broader scans vary explicit sale and in-stock wording and supported spelling variants, preserving your coin criteria and exclusions. They search the indexed public web without a country filter or restriction to your registered dealers. Finding a result on a dealer's domain no longer excludes that dealer from later coin queries. Coverage cannot guarantee every site or coin will be found. The query count is a maximum: the run may stop early on a provider error, edited/deleted search, cancellation, the sale-check time budget, or because fewer distinct queries fit the provider's length limit.

Dealer discovery uses up to two additional basic queries per dealer/combined scan, rotating among ancient-coin categories. Each requests up to twenty search results; at most ten new domains from web search are inspected, with at most three public pages per domain. Up to 150 known domains are excluded at the provider, and all known/dismissed domains are filtered locally. Ordinary combined scans therefore use at most seven basic queries; a CLI combined scan for one wanted search adds up to two dealer queries to its chosen coin-search budget. Search snippets alone never confirm a dealer's stock: inaccessible or unclear pages remain labelled for review. IAPN and Artemis interactive directories, and the access-blocked VCoins directory, are not automatic static-directory inputs.

Tavily results are candidate links, not automatically accepted listings. The scanner opens each candidate through the normal public-URL and robots checks. Only an individual coin product with a positive fixed price, currency and purchase-availability evidence appears in **For sale on the wider web**. Articles, reference pages, social posts, category pages, auctions, sold stock and replicas are excluded. Blocked or unclear pages stay hidden. Product-page identity checks prevent a related product elsewhere on an article from qualifying the article as a listing.

Words meaning only “old” or “antique” do not establish that a coin is ancient; the title needs a clearer ancient-period or denomination signal. Postcards, banknotes and mixed coin lots are excluded even when they mention ancient coins and have a purchase button. Current title checks also apply to previously saved web results.

Displayed web listings show the seller's price and **Availability checked** time. Checks older than 24 hours are hidden until rechecked. Known available matches across the selected searches are checked before fresh queries. Unverified pages are retried after 24 hours, rejected pages after 7 days; rediscovering the same URL does not bypass this waiting period or refresh its check timestamp. Checks stopped by the time budget or cancellation remain eligible for an immediate retry. After new queries, due unverified candidates take priority over due rejected pages. These page checks also work without a Tavily key and use no Tavily credits. The web phase has a ten-minute budget for daily and global scans. Manual searches for one saved search can use a chosen limit of 1–120 minutes (default 10). If the limit is reached, remaining wanted-search queries are skipped, checked results are kept, and the run reports partial coverage. Fewer listings than the candidate-link estimate is expected.

Expand **Why results are hidden** on a search's results page to see counts and reasons across retained candidates. New failures distinguish access refusals, rate limits, missing pages, server errors, robots restrictions, connection failures and exhausted scan budgets. Old generic failure messages become specific on the next check. Unsupported or inaccessible pages remain hidden; the scanner does not bypass site restrictions.

Web candidates remain separate from the monitored-dealer catalog. Up to 1,000 candidate records are retained per wanted search, including hidden ones, with duplicate URLs combined. Merging new results does not refresh another listing's availability timestamp. Old unverified leads are hidden when upgrading, until they pass a product-page check. Successful checks survive partial failures. Changing the search criteria clears the previous criteria's records.

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

CLI scans accept `scan --mode coins`, `scan --mode dealers`, or `scan --mode both` (the default). To refresh a wanted search, use `scan --mode coins --search-id 1 --web-queries 12 --web-minutes 30`, replacing `1` with the ID in that search's dashboard URL and the budgets with your choices. Omitting these flags uses one query and 10 minutes. Query counts above one or a custom time limit require a specific wanted search with wider-web checking enabled. `--web-minutes` accepts whole numbers from 1 to 120.

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
