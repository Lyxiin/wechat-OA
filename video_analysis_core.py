#!/usr/bin/env python3
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import asr_client
import stock_extractor


DEFAULT_OUTPUT_DIR = Path("./video_analysis_data")
DEFAULT_DB = DEFAULT_OUTPUT_DIR / "video_analysis.sqlite"
DEFAULT_COOKIES_FILE = Path("./cookies/cookies.txt")
DEFAULT_MODEL = stock_extractor.DEFAULT_MODEL
REQUIRED_ANALYSIS_LIST_FIELDS = (
    "topics",
    "keywords",
    "timeline",
    "key_points",
    "entities",
    "action_items",
    "open_questions",
)


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def content_hash(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def safe_video_id(value):
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip())
    cleaned = cleaned.strip("._-")
    if not cleaned:
        return "unknown"
    return cleaned[:80]


class VideoSourceReader:
    @staticmethod
    def read_sources(url=None, urls_file=None):
        candidates = []
        if url:
            candidates.append(str(url).strip())
        if urls_file:
            path = Path(urls_file)
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                candidates.append(line)
        seen = set()
        urls = []
        for candidate in candidates:
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            urls.append(candidate)
        if not urls:
            raise ValueError("Provide at least one URL with --url or --urls")
        return urls
