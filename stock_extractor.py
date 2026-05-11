#!/usr/bin/env python3
import argparse
import json
import os
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import requests

import wechat_db


DEFAULT_DB = Path("./wechat_articles/articles.sqlite")
DEFAULT_API_URL = "https://api.ikuncode.cc/v1/chat/completions"
DEFAULT_MODEL = "gpt-5.4-mini"
PLACEHOLDER_API_KEYS = {"replace_with_your_key", "your_key", "sk-xxx"}
SENSITIVE_RETRY_TERMS = {
    "地缘", "冲突", "战争", "军事", "导弹", "伊朗", "复仇", "霍尔木兹",
    "中东", "船只", "严禁通行", "白宫", "恐怖", "袭击",
}

MENTION_COLUMNS = [
    "article_db_id",
    "trade_date",
    "channel",
    "stock_name",
    "stock_code",
    "market",
    "theme",
    "reason",
    "evidence",
    "mention_type",
    "confidence",
    "llm_model",
    "extracted_at",
]


def load_env_file(env_path=".env"):
    env_path = Path(env_path)
    loaded = {}
    if not env_path.exists():
        return loaded
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        loaded[key] = value
        os.environ.setdefault(key, value)
    return loaded


def initialize_stock_tables(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stock_mentions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_db_id INTEGER NOT NULL,
            trade_date TEXT NOT NULL,
            channel TEXT NOT NULL,
            stock_name TEXT NOT NULL,
            stock_code TEXT,
            market TEXT,
            theme TEXT,
            reason TEXT,
            evidence TEXT,
            mention_type TEXT,
            confidence REAL,
            llm_model TEXT,
            extracted_at TEXT NOT NULL,
            FOREIGN KEY(article_db_id) REFERENCES articles(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS extraction_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_db_id INTEGER NOT NULL UNIQUE,
            content_hash TEXT,
            model TEXT,
            status TEXT NOT NULL,
            mention_count INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(article_db_id) REFERENCES articles(id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_stock_mentions_trade_date ON stock_mentions(trade_date)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_stock_mentions_stock_name ON stock_mentions(stock_name)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_stock_mentions_article ON stock_mentions(article_db_id)"
    )
    conn.commit()


def strip_json_fence(text):
    text = (text or "").strip()
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.S | re.I)
    return match.group(1).strip() if match else text


def parse_llm_stocks(text):
    payload = json.loads(strip_json_fence(text))
    stocks = payload.get("stocks", []) if isinstance(payload, dict) else []
    return [normalize_stock(item) for item in stocks if normalize_stock(item)]


def normalize_stock(item):
    if not isinstance(item, dict):
        return None
    stock_name = clean_text(item.get("stock_name") or item.get("name"))
    if not stock_name:
        return None
    confidence = item.get("confidence")
    try:
        confidence = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        confidence = None
    if confidence is not None:
        confidence = max(0.0, min(1.0, confidence))
    return {
        "stock_name": stock_name,
        "stock_code": clean_text(item.get("stock_code") or item.get("code")),
        "market": clean_text(item.get("market")) or "未知",
        "theme": clean_text(item.get("theme")),
        "reason": clean_text(item.get("reason")),
        "evidence": clean_text(item.get("evidence")),
        "mention_type": clean_text(item.get("mention_type")) or "普通提及",
        "confidence": confidence,
    }


def clean_text(value):
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def article_trade_date(article):
    publish_time = article.get("publish_time") or ""
    return publish_time[:10] if len(publish_time) >= 10 else ""


def build_prompt(article):
    return [
        {
            "role": "system",
            "content": (
                "你是金融文本信息抽取助手。只抽取文章中明确提到的股票或上市公司，"
                "不要编造代码，不确定代码就留空。只返回 JSON。"
            ),
        },
        {
            "role": "user",
            "content": (
                "请从下面微信公众号文章中提取被推荐、重点关注或明显提及的股票。"
                "返回格式必须是 JSON："
                '{"stocks":[{"stock_name":"","stock_code":"","market":"A股/港股/美股/未知",'
                '"theme":"","reason":"","evidence":"","mention_type":"推荐/重点关注/普通提及/风险提示",'
                '"confidence":0.0}]}。'
                "\n\n"
                f"标题：{article.get('title')}\n"
                f"公众号：{article.get('account_name')}\n"
                f"发布时间：{article.get('publish_time')}\n"
                f"正文：\n{article.get('body_text')}"
            ),
        },
    ]


def is_sensitive_words_error(exc):
    return "sensitive_words_detected" in str(exc)


def build_candidate_stock_text(body_text):
    candidates = []
    seen = set()
    for raw_line in (body_text or "").replace("\u200c", "").splitlines():
        line = clean_text(raw_line)
        if not line or line in seen:
            continue
        if should_drop_for_sensitive_retry(line):
            continue
        colon_pos = min(
            [pos for pos in (line.find("："), line.find(":")) if pos >= 0],
            default=-1,
        )
        if colon_pos < 0 or colon_pos > 24:
            continue
        left = line[:colon_pos]
        right = line[colon_pos + 1:]
        if left.startswith("事件") or re.match(r"^\d+[、.，,]", left):
            continue
        if not looks_like_stock_candidate_list(right):
            continue
        if len(line) > 260:
            line = line[:260]
        candidates.append(line)
        seen.add(line)
    return "\n".join(candidates)


def should_drop_for_sensitive_retry(line):
    return any(term in line for term in SENSITIVE_RETRY_TERMS)


def looks_like_stock_candidate_list(text):
    if "、" in text:
        tokens = [token.strip() for token in text.split("、")]
        stock_like = [
            token for token in tokens
            if re.search(r"[\u4e00-\u9fffA-Za-z*]{2,}", token)
        ]
        return len(stock_like) >= 2
    return bool(re.match(r"^[\u4e00-\u9fffA-Za-z* ]{2,16}[（(]", text.strip()))


def build_sensitive_retry_article(article):
    compact_text = build_candidate_stock_text(article.get("body_text"))
    if not compact_text:
        compact_text = article.get("body_text", "")[:1200]
    retry_article = dict(article)
    retry_article["body_text"] = (
        "以下是从原文中提取出的股票候选行，仅用于结构化识别股票名称、题材和证据：\n"
        f"{compact_text}"
    )
    return retry_article


class OpenAIStyleClient:
    def __init__(self, api_url=None, api_key=None, timeout=120, max_retries=2):
        self.api_url = api_url or os.environ.get("LLM_API_URL") or DEFAULT_API_URL
        self.api_key = api_key or os.environ.get("LLM_API_KEY")
        self.timeout = timeout
        self.max_retries = max_retries
        validate_api_key(self.api_key)

    def chat(self, messages, model):
        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = requests.post(
                    self.api_url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": messages,
                        "temperature": 0,
                    },
                    timeout=self.timeout,
                )
                if resp.status_code == 401:
                    raise RuntimeError(
                        "GPT API 认证失败: 请检查 .env 中的 LLM_API_KEY 是否已替换为真实可用的 key。"
                    )
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_error = RuntimeError(format_http_error(resp))
                    if attempt < self.max_retries:
                        time.sleep(2 ** attempt)
                        continue
                    raise last_error
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"]
            except requests.RequestException as exc:
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(2 ** attempt)
                    continue
                raise
        raise last_error or RuntimeError("GPT API 请求失败")


def format_http_error(resp):
    body = (resp.text or "").strip()
    if len(body) > 300:
        body = body[:300] + "..."
    if resp.status_code >= 500:
        message = f"{resp.status_code} Server Error: {resp.reason} for url: {resp.url}"
    else:
        message = f"{resp.status_code} Error: {resp.reason} for url: {resp.url}"
    if body:
        message += f" | response: {body}"
    return message


def validate_api_key(api_key):
    if not api_key:
        raise RuntimeError("缺少 LLM_API_KEY 环境变量或 .env 配置")
    if api_key.strip() in PLACEHOLDER_API_KEYS:
        raise RuntimeError("LLM_API_KEY 还是模板值，请在 .env 中替换为真实 key")
    if len(api_key.strip()) < 30:
        raise RuntimeError("LLM_API_KEY 看起来过短，请检查 .env 配置")


def extract_and_store_article(conn, article, client, model=DEFAULT_MODEL, force=False):
    initialize_stock_tables(conn)
    existing = conn.execute(
        "SELECT * FROM extraction_runs WHERE article_db_id = ?",
        (article["id"],),
    ).fetchone()
    if (
        existing
        and not force
        and existing["status"] == "success"
        and existing["content_hash"] == article.get("content_hash")
        and existing["model"] == model
    ):
        return existing["mention_count"]

    now = datetime.now(timezone.utc).isoformat()
    try:
        try:
            raw_content = client.chat(build_prompt(article), model=model)
        except Exception as exc:
            if not is_sensitive_words_error(exc):
                raise
            retry_article = build_sensitive_retry_article(article)
            raw_content = client.chat(build_prompt(retry_article), model=model)
        stocks = parse_llm_stocks(raw_content)
        replace_article_mentions(conn, article, stocks, model, now)
        upsert_run(
            conn,
            article_db_id=article["id"],
            content_hash=article.get("content_hash"),
            model=model,
            status="success",
            mention_count=len(stocks),
            error=None,
            now=now,
        )
        conn.commit()
        return len(stocks)
    except Exception as exc:
        upsert_run(
            conn,
            article_db_id=article["id"],
            content_hash=article.get("content_hash"),
            model=model,
            status="failed",
            mention_count=0,
            error=str(exc),
            now=now,
        )
        conn.commit()
        raise


def replace_article_mentions(conn, article, stocks, model, extracted_at):
    conn.execute(
        "DELETE FROM stock_mentions WHERE article_db_id = ?",
        (article["id"],),
    )
    for stock in stocks:
        row = {
            "article_db_id": article["id"],
            "trade_date": article_trade_date(article),
            "channel": article.get("account_name") or "",
            **stock,
            "llm_model": model,
            "extracted_at": extracted_at,
        }
        values = [row.get(column) for column in MENTION_COLUMNS]
        conn.execute(
            f"""
            INSERT INTO stock_mentions ({", ".join(MENTION_COLUMNS)})
            VALUES ({", ".join("?" for _ in MENTION_COLUMNS)})
            """,
            values,
        )


def upsert_run(conn, article_db_id, content_hash, model, status, mention_count, error, now):
    conn.execute(
        """
        INSERT INTO extraction_runs (
            article_db_id, content_hash, model, status, mention_count,
            error, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(article_db_id) DO UPDATE SET
            content_hash=excluded.content_hash,
            model=excluded.model,
            status=excluded.status,
            mention_count=excluded.mention_count,
            error=excluded.error,
            updated_at=excluded.updated_at
        """,
        (article_db_id, content_hash, model, status, mention_count, error, now, now),
    )


def iter_target_articles(conn, date=None, limit=None):
    params = []
    where = ""
    if date:
        where = "WHERE substr(publish_time, 1, 10) = ?"
        params.append(date)
    sql = f"""
        SELECT *
        FROM articles
        {where}
        ORDER BY publish_time DESC, id DESC
    """
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def run_articles_extraction(conn, articles, client, model=DEFAULT_MODEL, force=False, stop_on_error=False):
    total_mentions = 0
    succeeded = 0
    failed = 0
    for index, article in enumerate(articles, 1):
        try:
            count = extract_and_store_article(conn, article, client, model=model, force=force)
            total_mentions += count
            succeeded += 1
            print(f"[{index}/{len(articles)}] {article['title']} -> {count} 条股票信息")
        except Exception as exc:
            failed += 1
            print(f"[{index}/{len(articles)}] {article['title']} -> 失败: {exc}")
            if stop_on_error:
                raise
    return {
        "articles": len(articles),
        "succeeded": succeeded,
        "failed": failed,
        "mentions": total_mentions,
    }


def run_extraction(
    db_path=DEFAULT_DB,
    date=None,
    limit=None,
    model=DEFAULT_MODEL,
    force=False,
    stop_on_error=False,
):
    client = OpenAIStyleClient()
    with closing(wechat_db.connect(db_path)) as conn:
        wechat_db.initialize_db(conn)
        initialize_stock_tables(conn)
        articles = iter_target_articles(conn, date=date, limit=limit)
        return run_articles_extraction(
            conn,
            articles,
            client,
            model=model,
            force=force,
            stop_on_error=stop_on_error,
        )


def command_init_db(args):
    with closing(wechat_db.connect(args.db)) as conn:
        wechat_db.initialize_db(conn)
        initialize_stock_tables(conn)
    print(f"股票抽取表已初始化: {Path(args.db).resolve()}")


def command_run(args):
    load_env_file(args.env_file)
    model = args.model or os.environ.get("LLM_MODEL", DEFAULT_MODEL)
    result = run_extraction(
        db_path=args.db,
        date=args.date,
        limit=args.limit,
        model=model,
        force=args.force,
        stop_on_error=args.stop_on_error,
    )
    print(
        "抽取完成: "
        f"{result['articles']} 篇文章, 成功 {result['succeeded']} 篇, "
        f"失败 {result['failed']} 篇, {result['mentions']} 条股票信息"
    )


def build_parser():
    parser = argparse.ArgumentParser(description="调用 GPT API 抽取文章中的股票信息并写入 SQLite。")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径")
    parser.add_argument("--env-file", default=".env", help="环境变量配置文件，默认 .env")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init-db", help="初始化股票抽取相关表")
    init_parser.set_defaults(func=command_init_db)

    run_parser = subparsers.add_parser("run", help="执行 GPT 股票抽取")
    run_parser.add_argument("--date", help="只抽取指定日期，例如 2026-05-07")
    run_parser.add_argument("--limit", type=int, help="最多抽取多少篇文章")
    run_parser.add_argument("--model", default=None)
    run_parser.add_argument("--force", action="store_true", help="即使已成功抽取也重新抽取")
    run_parser.add_argument("--stop-on-error", action="store_true", help="遇到单篇失败时立即停止")
    run_parser.set_defaults(func=command_run)
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
