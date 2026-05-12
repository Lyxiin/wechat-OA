import json
import shutil
import unittest
from datetime import date
from pathlib import Path

import auto_pipeline
import wechat_db


def article_record(account_name, article_id, publish_time):
    return {
        "account_id": f"{account_name}-id",
        "article_id": article_id,
        "author": account_name,
        "title": f"{account_name} article",
        "url": f"https://mp.weixin.qq.com/s/{article_id}",
        "publish_time": publish_time,
        "collected_at": "2026-05-12T08:00:00+08:00",
        "cover_url": "",
        "digest": "digest",
        "body_text": "body text",
        "content_hash": f"hash-{article_id}",
        "source_type": "test",
    }


class FakeCrawler:
    WEWE_RSS_URL = ""

    def __init__(self):
        self.calls = []

    def get_feed_url(self, mp_name, seed_urls=None, limit=1000):
        self.calls.append(("get_feed_url", mp_name, tuple(seed_urls or []), limit))
        if mp_name == "missing":
            return None
        return f"http://wewe.test/feeds/{mp_name}.rss"

    def account_id_from_feed_url(self, feed_url, account_name):
        return f"{account_name}-id"

    def fetch_articles(
        self,
        feed_url,
        start_date,
        end_date,
        output_dir,
        account_id=None,
        account_name=None,
        jsonl_path=None,
    ):
        self.calls.append(("fetch_articles", account_name, start_date.date(), end_date.date()))
        records = [article_record(account_name, f"{account_name}-1", "2026-05-12T07:30:00+08:00")]
        Path(jsonl_path).parent.mkdir(parents=True, exist_ok=True)
        Path(jsonl_path).write_text(
            "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
            encoding="utf-8",
        )
        return records

    def find_feed(self, mp_name):
        return None


class AutoPipelineTests(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / ".test_tmp" / "auto_pipeline"
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        self.config_path = self.root / "accounts.json"
        self.db_path = self.root / "articles.sqlite"
        self.output_dir = self.root / "articles"

    def tearDown(self):
        if self.root.exists():
            shutil.rmtree(self.root)

    def write_config(self, accounts):
        self.config_path.write_text(
            json.dumps(
                {
                    "accounts": accounts,
                    "default_output_dir": str(self.output_dir),
                    "db_path": str(self.db_path),
                    "wewe_rss_url": "http://wewe.test",
                    "llm_model": "gpt-test",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def test_load_config_applies_defaults_and_creates_status_tables(self):
        self.write_config([{"name": "盘前纪要"}, {"name": "disabled", "enabled": False}])

        config = auto_pipeline.load_config(self.config_path)

        self.assertEqual(config["accounts"][0]["name"], "盘前纪要")
        self.assertEqual(config["accounts"][0]["crawl_days"], 7)
        self.assertEqual(config["accounts"][0]["limit"], 1000)
        self.assertEqual(config["accounts"][0]["seed_urls"], [])
        self.assertFalse(config["accounts"][1]["enabled"])

        conn = wechat_db.connect(self.db_path)
        try:
            auto_pipeline.initialize_pipeline_tables(conn)
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        finally:
            conn.close()

        self.assertIn("pipeline_runs", tables)
        self.assertIn("crawl_state", tables)

    def test_run_pipeline_crawls_imports_extracts_and_isolates_account_failures(self):
        self.write_config([
            {"name": "盘前纪要", "crawl_days": 3, "seed_urls": ["https://seed"]},
            {"name": "missing"},
        ])
        crawler = FakeCrawler()
        extracted = []

        def fake_extract(conn, articles, model, force=False):
            extracted.extend(article["title"] for article in articles)
            return {
                "articles": len(articles),
                "succeeded": len(articles),
                "failed": 0,
                "mentions": len(articles) * 2,
            }

        result = auto_pipeline.run_pipeline(
            config_path=self.config_path,
            today=date(2026, 5, 12),
            crawler=crawler,
            extract_articles=fake_extract,
        )

        self.assertEqual(result["status"], "partial_failed")
        self.assertEqual(result["accounts_total"], 2)
        self.assertEqual(result["accounts_succeeded"], 1)
        self.assertEqual(result["accounts_failed"], 1)
        self.assertEqual(result["articles_collected"], 1)
        self.assertEqual(result["articles_imported"], 1)
        self.assertEqual(result["mentions"], 2)
        self.assertEqual(extracted, ["盘前纪要 article"])

        conn = wechat_db.connect(self.db_path)
        try:
            auto_pipeline.initialize_pipeline_tables(conn)
            states = {
                row["account_name"]: dict(row)
                for row in conn.execute("SELECT * FROM crawl_state").fetchall()
            }
            articles = wechat_db.list_articles(conn, limit=10)
            latest = auto_pipeline.get_latest_pipeline_run(conn)
        finally:
            conn.close()

        self.assertEqual(len(articles), 1)
        self.assertEqual(states["盘前纪要"]["status"], "success")
        self.assertEqual(states["missing"]["status"], "failed")
        self.assertIn("feed not found", states["missing"]["last_error"])
        self.assertEqual(latest["status"], "partial_failed")

    def test_default_extract_articles_skips_empty_article_list(self):
        result = auto_pipeline.default_extract_articles(None, [], "gpt-test")

        self.assertEqual(
            result,
            {"articles": 0, "succeeded": 0, "failed": 0, "mentions": 0},
        )


if __name__ == "__main__":
    unittest.main()
