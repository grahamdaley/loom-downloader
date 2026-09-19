#!/usr/bin/env python3
"""Download the videos you created in your Loom account.

Loom does not publish a public export API. Paid plans can download a video as
MP4 from the library, one file at a time. This script uses your browser
session and the same GraphQL operations the loom.com library uses:

  * GetLoomsForLibrary — videos that are not in a folder
  * GetPublishedFolders — every folder you created, including nested ones
  * GetLoomsForLibrary again, once per folder
  * GetVideoTranscodedUrl — the signed MP4 URL Loom gives the Download button
  * GetVideoSource — fallback when the transcoded file is not ready

Videos that live only inside folders are not returned by an unfiltered library
query, so both passes are required.

Setup:
  cp .env.example .env
  # paste connect.sid into LOOM_COOKIE
  python3 -m pip install -r requirements.txt
  python3 download_loom.py
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse, urlunparse

import requests
from dotenv import load_dotenv

GRAPHQL_URL = "https://www.loom.com/graphql"
SHARE_URL = "https://www.loom.com/share/{video_id}"
PAGE_SIZE = 50
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

LIBRARY_QUERY = """
query GetLoomsForLibrary(
  $limit: Int!
  $cursor: String
  $folderId: String
  $source: LoomsSource!
  $sortType: LoomsSortType!
  $sortOrder: LoomsSortOrder!
  $filters: [[LoomsCollectionFilter!]!]
) {
  getLooms {
    __typename
    ... on GetLoomsPayload {
      videos(
        first: $limit
        after: $cursor
        folderId: $folderId
        source: $source
        sortType: $sortType
        sortOrder: $sortOrder
        filters: $filters
      ) {
        edges {
          node {
            id
            name
            visibility
          }
        }
        pageInfo {
          endCursor
          hasNextPage
        }
      }
    }
  }
}
""".strip()

FOLDERS_QUERY = """
query GetPublishedFolders(
  $first: Int!
  $after: String
  $source: FolderSource!
  $sortType: LoomsSortType!
  $sortOrder: LoomsSortOrder!
  $parentFolderId: String
  $filters: [LoomsCollectionFilter!]
) {
  getPublishedFolders {
    __typename
    ... on GetPublishedFoldersPayload {
      folders(
        first: $first
        after: $after
        source: $source
        sortType: $sortType
        sortOrder: $sortOrder
        parentFolderId: $parentFolderId
        filters: $filters
      ) {
        edges {
          node {
            id
            name
          }
        }
        pageInfo {
          endCursor
          hasNextPage
        }
      }
    }
  }
}
""".strip()

TRANSCODED_QUERY = """
query GetVideoTranscodedUrl($videoId: ID!, $forceOriginal: Boolean) {
  getVideoTranscodedUrl(videoId: $videoId, forceOriginal: $forceOriginal) {
    __typename
    ... on VideoSource {
      url
    }
  }
}
""".strip()

SOURCE_QUERY = """
query GetVideoSource($videoId: ID!, $acceptableMimes: [CloudfrontVideoAcceptableMime!]) {
  getVideo(id: $videoId) {
    __typename
    ... on RegularUserVideo {
      nullableRawCdnUrl(acceptableMimes: $acceptableMimes) {
        url
      }
    }
  }
}
""".strip()


class AuthError(RuntimeError):
    pass


def cookie_header(raw: str) -> str:
    value = raw.strip().strip('"').strip("'")
    if not value:
        raise AuthError("LOOM_COOKIE is empty. Copy connect.sid into .env — see .env.example.")
    if "connect.sid=" in value or ";" in value:
        return value
    return f"connect.sid={value}"


def next_cursor(page_info: dict | None) -> str | None:
    """Loom returns endCursor even on the last page. Follow it only when hasNextPage is true."""
    info = page_info or {}
    if not info.get("hasNextPage"):
        return None
    cursor = info.get("endCursor")
    return cursor or None


def safe_segment(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", name or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return (cleaned[:80] or "untitled")


def file_extension(url: str) -> str:
    path = unquote(urlparse(url).path).lower()
    for ext in (".mp4", ".webm", ".m3u8", ".mpd"):
        if path.endswith(ext):
            return ext
    return ""


class LoomClient:
    def __init__(self, cookie: str, delay: float) -> None:
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": "https://www.loom.com",
                "Cookie": cookie,
                "apollographql-client-name": "web",
                "x-loom-request-source": "loom_web_1",
            }
        )

    def graphql(self, operation: str, query: str, variables: dict) -> dict:
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                response = self.session.post(
                    GRAPHQL_URL,
                    headers={"graphql-operation-name": operation},
                    json={
                        "operationName": operation,
                        "query": query,
                        "variables": variables,
                    },
                    timeout=60,
                )
            except requests.RequestException as exc:
                last_error = exc
                time.sleep(min(2 ** attempt, 8))
                continue

            if response.status_code in (401, 403):
                raise AuthError(
                    f"Loom returned HTTP {response.status_code}. "
                    "The connect.sid cookie is missing or expired. Sign in again and update .env."
                )
            if response.status_code in (429, 500, 502, 503, 504):
                last_error = RuntimeError(f"HTTP {response.status_code} from {operation}")
                time.sleep(min(2 ** attempt, 8))
                continue
            response.raise_for_status()
            payload = response.json()
            errors = payload.get("errors") or []
            if errors:
                message = "; ".join(err.get("message", str(err)) for err in errors)
                if any((err.get("extensions") or {}).get("code") == "UNAUTHENTICATED" for err in errors):
                    raise AuthError(
                        "Loom rejected the session. Sign in at loom.com and paste a fresh connect.sid cookie."
                    )
                raise RuntimeError(f"{operation} failed: {message}")
            data = payload.get("data")
            if not isinstance(data, dict):
                raise RuntimeError(f"{operation} returned no data: {payload}")
            time.sleep(self.delay)
            return data

        raise RuntimeError(f"{operation} failed after retries: {last_error}")

    def paginate(self, operation: str, query: str, variables: dict, connection) -> list[dict]:
        rows: list[dict] = []
        cursor = None
        seen: set[str] = set()
        while True:
            if operation == "GetLoomsForLibrary":
                page_vars = {**variables, "cursor": cursor, "limit": PAGE_SIZE}
            else:
                page_vars = {**variables, "after": cursor, "first": PAGE_SIZE}
            data = self.graphql(operation, query, page_vars)
            edges, page_info = connection(data)
            rows.extend(edges)
            cursor = next_cursor(page_info)
            if not cursor:
                return rows
            if cursor in seen:
                raise RuntimeError(f"Repeated pagination cursor while running {operation}; stopping so this cannot loop forever.")
            seen.add(cursor)


def library_connection(data: dict) -> tuple[list[dict], dict]:
    payload = data.get("getLooms") or {}
    if payload.get("__typename") not in (None, "GetLoomsPayload") and "videos" not in payload:
        raise RuntimeError(f"Unexpected getLooms response: {payload.get('__typename') or payload}")
    videos = payload.get("videos") or {}
    nodes = []
    for edge in videos.get("edges") or []:
        node = (edge or {}).get("node") or {}
        if node.get("id"):
            nodes.append(node)
    return nodes, videos.get("pageInfo") or {}


def folder_connection(data: dict) -> tuple[list[dict], dict]:
    payload = data.get("getPublishedFolders") or {}
    if payload.get("__typename") not in (None, "GetPublishedFoldersPayload") and "folders" not in payload:
        raise RuntimeError(f"Unexpected getPublishedFolders response: {payload.get('__typename') or payload}")
    folders = payload.get("folders") or {}
    nodes = []
    for edge in folders.get("edges") or []:
        node = (edge or {}).get("node") or {}
        if node.get("id"):
            nodes.append(node)
    return nodes, folders.get("pageInfo") or {}


def list_folders(client: LoomClient, parent_id: str | None) -> list[dict]:
    return client.paginate(
        "GetPublishedFolders",
        FOLDERS_QUERY,
        {
            "source": "ACTIVE",
            "sortType": "RECENT",
            "sortOrder": "DESC",
            "parentFolderId": parent_id,
            "filters": [{"type": "CREATED_BY_ME"}],
        },
        folder_connection,
    )


def walk_folders(client: LoomClient, max_depth: int) -> list[dict]:
    found: list[dict] = []
    visited: set[str] = set()

    def visit(parent_id: str | None, depth: int, segments: list[str]) -> None:
        if depth > max_depth:
            raise RuntimeError(
                f"Folder nesting is deeper than {max_depth}. Re-run with a higher --max-depth if that is real."
            )
        for folder in list_folders(client, parent_id):
            folder_id = folder["id"]
            if folder_id in visited:
                raise RuntimeError(f"Folder cycle detected at {folder.get('name')!r} ({folder_id}).")
            visited.add(folder_id)
            path_segments = segments + [folder.get("name") or ""]
            found.append({"id": folder_id, "segments": path_segments})
            visit(folder_id, depth + 1, path_segments)

    visit(None, 0, [])
    return found


def list_videos(client: LoomClient, folder_id: str | None) -> list[dict]:
    filters: list[list[dict]] = [[{"type": "CREATED_BY_ME"}]]
    if folder_id is None:
        filters.append([{"type": "NOT_IN_FOLDER"}])
    return client.paginate(
        "GetLoomsForLibrary",
        LIBRARY_QUERY,
        {
            "source": "MINE",
            "sortType": "RECENT",
            "sortOrder": "DESC",
            "filters": filters,
            "folderId": folder_id,
        },
        library_connection,
    )


def discover(client: LoomClient, max_depth: int) -> list[dict]:
    """Videos you created, whether they sit in the library root or inside folders."""
    found: dict[str, dict] = {}

    def add(node: dict, segments: list[str]) -> None:
        video_id = node["id"]
        if video_id in found:
            return
        found[video_id] = {
            "id": video_id,
            "name": node.get("name") or "untitled",
            "visibility": node.get("visibility") or "",
            "segments": segments,
            "share_url": SHARE_URL.format(video_id=video_id),
        }

    for node in list_videos(client, None):
        add(node, [])
    folders = walk_folders(client, max_depth)
    for folder in folders:
        for node in list_videos(client, folder["id"]):
            add(node, folder["segments"])
    return list(found.values())


def media_url(client: LoomClient, video_id: str) -> str | None:
    """Prefer a single MP4 or WebM file. HLS is only used when Loom has nothing else."""
    candidates: list[str] = []

    transcoded = client.graphql(
        "GetVideoTranscodedUrl",
        TRANSCODED_QUERY,
        {"videoId": video_id, "forceOriginal": False},
    )
    url = ((transcoded.get("getVideoTranscodedUrl") or {}).get("url")) or None
    if url:
        candidates.append(url)
        if file_extension(url) in (".mp4", ".webm"):
            return url

    source = client.graphql(
        "GetVideoSource",
        SOURCE_QUERY,
        {"videoId": video_id, "acceptableMimes": ["MP4", "WEBM"]},
    )
    cdn = ((source.get("getVideo") or {}).get("nullableRawCdnUrl") or {}).get("url")
    if cdn:
        candidates.append(cdn)
        if file_extension(cdn) in (".mp4", ".webm"):
            return cdn

    if any(file_extension(item) == ".m3u8" for item in candidates):
        return next(item for item in candidates if file_extension(item) == ".m3u8")

    hls = client.graphql(
        "GetVideoSource",
        SOURCE_QUERY,
        {"videoId": video_id, "acceptableMimes": ["M3U8"]},
    )
    return ((hls.get("getVideo") or {}).get("nullableRawCdnUrl") or {}).get("url")


def destination(root: Path, video: dict, ext: str) -> Path:
    folder = root
    for segment in video["segments"]:
        folder = folder / safe_segment(segment)
    filename = f"{safe_segment(video['name'])} [{video['id']}]{ext}"
    return folder / filename


def download_file(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(dest.suffix + ".part")
    headers = {"User-Agent": USER_AGENT, "Referer": "https://www.loom.com/"}
    with requests.get(url, headers=headers, stream=True, timeout=(30, 120)) as response:
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "")
        if "text/html" in content_type:
            raise RuntimeError("Loom returned a web page instead of the video file.")
        with partial.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)
    partial.replace(dest)


_PLAYLIST_URI = re.compile(r'URI="([^"]+)"')


def cloudfront_resources(query: str) -> list[str]:
    policy = parse_qs(query).get("Policy", [""])[0]
    if not policy:
        return []
    translated = policy.replace("-", "+").replace("~", "/").replace("_", "=")
    translated += "=" * (-len(translated) % 4)
    try:
        payload = json.loads(base64.b64decode(translated))
    except (ValueError, json.JSONDecodeError):
        return []
    resources: list[str] = []
    for statement in payload.get("Statement") or []:
        resource = statement.get("Resource")
        if isinstance(resource, str):
            resources.append(resource)
        elif isinstance(resource, list):
            resources.extend(item for item in resource if isinstance(item, str))
    return resources


def signature_covers_segments(url: str) -> bool:
    """True when the CloudFront policy is a wildcard, not just the playlist file."""
    return any("*" in resource for resource in cloudfront_resources(urlparse(url).query))


def raw_manifest_url(client: LoomClient, video_id: str) -> str | None:
    """Signed HLS URL from the same endpoint the Loom player uses for raw video.

    GraphQL sometimes returns an older cdn.loom.com playlist whose signature
    covers only the .m3u8. Segment requests then fail. raw-url returns a
    signature that covers every file under resource/*.
    """
    response = client.session.post(
        f"https://www.loom.com/api/campaigns/sessions/{video_id}/raw-url",
        json={
            "anonID": str(uuid.uuid4()),
            "deviceID": None,
            "force_original": False,
            "password": None,
        },
        timeout=60,
    )
    if response.status_code == 204 or not response.content:
        return None
    if response.status_code in (401, 403):
        raise AuthError("Loom rejected the session while requesting the raw video URL.")
    response.raise_for_status()
    url = (response.json() or {}).get("url")
    return url or None


def policy_covers(query: str, target: str) -> bool:
    bare = urlunparse(urlparse(target)._replace(query="", fragment=""))
    for resource in cloudfront_resources(query):
        if "*" not in resource:
            if bare == resource:
                return True
            continue
        pattern = re.escape(resource).replace(r"\*", ".*")
        if re.fullmatch(pattern, bare):
            return True
    return False


def with_signed_query(absolute: str, query: str) -> str:
    """Copy a CloudFront signature onto a URL only when that signature covers it.

    A playlist signature often allows every file under resource/*, but some older
    videos sign only the .m3u8 itself. Putting that signature on a .ts segment
    makes CloudFront reject the segment.
    """
    if not query:
        return absolute
    parsed = urlparse(absolute)
    if "Signature=" in parsed.query or not policy_covers(query, absolute):
        return absolute
    combined = f"{parsed.query}&{query}" if parsed.query else query
    return urlunparse(parsed._replace(query=combined))


def rewrite_playlist(url: str, directory: Path, headers: dict, counter: list[int]) -> Path:
    response = requests.get(url, headers=headers, timeout=60)
    response.raise_for_status()
    query = urlparse(url).query
    lines: list[str] = []
    for line in response.text.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append(line)
            continue
        if stripped.startswith("#"):
            def replace_uri(match: re.Match[str], playlist_url: str = url) -> str:
                target = with_signed_query(urljoin(playlist_url, match.group(1)), query)
                if urlparse(target).path.lower().endswith(".m3u8"):
                    local = rewrite_playlist(target, directory, headers, counter)
                    return f'URI="{local.name}"'
                return f'URI="{target}"'

            lines.append(_PLAYLIST_URI.sub(replace_uri, line))
            continue
        target = with_signed_query(urljoin(url, stripped), query)
        if urlparse(target).path.lower().endswith(".m3u8"):
            lines.append(rewrite_playlist(target, directory, headers, counter).name)
        else:
            lines.append(target)
    counter[0] += 1
    path = directory / f"playlist-{counter[0]}.m3u8"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def download_hls(url: str, dest: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("This video is HLS-only and ffmpeg is not installed. Install ffmpeg and re-run.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Must end in .mp4. ffmpeg will not mux a file whose name ends in .mp4.part.
    partial = dest.with_name(dest.stem + ".part.mp4")
    leftover = Path(str(dest) + ".part")
    if leftover.exists():
        leftover.unlink()
    headers = {"User-Agent": USER_AGENT, "Referer": "https://www.loom.com/"}
    with tempfile.TemporaryDirectory(prefix="loom-hls-") as temporary:
        playlist = rewrite_playlist(url, Path(temporary), headers, [0])
        command = [
            ffmpeg,
            "-y",
            "-loglevel",
            "error",
            # A local playlist is only allowed to open file: URLs unless this is set.
            # -headers is an HTTP option and fails when the input itself is a local file.
            "-protocol_whitelist",
            "file,http,https,tcp,tls,crypto,data",
            "-i",
            str(playlist),
            "-c",
            "copy",
            "-f",
            "mp4",
            str(partial),
        ]
        result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        if partial.exists():
            partial.unlink()
        raise RuntimeError(detail or f"ffmpeg exited {result.returncode}")
    partial.replace(dest)


def download_video(client: LoomClient, video: dict, root: Path) -> str:
    url = media_url(client, video["id"])
    if not url:
        return "no download URL (downloads may be disabled for this video, or the MP4 is not ready)"

    kind = file_extension(url)
    if kind == ".m3u8" and not signature_covers_segments(url):
        replacement = raw_manifest_url(client, video["id"])
        if replacement:
            url = replacement
            kind = file_extension(url)
    if kind == ".mpd":
        raise RuntimeError("Loom only offered a DASH stream for this video, which this script does not download.")
    ext = kind if kind in (".mp4", ".webm") else ".mp4"
    dest = destination(root, video, ext)
    if dest.exists() and dest.stat().st_size > 0:
        return f"already saved {dest}"

    if kind == ".m3u8":
        download_hls(url, dest)
    else:
        download_file(url, dest)
    return f"saved {dest}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download videos you created in your Loom account.")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output directory (default: LOOM_DOWNLOAD_DIR or ./downloads)",
    )
    parser.add_argument("--list", action="store_true", help="Print share URLs and exit without downloading")
    parser.add_argument("--delay", type=float, default=0.4, help="Seconds to wait between Loom API calls")
    parser.add_argument("--max-depth", type=int, default=10, help="Maximum folder nesting to walk")
    return parser.parse_args()


def main() -> int:
    load_dotenv(Path(__file__).with_name(".env"))
    args = parse_args()
    raw_cookie = os.environ.get("LOOM_COOKIE", "")
    try:
        cookie = cookie_header(raw_cookie)
    except AuthError as exc:
        print(exc, file=sys.stderr)
        return 1

    out = args.out or Path(os.environ.get("LOOM_DOWNLOAD_DIR") or "downloads")
    if not out.is_absolute():
        out = Path(__file__).parent / out

    client = LoomClient(cookie, delay=args.delay)
    try:
        videos = discover(client, max_depth=args.max_depth)
    except AuthError as exc:
        print(exc, file=sys.stderr)
        return 1
    except (RuntimeError, requests.RequestException) as exc:
        print(f"Could not list videos: {exc}", file=sys.stderr)
        return 1

    print(f"Found {len(videos)} video(s).")
    if args.list:
        for video in videos:
            folder = "/".join(safe_segment(part) for part in video["segments"])
            prefix = f"[{folder}] " if folder else ""
            print(f"{prefix}{video['name']}\t{video['share_url']}")
        return 0

    saved = skipped = failed = unavailable = 0
    for index, video in enumerate(videos, start=1):
        label = f"[{index}/{len(videos)}] {video['name']}"
        try:
            result = download_video(client, video, out)
        except (RuntimeError, requests.RequestException, subprocess.CalledProcessError) as exc:
            failed += 1
            print(f"{label}: failed — {exc}")
            continue
        if result.startswith("already saved"):
            skipped += 1
        elif result.startswith("saved "):
            saved += 1
        else:
            unavailable += 1
        print(f"{label}: {result}")

    print(
        f"Done. saved={saved} already_present={skipped} "
        f"unavailable={unavailable} failed={failed} directory={out}"
    )
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
