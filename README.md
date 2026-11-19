# Youtube Live Comment Analyzer

Realtime YouTube live-chat monitor for hashtags and keywords.

## Requirements

- Python 3

No third-party Python packages are required.

## Install

Double-click `build.bat`. It checks Python and pip, then installs
`requirements.txt` when populated.

## Run

Double-click `run.bat`. The server listens on port `8765`.

Run `install_task.bat` once to start the server hidden at every logon.
Use `uninstall_task.bat` to remove that entry.

- Local: http://127.0.0.1:8765

## Use

1. Open **Settings**.
2. Paste a YouTube video URL or ID.
3. Select **Start monitoring**.

Edit hashtags and keywords in Settings; changes apply to new comments
immediately. Select a chip above the ledger to filter by term, or **All** to
reset. The **General Chat** tab shows all public messages from the active
stream. Search the feed by username or message, pause live scrolling, and
matched terms are highlighted inline. The **Statistics** tab groups mentions
by username and term. **Erase
all** clears records and stops monitoring.

All displayed timestamps and exports follow the **Time zone** selected in Settings. Default: Asia/Manila.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/state` | Counters, history and active terms |
| GET | `/api/events` | Server-Sent Events stream |
| GET / POST | `/api/hashtags` | Read or replace hashtags |
| GET / POST | `/api/keywords` | Read or replace keywords |
| POST | `/api/start` | Start monitoring a video |
| POST | `/api/stop` | Stop monitoring |
| DELETE | `/api/mentions/<id>` | Delete one record |
| DELETE | `/api/mentions` | Erase all records |

## Data

- `mentions.db` — SQLite records
- `hashtags.json`, `keywords.json` — configured terms

## Check

```text
python test_monitor.py
```

The service has no built-in authentication. Restrict access with Tailscale
ACLs.

## License

MIT — see [LICENSE](LICENSE).
