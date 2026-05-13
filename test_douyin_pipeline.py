import json
import shutil
import unittest
from datetime import date
from pathlib import Path

import douyin_crawler
import douyin_pipeline
import wechat_db


class FakeCrawler:
    def __init__(self):
        self.calls = []

    def fetch_recent_videos(self, account, since_date, limit):
        self.calls.append(("fetch", account["name"], since_date, limit))
        return [
            {
                "video_id": "video-1",
                "title": "盘前聊算力",
                "url": "https://www.douyin.com/video/video-1",
                "publish_time": "2026-05-13T08:05:00+08:00",
                "description": "算力方向",
                "cover_url": "https://example.com/cover.jpg",
                "author": account["name"],
            }
        ]

    def download_audio(self, video, account, output_dir):
        self.calls.append(("audio", video["video_id"], account["name"]))
        audio_path = Path(output_dir) / "audio" / f"{video['video_id']}.mp3"
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(b"audio")
        return audio_path


class FakeASR:
    def __init__(self):
        self.calls = []

    def transcribe(self, audio_path):
        self.calls.append(Path(audio_path).name)
        return "算力方向提到了中际旭创和新易盛。"


class DouyinPipelineTests(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / ".test_tmp" / "douyin_pipeline"
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        self.config_path = self.root / "douyin_accounts.json"
        self.db_path = self.root / "articles.sqlite"
        self.output_dir = self.root / "douyin_data"
        self.config_path.write_text(
            json.dumps(
                {
                    "accounts": [
                        {
                            "name": "老白分析室",
                            "douyin_id": "luge0226",
                            "profile_url": "https://www.douyin.com/user/test-sec-uid",
                            "enabled": True,
                            "crawl_days": 3,
                            "limit": 5,
                        }
                    ],
                    "default_output_dir": str(self.output_dir),
                    "db_path": str(self.db_path),
                    "llm_model": "gpt-test",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        if self.root.exists():
            shutil.rmtree(self.root)

    def test_run_pipeline_transcribes_videos_imports_articles_and_extracts(self):
        extracted = []

        def fake_extract(conn, articles, model, force=False):
            extracted.extend(article["title"] for article in articles)
            return {
                "articles": len(articles),
                "succeeded": len(articles),
                "failed": 0,
                "mentions": len(articles) * 3,
            }

        result = douyin_pipeline.run_pipeline(
            config_path=self.config_path,
            today=date(2026, 5, 13),
            crawler=FakeCrawler(),
            asr=FakeASR(),
            extract_articles=fake_extract,
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["videos_collected"], 1)
        self.assertEqual(result["transcripts_created"], 1)
        self.assertEqual(result["articles_imported"], 1)
        self.assertEqual(result["mentions"], 3)
        self.assertEqual(extracted, ["盘前聊算力"])

        jsonl_path = self.output_dir / "老白分析室" / "articles.jsonl"
        record = json.loads(jsonl_path.read_text(encoding="utf-8").strip())
        self.assertEqual(record["account_id"], "luge0226")
        self.assertEqual(record["article_id"], "video-1")
        self.assertEqual(record["source_type"], "douyin_video")
        self.assertEqual(record["body_text"], "算力方向提到了中际旭创和新易盛。")

        conn = wechat_db.connect(self.db_path)
        try:
            articles = wechat_db.list_articles(conn, limit=10)
        finally:
            conn.close()
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["account_name"], "老白分析室")

    def test_transcript_cache_skips_second_asr_call(self):
        crawler = FakeCrawler()
        asr = FakeASR()

        douyin_pipeline.run_pipeline(
            config_path=self.config_path,
            today=date(2026, 5, 13),
            crawler=crawler,
            asr=asr,
            extract_articles=lambda conn, articles, model, force=False: {
                "articles": len(articles),
                "succeeded": len(articles),
                "failed": 0,
                "mentions": 0,
            },
        )
        douyin_pipeline.run_pipeline(
            config_path=self.config_path,
            today=date(2026, 5, 13),
            crawler=crawler,
            asr=asr,
            extract_articles=lambda conn, articles, model, force=False: {
                "articles": len(articles),
                "succeeded": len(articles),
                "failed": 0,
                "mentions": 0,
            },
        )

        self.assertEqual(asr.calls, ["video-1.mp3"])

    def test_load_config_requires_profile_url_for_douyin_id_only_accounts(self):
        self.config_path.write_text(
            json.dumps({
                "accounts": [{"name": "都业华", "douyin_id": "yxtduyehua"}],
                "default_output_dir": str(self.output_dir),
                "db_path": str(self.db_path),
            }, ensure_ascii=False),
            encoding="utf-8",
        )

        config = douyin_pipeline.load_config(self.config_path)

        self.assertEqual(config["accounts"][0]["name"], "都业华")
        self.assertEqual(config["accounts"][0]["profile_url"], "")
        self.assertIn("复制抖音主页分享链接", config["accounts"][0]["setup_hint"])

    def test_run_pipeline_reports_profile_url_hint_before_requiring_asr_key(self):
        self.config_path.write_text(
            json.dumps({
                "accounts": [{"name": "都业华", "douyin_id": "yxtduyehua"}],
                "default_output_dir": str(self.output_dir),
                "db_path": str(self.db_path),
                "llm_model": "gpt-test",
            }, ensure_ascii=False),
            encoding="utf-8",
        )

        result = douyin_pipeline.run_pipeline(
            config_path=self.config_path,
            today=date(2026, 5, 13),
            crawler=douyin_pipeline.douyin_crawler.DouyinCrawler(),
            asr=None,
            extract_articles=lambda conn, articles, model, force=False: {
                "articles": 0,
                "succeeded": 0,
                "failed": 0,
                "mentions": 0,
            },
        )

        self.assertEqual(result["status"], "failed")
        self.assertIn("复制抖音主页分享链接", result["errors"][0]["error"])

    def test_canonical_profile_url_extracts_sec_uid_from_share_redirect(self):
        url = (
            "https://www.iesdouyin.com/share/user/MS4w"
            "?sec_uid=MS4wLjABAAAA-profile-id&from=web_code_link"
        )

        canonical = douyin_crawler.canonical_profile_url(url)

        self.assertEqual(canonical, "https://www.douyin.com/user/MS4wLjABAAAA-profile-id")

    def test_cookies_file_is_only_used_when_it_exists(self):
        crawler = douyin_crawler.DouyinCrawler(cookies_file=str(self.root / "missing.txt"))

        args = crawler._cookies_args({})

        self.assertEqual(args, [])


if __name__ == "__main__":
    unittest.main()
