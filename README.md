# Pocket Option Trade Mirror

A one-way Python trade mirror for Pocket Option accounts. The bot watches the master account for newly opened deals and places a matching trade on every configured child account.

## How it works

1. Connects to the master account and all configured child accounts.
2. Receives newly opened master deals through the client's websocket event stream.
3. Ignores deals that were already open when the process started by default.
4. Mirrors each newly detected master deal to all children concurrently with no intentional one-second delay.
5. Uses the master's asset, direction, amount, and duration when those values are available.

This is intentionally one-way: child trades are not copied back to the master, and closing or modifying a master deal is not mirrored.

## Requirements

- Python 3.10 or newer
- A valid Pocket Option session SSID for the master account
- At least one valid child account session SSID
- Network access to the Pocket Option service

The project uses the async `pocket-option` client package. Dependencies are listed in `requirements.txt`.

## Installation

From the repository directory, create and activate a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

On systems where PowerShell script execution is restricted, activate the environment from another supported shell or run the `.venv` Python executable directly.

## Configuration

Create the profile files from the templates and fill in the account sessions:

```powershell
Copy-Item .env.demo.example .env.demo
Copy-Item .env.live.example .env.live
```

Example:

```dotenv
MASTER_SSID=master-session-ssid
CHILD_SSID=first-child-session-ssid
CHILD_SSID_2=second-child-session-ssid
CHILD_SSID_3=third-child-session-ssid
AMOUNT=1
DURATION=60
POCKET_OPTION_REGION=DEMO
MIRROR_EXISTING_OPEN_DEALS=false
LIVE_CONFIRMATION=LIVE
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_CHAT_ID=
```

### Configuration values

| Variable | Required | Description |
| --- | --- | --- |
| `MASTER_SSID` | Yes | Session SSID for the account used as the trade source. |
| `CHILD_SSID` | Yes | First child account. This legacy name remains supported. |
| `CHILD_SSID_2`, `CHILD_SSID_3`, ... | No | Additional child accounts. Numbered values are processed in numeric order. |
| `AMOUNT` | No | Fallback trade amount when the master deal does not provide one. Defaults to `1.0`. |
| `DURATION` | No | Fallback duration in seconds when the master deal does not provide one. Defaults to `60`. |
| `POCKET_OPTION_REGION` | No | Client region name. The default is `DEMO` for demo sessions and `EUROPA` otherwise. |
| `MIRROR_EXISTING_OPEN_DEALS` | No | Set to `true`, `1`, `yes`, or `on` to mirror deals already open at startup. Defaults to false. |
| `LIVE_CONFIRMATION` | Live only | Set to `LIVE` to allow unattended live-mode startup. |
| `TELEGRAM_BOT_TOKEN` | No | Optional Telegram bot token for mirror and account controls. |
| `TELEGRAM_ALLOWED_CHAT_ID` | No | Only this Telegram chat can send control commands. Keep this restricted to a trusted user. |
| `TELEGRAM_ALLOW_INSECURE_SSL` | No | Set to `true` or `1` only on a personal machine with a broken certificate chain; disables TLS verification for Telegram polling only. |

Values supplied as environment variables override `MASTER_SSID` and `CHILD_SSID`; numbered `CHILD_SSID_N` environment variables are also supported. Keep `.env` private: it is ignored by Git.

The selected mode reads its matching profile when present: demo mode reads `.env.demo` and live mode reads `.env.live`. Both profile files are ignored by Git, and the required account credentials may instead be supplied as environment variables (for example, on Railway). The older `.env` file is retained for compatibility with older revisions but is not used by the mode-based command.

Some Pocket Option clients provide an auth wrapper rather than only the session token. The executor accepts either the plain SSID or the compatible `...["auth",{...}]` payload format.

## Running

With the virtual environment active, choose a mode explicitly:

```powershell
python mirror_trade.py --mode demo
python mirror_trade.py --mode live
```

Running without `--mode` asks which profile to use. Live mode additionally requires typing `LIVE` as an explicit confirmation before the accounts are connected.

The process runs continuously until interrupted with `Ctrl+C`. Runtime messages are written to the console and to `mirror_trade.log`. The log file is ignored by Git.

If `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_CHAT_ID` are configured, the bot listens for commands from that single chat. It provides account connection and mirror status, refreshable balances, recent trade outcomes, child enable/disable controls, and master selection. Commands are `/status`, `/pause`, `/resume`, `/master`, `/children`, `/recent`, `/balance`, and `/help` (`/start` opens Help). Master selection requires mirroring to be paused and in-flight child copies to finish; the former master is then enabled as a child. The control layer never sends SSIDs or sensitive session data in Telegram messages. These controls are runtime-only and reset to the profile configuration after restart.

The primary path is event-driven for low latency. A one-second polling request remains as a recovery fallback when the client does not deliver an open-order event, such as when using a legacy client or after a missed websocket notification.

## Deploying to Render

This process runs continuously and does not serve HTTP requests, so deploy it as a **Background Worker** using the repository's `render.yaml` Blueprint:

1. Push the repository to GitHub.
2. In the [Render Dashboard](https://dashboard.render.com/), choose **New > Blueprint** and connect the repository.
3. Review the Blueprint and create the worker. Background Workers require a paid Render plan; review the current pricing before confirming.
4. In the worker's **Environment** settings, set `MASTER_SSID` and `CHILD_SSID` to demo-account sessions. Add `CHILD_SSID_2`, `CHILD_SSID_3`, and so on for additional child accounts.
5. Optionally set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_CHAT_ID` to enable Telegram controls, then deploy.
6. Check the worker's logs and confirm that the demo accounts connect successfully before considering any live setup.

The Blueprint starts in **demo** mode with a fallback amount of `1` and duration of `60` seconds. Keep the session SSIDs and Telegram token in Render's environment settings; never put them in `render.yaml` or commit them to Git. Live mode is not enabled by this Blueprint. To use live mode later, deliberately change the start command to `python mirror_trade.py --mode live`, set `LIVE_CONFIRMATION=LIVE`, and configure live-account sessions only after validating the deployment with demo accounts.

## Testing

Run the focused test suite with:

```powershell
python -m pytest -q tests/test_mirror_logic.py
```

The tests cover action normalization, trade event parsing, child ordering, trade plan creation, and logging.

## Safety notes

- Start with demo accounts and a small fallback amount.
- Confirm that every SSID belongs to the intended account before starting the process.
- Do not commit `.env`, session SSIDs, or log files containing sensitive data.
- The bot can place trades automatically and does not provide investment advice.
- Stop the process immediately if the detected asset, direction, amount, duration, or account connections are unexpected.
- Pocket Option client APIs and account behavior can change; validate the client package and account permissions before live use.

## Project layout

- `mirror_trade.py` - connection, deal detection, normalization, and mirroring loop
- `logger.py` - console and file logging setup
- `tests/test_mirror_logic.py` - unit tests for the mirror logic
- `.env.demo.example` - demo profile template
- `.env.live.example` - live profile template
- `requirements.txt` - Python dependencies
