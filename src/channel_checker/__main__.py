#!/usr/bin/env python3
"""
channel-checker - Check YouTube channels for unwatched videos via Google Tasks.

Each task in the Tasks list should contain a YouTube channel URL as its title.
The script marks a task complete when all recent videos are watched, and
incomplete when at least one recent video is unwatched (< threshold % progress).

Setup
-----
  pip install google-api-python-client google-auth-oauthlib playwright
  playwright install chromium

  1. Go to console.cloud.google.com
  2. Create a project, enable "YouTube Data API v3" and "Tasks API"
  3. Create OAuth 2.0 credentials → Desktop Application
  4. Download the JSON and save it to:
       ~/.config/channel-checker/credentials.json

  5. First run: omit --headless so you can sign in to Google when the browser
     opens. The session is persisted for future headless runs.

Usage
-----
  channel-checker [OPTIONS]

  -n / --list       Google Tasks list name (default: "Sailing Channels")
  -d / --days       Recent window in days (default: 30)
  -t / --threshold  Percent watched to count as fully watched (default: 95)
  --headless        Run browser without a visible window
  --dry-run         Report findings without updating Tasks
"""

import argparse
import pickle
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from playwright.sync_api import Page, sync_playwright

CONFIG = Path.home() / ".config" / "channel-checker"
YT_TOKEN = CONFIG / "youtube_token.pickle"
TASKS_TOKEN = CONFIG / "tasks_token.pickle"
YT_CREDS = CONFIG / "credentials.json"
CHROME = CONFIG / "chrome-profile"

YT_SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]
TASKS_SCOPES = ["https://www.googleapis.com/auth/tasks"]


# ── Auth helper ───────────────────────────────────────────────────────────────

def _get_service(token_file: Path, scopes: list, api: str, version: str):
    if not YT_CREDS.exists():
        sys.exit(f"Missing {YT_CREDS} — see setup instructions at the top of this file.")

    creds = None
    if token_file.exists():
        creds = pickle.loads(token_file.read_bytes())

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(YT_CREDS), scopes)
            creds = flow.run_local_server(port=0)
        token_file.write_bytes(pickle.dumps(creds))

    return build(api, version, credentials=creds)


# ── Google Tasks API ──────────────────────────────────────────────────────────

def tasks_service():
    return _get_service(TASKS_TOKEN, TASKS_SCOPES, "tasks", "v1")


def tasks_find_list(svc, name: str) -> str:
    resp = svc.tasklists().list(maxResults=100).execute()
    for tl in resp.get("items", []):
        if tl["title"].strip().lower() == name.strip().lower():
            return tl["id"]
    available = [tl["title"] for tl in resp.get("items", [])]
    sys.exit(f"Task list '{name}' not found. Available lists: {available}")


def tasks_read_items(svc, list_id: str) -> list[dict]:
    """Return top-level tasks as [{'id', 'text', 'checked'}]."""
    items, page_token = [], None
    while True:
        resp = svc.tasks().list(
            tasklist=list_id,
            showCompleted=True,
            showHidden=True,
            maxResults=100,
            pageToken=page_token,
        ).execute()
        for task in resp.get("items", []):
            if task.get("parent"):
                continue  # skip subtasks
            items.append({
                "id": task["id"],
                "text": task.get("title", "").strip(),
                "checked": task.get("status") == "completed",
            })
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return items


def tasks_apply_changes(svc, list_id: str, changes: dict, debug: bool = False) -> None:
    """Patch task statuses. changes = {task_id: want_checked}."""
    for task_id, want_checked in changes.items():
        if want_checked:
            body = {
                "status": "completed",
                "completed": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            }
        else:
            body = {"status": "needsAction"}
        if debug:
            print(f"   [debug] PATCH list={list_id!r} task={task_id!r} body={body}")
        result = svc.tasks().patch(tasklist=list_id, task=task_id, body=body).execute()
        if debug:
            print(f"   [debug] response status={result.get('status')!r} completed={result.get('completed')!r} title={result.get('title')!r}")


# ── YouTube Data API ──────────────────────────────────────────────────────────

def yt_service():
    return _get_service(YT_TOKEN, YT_SCOPES, "youtube", "v3")


def channel_id_for(yt, url: str):
    url = url.strip().rstrip("/")

    m = re.search(r"youtube\.com/channel/(UC[a-zA-Z0-9_-]{22})", url)
    if m:
        return m.group(1)

    m = re.search(r"youtube\.com/@([a-zA-Z0-9_.\-]+)", url)
    if m:
        r = yt.channels().list(part="id", forHandle="@" + m.group(1)).execute()
        items = r.get("items") or []
        return items[0].get("id") if items else None

    m = re.search(r"youtube\.com/(?:c/|user/)([a-zA-Z0-9_.\-]+)", url)
    if m:
        r = yt.channels().list(part="id", forUsername=m.group(1)).execute()
        items = r.get("items") or []
        return items[0].get("id") if items else None

    return None


def videos_page_url(url: str, channel_id: str) -> str:
    m = re.match(
        r"(https://(?:www\.)?youtube\.com/"
        r"(?:channel/UC[a-zA-Z0-9_-]{22}|@[a-zA-Z0-9_.\-]+|c/[^/?#]+|user/[^/?#]+))",
        url,
    )
    base = m.group(1) if m else f"https://www.youtube.com/channel/{channel_id}"
    return base + "/videos"


def recent_uploads(yt, channel_id: str, since: datetime) -> list[dict]:
    r = yt.channels().list(part="contentDetails", id=channel_id).execute()
    if not r.get("items"):
        return []
    playlist = r["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]

    videos, page_token = [], None
    while True:
        r = yt.playlistItems().list(
            part="contentDetails,snippet",
            playlistId=playlist,
            maxResults=50,
            pageToken=page_token,
        ).execute()

        stop = False
        for item in r.get("items", []):
            pub = (
                item["contentDetails"].get("videoPublishedAt")
                or item["snippet"].get("publishedAt", "")
            )
            if not pub:
                continue
            pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            if pub_dt < since:
                stop = True
                break
            videos.append({
                "id": item["contentDetails"]["videoId"],
                "title": item["snippet"]["title"],
                "published_at": pub_dt,
            })

        if stop:
            break
        page_token = r.get("nextPageToken")
        if not page_token:
            break

    return videos


# ── YouTube watch-progress check (Playwright) ─────────────────────────────────

def watch_progress(page: Page, vids_url: str, video_ids: set, debug: bool = False) -> dict:
    """
    Navigate to a channel's Videos page and return {video_id: fraction_watched}.

    Progress is read from the red progress bar on each thumbnail:
      - No bar                                 → 0.0 (not started)
      - ytd-thumbnail-overlay-resume-playback  → width percentage / 100
      - ytd-thumbnail-overlay-playback-status  → 1.0 (fully watched indicator)
    """
    page.goto(vids_url, timeout=30_000)
    page.wait_for_load_state("networkidle", timeout=20_000)
    page.wait_for_timeout(2_000)

    found, remaining = {}, set(video_ids)

    for _ in range(30):
        if not remaining:
            break

        for link in page.query_selector_all('a[href*="watch?v="], a[href*="/shorts/"]'):
            href = link.get_attribute("href") or ""
            m = re.search(r"(?:[?&]v=|/shorts/)([a-zA-Z0-9_-]{11})", href)
            if not m or m.group(1) not in remaining:
                continue

            vid = m.group(1)
            item = link.evaluate_handle(
                "el => el.closest('ytd-rich-item-renderer, ytd-grid-video-renderer, ytd-video-renderer')"
            ).as_element()

            if item is None:
                if debug:
                    print(f"   [debug] vid={vid} href={href!r} — no item container found")
                found[vid] = 0.0
                remaining.discard(vid)
                continue

            info = item.evaluate("""el => {
                function dqs(root, sel) {
                    const el = root.querySelector(sel);
                    if (el) return el;
                    for (const child of root.querySelectorAll('*')) {
                        if (child.shadowRoot) {
                            const found = dqs(child.shadowRoot, sel);
                            if (found) return found;
                        }
                    }
                    return null;
                }
                const seg = dqs(el, '.ytThumbnailOverlayProgressBarHostWatchedProgressBarSegment');
                return { barStyle: seg ? seg.getAttribute('style') : null };
            }""")
            bar_style = info.get("barStyle")
            if debug:
                print(f"   [debug] found vid={vid} bar_style={bar_style!r}")
            if bar_style is not None:
                sm = re.search(r"width:\s*([\d.]+)%", bar_style)
                found[vid] = float(sm.group(1)) / 100 if sm else 0.5
            else:
                found[vid] = 0.0
            remaining.discard(vid)

        page.evaluate("window.scrollBy(0, 2000)")
        page.wait_for_timeout(1_500)

    if debug and remaining:
        all_ids = page.evaluate("""() => {
            return [...document.querySelectorAll('a[href]')]
                .map(a => a.getAttribute('href'))
                .filter(h => h && (h.includes('watch?v=') || h.includes('/shorts/')))
                .map(h => { const m = h.match(/(?:[?&]v=|\\/shorts\\/)([a-zA-Z0-9_-]{11})/); return m ? m[1] : null; })
                .filter(Boolean);
        }""")
        print(f"   [debug] video IDs never found on page: {remaining}")
        print(f"   [debug] all video IDs on page: {sorted(set(all_ids))}")
    # Videos not found on the page are left out of `found` entirely (None = skip).
    return found


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--list", "-n", default="Sailing Channels",
                    help="Google Tasks list name (default: 'Sailing Channels')")
    ap.add_argument("--days", "-d", type=int, default=30,
                    help="Check videos published within this many days (default: 30)")
    ap.add_argument("--threshold", "-t", type=float, default=95.0,
                    help="Percent watched to count as fully watched (default: 95)")
    ap.add_argument("--headless", action="store_true",
                    help="Run browser headless (requires prior Google login)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report findings without updating Tasks")
    ap.add_argument("--debug", action="store_true",
                    help="Print API request and response details")
    args = ap.parse_args()

    CONFIG.mkdir(parents=True, exist_ok=True)
    CHROME.mkdir(parents=True, exist_ok=True)

    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    threshold = args.threshold / 100

    print(f"Window  : last {args.days} day(s) (since {since.date()})")
    print(f"Watched : >= {args.threshold:.0f}%")
    if args.dry_run:
        print("[dry-run mode — no Tasks changes will be written]")
    print()

    yt = yt_service()
    tasks_svc = tasks_service()
    list_id = tasks_find_list(tasks_svc, args.list)
    all_tasks = tasks_read_items(tasks_svc, list_id)
    channel_tasks = [t for t in all_tasks if re.search(r"https?://", t["text"])]

    if not channel_tasks:
        sys.exit("No channel URLs found in the task list.")
    print(f"Found {len(channel_tasks)} channel(s) in '{args.list}'.\n")

    changes = {}  # {task_id: want_checked}

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(CHROME),
            headless=args.headless,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # Verify YouTube login
        page.goto("https://www.youtube.com", timeout=30_000)
        page.wait_for_load_state("networkidle", timeout=15_000)
        if not page.query_selector("ytd-masthead button#avatar-btn, ytd-masthead #avatar-btn"):
            if args.headless:
                ctx.close()
                sys.exit("Not logged in to YouTube. Run channel-checker once without --headless to sign in.")
            print("Sign in to YouTube in the browser, then press Enter…")
            input()

        for task in channel_tasks:
            url_m = re.search(r"https?://\S+", task["text"])
            if not url_m:
                continue
            url = url_m.group(0).rstrip(".,;)")

            print(f"▶  {url}")

            cid = channel_id_for(yt, url)
            if not cid:
                print("   Could not resolve channel ID — skipping.\n")
                continue

            try:
                vids = recent_uploads(yt, cid, since)
            except Exception as e:
                print(f"   YouTube API error: {e} — skipping.\n")
                continue

            if vids:
                valid_ids = {
                    item["id"]
                    for item in yt.videos()
                    .list(part="id,status", id=",".join(v["id"] for v in vids))
                    .execute()
                    .get("items", [])
                    if item.get("status", {}).get("privacyStatus") == "public"
                }
                stale = [v for v in vids if v["id"] not in valid_ids]
                if stale:
                    for v in stale:
                        print(f"   Skipping non-public video (unlisted/deleted/re-uploaded): {v['title'][:65]}")
                vids = [v for v in vids if v["id"] in valid_ids]

            if not vids:
                print(f"   No uploads in the last {args.days} day(s).")
                want_checked = True
            else:
                print(f"   {len(vids)} recent video(s) — checking watch progress…")
                vp_url = videos_page_url(url, cid)

                try:
                    progress = watch_progress(page, vp_url, {v["id"] for v in vids}, debug=args.debug)
                except Exception as e:
                    print(f"   Browser error: {e} — skipping.\n")
                    continue

                skipped = [v for v in vids if v["id"] not in progress]
                if skipped:
                    for v in skipped:
                        print(f"   Skipping (not found on page): {v['title'][:65]}")
                checkable = [v for v in vids if v["id"] in progress]
                unwatched = [v for v in checkable if progress[v["id"]] < threshold]

                if unwatched:
                    print(f"   {len(unwatched)} unwatched video(s):")
                    for v in unwatched:
                        pct = progress[v["id"]] * 100
                        print(
                            f"     [{v['published_at'].date()}] "
                            f"{v['title'][:65]}  ({pct:.0f}%)"
                        )
                    want_checked = False
                else:
                    print(f"   All {len(checkable)} video(s) watched.")
                    want_checked = True

            if task["checked"] != want_checked:
                action = "complete" if want_checked else "incomplete"
                print(f"   -> mark {action}")
                if not args.dry_run:
                    tasks_apply_changes(tasks_svc, list_id, {task["id"]: want_checked}, debug=args.debug)
                    changes[task["id"]] = want_checked
            else:
                state = "complete" if task["checked"] else "incomplete"
                print(f"   -> Already {state}, no change.")
            print()

        ctx.close()

    if args.dry_run:
        print("[dry-run] No changes written.")
    elif not changes:
        print("No changes needed.")


if __name__ == "__main__":
    main()
