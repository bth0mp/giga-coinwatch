# Packaging validation

Date: 2026-09-25

## Included packaging

- `Dockerfile`: Python 3.13, dependencies constrained by `requirements.lock`, non-root UID/GID 10001, `/data` directory, HTTP `/health` check, one foreground server process.
- `compose.yaml`: loopback-only published port, named volume, init process, restart policy and shutdown grace period.
- Windows scripts: editable `.venv` install constrained by `requirements.lock`; current-user at-logon task launched by `pythonw.exe`; start, stop, status and removal. The task is for process startup only; the app owns daily scheduling.

## Validation performed

- PowerShell scripts: syntax parsed with `System.Management.Automation.Language.Parser`. Task Scheduler's COM service connects and constructs an in-memory task definition with interactive-token logon, one-minute restart interval, 999 retries, unlimited execution time and a direct `pythonw.exe` action. No task was registered or started during packaging work. The ScheduledTasks PowerShell CIM provider fails to connect in this environment, so the scripts use the working Task Scheduler COM interface.
- Docker: executable absent from `PATH` and standard locations in this environment. Image build, Compose config, health and volume persistence await a Docker runtime; Docker was not installed as part of this task.
- Final integration ran `scripts/install.ps1`, registered the current-user logon task (then named Coinwatch), and started the hidden `pythonw.exe` process. `/health` and `/api/status` responded while no browser was open. Stop/start scripts were exercised; the same database and source baselines persisted under `%LOCALAPPDATA%\Coinwatch`.
- The restarted native app performed a scheduled scan: all five enabled categories completed with 259 observations and zero newly found/duplicate listings. Discovery failures were retained in History and the scheduled occurrence was consumed, avoiding a retry loop. The default schedule is 09:00 Europe/London.
- The live dashboard was opened with actual coin images and listings. Browser closure is independent of the task. The at-logon trigger was inspected, but an actual Windows sign-out/sign-in was not performed during this session.
- Python wheel build, package compilation, dependency check and native backup/restore tests succeeded. Cross-mode migration and Linux runtime behavior remain untested without Docker.
- Wanted-search update: 161 automated tests passed, covering combined keyword/phrase matching, existing inventory, currency limits, saved-search persistence, separate scan modes, daily scheduling, API-key isolation, provider errors, edited/deleted searches and interrupted scans. Provider requests were mocked at that stage because no Tavily key was available yet.
- Wanted-search UI was exercised on an isolated fixture database, including creation and matching, desktop and narrow-screen layouts. The native task was then restarted; live Wanted coins and Settings pages loaded, `/health` returned healthy, and dependency checks passed. The additive migration preserved 565 listings, 95 sources, all six user-enabled monitors and existing discovery decisions. A database backup was saved before the upgrade. Wider-web setup was confirmed unconfigured, ready for a key later.
- Dealer-discovery follow-up: after the user configured Tavily, an authenticated live dealer scan returned new leads and confirmed fixed-price stock on two public shop pages. Inaccessible pages retained explicit inspection errors. Social-platform results are filtered out. Shanna's parser was checked separately against its live catalog: all 157 readable cards were retained (131 available, 26 sold), with honest partial coverage for two untitled products. The native app was restarted with these fixes; existing catalog data, wanted searches and web leads were preserved.
- Selectable broad searches: 225 automated tests passed, including the 1–50 query limit, 20 results per request, retention of 1,000 distinct leads, daily-result merging, partial failures, edits, cancellation and lease protection. Provider calls were mocked; validation used no Tavily credits. After a database backup and native task restart, `/health` returned healthy and all 95 sources, 722 listings, 23 dealer candidates, one wanted search and ten existing web leads remained. The live browser showed the new number control; entering 17 updated the estimate to 17 credits and 340 results, then the control was restored to its default of five without starting a scan. The daily schedule remains 09:00 Europe/London.

The Search-tab update passed 227 automated tests, including selection of a saved search, forwarding its chosen query budget, and rejecting invalid budgets, missing searches and invalid form tokens. The native app was restarted and the live browser verified the new navigation, saved-search selector and changing credit estimate. No scan was submitted during UI validation and no Tavily credits were used.

The Shanna title-recovery follow-up passed 235 automated tests. A targeted live dealer check, run under the normal instance lock and database scan lease after a backup, read all 159 products (133 available, 26 sold). The two untitled cards were recovered from matching product-page descriptions, and the completed result established the source baseline. The native app was restarted; its 98 existing wider-web leads were preserved. This check made no Tavily requests. Historical partial runs remain unchanged.

## Integration checks to run

1. At a convenient time, sign out and back in to verify the configured logon trigger. This was not done automatically because it would interrupt the user's desktop session.
2. Native startup, stop/start and database persistence have been checked as described above.
3. With Docker available, run `docker compose config`, `docker compose up -d --build`, `docker compose ps`, check `/health` and inspect `docker inspect` for `127.0.0.1:8000` publication. Replace the container and confirm saved state in the named volume.
4. Exercise the README's backup and restore commands in both directions after stopping the old mode. Compare source baselines, listing identities, saved coins and discovery decisions.
