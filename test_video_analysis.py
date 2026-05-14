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
