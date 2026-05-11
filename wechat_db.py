#!/usr/bin/env python3
import argparse
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_INPUT = Path("./wechat_articles")
DEFAULT_DB = Path("./wechat_articles/articles.sqlite")

ARTICLE_FIELDS = [
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

ARTICLE_COLUMNS = [
    "account_name",
    *ARTICLE_FIELDS,
    "imported_at",
]


def connect(db_path):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def initialize_db(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_name TEXT NOT NULL,
            account_id TEXT,
            article_id TEXT,
            author TEXT,
            title TEXT NOT NULL,
            url TEXT NOT NULL UNIQUE,
            publish_time TEXT,
            collected_at TEXT,
            cover_url TEXT,
            digest TEXT,
            body_text TEXT NOT NULL,
            content_hash TEXT,
            source_type TEXT,
            imported_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_articles_account_name ON articles(account_name)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_articles_publish_time ON articles(publish_time)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_articles_article_id ON articles(article_id)"
    )
    conn.commit()


def iter_jsonl_files(input_path):
    input_path = Path(input_path)
    if input_path.is_file():
        yield input_path
        return
    for jsonl_path in sorted(input_path.rglob("articles.jsonl")):
        if any(part.startswith("_") for part in jsonl_path.parts):
            continue
        yield jsonl_path


def normalize_article(record, account_name):
    normalized = {field: record.get(field) for field in ARTICLE_FIELDS}
    normalized["title"] = normalized.get("title") or ""
    normalized["url"] = normalized.get("url") or ""
    normalized["body_text"] = normalized.get("body_text") or ""
    if not normalized["url"]:
        raise ValueError("article url is required")
    if not normalized["body_text"].strip():
        raise ValueError(f"body_text is empty for {normalized['url']}")
    return {
        "account_name": account_name,
        **normalized,
        "imported_at": datetime.now(timezone.utc).isoformat(),
    }


def upsert_article(conn, article):
    placeholders = ", ".join("?" for _ in ARTICLE_COLUMNS)
    update_columns = [column for column in ARTICLE_COLUMNS if column != "url"]
    update_clause = ", ".join(
        f"{column}=excluded.{column}" for column in update_columns
    )
    values = [article.get(column) for column in ARTICLE_COLUMNS]
    conn.execute(
        f"""
        INSERT INTO articles ({", ".join(ARTICLE_COLUMNS)})
        VALUES ({placeholders})
        ON CONFLICT(url) DO UPDATE SET {update_clause}
        """,
        values,
    )


def import_jsonl_file(conn, jsonl_path, account_name=None):
    jsonl_path = Path(jsonl_path)
    account_name = account_name or jsonl_path.parent.name
    imported = 0
    with jsonl_path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                article = normalize_article(record, account_name)
                upsert_article(conn, article)
                imported += 1
            except Exception as exc:
                raise RuntimeError(f"{jsonl_path}:{line_no}: {exc}") from exc
    conn.commit()
    return imported


def import_jsonl_directory(input_path=DEFAULT_INPUT, db_path=DEFAULT_DB):
    with closing(connect(db_path)) as conn:
        initialize_db(conn)
        total = 0
        for jsonl_path in iter_jsonl_files(input_path):
            total += import_jsonl_file(conn, jsonl_path)
        return total


def rows_to_dicts(cursor):
    return [dict(row) for row in cursor.fetchall()]


def list_articles(conn, limit=20):
    return rows_to_dicts(
        conn.execute(
            """
            SELECT id, account_name, title, publish_time, url
            FROM articles
            ORDER BY publish_time DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        )
    )


def search_articles(conn, keyword, limit=20):
    pattern = f"%{keyword}%"
    return rows_to_dicts(
        conn.execute(
            """
            SELECT id, account_name, title, publish_time, url
            FROM articles
            WHERE title LIKE ? OR digest LIKE ? OR body_text LIKE ?
            ORDER BY publish_time DESC, id DESC
            LIMIT ?
            """,
            (pattern, pattern, pattern, limit),
        )
    )


def get_article(conn, article_id):
    row = conn.execute(
        """
        SELECT *
        FROM articles
        WHERE id = ?
        """,
        (article_id,),
    ).fetchone()
    return dict(row) if row else None


def get_stats(conn):
    rows = rows_to_dicts(
        conn.execute(
            """
            SELECT account_name, COUNT(*) AS article_count,
                   MIN(publish_time) AS first_publish_time,
                   MAX(publish_time) AS last_publish_time
            FROM articles
            GROUP BY account_name
            ORDER BY account_name
            """
        )
    )
    total = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    return {"total": total, "accounts": rows}


def print_table(rows):
    for row in rows:
        print(
            f"{row['id']:>4}  {row['publish_time'] or '':<25} "
            f"{row['account_name']}  {row['title']}"
        )
        print(f"      {row['url']}")


def command_import(args):
    imported = import_jsonl_directory(args.input, args.db)
    print(f"已导入/更新 {imported} 条记录到 {Path(args.db).resolve()}")


def command_list(args):
    with closing(connect(args.db)) as conn:
        initialize_db(conn)
        print_table(list_articles(conn, args.limit))


def command_search(args):
    with closing(connect(args.db)) as conn:
        initialize_db(conn)
        print_table(search_articles(conn, args.keyword, args.limit))


def command_show(args):
    with closing(connect(args.db)) as conn:
        initialize_db(conn)
        article = get_article(conn, args.id)
    if not article:
        raise SystemExit(f"未找到 id={args.id} 的文章")
    print(f"ID: {article['id']}")
    print(f"公众号: {article['account_name']}")
    print(f"标题: {article['title']}")
    print(f"发布时间: {article['publish_time']}")
    print(f"链接: {article['url']}")
    print()
    print(article["body_text"])


def command_stats(args):
    with closing(connect(args.db)) as conn:
        initialize_db(conn)
        stats = get_stats(conn)
    print(f"总文章数: {stats['total']}")
    for account in stats["accounts"]:
        print(
            f"{account['account_name']}: {account['article_count']} 篇 "
            f"({account['first_publish_time']} 至 {account['last_publish_time']})"
        )


def build_parser():
    parser = argparse.ArgumentParser(
        description="把清洗后的微信公众号 articles.jsonl 导入 SQLite，并提供轻量查看命令。"
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB),
        help="SQLite 数据库路径，默认 ./wechat_articles/articles.sqlite",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser("import", help="导入 JSONL 到 SQLite")
    import_parser.add_argument(
        "--input",
        default=str(DEFAULT_INPUT),
        help="JSONL 文件或 wechat_articles 目录，默认 ./wechat_articles",
    )
    import_parser.set_defaults(func=command_import)

    list_parser = subparsers.add_parser("list", help="列出最新文章")
    list_parser.add_argument("--limit", type=int, default=20)
    list_parser.set_defaults(func=command_list)

    search_parser = subparsers.add_parser("search", help="按标题/摘要/正文搜索")
    search_parser.add_argument("keyword")
    search_parser.add_argument("--limit", type=int, default=20)
    search_parser.set_defaults(func=command_search)

    show_parser = subparsers.add_parser("show", help="查看指定文章全文")
    show_parser.add_argument("id", type=int)
    show_parser.set_defaults(func=command_show)

    stats_parser = subparsers.add_parser("stats", help="查看文章统计")
    stats_parser.set_defaults(func=command_stats)
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
