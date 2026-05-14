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


@dataclass
class VideoMetadata:
    video_id: str
    source_url: str
    canonical_url: str
    title: str = ""
    author: str = ""
    publish_time: str = ""
    duration: float | None = None
    thumbnail_url: str = ""
    raw: dict[str, Any] | None = None

    def to_dict(self):
        return {
            "video_id": self.video_id,
            "source_url": self.source_url,
            "canonical_url": self.canonical_url,
            "title": self.title,
            "author": self.author,
            "publish_time": self.publish_time,
            "duration": self.duration,
            "thumbnail_url": self.thumbnail_url,
            "raw": self.raw or {},
        }


@dataclass
class VideoPaths:
    folder: Path
    metadata_path: Path
    audio_path: Path
    transcript_path: Path
    analysis_path: Path
    summary_path: Path
    raw_analysis_path: Path


class VideoAnalysisStore:
    def __init__(self, output_dir=DEFAULT_OUTPUT_DIR, db_path=DEFAULT_DB):
        self.output_dir = Path(output_dir)
        self.db_path = Path(db_path)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row

    def close(self):
        self.conn.close()

    def initialize_db(self):
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS video_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id TEXT NOT NULL UNIQUE,
                source_url TEXT NOT NULL,
                canonical_url TEXT,
                title TEXT,
                author TEXT,
                publish_time TEXT,
                duration REAL,
                thumbnail_url TEXT,
                status TEXT NOT NULL,
                error TEXT,
                transcript_hash TEXT,
                analysis_hash TEXT,
                metadata_path TEXT,
                audio_path TEXT,
                transcript_path TEXT,
                analysis_path TEXT,
                summary_path TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_video_items_status ON video_items(status)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_video_items_author ON video_items(author)")
        self.conn.commit()

    def paths_for(self, video_id):
        folder = self.output_dir / safe_video_id(video_id)
        return VideoPaths(
            folder=folder,
            metadata_path=folder / "metadata.json",
            audio_path=folder / "audio.mp3",
            transcript_path=folder / "transcript.txt",
            analysis_path=folder / "analysis.json",
            summary_path=folder / "summary.md",
            raw_analysis_path=folder / "analysis.raw.txt",
        )

    def write_text(self, path, text):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text or "", encoding="utf-8")

    def write_json(self, path, payload):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def read_text_if_present(self, path):
        path = Path(path)
        if path.exists() and path.read_text(encoding="utf-8").strip():
            return path.read_text(encoding="utf-8").strip()
        return ""

    def read_json_if_present(self, path):
        path = Path(path)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def get_item(self, video_id):
        row = self.conn.execute(
            "SELECT * FROM video_items WHERE video_id = ?",
            (video_id,),
        ).fetchone()
        return dict(row) if row else None

    def upsert_item(self, metadata, paths, status, error=None, transcript_hash=None, analysis_hash=None):
        existing = self.get_item(metadata.video_id)
        now = utc_now_iso()
        created_at = existing["created_at"] if existing else now
        values = (
            metadata.video_id,
            metadata.source_url,
            metadata.canonical_url,
            metadata.title,
            metadata.author,
            metadata.publish_time,
            metadata.duration,
            metadata.thumbnail_url,
            status,
            error,
            transcript_hash,
            analysis_hash,
            str(paths.metadata_path),
            str(paths.audio_path),
            str(paths.transcript_path),
            str(paths.analysis_path),
            str(paths.summary_path),
            created_at,
            now,
        )
        self.conn.execute(
            """
            INSERT INTO video_items (
                video_id, source_url, canonical_url, title, author, publish_time,
                duration, thumbnail_url, status, error, transcript_hash, analysis_hash,
                metadata_path, audio_path, transcript_path, analysis_path, summary_path,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(video_id) DO UPDATE SET
                source_url=excluded.source_url,
                canonical_url=excluded.canonical_url,
                title=excluded.title,
                author=excluded.author,
                publish_time=excluded.publish_time,
                duration=excluded.duration,
                thumbnail_url=excluded.thumbnail_url,
                status=excluded.status,
                error=excluded.error,
                transcript_hash=excluded.transcript_hash,
                analysis_hash=excluded.analysis_hash,
                metadata_path=excluded.metadata_path,
                audio_path=excluded.audio_path,
                transcript_path=excluded.transcript_path,
                analysis_path=excluded.analysis_path,
                summary_path=excluded.summary_path,
                updated_at=excluded.updated_at
            """,
            values,
        )
        self.conn.commit()


class AnalysisValidationError(RuntimeError):
    pass


def strip_json_fence(text):
    return stock_extractor.strip_json_fence(text)


def build_analysis_messages(metadata, transcript):
    return [
        {
            "role": "system",
            "content": (
                "You analyze transcripts from publicly shared videos for learning and research. "
                "Return only valid JSON with keys: summary, topics, keywords, timeline, "
                "key_points, entities, action_items, open_questions, extensions. "
                "Keep the analysis concise and do not invent facts not supported by the transcript."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Title: {metadata.title}\n"
                f"Author: {metadata.author}\n"
                f"URL: {metadata.canonical_url or metadata.source_url}\n\n"
                f"Transcript:\n{transcript}"
            ),
        },
    ]


def validate_analysis(payload):
    if not isinstance(payload, dict):
        raise AnalysisValidationError("LLM response must be a JSON object")
    summary = payload.get("summary")
    if not isinstance(summary, str):
        raise AnalysisValidationError("analysis.summary must be a string")
    for field in REQUIRED_ANALYSIS_LIST_FIELDS:
        if field not in payload:
            payload[field] = []
        if not isinstance(payload[field], list):
            raise AnalysisValidationError(f"analysis.{field} must be a list")
    extensions = payload.get("extensions")
    if extensions is None:
        payload["extensions"] = {}
    if not isinstance(payload["extensions"], dict):
        raise AnalysisValidationError("analysis.extensions must be an object")
    return payload


class ContentAnalyzer:
    def __init__(self, client=None, model=None):
        self.client = client or stock_extractor.OpenAIStyleClient()
        self.model = model or os.environ.get("LLM_MODEL", DEFAULT_MODEL)

    def analyze(self, metadata, transcript):
        raw = self.client.chat(build_analysis_messages(metadata, transcript), model=self.model)
        try:
            payload = json.loads(strip_json_fence(raw))
        except json.JSONDecodeError as exc:
            raise AnalysisValidationError("LLM response must be valid JSON") from exc
        return validate_analysis(payload), raw


def _markdown_list(items):
    if not items:
        return "- None"
    return "\n".join(f"- {item}" for item in items)


def _timeline_list(items):
    if not items:
        return "- None"
    lines = []
    for item in items:
        if isinstance(item, dict):
            lines.append(f"- {item.get('time', '')} - {item.get('event', '')}".strip())
        else:
            lines.append(f"- {item}")
    return "\n".join(lines)


def render_summary_markdown(metadata, analysis):
    title = metadata.title or metadata.video_id
    return "\n".join([
        f"# {title}",
        "",
        f"- Source: {metadata.canonical_url or metadata.source_url}",
        f"- Author: {metadata.author}",
        f"- Published: {metadata.publish_time}",
        "",
        "## Summary",
        "",
        analysis.get("summary", ""),
        "",
        "## Topics",
        "",
        _markdown_list(analysis.get("topics", [])),
        "",
        "## Keywords",
        "",
        _markdown_list(analysis.get("keywords", [])),
        "",
        "## Key Points",
        "",
        _markdown_list(analysis.get("key_points", [])),
        "",
        "## Timeline",
        "",
        _timeline_list(analysis.get("timeline", [])),
        "",
        "## Entities",
        "",
        _markdown_list(analysis.get("entities", [])),
        "",
        "## Open Questions",
        "",
        _markdown_list(analysis.get("open_questions", [])),
        "",
    ])
