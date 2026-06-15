# channel-checker

Checks YouTube channels for unwatched videos and syncs the results to a Google Tasks list.

Each task in the list should have a YouTube channel URL as its title. The script marks a task **complete** when all recent videos from that channel have been watched, and **incomplete** when at least one hasn't.

Watch progress is read directly from YouTube's thumbnail overlays using a logged-in browser session (via Playwright), so no watch history API access is required.

## Setup

### 1. Install dependencies

```bash
pip install -e .
playwright install chromium
```

### 2. Create Google API credentials

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a project and enable the **YouTube Data API v3** and **Tasks API**
3. Create **OAuth 2.0 credentials** → Desktop Application
4. Download the JSON file and save it to:

```
~/.config/channel-checker/credentials.json
```

### 3. First run (sign in)

Run `channel-checker -a` to authenticate. This will:

1. Print a Google OAuth URL — open it in a browser, sign in, and paste the code back for each API (YouTube and Tasks).
2. Open a browser window for you to sign in to YouTube. The session is saved to `~/.config/channel-checker/chrome-profile/`.

```bash
channel-checker -a
```

Subsequent runs can use `--headless`.

## Usage

```
channel-checker [OPTIONS]

  -n, --list          Google Tasks list name (default: "Sailing Channels")
  -d, --days          Check videos published within this many days (default: 30)
  -t, --threshold     Percent watched to count as fully watched (default: 95)
  -a, --auth          Authenticate with Google and sign in to YouTube, then exit
  -m, --mqtt-config   Path to MQTT config JSON for error notifications
                      (default: ~/.config/channel-checker/mqtt.json)
  -V, --version       Print version and exit
  --headless          Run browser without a visible window
  --dry-run           Report findings without updating Tasks
  --debug             Print extra detail about API calls and page scraping
```

## MQTT error notifications (optional)

Errors can be published to an MQTT broker. Create a config file at the default location or pass a custom path with `-m`:

**`~/.config/channel-checker/mqtt.json`**
```json
{
  "broker": "mqtt.example.com",
  "port": 1883,
  "protocol": "mqtt",
  "topic": "channel-checker/errors",
  "username": "user",
  "password": "pass"
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `broker` | yes | MQTT broker hostname |
| `port` | yes | Broker port (typically 1883 for mqtt, 8883 for mqtts) |
| `protocol` | yes | `mqtt` (plain) or `mqtts` (TLS) |
| `topic` | yes | Topic to publish errors to |
| `username` | no | Broker username |
| `password` | no | Broker password |

If the config file is not present, MQTT publishing is silently disabled.

## How it works

1. Reads channel URLs from a Google Tasks list
2. Fetches recent uploads for each channel via the YouTube Data API
3. Opens the channel's Videos page in a Playwright browser and reads the progress bar width from each thumbnail
4. Marks the task complete if all recent videos are at or above the watch threshold; marks it incomplete if any are below
5. Videos that are unlisted, deleted, or re-uploaded are skipped automatically
