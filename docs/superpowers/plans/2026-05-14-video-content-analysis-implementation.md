# Video Content Analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a command-line public video analysis pipeline that reads public video URLs, extracts audio, transcribes speech, generates structured content analysis, and writes file artifacts plus SQLite index rows.

**Architecture:** Add a focused core module plus a small CLI wrapper. The core module owns source reading, yt-dlp download/metadata, ASR and LLM adapters, artifact storage, SQLite indexing, caching, and orchestration. The CLI only parses arguments, loads environment variables, wires default adapters, and prints a JSON run summary.

**Tech Stack:** Python standard library, `requests` through existing clients, `yt-dlp`, SQLite, `unittest`, existing `asr_client.GroqASRClient`, existing `stock_extractor.OpenAIStyleClient`.

---

## File Structure

- Create `video_analysis_core.py`
  - Responsibility: reusable pipeline logic with no command-line parsing.
  - Contains: `VideoSourceReader`, `VideoMetadata`, `VideoPaths`, `YtDlpVideoDownloader`, `ContentAnalyzer`, `VideoAnalysisStore`, `PipelineRunner`, and helper functions.
- Create `video_analysis.py`
  - Responsibility: command-line interface only.
  - Contains: `build_parser()`, `command_init_db()`, `command_run()`, `main()`.
- Create `test_video_analysis.py`
  - Responsibility: fake-client tests for URL reading, storage, analyzer validation, downloader behavior, pipeline orchestration, and CLI parser behavior.
- Modify `.gitignore`
  - Add `video_analysis_data/` so generated artifacts are not committed.
- Reuse existing files without modifying them unless implementation discovers a real defect:
  - `asr_client.py`
  - `stock_extractor.py`
  - `douyin_crawler.py`

## Task 1: URL Source Reading And Stable IDs

**Files:**
- Create: `test_video_analysis.py`
- Create: `video_analysis_core.py`

- [ ] **Step 1: Write failing URL reader and helper tests**

Add this initial test file:

```python
import json
import shutil
import unittest
from pathlib import Path

import video_analysis_core as core


class VideoSourceReaderTests(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / ".test_tmp" / "video_analysis"
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)

    def tearDown(self):
        if self.root.exists():
            shutil.rmtree(self.root)

    def test_reads_single_url_and_url_file_with_comments_and_deduplication(self):
        urls_path = self.root / "urls.txt"
        urls_path.write_text(
            "\n".join([
                "# comment",
                "https://example.com/video/1",
                "",
                " https://example.com/video/2 ",
                "https://example.com/video/1",
            ]),
            encoding="utf-8",
        )

        urls = core.VideoSourceReader.read_sources(
            url="https://example.com/video/0",
            urls_file=urls_path,
        )

        self.assertEqual(urls, [
            "https://example.com/video/0",
            "https://example.com/video/1",
            "https://example.com/video/2",
        ])

    def test_read_sources_requires_at_least_one_input(self):
        with self.assertRaisesRegex(ValueError, "one URL"):
            core.VideoSourceReader.read_sources()

    def test_safe_video_id_keeps_stable_readable_ids(self):
        self.assertEqual(core.safe_video_id("abc DEF/123"), "abc_DEF_123")
        self.assertEqual(core.safe_video_id(""), "unknown")
        self.assertLessEqual(len(core.safe_video_id("x" * 140)), 80)

    def test_content_hash_is_stable(self):
        self.assertEqual(core.content_hash("hello"), core.content_hash("hello"))
        self.assertNotEqual(core.content_hash("hello"), core.content_hash("world"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
python -m unittest test_video_analysis.py
```

Expected: FAIL with `ModuleNotFoundError: No module named 'video_analysis_core'`.

- [ ] **Step 3: Add minimal source reader and helper implementation**

Create `video_analysis_core.py` with this content:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```powershell
python -m unittest test_video_analysis.py
```

Expected: PASS for 4 tests.

- [ ] **Step 5: Commit**

```powershell
git add video_analysis_core.py test_video_analysis.py
git commit -m "Add video analysis source reader"
```

## Task 2: SQLite Store And Artifact Paths

**Files:**
- Modify: `test_video_analysis.py`
- Modify: `video_analysis_core.py`

- [ ] **Step 1: Add failing store tests**

Add this test class above the `if __name__ == "__main__"` block in `test_video_analysis.py`:

```python
class VideoAnalysisStoreTests(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / ".test_tmp" / "video_analysis_store"
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        self.store = core.VideoAnalysisStore(
            output_dir=self.root / "data",
            db_path=self.root / "data" / "video_analysis.sqlite",
        )

    def tearDown(self):
        if self.root.exists():
            shutil.rmtree(self.root)

    def test_initialize_db_creates_video_items_table(self):
        self.store.initialize_db()

        rows = self.store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'video_items'"
        ).fetchall()

        self.assertEqual(len(rows), 1)

    def test_paths_for_video_are_stable(self):
        paths = self.store.paths_for("video/1")

        self.assertEqual(paths.folder, self.root / "data" / "video_1")
        self.assertEqual(paths.metadata_path.name, "metadata.json")
        self.assertEqual(paths.audio_path.name, "audio.mp3")
        self.assertEqual(paths.transcript_path.name, "transcript.txt")
        self.assertEqual(paths.analysis_path.name, "analysis.json")
        self.assertEqual(paths.summary_path.name, "summary.md")

    def test_writes_artifacts_and_upserts_success_row(self):
        self.store.initialize_db()
        metadata = core.VideoMetadata(
            video_id="video-1",
            source_url="https://example.com/share",
            canonical_url="https://example.com/video/video-1",
            title="Title",
            author="Author",
            publish_time="2026-05-14T08:00:00+08:00",
            duration=12.5,
            thumbnail_url="https://example.com/cover.jpg",
            raw={"id": "video-1"},
        )
        paths = self.store.paths_for(metadata.video_id)
        self.store.write_json(paths.metadata_path, metadata.to_dict())
        self.store.write_text(paths.transcript_path, "transcript")
        self.store.write_json(paths.analysis_path, {"summary": "summary"})
        self.store.write_text(paths.summary_path, "# Summary")
        self.store.upsert_item(
            metadata=metadata,
            paths=paths,
            status="success",
            error=None,
            transcript_hash=core.content_hash("transcript"),
            analysis_hash=core.content_hash("summary"),
        )

        row = self.store.get_item("video-1")

        self.assertEqual(row["status"], "success")
        self.assertEqual(row["title"], "Title")
        self.assertTrue(Path(row["transcript_path"]).exists())
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
python -m unittest test_video_analysis.py
```

Expected: FAIL with `AttributeError` for missing `VideoAnalysisStore` or `VideoMetadata`.

- [ ] **Step 3: Add metadata, paths, and store implementation**

Append this code to `video_analysis_core.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```powershell
python -m unittest test_video_analysis.py
```

Expected: PASS for URL reader and store tests.

- [ ] **Step 5: Commit**

```powershell
git add video_analysis_core.py test_video_analysis.py
git commit -m "Add video analysis storage"
```

## Task 3: Content Analyzer And Markdown Summary

**Files:**
- Modify: `test_video_analysis.py`
- Modify: `video_analysis_core.py`

- [ ] **Step 1: Add failing analyzer tests**

Add this test class above the `if __name__ == "__main__"` block:

```python
class FakeLLMClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def chat(self, messages, model):
        self.calls.append({"messages": messages, "model": model})
        return self.response


class ContentAnalyzerTests(unittest.TestCase):
    def test_analyzer_parses_and_validates_json(self):
        client = FakeLLMClient(
            """```json
            {
              "summary": "A concise summary",
              "topics": ["topic"],
              "keywords": ["keyword"],
              "timeline": [{"time": "00:00", "event": "intro"}],
              "key_points": ["point"],
              "entities": ["entity"],
              "action_items": [],
              "open_questions": [],
              "extensions": {}
            }
            ```"""
        )
        analyzer = core.ContentAnalyzer(client=client, model="model-test")
        metadata = core.VideoMetadata(
            video_id="video-1",
            source_url="https://example.com/share",
            canonical_url="https://example.com/video/video-1",
            title="Title",
            author="Author",
        )

        analysis, raw = analyzer.analyze(metadata, "transcript text")

        self.assertEqual(analysis["summary"], "A concise summary")
        self.assertEqual(raw, client.response)
        self.assertEqual(client.calls[0]["model"], "model-test")

    def test_analyzer_rejects_invalid_json(self):
        analyzer = core.ContentAnalyzer(client=FakeLLMClient("not json"), model="model-test")
        metadata = core.VideoMetadata("video-1", "url", "url")

        with self.assertRaisesRegex(core.AnalysisValidationError, "valid JSON"):
            analyzer.analyze(metadata, "transcript text")

    def test_render_summary_markdown_contains_human_readable_sections(self):
        metadata = core.VideoMetadata(
            video_id="video-1",
            source_url="https://example.com/share",
            canonical_url="https://example.com/video/video-1",
            title="Title",
            author="Author",
            publish_time="2026-05-14",
        )
        analysis = {
            "summary": "Summary",
            "topics": ["Topic"],
            "keywords": ["Keyword"],
            "timeline": [{"time": "00:00", "event": "Intro"}],
            "key_points": ["Point"],
            "entities": ["Entity"],
            "action_items": [],
            "open_questions": ["Question"],
            "extensions": {},
        }

        markdown = core.render_summary_markdown(metadata, analysis)

        self.assertIn("# Title", markdown)
        self.assertIn("Summary", markdown)
        self.assertIn("- Topic", markdown)
        self.assertIn("- 00:00 - Intro", markdown)
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
python -m unittest test_video_analysis.py
```

Expected: FAIL with missing `ContentAnalyzer`, `AnalysisValidationError`, or `render_summary_markdown`.

- [ ] **Step 3: Add analyzer implementation**

Append this code to `video_analysis_core.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```powershell
python -m unittest test_video_analysis.py
```

Expected: PASS for source reader, store, and analyzer tests.

- [ ] **Step 5: Commit**

```powershell
git add video_analysis_core.py test_video_analysis.py
git commit -m "Add video content analyzer"
```

## Task 4: yt-dlp Metadata And Audio Downloader

**Files:**
- Modify: `test_video_analysis.py`
- Modify: `video_analysis_core.py`

- [ ] **Step 1: Add failing downloader tests**

Add this test class above the `if __name__ == "__main__"` block:

```python
class FakeCompleted:
    def __init__(self, stdout=""):
        self.stdout = stdout


class YtDlpDownloaderTests(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / ".test_tmp" / "video_analysis_downloader"
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        self.cookies = self.root / "cookies.txt"
        self.cookies.write_text("# cookies", encoding="utf-8")
        self.calls = []

    def tearDown(self):
        if self.root.exists():
            shutil.rmtree(self.root)

    def fake_runner(self, cmd):
        self.calls.append(cmd)
        if "--dump-json" in cmd:
            return FakeCompleted(stdout=json.dumps({
                "id": "video-1",
                "webpage_url": "https://example.com/video/video-1",
                "title": "Title",
                "uploader": "Author",
                "timestamp": 1778736000,
                "duration": 12.5,
                "thumbnail": "https://example.com/cover.jpg"
            }))
        output_index = cmd.index("-o") + 1
        output_template = Path(cmd[output_index])
        audio_path = output_template.parent / "video-1.mp3"
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(b"audio")
        return FakeCompleted()

    def test_resolve_metadata_uses_ytdlp_and_cookies(self):
        downloader = core.YtDlpVideoDownloader(cookies_file=self.cookies, runner=self.fake_runner)

        metadata = downloader.resolve_metadata("https://example.com/share")

        self.assertEqual(metadata.video_id, "video-1")
        self.assertEqual(metadata.title, "Title")
        self.assertEqual(metadata.author, "Author")
        self.assertIn("--cookies", self.calls[0])

    def test_download_audio_creates_expected_audio_path(self):
        downloader = core.YtDlpVideoDownloader(cookies_file=self.cookies, runner=self.fake_runner)
        metadata = downloader.resolve_metadata("https://example.com/share")
        paths = core.VideoAnalysisStore(self.root / "data", self.root / "data" / "db.sqlite").paths_for(metadata.video_id)

        audio_path = downloader.download_audio(metadata, paths, force=False)

        self.assertEqual(audio_path, paths.audio_path)
        self.assertTrue(audio_path.exists())

    def test_download_audio_reuses_existing_file_without_force(self):
        downloader = core.YtDlpVideoDownloader(cookies_file=self.cookies, runner=self.fake_runner)
        metadata = core.VideoMetadata("video-1", "source", "canonical")
        paths = core.VideoAnalysisStore(self.root / "data", self.root / "data" / "db.sqlite").paths_for(metadata.video_id)
        paths.audio_path.parent.mkdir(parents=True, exist_ok=True)
        paths.audio_path.write_bytes(b"cached")

        audio_path = downloader.download_audio(metadata, paths, force=False)

        self.assertEqual(audio_path.read_bytes(), b"cached")
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
python -m unittest test_video_analysis.py
```

Expected: FAIL with missing `YtDlpVideoDownloader`.

- [ ] **Step 3: Add downloader implementation**

Append this code to `video_analysis_core.py`:

```python
def _timestamp_to_iso(value):
    if not value:
        return ""
    return datetime.fromtimestamp(int(value), timezone.utc).isoformat()


def _upload_date_to_iso(value):
    if not value:
        return ""
    text = str(value)
    if len(text) != 8:
        return ""
    return f"{text[0:4]}-{text[4:6]}-{text[6:8]}"


def _first_ytdlp_record(stdout):
    records = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        payload = json.loads(line)
        if payload.get("_type") == "playlist":
            records.extend(payload.get("entries") or [])
        else:
            records.append(payload)
    if not records:
        raise RuntimeError("yt-dlp returned no metadata")
    return records[0]


class YtDlpVideoDownloader:
    def __init__(self, cookies_file=DEFAULT_COOKIES_FILE, ytdlp_cmd=None, runner=None):
        self.cookies_file = Path(cookies_file) if cookies_file else None
        self.ytdlp_cmd = ytdlp_cmd or [sys.executable, "-m", "yt_dlp"]
        self.runner = runner

    def _cookies_args(self):
        if self.cookies_file and self.cookies_file.exists():
            return ["--cookies", str(self.cookies_file)]
        return []

    def _run_ytdlp(self, args):
        cmd = [*self.ytdlp_cmd, *args]
        if self.runner:
            return self.runner(cmd)
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

    def resolve_metadata(self, source_url):
        args = ["--dump-json", "--no-warnings", "--no-playlist"]
        args.extend(self._cookies_args())
        args.append(source_url)
        completed = self._run_ytdlp(args)
        info = _first_ytdlp_record(completed.stdout)
        video_id = safe_video_id(info.get("id") or info.get("display_id") or content_hash(source_url)[:16])
        canonical_url = info.get("webpage_url") or info.get("original_url") or source_url
        publish_time = (
            _timestamp_to_iso(info.get("timestamp"))
            or _timestamp_to_iso(info.get("release_timestamp"))
            or _upload_date_to_iso(info.get("upload_date"))
        )
        return VideoMetadata(
            video_id=video_id,
            source_url=source_url,
            canonical_url=canonical_url,
            title=(info.get("title") or info.get("description") or video_id).strip(),
            author=(info.get("uploader") or info.get("channel") or "").strip(),
            publish_time=publish_time,
            duration=info.get("duration"),
            thumbnail_url=info.get("thumbnail") or "",
            raw=info,
        )

    def download_audio(self, metadata, paths, force=False):
        if paths.audio_path.exists() and paths.audio_path.stat().st_size > 0 and not force:
            return paths.audio_path
        paths.audio_path.parent.mkdir(parents=True, exist_ok=True)
        args = [
            "--extract-audio",
            "--audio-format",
            "mp3",
            "--audio-quality",
            "0",
            "--no-playlist",
            "-o",
            str(paths.folder / "%(id)s.%(ext)s"),
        ]
        args.extend(self._cookies_args())
        args.append(metadata.canonical_url or metadata.source_url)
        self._run_ytdlp(args)
        expected = paths.folder / f"{metadata.video_id}.mp3"
        if expected.exists() and expected != paths.audio_path:
            expected.replace(paths.audio_path)
        if paths.audio_path.exists() and paths.audio_path.stat().st_size > 0:
            return paths.audio_path
        matches = sorted(paths.folder.glob("*.mp3"))
        if matches:
            matches[0].replace(paths.audio_path)
            return paths.audio_path
        raise RuntimeError(f"audio file was not created for {metadata.source_url}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```powershell
python -m unittest test_video_analysis.py
```

Expected: PASS for downloader tests and earlier tests.

- [ ] **Step 5: Commit**

```powershell
git add video_analysis_core.py test_video_analysis.py
git commit -m "Add yt-dlp video downloader"
```

## Task 5: Pipeline Runner, Caching, And Failure Isolation

**Files:**
- Modify: `test_video_analysis.py`
- Modify: `video_analysis_core.py`

- [ ] **Step 1: Add failing pipeline tests**

Add this test class above the `if __name__ == "__main__"` block:

```python
class FakeDownloader:
    def __init__(self):
        self.metadata_calls = []
        self.audio_calls = []

    def resolve_metadata(self, source_url):
        self.metadata_calls.append(source_url)
        if "bad" in source_url:
            raise RuntimeError("download failed")
        video_id = source_url.rsplit("/", 1)[-1]
        return core.VideoMetadata(
            video_id=video_id,
            source_url=source_url,
            canonical_url=source_url,
            title=f"Title {video_id}",
            author="Author",
        )

    def download_audio(self, metadata, paths, force=False):
        self.audio_calls.append((metadata.video_id, force))
        paths.audio_path.parent.mkdir(parents=True, exist_ok=True)
        paths.audio_path.write_bytes(b"audio")
        return paths.audio_path


class FakeASR:
    def __init__(self):
        self.calls = []

    def transcribe(self, audio_path):
        self.calls.append(Path(audio_path).name)
        return "transcript text"


class FakeAnalyzer:
    def __init__(self):
        self.calls = []

    def analyze(self, metadata, transcript):
        self.calls.append((metadata.video_id, transcript))
        return {
            "summary": f"Summary {metadata.video_id}",
            "topics": ["Topic"],
            "keywords": ["Keyword"],
            "timeline": [],
            "key_points": ["Point"],
            "entities": [],
            "action_items": [],
            "open_questions": [],
            "extensions": {},
        }, "{\"summary\":\"raw\"}"


class PipelineRunnerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / ".test_tmp" / "video_analysis_pipeline"
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        self.store = core.VideoAnalysisStore(
            output_dir=self.root / "data",
            db_path=self.root / "data" / "video_analysis.sqlite",
        )
        self.store.initialize_db()

    def tearDown(self):
        self.store.close()
        if self.root.exists():
            shutil.rmtree(self.root)

    def test_pipeline_writes_artifacts_and_continues_after_failure(self):
        runner = core.PipelineRunner(
            store=self.store,
            downloader=FakeDownloader(),
            asr=FakeASR(),
            analyzer=FakeAnalyzer(),
        )

        result = runner.run_urls(["https://example.com/good", "https://example.com/bad"])

        self.assertEqual(result["status"], "partial_failed")
        self.assertEqual(result["videos_total"], 2)
        self.assertEqual(result["videos_succeeded"], 1)
        self.assertEqual(result["videos_failed"], 1)
        good_row = self.store.get_item("good")
        bad_row = self.store.get_item(core.content_hash("https://example.com/bad")[:16])
        self.assertEqual(good_row["status"], "success")
        self.assertEqual(bad_row["status"], "failed")
        self.assertTrue(Path(good_row["analysis_path"]).exists())

    def test_pipeline_reuses_cached_transcript_and_analysis(self):
        downloader = FakeDownloader()
        asr = FakeASR()
        analyzer = FakeAnalyzer()
        runner = core.PipelineRunner(self.store, downloader, asr, analyzer)

        runner.run_urls(["https://example.com/good"])
        runner.run_urls(["https://example.com/good"])

        self.assertEqual(asr.calls, ["audio.mp3"])
        self.assertEqual(len(analyzer.calls), 1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
python -m unittest test_video_analysis.py
```

Expected: FAIL with missing `PipelineRunner`.

- [ ] **Step 3: Add pipeline implementation**

Append this code to `video_analysis_core.py`:

```python
def empty_run_result(total):
    return {
        "status": "running",
        "videos_total": total,
        "videos_succeeded": 0,
        "videos_failed": 0,
        "items": [],
    }


def final_run_status(result):
    if result["videos_failed"] and result["videos_succeeded"]:
        return "partial_failed"
    if result["videos_failed"]:
        return "failed"
    return "success"


def should_reuse_analysis(store, paths, transcript):
    payload = store.read_json_if_present(paths.analysis_path)
    if not isinstance(payload, dict):
        return False
    return payload.get("_transcript_hash") == content_hash(transcript)


class PipelineRunner:
    def __init__(self, store, downloader, asr, analyzer):
        self.store = store
        self.downloader = downloader
        self.asr = asr
        self.analyzer = analyzer

    def run_urls(
        self,
        urls,
        force_download=False,
        force_transcribe=False,
        force_analyze=False,
    ):
        result = empty_run_result(len(urls))
        for source_url in urls:
            item = self.run_one(
                source_url,
                force_download=force_download,
                force_transcribe=force_transcribe,
                force_analyze=force_analyze,
            )
            result["items"].append(item)
            if item["status"] == "success":
                result["videos_succeeded"] += 1
            else:
                result["videos_failed"] += 1
        result["status"] = final_run_status(result)
        return result

    def run_one(self, source_url, force_download=False, force_transcribe=False, force_analyze=False):
        fallback_id = content_hash(source_url)[:16]
        fallback_metadata = VideoMetadata(
            video_id=fallback_id,
            source_url=source_url,
            canonical_url=source_url,
        )
        fallback_paths = self.store.paths_for(fallback_id)
        try:
            metadata = self.downloader.resolve_metadata(source_url)
            paths = self.store.paths_for(metadata.video_id)
            self.store.write_json(paths.metadata_path, metadata.to_dict())
            audio_path = self.downloader.download_audio(metadata, paths, force=force_download)
            transcript = self.store.read_text_if_present(paths.transcript_path)
            if force_transcribe or not transcript:
                transcript = self.asr.transcribe(audio_path).strip()
                if not transcript:
                    raise RuntimeError(f"empty transcript for {metadata.source_url}")
                self.store.write_text(paths.transcript_path, transcript)
            if force_analyze or not should_reuse_analysis(self.store, paths, transcript):
                try:
                    analysis, raw = self.analyzer.analyze(metadata, transcript)
                except Exception as exc:
                    if hasattr(exc, "__cause__"):
                        self.store.write_text(paths.raw_analysis_path, str(exc.__cause__))
                    raise
                analysis["_transcript_hash"] = content_hash(transcript)
                self.store.write_json(paths.analysis_path, analysis)
                self.store.write_text(paths.summary_path, render_summary_markdown(metadata, analysis))
            else:
                analysis = self.store.read_json_if_present(paths.analysis_path)
            transcript_hash = content_hash(transcript)
            analysis_hash = content_hash(json.dumps(analysis, ensure_ascii=False, sort_keys=True))
            self.store.upsert_item(
                metadata=metadata,
                paths=paths,
                status="success",
                error=None,
                transcript_hash=transcript_hash,
                analysis_hash=analysis_hash,
            )
            return {
                "status": "success",
                "video_id": metadata.video_id,
                "title": metadata.title,
                "summary_path": str(paths.summary_path),
            }
        except Exception as exc:
            self.store.upsert_item(
                metadata=fallback_metadata,
                paths=fallback_paths,
                status="failed",
                error=str(exc),
            )
            return {
                "status": "failed",
                "video_id": fallback_metadata.video_id,
                "url": source_url,
                "error": str(exc),
            }
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```powershell
python -m unittest test_video_analysis.py
```

Expected: PASS for pipeline tests and earlier tests.

- [ ] **Step 5: Commit**

```powershell
git add video_analysis_core.py test_video_analysis.py
git commit -m "Add video analysis pipeline runner"
```

## Task 6: CLI Wrapper And Generated Artifact Ignore Rule

**Files:**
- Create: `video_analysis.py`
- Modify: `test_video_analysis.py`
- Modify: `.gitignore`

- [ ] **Step 1: Add failing CLI parser tests**

Add this test class above the `if __name__ == "__main__"` block:

```python
import video_analysis


class VideoAnalysisCliTests(unittest.TestCase):
    def test_parser_accepts_run_url(self):
        parser = video_analysis.build_parser()

        args = parser.parse_args(["run", "--url", "https://example.com/video/1"])

        self.assertEqual(args.command, "run")
        self.assertEqual(args.url, "https://example.com/video/1")
        self.assertFalse(args.force_analyze)

    def test_parser_accepts_init_db(self):
        parser = video_analysis.build_parser()

        args = parser.parse_args(["init-db", "--db", "custom.sqlite"])

        self.assertEqual(args.command, "init-db")
        self.assertEqual(args.db, "custom.sqlite")
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
python -m unittest test_video_analysis.py
```

Expected: FAIL with `ModuleNotFoundError: No module named 'video_analysis'`.

- [ ] **Step 3: Add CLI implementation**

Create `video_analysis.py` with this content:

```python
#!/usr/bin/env python3
import argparse
import json
import os
from pathlib import Path

import asr_client
import stock_extractor
from video_analysis_core import (
    DEFAULT_COOKIES_FILE,
    DEFAULT_DB,
    DEFAULT_MODEL,
    DEFAULT_OUTPUT_DIR,
    ContentAnalyzer,
    PipelineRunner,
    VideoAnalysisStore,
    VideoSourceReader,
    YtDlpVideoDownloader,
)


def command_init_db(args):
    store = VideoAnalysisStore(output_dir=args.output_dir, db_path=args.db)
    try:
        store.initialize_db()
    finally:
        store.close()
    print(f"Video analysis database initialized: {Path(args.db).resolve()}")


def command_run(args):
    if args.env_file:
        asr_client.load_env_file(args.env_file)
        stock_extractor.load_env_file(args.env_file)
    urls = VideoSourceReader.read_sources(url=args.url, urls_file=args.urls)
    store = VideoAnalysisStore(output_dir=args.output_dir, db_path=args.db)
    try:
        store.initialize_db()
        downloader = YtDlpVideoDownloader(cookies_file=args.cookies_file)
        asr = asr_client.GroqASRClient()
        analyzer = ContentAnalyzer(model=args.model or os.environ.get("LLM_MODEL", DEFAULT_MODEL))
        runner = PipelineRunner(store=store, downloader=downloader, asr=asr, analyzer=analyzer)
        result = runner.run_urls(
            urls,
            force_download=args.force_download,
            force_transcribe=args.force_transcribe,
            force_analyze=args.force_analyze,
        )
    finally:
        store.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))


def build_parser():
    parser = argparse.ArgumentParser(
        description="Analyze publicly shared video content by extracting audio, transcribing, and summarizing."
    )
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--env-file", default=".env")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init-db", help="Create the video analysis SQLite schema.")
    init_parser.add_argument("--db", default=str(DEFAULT_DB))
    init_parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    init_parser.set_defaults(func=command_init_db)

    run_parser = subparsers.add_parser("run", help="Analyze one video URL or a URL list file.")
    run_parser.add_argument("--url", help="One public video or share URL.")
    run_parser.add_argument("--urls", help="Text file containing one public video URL per line.")
    run_parser.add_argument("--cookies-file", default=str(DEFAULT_COOKIES_FILE))
    run_parser.add_argument("--model", default=None)
    run_parser.add_argument("--force-download", action="store_true")
    run_parser.add_argument("--force-transcribe", action="store_true")
    run_parser.add_argument("--force-analyze", action="store_true")
    run_parser.set_defaults(func=command_run)
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Add artifact ignore rule**

Modify `.gitignore` by adding this line near the other generated data directories:

```gitignore
video_analysis_data/
```

- [ ] **Step 5: Run tests to verify they pass**

Run:

```powershell
python -m unittest test_video_analysis.py
```

Expected: PASS for CLI tests and earlier tests.

- [ ] **Step 6: Compile new modules**

Run:

```powershell
python -m py_compile video_analysis.py video_analysis_core.py
```

Expected: command exits with code 0 and no syntax errors.

- [ ] **Step 7: Commit**

```powershell
git add .gitignore video_analysis.py video_analysis_core.py test_video_analysis.py
git commit -m "Add video analysis CLI"
```

## Task 7: Full Verification And Documentation Touch-Up

**Files:**
- Modify: `README.md` only if the final command examples need to be discoverable from the root README.

- [ ] **Step 1: Run the new focused test suite**

Run:

```powershell
python -m unittest test_video_analysis.py
```

Expected: all tests pass.

- [ ] **Step 2: Run related existing tests**

Run:

```powershell
python -m unittest test_asr_client.py test_douyin_pipeline.py
```

Expected: all tests pass. These guard the reused ASR and yt-dlp-adjacent behavior.

- [ ] **Step 3: Run syntax verification**

Run:

```powershell
python -m py_compile video_analysis.py video_analysis_core.py asr_client.py stock_extractor.py douyin_crawler.py
```

Expected: command exits with code 0 and no syntax errors.

- [ ] **Step 4: Optional manual dry run without network**

Run:

```powershell
python video_analysis.py init-db --db .test_tmp/video_analysis_manual/video_analysis.sqlite --output-dir .test_tmp/video_analysis_manual
```

Expected: prints `Video analysis database initialized:` and creates `.test_tmp/video_analysis_manual/video_analysis.sqlite`.

- [ ] **Step 5: Optional README addition**

If README encoding and existing content are safe to edit, add this short section near the usage examples:

```markdown
## Public Video Analysis

Initialize the local video analysis database:

```powershell
python video_analysis.py init-db
```

Analyze one public video URL:

```powershell
python video_analysis.py run --url "https://..."
```

Analyze a list of public video URLs:

```powershell
python video_analysis.py run --urls urls.txt
```
```

If README content is visibly mojibake in the editor, skip this step and report that the root README should be cleaned up separately before adding more Chinese documentation.

- [ ] **Step 6: Commit verification documentation if README changed**

If Step 5 changed README:

```powershell
git add README.md
git commit -m "Document video analysis commands"
```

If Step 5 was skipped, no commit is needed for this task.

## Self-Review

Spec coverage:

- URL list and single URL input: Task 1 and Task 6.
- File artifacts and SQLite index records: Task 2 and Task 5.
- ASR adapter and OpenAI-style LLM adapter: Task 3, Task 5, and Task 6.
- yt-dlp metadata and audio extraction with cookies: Task 4.
- Caching behavior: Task 5.
- Per-video failure isolation: Task 5.
- CLI commands: Task 6.
- Generated artifact ignore rule: Task 6.
- Verification without real network or credentials: Task 7.

Placeholder scan:

- This plan intentionally contains no unfinished placeholder markers or deferred implementation steps.
- Each code-changing task includes concrete test code, implementation code, commands, and expected results.

Type consistency:

- `VideoMetadata`, `VideoPaths`, `VideoAnalysisStore`, `ContentAnalyzer`, `YtDlpVideoDownloader`, and `PipelineRunner` are introduced before later tasks use them.
- `summary_path`, `analysis_path`, `transcript_hash`, and `analysis_hash` match the SQLite fields from the design spec.
- CLI defaults reference `DEFAULT_OUTPUT_DIR`, `DEFAULT_DB`, `DEFAULT_COOKIES_FILE`, and `DEFAULT_MODEL` from `video_analysis_core.py`.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-14-video-content-analysis-implementation.md`. Two execution options:

1. **Subagent-Driven (recommended)** - dispatch a fresh subagent per task, review between tasks, fast iteration.

2. **Inline Execution** - execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
