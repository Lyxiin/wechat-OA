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
        self.store.close()
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
        store = core.VideoAnalysisStore(self.root / "data", self.root / "data" / "db.sqlite")
        try:
            paths = store.paths_for(metadata.video_id)
        finally:
            store.close()

        audio_path = downloader.download_audio(metadata, paths, force=False)

        self.assertEqual(audio_path, paths.audio_path)
        self.assertTrue(audio_path.exists())

    def test_download_audio_reuses_existing_file_without_force(self):
        downloader = core.YtDlpVideoDownloader(cookies_file=self.cookies, runner=self.fake_runner)
        metadata = core.VideoMetadata("video-1", "source", "canonical")
        store = core.VideoAnalysisStore(self.root / "data", self.root / "data" / "db.sqlite")
        try:
            paths = store.paths_for(metadata.video_id)
        finally:
            store.close()
        paths.audio_path.parent.mkdir(parents=True, exist_ok=True)
        paths.audio_path.write_bytes(b"cached")

        audio_path = downloader.download_audio(metadata, paths, force=False)

        self.assertEqual(audio_path.read_bytes(), b"cached")


if __name__ == "__main__":
    unittest.main()
