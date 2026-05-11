import json
import shutil
import sqlite3
import unittest
from pathlib import Path

import wechat_db


class WechatDbTests(unittest.TestCase):
    def test_import_jsonl_directory_upserts_clean_articles(self):
        root = Path.cwd() / ".test_tmp" / "wechat_db_unit"
        if root.exists():
            shutil.rmtree(root)
        try:
            root.mkdir(parents=True)
            account_dir = root / "sample_account"
            account_dir.mkdir()
            jsonl_path = account_dir / "articles.jsonl"
            records = [
                {
                    "account_id": "MP_TEST",
                    "article_id": "article-1",
                    "author": "Sample Author",
                    "title": "Original title",
                    "url": "https://mp.weixin.qq.com/s/article-1",
                    "publish_time": "2026-05-07T08:00:00+08:00",
                    "collected_at": "2026-05-07T09:00:00+00:00",
                    "cover_url": "https://example.com/cover.jpg",
                    "digest": "Digest",
                    "body_text": "Useful page text",
                    "content_hash": "hash-1",
                    "source_type": "wewe_rss",
                },
                {
                    "account_id": "MP_TEST",
                    "article_id": "article-1",
                    "author": "Sample Author",
                    "title": "Updated title",
                    "url": "https://mp.weixin.qq.com/s/article-1",
                    "publish_time": "2026-05-07T08:00:00+08:00",
                    "collected_at": "2026-05-07T10:00:00+00:00",
                    "cover_url": "https://example.com/cover.jpg",
                    "digest": "Updated digest",
                    "body_text": "Updated useful page text",
                    "content_hash": "hash-2",
                    "source_type": "wewe_rss",
                },
            ]
            with jsonl_path.open("w", encoding="utf-8") as f:
                for record in records:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")

            db_path = root / "articles.sqlite"
            imported = wechat_db.import_jsonl_directory(root, db_path)

            self.assertEqual(imported, 2)
            conn = sqlite3.connect(db_path)
            try:
                conn.row_factory = sqlite3.Row
                rows = conn.execute("SELECT * FROM articles").fetchall()
                self.assertEqual(len(rows), 1)
                row = rows[0]
                self.assertEqual(row["account_name"], "sample_account")
                self.assertEqual(row["title"], "Updated title")
                self.assertEqual(row["body_text"], "Updated useful page text")
                self.assertEqual(row["content_hash"], "hash-2")

                matches = wechat_db.search_articles(conn, "Updated", limit=10)
                self.assertEqual(len(matches), 1)
                self.assertEqual(matches[0]["title"], "Updated title")
            finally:
                conn.close()
        finally:
            if root.exists():
                shutil.rmtree(root)


if __name__ == "__main__":
    unittest.main()
