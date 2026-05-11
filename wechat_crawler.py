#!/usr/bin/env python3
import argparse
import hashlib
import html
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

import feedparser
import requests

WEWE_RSS_URL = "http://localhost:4000"
OUTPUT_DIR = "./wechat_articles"
PLATFORM_URLS = [
    "https://weread.111965.xyz",
    "https://weread.965111.xyz",
]

# WeWe RSS v2 cannot search official accounts by name. It needs at least one
# article URL from the target account to discover the feed id.
KNOWN_MP_LINKS = {
    "人民日报": [
        "https://mp.weixin.qq.com/s/7piYPXB1XEt-y4sqEbnbdA",
        "https://mp.weixin.qq.com/s/9qmAgqWfVxx7SS2BVooTWg",
        "https://mp.weixin.qq.com/s/hwXuxOZx7LR-eH4q_1GffA",
    ],
}

CONTENT_ENCODED = "{http://purl.org/rss/1.0/modules/content/}encoded"
CHINA_TZ = timezone(timedelta(hours=8))
CLEAN_ARTICLE_JSONL_FIELDS = [
    "account_id",
    "article_id",
    "author",
    "title",
    "url",
    "publish_time",
    "collected_at",
    "cover_url",
    "digest",
    "body_text",
    "content_hash",
    "source_type",
]
VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


class RichMediaContentExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.capturing = False
        self.done = False
        self.depth = 0
        self.parts = []

    def _start_tag(self, tag, attrs):
        attrs_text = "".join(
            f' {name}="{html.escape(value or "", quote=True)}"'
            for name, value in attrs
        )
        return f"<{tag}{attrs_text}>"

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        attrs_dict = dict(attrs)
        if not self.capturing and attrs_dict.get("id") != "js_content":
            return
        if not self.capturing:
            self.capturing = True
            self.depth = 0
        self.parts.append(self._start_tag(tag, attrs))
        if tag.lower() not in VOID_TAGS:
            self.depth += 1

    def handle_startendtag(self, tag, attrs):
        if self.capturing and not self.done:
            attrs_text = "".join(
                f' {name}="{html.escape(value or "", quote=True)}"'
                for name, value in attrs
            )
            self.parts.append(f"<{tag}{attrs_text} />")

    def handle_endtag(self, tag):
        if not self.capturing or self.done:
            return
        self.parts.append(f"</{tag}>")
        if tag.lower() not in VOID_TAGS:
            self.depth -= 1
        if self.depth <= 0:
            self.capturing = False
            self.done = True

    def handle_data(self, data):
        if self.capturing and not self.done:
            self.parts.append(data)

    def handle_entityref(self, name):
        if self.capturing and not self.done:
            self.parts.append(f"&{name};")

    def handle_charref(self, name):
        if self.capturing and not self.done:
            self.parts.append(f"&#{name};")

    def get_html(self):
        return "".join(self.parts).strip()


class MetaExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "meta":
            return
        attrs_dict = {name.lower(): value for name, value in attrs}
        key = attrs_dict.get("property") or attrs_dict.get("name")
        content = attrs_dict.get("content")
        if key and content is not None:
            self.meta[key] = content


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in {"script", "style"}:
            self.skip_depth += 1
        if tag in {"br", "p", "div", "section", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {"script", "style"} and self.skip_depth:
            self.skip_depth -= 1
        if tag in {"p", "div", "section", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip_depth:
            self.parts.append(data)

    def get_text(self):
        text = html.unescape("".join(self.parts))
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n", text)
        return text.strip()


def trpc_get(path, payload=None):
    params = {"input": json.dumps(payload or {}, ensure_ascii=False)}
    resp = requests.get(f"{WEWE_RSS_URL}/trpc/{path}", params=params, timeout=60)
    resp.raise_for_status()
    return resp.json()["result"]["data"]


def trpc_post(path, payload):
    resp = requests.post(f"{WEWE_RSS_URL}/trpc/{path}", json=payload, timeout=180)
    resp.raise_for_status()
    body = resp.json()
    if "error" in body:
        raise RuntimeError(body["error"].get("message", body["error"]))
    return body.get("result", {}).get("data")


def find_feed(mp_name):
    feeds = trpc_get("feed.list").get("items", [])
    return next((feed for feed in feeds if feed.get("mpName") == mp_name), None)


def get_enabled_account():
    accounts = trpc_get("account.list").get("items", [])
    account = next((item for item in accounts if item.get("status") == 1), None)
    if not account:
        return None
    return trpc_get("account.byId", account["id"])


def discover_feed(mp_name, seed_urls):
    for wxs_link in seed_urls:
        infos = trpc_post("platform.getMpInfo", {"wxsLink": wxs_link}) or []
        feed_info = next((item for item in infos if item.get("name") == mp_name), None)
        if not feed_info:
            continue
        trpc_post("feed.add", {
            "id": feed_info["id"],
            "mpName": feed_info["name"],
            "mpCover": feed_info.get("cover", ""),
            "mpIntro": feed_info.get("intro", ""),
            "updateTime": feed_info.get("updateTime", int(datetime.now().timestamp())),
            "status": 1,
        })
        trpc_post("feed.refreshArticles", {"mpId": feed_info["id"]})
        trpc_post("feed.getHistoryArticles", {"mpId": feed_info["id"]})
        return find_feed(mp_name)
    return None


def get_feed_url(mp_name, seed_urls=None, limit=1000):
    try:
        resp = requests.post(f"{WEWE_RSS_URL}/api/search", json={"keyword": mp_name}, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("data"):
                fakeid = data["data"][0]["fakeid"]
                requests.post(f"{WEWE_RSS_URL}/api/subscribe", json={"fakeid": fakeid}, timeout=30)
                return f"{WEWE_RSS_URL}/feed/{fakeid}.xml"
    except requests.RequestException:
        pass

    try:
        feed = find_feed(mp_name)
        all_seed_urls = list(seed_urls or []) + KNOWN_MP_LINKS.get(mp_name, [])
        if not feed and all_seed_urls:
            feed = discover_feed(mp_name, all_seed_urls)
        if not feed:
            return None
        return f"{WEWE_RSS_URL}/feeds/{feed['id']}.rss?limit={limit}&mode=fulltext"
    except Exception as exc:
        print(f"WeWe RSS v2 subscription/query failed: {exc}")
        return None


def parse_pub_date(value):
    if not value:
        return datetime.now()
    parsed = parsedate_to_datetime(value)
    if parsed.tzinfo:
        parsed = parsed.astimezone(CHINA_TZ).replace(tzinfo=None)
    return parsed


def parse_rss_xml(xml_text):
    root = ET.fromstring(xml_text)
    channel = root.find("channel")
    if channel is None:
        return []

    entries = []
    for item in channel.findall("item"):
        title = item.findtext("title") or ""
        link = item.findtext("link") or ""
        pub_date = item.findtext("pubDate") or ""
        content = item.findtext(CONTENT_ENCODED) or item.findtext("description") or ""
        entries.append({
            "title": html.unescape(title).strip(),
            "link": html.unescape(link).strip(),
            "published_at": parse_pub_date(pub_date),
            "html": content,
        })
    return entries


def fetch_raw_rss_entries(feed_url):
    resp = requests.get(feed_url, headers=REQUEST_HEADERS, timeout=120)
    resp.raise_for_status()
    return parse_rss_xml(resp.text)


def fetch_feedparser_entries(feed_url):
    feed = feedparser.parse(feed_url)
    entries = []
    for entry in feed.entries:
        source_html = ""
        if entry.get("content"):
            source_html = entry.content[0].get("value", "")
        if not source_html:
            source_html = entry.get("summary", "")
        published_at = (
            datetime(*entry.published_parsed[:6])
            if "published_parsed" in entry
            else datetime.now()
        )
        entries.append({
            "title": entry.get("title", ""),
            "link": entry.get("link", ""),
            "published_at": published_at,
            "html": source_html,
        })
    return entries


def fetch_platform_entries(mp_id, limit=1000, max_pages=None):
    account = get_enabled_account()
    if not account or not account.get("token"):
        return []

    headers = {
        "xid": account["id"],
        "Authorization": f"Bearer {account['token']}",
    }
    page_variants = [
        lambda page: str(page),
        lambda page: page,
    ]
    max_pages = max_pages or max(5, min(50, int(limit / 20) + 3))
    entries = []
    seen = set()
    for platform_url in PLATFORM_URLS:
        for page_value in page_variants:
            empty_pages = 0
            for page in range(1, max_pages + 1):
                before_count = len(entries)
                resp = requests.get(
                    f"{platform_url}/api/v2/platform/mps/{mp_id}/articles",
                    headers=headers,
                    params={"page": page_value(page)},
                    timeout=60,
                )
                resp.raise_for_status()
                items = resp.json()
                if not isinstance(items, list):
                    continue
                for item in items:
                    article_id = item.get("id")
                    link = item.get("url") or (
                        f"https://mp.weixin.qq.com/s/{article_id}"
                        if article_id
                        else ""
                    )
                    if not link or link in seen:
                        continue
                    seen.add(link)
                    publish_time = datetime.fromtimestamp(
                        int(item.get("publishTime") or 0),
                        CHINA_TZ,
                    ).replace(tzinfo=None)
                    entries.append({
                        "title": item.get("title", ""),
                        "link": link,
                        "published_at": publish_time,
                        "html": "",
                        "cover_url": item.get("picUrl"),
                    })
                    if len(entries) >= limit:
                        return entries
                if len(entries) == before_count:
                    empty_pages += 1
                else:
                    empty_pages = 0
                if empty_pages >= 3:
                    break
    return entries


def extract_meta_title(source_html):
    match = re.search(
        r"<meta[^>]+property=[\"']og:title[\"'][^>]+content=[\"']([^\"']+)[\"']",
        source_html,
        re.IGNORECASE,
    )
    return html.unescape(match.group(1)).strip() if match else ""


def extract_meta(source_html):
    extractor = MetaExtractor()
    extractor.feed(source_html or "")
    return {key: html.unescape(value).strip() for key, value in extractor.meta.items()}


def strip_script_style(source_html):
    return re.sub(r"<(script|style)\b.*?</\1>", "", source_html or "", flags=re.I | re.S).strip()


def visible_text_length(source_html):
    text = re.sub(r"<[^>]+>", "", source_html or "")
    return len(html.unescape(text).strip())


def read_js_single_quoted_value(source, start):
    value = []
    escaped = False
    for index in range(start, len(source)):
        char = source[index]
        if char == "'" and not escaped:
            return "".join(value)
        value.append(char)
        if char == "\\" and not escaped:
            escaped = True
        else:
            escaped = False
    return ""


def decode_wechat_jsdecode_html(value):
    def decode_hex(match):
        return chr(int(match.group(1), 16))

    decoded = re.sub(r"\\x([0-9a-fA-F]{2})", decode_hex, value or "")
    decoded = re.sub(r"\\u([0-9a-fA-F]{4})", decode_hex, decoded)
    decoded = (
        decoded
        .replace(r"\/", "/")
        .replace(r"\'", "'")
        .replace(r'\"', '"')
        .replace(r"\n", "\n")
        .replace(r"\r", "\r")
        .replace(r"\t", "\t")
    )
    return html.unescape(decoded).strip()


def extract_wechat_jsdecode_content(source_html):
    for field_name in ("content_noencode", "content"):
        match = re.search(
            rf"{field_name}\s*:\s*JsDecode\('",
            source_html or "",
            re.S,
        )
        if not match:
            continue
        encoded = read_js_single_quoted_value(source_html, match.end())
        decoded = decode_wechat_jsdecode_html(encoded)
        if visible_text_length(decoded) > 0:
            return decoded
    return ""


def extract_article_body(source_html):
    extractor = RichMediaContentExtractor()
    extractor.feed(source_html or "")
    body = strip_script_style(extractor.get_html())
    if body and visible_text_length(body) >= 40:
        return body
    jsdecode_body = extract_wechat_jsdecode_content(source_html)
    if jsdecode_body:
        return strip_script_style(jsdecode_body)
    if body:
        return body
    return (source_html or "").strip()


def html_to_text(source_html):
    extractor = TextExtractor()
    extractor.feed(source_html or "")
    return extractor.get_text()


def article_id_from_url(url):
    match = re.search(r"/s/([^/?#]+)", url or "")
    return match.group(1) if match else None


def format_publish_time(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=CHINA_TZ)
    return value.isoformat()


def build_clean_article_record(entry, title, content_html, account_id, account_name, source_html):
    meta = extract_meta(source_html)
    body_text = html_to_text(content_html)
    record = {
        "account_id": account_id,
        "article_id": article_id_from_url(entry.get("link")),
        "author": meta.get("author") or meta.get("og:article:author") or account_name,
        "title": title,
        "url": entry.get("link"),
        "publish_time": format_publish_time(entry["published_at"]),
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "cover_url": meta.get("og:image") or meta.get("twitter:image"),
        "digest": meta.get("description") or meta.get("og:description") or meta.get("twitter:description"),
        "body_text": body_text,
        "content_hash": hashlib.sha256(body_text.encode("utf-8")).hexdigest(),
        "source_type": "wewe_rss",
    }
    return {field: record.get(field) for field in CLEAN_ARTICLE_JSONL_FIELDS}


def clean_existing_record(record):
    body_text = record.get("body_text") or html_to_text(record.get("body_html") or "")
    cleaned = {
        "account_id": record.get("account_id"),
        "article_id": record.get("article_id") or article_id_from_url(record.get("url")),
        "author": record.get("author"),
        "title": record.get("title"),
        "url": record.get("url"),
        "publish_time": record.get("publish_time"),
        "collected_at": record.get("collected_at") or datetime.now(timezone.utc).isoformat(),
        "cover_url": record.get("cover_url"),
        "digest": record.get("digest"),
        "body_text": body_text,
        "content_hash": hashlib.sha256(body_text.encode("utf-8")).hexdigest(),
        "source_type": record.get("source_type") or "wewe_rss",
    }
    return {field: cleaned.get(field) for field in CLEAN_ARTICLE_JSONL_FIELDS}


def clean_jsonl_file(input_path, output_path=None):
    input_path = Path(input_path)
    output_path = Path(output_path) if output_path else input_path
    records = []
    for line in input_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(clean_existing_record(json.loads(line)))
    write_jsonl(records, output_path)
    return output_path, len(records)


def has_substantial_content(source_html):
    return visible_text_length(source_html) >= 40


def account_id_from_feed_url(feed_url, account_name):
    match = re.search(r"/feeds/([^./?]+)", feed_url or "")
    return match.group(1) if match else account_name


def write_jsonl(records, jsonl_path):
    jsonl_path = Path(jsonl_path)
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def fetch_articles_from_entries(
    entries,
    start_date,
    end_date,
    output_dir,
    account_id=None,
    account_name=None,
    jsonl_path=None,
):
    records = []
    account_name = account_name or Path(output_dir).name
    account_id = account_id or account_name
    for entry in entries:
        pub_time = entry["published_at"]
        if not (start_date <= pub_time <= end_date):
            continue

        source_html = entry["html"]
        content = extract_article_body(source_html)
        if not has_substantial_content(content) and entry["link"]:
            resp = requests.get(entry["link"], headers=REQUEST_HEADERS, timeout=60)
            if resp.ok:
                content = extract_article_body(resp.text)
                source_html = resp.text

        title = extract_meta_title(source_html) or entry["title"]
        records.append(build_clean_article_record(
            entry,
            title=title,
            content_html=content,
            account_id=account_id,
            account_name=account_name,
            source_html=source_html,
        ))
        print(f"已整理: {title}")
    if jsonl_path is None:
        jsonl_path = Path(output_dir) / "articles.jsonl"
    write_jsonl(records, jsonl_path)
    print(f"JSONL已保存: {jsonl_path}")
    return records


def fetch_articles(
    feed_url,
    start_date,
    end_date,
    output_dir,
    account_id=None,
    account_name=None,
    jsonl_path=None,
):
    try:
        entries = fetch_raw_rss_entries(feed_url)
    except Exception:
        entries = fetch_feedparser_entries(feed_url)

    return fetch_articles_from_entries(
        entries,
        start_date,
        end_date,
        output_dir,
        account_id=account_id or account_id_from_feed_url(
            feed_url,
            account_name or Path(output_dir).name,
        ),
        account_name=account_name,
        jsonl_path=jsonl_path,
    )


def main():
    global WEWE_RSS_URL
    parser = argparse.ArgumentParser(
        description="采集微信公众号文章，清洗页面正文，并只保存 JSONL 数据文件。",
        epilog=(
            '示例: python wechat_crawler.py "人民日报" 20260101 20260601 '
            '--output ./wechat_articles'
        ),
    )
    parser.add_argument("mp_name", nargs="?", help="目标公众号名称，例如：人民日报")
    parser.add_argument("start_date", nargs="?", help="采集开始日期，格式 YYYYMMDD，例如：20260101")
    parser.add_argument("end_date", nargs="?", help="采集结束日期，格式 YYYYMMDD，例如：20260601")
    parser.add_argument("--output", default=OUTPUT_DIR, help="输出目录，默认 ./wechat_articles")
    parser.add_argument(
        "--jsonl-output",
        default=None,
        help="JSONL输出路径，默认写入 输出目录/公众号名/articles.jsonl",
    )
    parser.add_argument(
        "--clean-existing-jsonl",
        default=None,
        help="清洗已有 JSONL 文件并覆盖输出；使用该参数时不会访问 WeWe RSS。",
    )
    parser.add_argument("--wewe-url", default=WEWE_RSS_URL, help="WeWe RSS 地址，默认 http://localhost:4000")
    parser.add_argument(
        "--seed-url",
        action="append",
        default=[],
        help="该公众号任意一篇 mp.weixin.qq.com/s/ 文章链接。公众号未订阅时用于发现 feed，可传多次。",
    )
    parser.add_argument("--limit", type=int, default=1000, help="从 WeWe RSS feed 读取的最大文章数")
    args = parser.parse_args()

    if args.clean_existing_jsonl:
        output_path = args.jsonl_output or args.clean_existing_jsonl
        output_path, count = clean_jsonl_file(args.clean_existing_jsonl, output_path)
        print(f"清洗完成，共保存 {count} 条记录: {output_path}")
        return

    if not args.mp_name or not args.start_date or not args.end_date:
        parser.error("采集新数据时必须提供 mp_name start_date end_date")

    WEWE_RSS_URL = args.wewe_url.rstrip("/")
    start = datetime.strptime(args.start_date, "%Y%m%d")
    end = datetime.strptime(args.end_date, "%Y%m%d") + timedelta(days=1) - timedelta(seconds=1)
    feed_url = get_feed_url(args.mp_name, seed_urls=args.seed_url, limit=args.limit)
    if not feed_url:
        print("公众号不存在或订阅失败。WeWe RSS v2 首次订阅新公众号时，请追加 --seed-url 文章链接。")
        sys.exit(1)
    output_dir = Path(args.output) / args.mp_name
    jsonl_path = args.jsonl_output or output_dir / "articles.jsonl"
    records = fetch_articles(
        feed_url,
        start,
        end,
        output_dir,
        account_id=account_id_from_feed_url(feed_url, args.mp_name),
        account_name=args.mp_name,
        jsonl_path=jsonl_path,
    )
    if not records:
        feed = find_feed(args.mp_name)
        if feed:
            print("RSS returned 0 records; trying upstream platform fallback.")
            entries = fetch_platform_entries(feed["id"], limit=args.limit)
            if entries:
                records = fetch_articles_from_entries(
                    entries,
                    start,
                    end,
                    output_dir,
                    account_id=feed["id"],
                    account_name=args.mp_name,
                    jsonl_path=jsonl_path,
                )
    print(f"完成，共保存 {len(records)} 条 JSONL 记录")


if __name__ == "__main__":
    main()
