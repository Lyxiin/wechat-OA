import os
import shutil
import unittest
from pathlib import Path

import stock_extractor
import stock_web
import wechat_db


class StockWebTests(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / ".test_tmp" / "stock_web"
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        self.db_path = self.root / "articles.sqlite"
        self.conn = wechat_db.connect(self.db_path)
        wechat_db.initialize_db(self.conn)
        stock_extractor.initialize_stock_tables(self.conn)
        article = {
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
            "body_text": "算力：东阳光、中嘉博创。CPU：中国长城。",
            "content_hash": "hash-article-1",
            "source_type": "wewe_rss",
            "imported_at": "2026-05-07T09:00:00+00:00",
        }
        wechat_db.upsert_article(self.conn, article)
        db_article = dict(self.conn.execute("SELECT * FROM articles").fetchone())
        stock_extractor.replace_article_mentions(
            self.conn,
            db_article,
            [
                {
                    "stock_name": "东阳光",
                    "stock_code": "600673",
                    "market": "A股",
                    "theme": "算力",
                    "reason": "文章将其列入算力方向",
                    "evidence": "算力：东阳光、中嘉博创。",
                    "mention_type": "重点关注",
                    "confidence": 0.91,
                },
                {
                    "stock_name": "中国长城",
                    "stock_code": "000066",
                    "market": "A股",
                    "theme": "CPU",
                    "reason": "文章将其列入CPU方向",
                    "evidence": "CPU：中国长城。",
                    "mention_type": "重点关注",
                    "confidence": 0.88,
                },
            ],
            model="gpt-5.4-mini",
            extracted_at="2026-05-07T10:00:00+00:00",
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        if self.root.exists():
            shutil.rmtree(self.root)

    def test_auth_creates_users_and_validates_sessions(self):
        stock_web.initialize_auth_tables(self.conn)
        admin = stock_web.create_user(self.conn, "admin", "secret123", role="admin")
        normal = stock_web.create_user(self.conn, "viewer", "viewer123", role="user")

        self.assertEqual(admin["username"], "admin")
        self.assertEqual(admin["role"], "admin")
        self.assertEqual(normal["role"], "user")
        self.assertNotIn("password_hash", admin)

        self.assertIsNone(stock_web.authenticate_user(self.conn, "admin", "wrong"))
        logged_in = stock_web.authenticate_user(self.conn, "admin", "secret123")
        self.assertEqual(logged_in["username"], "admin")
        self.assertEqual(logged_in["role"], "admin")

        token = stock_web.create_session(self.conn, logged_in["id"])
        session_user = stock_web.user_from_session(self.conn, token)
        self.assertEqual(session_user["username"], "admin")
        self.assertEqual(session_user["role"], "admin")

        stock_web.delete_session(self.conn, token)
        self.assertIsNone(stock_web.user_from_session(self.conn, token))

    def test_default_admin_does_not_use_fixed_password_without_env(self):
        old_user = os.environ.pop("STOCK_WEB_ADMIN_USER", None)
        old_password = os.environ.pop("STOCK_WEB_ADMIN_PASSWORD", None)
        try:
            admin = stock_web.ensure_default_admin(self.conn)

            self.assertEqual(admin["username"], "admin")
            self.assertIsNone(
                stock_web.authenticate_user(self.conn, "admin", "admin123456")
            )
        finally:
            if old_user is not None:
                os.environ["STOCK_WEB_ADMIN_USER"] = old_user
            if old_password is not None:
                os.environ["STOCK_WEB_ADMIN_PASSWORD"] = old_password

    def test_get_dashboard_returns_date_summary_and_grouped_stocks(self):
        dashboard = stock_web.get_dashboard(self.conn, date="2026-05-07")

        self.assertEqual(dashboard["selected_date"], "2026-05-07")
        self.assertEqual(dashboard["summary"]["article_count"], 1)
        self.assertEqual(dashboard["summary"]["mention_count"], 2)
        self.assertEqual(dashboard["summary"]["stock_count"], 2)
        self.assertEqual(dashboard["dates"], ["2026-05-07"])
        self.assertEqual(dashboard["channels"], ["盘前纪要"])
        self.assertEqual(dashboard["stocks"][0]["stock_name"], "东阳光")
        self.assertEqual(dashboard["stocks"][0]["mention_count"], 1)
        self.assertIn("算力", dashboard["stocks"][0]["themes"])
        self.assertEqual(dashboard["mentions"][0]["article_title"], "5月7日盘前纪要")
        self.assertEqual(dashboard["mentions"][0]["url"], "https://mp.weixin.qq.com/s/article-1")

    def test_get_dashboard_includes_stock_view_window_sentiment_counts(self):
        self._insert_article_with_mentions(
            article_id="article-2",
            title="May 6 briefing",
            url="https://mp.weixin.qq.com/s/article-2",
            publish_time="2026-05-06T07:00:00+08:00",
            mentions=[
                {
                    "stock_name": "AlphaTech",
                    "stock_code": "000001",
                    "market": "A股",
                    "theme": "AI",
                    "reason": "listed as a key opportunity",
                    "evidence": "AI: AlphaTech",
                    "mention_type": "推荐",
                    "confidence": 0.92,
                },
                {
                    "stock_name": "AlphaTech",
                    "stock_code": "000001",
                    "market": "A股",
                    "theme": "公告",
                    "reason": "shareholder reduction risk",
                    "evidence": "AlphaTech: shareholder plans to reduce holdings",
                    "mention_type": "风险提示",
                    "confidence": 0.9,
                },
            ],
        )
        self._insert_article_with_mentions(
            article_id="article-3",
            title="May 2 briefing",
            url="https://mp.weixin.qq.com/s/article-3",
            publish_time="2026-05-02T07:00:00+08:00",
            mentions=[
                {
                    "stock_name": "AlphaTech",
                    "stock_code": "000001",
                    "market": "A股",
                    "theme": "AI",
                    "reason": "mentioned in related names",
                    "evidence": "related names include AlphaTech",
                    "mention_type": "普通提及",
                    "confidence": 0.75,
                }
            ],
        )
        self._insert_article_with_mentions(
            article_id="article-4",
            title="April 20 briefing",
            url="https://mp.weixin.qq.com/s/article-4",
            publish_time="2026-04-20T07:00:00+08:00",
            mentions=[
                {
                    "stock_name": "AlphaTech",
                    "stock_code": "000001",
                    "market": "A股",
                    "theme": "AI",
                    "reason": "outside the 7 day window",
                    "evidence": "old AlphaTech mention",
                    "mention_type": "推荐",
                    "confidence": 0.8,
                }
            ],
        )

        dashboard = stock_web.get_dashboard(
            self.conn,
            date="2026-05-07",
            stock_window=7,
            stock_keyword="Alpha",
        )

        stock_view = dashboard["stock_view"]
        self.assertEqual(stock_view["window_days"], 7)
        self.assertEqual(stock_view["start_date"], "2026-05-01")
        self.assertEqual(stock_view["end_date"], "2026-05-07")
        self.assertEqual(stock_view["summary"]["mention_count"], 3)
        self.assertEqual(stock_view["summary"]["bullish_count"], 1)
        self.assertEqual(stock_view["summary"]["bearish_count"], 1)
        self.assertEqual(stock_view["summary"]["neutral_count"], 1)
        self.assertEqual(stock_view["stocks"][0]["stock_name"], "AlphaTech")
        self.assertEqual(stock_view["stocks"][0]["mention_count"], 3)
        self.assertEqual(stock_view["stocks"][0]["active_days"], 2)
        self.assertEqual(stock_view["stocks"][0]["bullish_count"], 1)
        self.assertEqual(stock_view["stocks"][0]["bearish_count"], 1)
        self.assertEqual(stock_view["stocks"][0]["neutral_count"], 1)

    def _insert_article_with_mentions(self, article_id, title, url, publish_time, mentions):
        article = {
            "account_name": "盘前纪要",
            "account_id": "MP_TEST",
            "article_id": article_id,
            "author": "盘前纪要",
            "title": title,
            "url": url,
            "publish_time": publish_time,
            "collected_at": "2026-05-07T09:00:00+00:00",
            "cover_url": "",
            "digest": "",
            "body_text": title,
            "content_hash": f"hash-{article_id}",
            "source_type": "wewe_rss",
            "imported_at": "2026-05-07T09:00:00+00:00",
        }
        wechat_db.upsert_article(self.conn, article)
        db_article = dict(
            self.conn.execute("SELECT * FROM articles WHERE url = ?", (url,)).fetchone()
        )
        stock_extractor.replace_article_mentions(
            self.conn,
            db_article,
            mentions,
            model="gpt-5.4-mini",
            extracted_at="2026-05-07T10:00:00+00:00",
        )
        self.conn.commit()


if __name__ == "__main__":
    unittest.main()
