import unittest
from datetime import datetime

import wechat_crawler as crawler


class WechatCrawlerContentTests(unittest.TestCase):
    def test_parse_raw_rss_keeps_detail_page_body(self):
        xml = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <item>
      <title><![CDATA[Sample title]]></title>
      <link>https://mp.weixin.qq.com/s/sample</link>
      <pubDate>Thu, 07 May 2026 06:02:09 GMT</pubDate>
      <content:encoded><![CDATA[
        <html>
          <head><meta property="og:title" content="Meta title" /></head>
          <body>
            <div class="rich_media_content" id="js_content">
              <p>Detail body from page</p>
              <div><strong>Nested text</strong></div>
            </div>
          </body>
        </html>
      ]]></content:encoded>
    </item>
  </channel>
</rss>"""

        entries = crawler.parse_rss_xml(xml)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["title"], "Sample title")
        self.assertEqual(entries[0]["link"], "https://mp.weixin.qq.com/s/sample")
        body = crawler.extract_article_body(entries[0]["html"])
        self.assertIn("Detail body from page", body)
        self.assertIn("Nested text", body)
        self.assertNotIn("<html>", body)

    def test_build_clean_article_record_keeps_only_useful_jsonl_fields(self):
        source_html = """
        <html>
          <head>
            <meta name="author" content="人民日报" />
            <meta property="og:description" content="digest text" />
            <meta property="og:image" content="https://example.com/cover.jpg" />
          </head>
          <body>
            <div id="js_content"><p>正文第一段</p><p>正文第二段</p></div>
          </body>
        </html>
        """
        entry = {
            "title": "测试标题",
            "link": "https://mp.weixin.qq.com/s/article-token",
            "published_at": datetime(2026, 5, 7, 14, 2, 9),
            "html": source_html,
        }

        record = crawler.build_clean_article_record(
            entry,
            title="测试标题",
            content_html=crawler.extract_article_body(source_html),
            account_id="MP_WXS_TEST",
            account_name="人民日报",
            source_html=source_html,
        )

        self.assertEqual(list(record.keys()), crawler.CLEAN_ARTICLE_JSONL_FIELDS)
        self.assertEqual(record["account_id"], "MP_WXS_TEST")
        self.assertEqual(record["article_id"], "article-token")
        self.assertEqual(record["author"], "人民日报")
        self.assertIn("正文第一段", record["body_text"])
        self.assertNotIn("<p>", record["body_text"])
        self.assertNotIn("style=", json_dump := str(record))
        self.assertNotIn("body_html", record)
        self.assertNotIn("raw", record)
        self.assertEqual(record["cover_url"], "https://example.com/cover.jpg")
        self.assertEqual(record["digest"], "digest text")
        self.assertEqual(record["publish_time"], "2026-05-07T14:02:09+08:00")
        self.assertEqual(record["source_type"], "wewe_rss")
        self.assertEqual(record["url"], "https://mp.weixin.qq.com/s/article-token")

    def test_extract_article_body_reads_wechat_jsdecode_content(self):
        encoded_body = (
            "\\x3cp\\x3e"
            "\u6b63\u6587\u7b2c\u4e00\u6bb5"
            "\\x3c/p\\x3e\\x3cp\\x3eNo.1 "
            "\u76d8\u524d\u70ed\u70b9"
            "\\x3c/p\\x3e"
        )
        source_html = f"""
        <html>
          <body>
            <div id="js_content" aria-hidden="true"></div>
            <script>
              window.articleData = {{
                content_noencode: JsDecode('{encoded_body}')
              }};
            </script>
          </body>
        </html>
        """

        body = crawler.extract_article_body(source_html)
        text = crawler.html_to_text(body)

        self.assertIn("\u6b63\u6587\u7b2c\u4e00\u6bb5", body)
        self.assertIn("\u76d8\u524d\u70ed\u70b9", body)
        self.assertIn("\u6b63\u6587\u7b2c\u4e00\u6bb5", text)
        self.assertNotIn("content_noencode", text)


if __name__ == "__main__":
    unittest.main()
