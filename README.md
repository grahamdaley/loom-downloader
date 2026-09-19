# Loom downloader

Downloads the videos you created in your Loom account and saves them as MP4 files on your computer.

Loom does not offer a public export API, and an Atlassian API token cannot list or download these files. This script signs in with the same browser session cookie the Loom website uses, walks your library (including folders), and asks Loom for each video's download URL.

## What it downloads

- Videos you created that are not in a folder
- Videos you created inside folders, including nested folders

It does not download videos other people created, even if they are in your workspace. Files are saved as:

```text
downloads/Folder name/Video title [video-id].mp4
```

Running the script again skips files that are already there.

Most videos come down as a single MP4. When Loom only offers an HLS stream, the script uses `ffmpeg` to save an MP4 instead. Install [ffmpeg](https://ffmpeg.org/) if you want those videos. Without it, they are reported as failed and the rest of the library still downloads.

Downloads require a paid Loom plan and a role that is allowed to download (Admin or Creator on Business). A video Loom will not give a file for is marked unavailable and skipped.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
cp .env.example .env
```

Open `.env` and set `LOOM_COOKIE`.

1. Sign in at [loom.com](https://www.loom.com).
2. Open DevTools, then Application, then Cookies, then `https://www.loom.com`.
3. Copy the value of `connect.sid`. It usually starts with `s%3A`.
4. Paste that value into `LOOM_COOKIE`. You can paste the raw value or `connect.sid=<value>`.

The cookie lasts about 30 days. When Loom returns an authentication error, sign in again and replace it. Do not commit `.env`.

`LOOM_DOWNLOAD_DIR` is where files are saved. A relative path is resolved from this folder. An absolute path, such as a OneDrive folder, is used as-is.

## Use

List titles and share URLs without downloading:

```bash
python3 download_loom.py --list
```

Download everything:

```bash
python3 download_loom.py
```

Other options:

| Option | Default | Purpose |
| --- | --- | --- |
| `--out PATH` | `LOOM_DOWNLOAD_DIR` or `downloads` | Directory to save files in |
| `--list` | off | Print share URLs and exit |
| `--delay SECONDS` | `0.4` | Pause between Loom requests |
| `--max-depth N` | `10` | How deep to walk nested folders |

When a run finishes it prints how many files were saved, already present, unavailable, or failed. Re-run the same command to retry failures. Successful files are left alone.
