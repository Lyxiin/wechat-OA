import json
import shutil
import sqlite3
import unittest
from pathlib import Path

import wechat_db
import stock_extractor


class FakeLLMClient:
    def __init__(self, content):
        self.content = content
        self.calls = []

    def chat(self, messages, model):
        self.calls.append({"messages": messages, "model": model})
        return self.content


class FailingForArticleClient:
    def __init__(self, failing_title):
        self.failing_title = failing_title

    def chat(self, messages, model):
        article_text = messages[-1]["content"]
        if self.failing_title in article_text:
            raise RuntimeError("500 Server Error: Internal Server Error")
        return json.dumps({
            "stocks": [{"stock_name": "东阳光", "confidence": 0.8}]
        }, ensure_ascii=False)


class SensitiveThenCompactClient:
    def __init__(self):
        self.calls = []

    def chat(self, messages, model):
        content = messages[-1]["content"]
        self.calls.append(content)
        if "战争" in content:
            raise RuntimeError('500 Server Error | response: {"code":"sensitive_words_detected"}')
        if "算力：东阳光、中嘉博创" not in content:
            raise RuntimeError("compact prompt lost stock candidate lines")
        return json.dumps({
            "stocks": [{"stock_name": "东阳光", "theme": "算力", "confidence": 0.86}]
        }, ensure_ascii=False)


class StockExtractorTests(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / ".test_tmp" / "stock_extractor"
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        self.db_path = self.root / "articles.sqlite"
        self.conn = wechat_db.connect(self.db_path)
        wechat_db.initialize_db(self.conn)
        stock_extractor.initialize_stock_tables(self.conn)
        self.article = {
            "account_name": "盘前纪要",
            "account_id": "MP_TEST",
            "article_id": "article-1",
            "author": "盘前纪要",
            "title": "5月7日盘前纪要",
            "url": "https://mp.weixin.qq.com/s/article-1",
            "publish_time": "2026-05-07T07:06:21+08:00",
            "collected_at": "2026-05-07T09:00:00+00:00",
            "cover_url": "",
            "digest": "算力和CPU方向",
            "body_text": "算力：东阳光、中嘉博创。CPU：中国长城、禾盛新材。",
            "content_hash": "hash-article-1",
            "source_type": "wewe_rss",
            "imported_at": "2026-05-07T09:00:00+00:00",
        }
        wechat_db.upsert_article(self.conn, self.article)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        if self.root.exists():
            shutil.rmtree(self.root)

    def test_extract_and_store_article_stocks_replaces_existing_mentions(self):
        content = json.dumps({
            "stocks": [
                {
                    "stock_name": "东阳光",
                    "stock_code": "600673",
                    "market": "A股",
                    "theme": "算力",
                    "reason": "文章将其列入算力热点方向",
                    "evidence": "算力：东阳光、中嘉博创。",
                    "mention_type": "重点关注",
                    "confidence": 0.91,
                },
                {
                    "stock_name": "中国长城",
                    "stock_code": "000066",
                    "market": "A股",
                    "theme": "CPU",
                    "reason": "文章将其列入CPU热点方向",
                    "evidence": "CPU：中国长城、禾盛新材。",
                    "mention_type": "重点关注",
                    "confidence": 0.88,
                },
            ]
        }, ensure_ascii=False)
        client = FakeLLMClient(content)

        article = dict(self.conn.execute("SELECT * FROM articles").fetchone())
        inserted = stock_extractor.extract_and_store_article(
            self.conn,
            article,
            client,
            model="gpt-5.4-mini",
            force=True,
        )
        inserted_again = stock_extractor.extract_and_store_article(
            self.conn,
            article,
            client,
            model="gpt-5.4-mini",
            force=True,
        )

        self.assertEqual(inserted, 2)
        self.assertEqual(inserted_again, 2)
        rows = self.conn.execute(
            """
            SELECT stock_name, stock_code, market, theme, mention_type, confidence
            FROM stock_mentions
            ORDER BY stock_name
            """
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["stock_name"], "东阳光")
        self.assertEqual(rows[0]["stock_code"], "600673")
        self.assertEqual(rows[0]["theme"], "算力")
        self.assertEqual(rows[1]["stock_name"], "中国长城")
        self.assertEqual(rows[1]["mention_type"], "重点关注")

        run = self.conn.execute("SELECT * FROM extraction_runs").fetchone()
        self.assertEqual(run["status"], "success")
        self.assertEqual(run["model"], "gpt-5.4-mini")
        self.assertEqual(run["mention_count"], 2)

    def test_parse_llm_response_accepts_markdown_json_fence(self):
        payload = """```json
        {"stocks": [{"stock_name": "中嘉博创", "confidence": 0.75}]}
        ```"""

        stocks = stock_extractor.parse_llm_stocks(payload)

        self.assertEqual(len(stocks), 1)
        self.assertEqual(stocks[0]["stock_name"], "中嘉博创")
        self.assertEqual(stocks[0]["confidence"], 0.75)

    def test_load_env_file_reads_key_value_pairs_without_overriding_existing(self):
        env_path = self.root / ".env"
        env_path.write_text(
            "LLM_API_URL=https://example.com/v1/chat/completions\n"
            "LLM_MODEL=gpt-5.4-mini\n"
            "EXISTING_VALUE=from-file\n",
            encoding="utf-8",
        )
        old_value = stock_extractor.os.environ.get("EXISTING_VALUE")
        try:
            stock_extractor.os.environ["EXISTING_VALUE"] = "from-env"

            loaded = stock_extractor.load_env_file(env_path)

            self.assertEqual(loaded["LLM_API_URL"], "https://example.com/v1/chat/completions")
            self.assertEqual(stock_extractor.os.environ["LLM_MODEL"], "gpt-5.4-mini")
            self.assertEqual(stock_extractor.os.environ["EXISTING_VALUE"], "from-env")
        finally:
            if old_value is None:
                stock_extractor.os.environ.pop("EXISTING_VALUE", None)
            else:
                stock_extractor.os.environ["EXISTING_VALUE"] = old_value

    def test_openai_style_client_rejects_placeholder_api_key(self):
        old_value = stock_extractor.os.environ.get("LLM_API_KEY")
        try:
            stock_extractor.os.environ["LLM_API_KEY"] = "replace_with_your_key"

            with self.assertRaisesRegex(RuntimeError, "LLM_API_KEY"):
                stock_extractor.OpenAIStyleClient(api_url="https://example.com")
        finally:
            if old_value is None:
                stock_extractor.os.environ.pop("LLM_API_KEY", None)
            else:
                stock_extractor.os.environ["LLM_API_KEY"] = old_value

    def test_run_articles_extraction_continues_after_one_article_fails(self):
        second_article = dict(self.article)
        second_article.update({
            "article_id": "article-2",
            "title": "3月13日盘前纪要",
            "url": "https://mp.weixin.qq.com/s/article-2",
            "content_hash": "hash-article-2",
        })
        wechat_db.upsert_article(self.conn, second_article)
        self.conn.commit()
        articles = [
            dict(row) for row in self.conn.execute(
                "SELECT * FROM articles ORDER BY id"
            ).fetchall()
        ]
        client = FailingForArticleClient("3月13日盘前纪要")

        result = stock_extractor.run_articles_extraction(
            self.conn,
            articles,
            client,
            model="gpt-5.4-mini",
            stop_on_error=False,
        )

        self.assertEqual(result["articles"], 2)
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["mentions"], 1)
        failed = self.conn.execute(
            """
            SELECT er.status, er.error
            FROM extraction_runs er
            JOIN articles a ON a.id = er.article_db_id
            WHERE a.title = '3月13日盘前纪要'
            """
        ).fetchone()
        self.assertEqual(failed["status"], "failed")
        self.assertIn("500 Server Error", failed["error"])

    def test_sensitive_words_error_retries_with_compact_stock_lines(self):
        article = dict(self.conn.execute("SELECT * FROM articles").fetchone())
        article["body_text"] = "战争相关新闻段落。\n算力：东阳光、中嘉博创\n普通长句。"
        client = SensitiveThenCompactClient()

        inserted = stock_extractor.extract_and_store_article(
            self.conn,
            article,
            client,
            model="gpt-5.4-mini",
            force=True,
        )

        self.assertEqual(inserted, 1)
        self.assertEqual(len(client.calls), 2)
        self.assertIn("战争", client.calls[0])
        self.assertNotIn("战争", client.calls[1])
        self.assertIn("算力：东阳光、中嘉博创", client.calls[1])

    def test_candidate_stock_text_drops_sensitive_event_lines(self):
        body_text = "\n".join([
            "算力：东阳光、中嘉博创",
            "事件：地缘冲突影响，相关商品涨价。",
            "5、伊朗：相关船只严禁通行。",
            "光纤：长飞光纤、烽火通信、亨通光电",
        ])

        compact = stock_extractor.build_candidate_stock_text(body_text)

        self.assertIn("算力：东阳光、中嘉博创", compact)
        self.assertIn("光纤：长飞光纤、烽火通信、亨通光电", compact)
        self.assertNotIn("地缘冲突", compact)
        self.assertNotIn("伊朗", compact)


if __name__ == "__main__":
    unittest.main()
