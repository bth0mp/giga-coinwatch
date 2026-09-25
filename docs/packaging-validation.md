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
- Wanted-search update: 161 automated tests passed, covering combined keyword/phrase matching, existing inventory, currency limits, saved-search persistence, separate scan modes, daily scheduling, API-key isolation, provider errors, edited/deleted searches and interrupted scans. Provider requests were mocked; no Tavily account or key was available, so authenticated live searches remain untested.
- Wanted-search UI was exercised on an isolated fixture database, including creation and matching, desktop and narrow-screen layouts. The native task was then restarted; live Wanted coins and Settings pages loaded, `/health` returned healthy, and dependency checks passed. The additive migration preserved 565 listings, 95 sources, all six user-enabled monitors and existing discovery decisions. A database backup was saved before the upgrade. Wider-web setup was confirmed unconfigured, ready for a key later.

## Integration checks to run

1. At a convenient time, sign out and back in to verify the configured logon trigger. This was not done automatically because it would interrupt the user's desktop session.
2. Native startup, stop/start and database persistence have been checked as described above.
3. With Docker available, run `docker compose config`, `docker compose up -d --build`, `docker compose ps`, check `/health` and inspect `docker inspect` for `127.0.0.1:8000` publication. Replace the container and confirm saved state in the named volume.
4. Exercise the README's backup and restore commands in both directions after stopping the old mode. Compare source baselines, listing identities, saved coins and discovery decisions.
