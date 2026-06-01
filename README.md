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

Run without `--headless` so you can sign in to Google in the browser that opens. Your session is saved for future headless runs.

```bash
channel-checker
```

Subsequent runs can use `--headless`.

## Usage

```
channel-checker [OPTIONS]

  -n, --list        Google Tasks list name (default: "Sailing Channels")
  -d, --days        Check videos published within this many days (default: 30)
  -t, --threshold   Percent watched to count as fully watched (default: 95)
  --headless        Run browser without a visible window
  --dry-run         Report findings without updating Tasks
  --debug           Print extra detail about API calls and page scraping
```

## How it works

1. Reads channel URLs from a Google Tasks list
2. Fetches recent uploads for each channel via the YouTube Data API
3. Opens the channel's Videos page in a Playwright browser and reads the progress bar width from each thumbnail
4. Marks the task complete if all recent videos are at or above the watch threshold; marks it incomplete if any are below
5. Videos that are unlisted, deleted, or re-uploaded are skipped automatically
