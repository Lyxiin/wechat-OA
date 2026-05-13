#!/usr/bin/env python3
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


CHINA_TZ = timezone(timedelta(hours=8))


class DouyinSetupRequired(RuntimeError):
    pass


def profile_setup_hint(account_name, douyin_id):
    return (
        f"{account_name}({douyin_id}) 需要先提供抖音主页分享链接。"
        "获取方式：打开抖音 App -> 搜索该博主 -> 进入主页 -> 点击分享 -> 复制抖音主页分享链接，"
        "把复制出的整段文字发回来。如果提示需要登录，再导出浏览器 cookies.txt。"
    )


def timestamp_to_iso(value):
    if not value:
        return ""
    return datetime.fromtimestamp(int(value), CHINA_TZ).isoformat()


def upload_date_to_iso(value):
    if not value:
        return ""
    return datetime.strptime(str(value), "%Y%m%d").replace(tzinfo=CHINA_TZ).isoformat()


def parse_ytdlp_output(output):
    records = []
    for line in (output or "").splitlines():
        line = line.strip()
        if not line:
            continue
        payload = json.loads(line)
        if payload.get("_type") == "playlist":
            records.extend(payload.get("entries") or [])
        else:
            records.append(payload)
    return records


def normalize_video(info, account):
    video_id = str(info.get("id") or info.get("display_id") or "").strip()
    url = info.get("webpage_url") or info.get("original_url") or info.get("url") or ""
    publish_time = (
        timestamp_to_iso(info.get("timestamp"))
        or timestamp_to_iso(info.get("release_timestamp"))
        or upload_date_to_iso(info.get("upload_date"))
    )
    title = info.get("title") or info.get("description") or f"{account['name']} 抖音视频"
    return {
        "video_id": video_id,
        "title": title.strip(),
        "url": url,
        "publish_time": publish_time,
        "description": (info.get("description") or "").strip(),
        "cover_url": info.get("thumbnail") or "",
        "author": info.get("uploader") or account["name"],
    }


class DouyinCrawler:
    def __init__(self, cookies_file=None, ytdlp_cmd=None):
        self.cookies_file = cookies_file or os.environ.get("DOUYIN_COOKIES_FILE", "")
        self.ytdlp_cmd = ytdlp_cmd or [sys.executable, "-m", "yt_dlp"]

    def _run_ytdlp(self, args):
        cmd = [*self.ytdlp_cmd, *args]
        try:
            return subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as exc:
            raise RuntimeError("yt-dlp is not installed. Run: python -m pip install yt-dlp") from exc
        except subprocess.CalledProcessError as exc:
            message = (exc.stderr or exc.stdout or str(exc)).strip()
            raise RuntimeError(f"yt-dlp failed: {message}") from exc

    def fetch_recent_videos(self, account, since_date, limit):
        profile_url = account.get("profile_url") or ""
        if not profile_url:
            raise DouyinSetupRequired(profile_setup_hint(account["name"], account.get("douyin_id", "")))

        args = [
            "--dump-json",
            "--playlist-end",
            str(limit),
            "--no-warnings",
        ]
        cookies_file = account.get("cookies_file") or self.cookies_file
        if cookies_file:
            args.extend(["--cookies", cookies_file])
        args.append(profile_url)

        completed = self._run_ytdlp(args)
        videos = [normalize_video(item, account) for item in parse_ytdlp_output(completed.stdout)]
        videos = [video for video in videos if video["video_id"] and video["url"]]
        if since_date:
            videos = [
                video for video in videos
                if not video["publish_time"] or datetime.fromisoformat(video["publish_time"]).date() >= since_date
            ]
        return videos[:limit]

    def download_audio(self, video, account, output_dir):
        output_dir = Path(output_dir)
        audio_dir = output_dir / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        expected = audio_dir / f"{video['video_id']}.mp3"
        if expected.exists() and expected.stat().st_size > 0:
            return expected

        args = [
            "--extract-audio",
            "--audio-format",
            "mp3",
            "--audio-quality",
            "0",
            "--no-playlist",
            "-o",
            str(audio_dir / "%(id)s.%(ext)s"),
        ]
        cookies_file = account.get("cookies_file") or self.cookies_file
        if cookies_file:
            args.extend(["--cookies", cookies_file])
        args.append(video["url"])
        self._run_ytdlp(args)

        if expected.exists():
            return expected
        matches = sorted(audio_dir.glob(f"{video['video_id']}.*"))
        if matches:
            return matches[0]
        raise RuntimeError(f"audio file was not created for {video['url']}")
