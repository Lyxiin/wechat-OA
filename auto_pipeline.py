#!/usr/bin/env python3
import argparse
import json
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import stock_extractor
import wechat_crawler
import wechat_db


DEFAULT_CONFIG = Path("./config/accounts.json")
DEFAULT_OUTPUT_DIR = Path("./wechat_articles")
DEFAULT_DB = Path("./wechat_articles/articles.sqlite")
DEFAULT_WEWE_RSS_URL = "http://localhost:4000"
DEFAULT_CRAWL_DAYS = 7
DEFAULT_LIMIT = 1000
CHINA_TZ = timezone(timedelta(hours=8))


class PipelineAccountError(RuntimeError):
    pass


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_config(config_path=DEFAULT_CONFIG):
    config_path = Path(config_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    accounts = [normalize_account(item) for item in data.get("accounts", [])]
    if not accounts:
        raise ValueError("config must contain at least one account")
    return {
        "config_path": str(config_path),
        "accounts": accounts,
        "default_output_dir": data.get("default_output_dir", str(DEFAULT_OUTPUT_DIR)),
        "db_path": data.get("db_path", str(DEFAULT_DB)),
        "wewe_rss_url": data.get("wewe_rss_url", DEFAULT_WEWE_RSS_URL).rstrip("/"),
        "llm_model": data.get("llm_model", stock_extractor.DEFAULT_MODEL),
    }


def normalize_account(item):
    if isinstance(item, str):
        item = {"name": item}
    if not isinstance(item, dict):
        raise ValueError("account item must be an object or string")
    name = str(item.get("name", "")).strip()
    if not name:
        raise ValueError("account name is required")
    return {
        "name": name,
        "enabled": bool(item.get("enabled", True)),
        "crawl_days": int(item.get("crawl_days", DEFAULT_CRAWL_DAYS)),
        "limit": int(item.get("limit", DEFAULT_LIMIT)),
        "seed_urls": list(item.get("seed_urls", [])),
        "extract": bool(item.get("extract", True)),
        "output_dir": item.get("output_dir"),
    }


def initialize_pipeline_tables(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pipeline_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            accounts_total INTEGER NOT NULL DEFAULT 0,
            accounts_succeeded INTEGER NOT NULL DEFAULT 0,
            accounts_failed INTEGER NOT NULL DEFAULT 0,
            articles_collected INTEGER NOT NULL DEFAULT 0,
            articles_imported INTEGER NOT NULL DEFAULT 0,
            articles_new INTEGER NOT NULL DEFAULT 0,
            extraction_articles INTEGER NOT NULL DEFAULT 0,
            extraction_succeeded INTEGER NOT NULL DEFAULT 0,
            extraction_failed INTEGER NOT NULL DEFAULT 0,
            mentions INTEGER NOT NULL DEFAULT 0,
            error_message TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crawl_state (
            account_name TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            last_run_at TEXT NOT NULL,
            last_success_at TEXT,
            last_article_time TEXT,
            articles_collected INTEGER NOT NULL DEFAULT 0,
            articles_imported INTEGER NOT NULL DEFAULT 0,
            articles_new INTEGER NOT NULL DEFAULT 0,
            mentions INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def initialize_all_tables(conn):
    wechat_db.initialize_db(conn)
    stock_extractor.initialize_stock_tables(conn)
    initialize_pipeline_tables(conn)


def create_pipeline_run(conn):
    now = utc_now_iso()
    cursor = conn.execute(
        """
        INSERT INTO pipeline_runs (started_at, status)
        VALUES (?, 'running')
        """,
        (now,),
    )
    conn.commit()
    return cursor.lastrowid


def finish_pipeline_run(conn, run_id, result):
    conn.execute(
        """
        UPDATE pipeline_runs
        SET finished_at = ?,
            status = ?,
            accounts_total = ?,
            accounts_succeeded = ?,
            accounts_failed = ?,
            articles_collected = ?,
            articles_imported = ?,
            articles_new = ?,
            extraction_articles = ?,
            extraction_succeeded = ?,
            extraction_failed = ?,
            mentions = ?,
            error_message = ?
        WHERE id = ?
        """,
        (
            utc_now_iso(),
            result["status"],
            result["accounts_total"],
            result["accounts_succeeded"],
            result["accounts_failed"],
            result["articles_collected"],
            result["articles_imported"],
            result["articles_new"],
            result["extraction_articles"],
            result["extraction_succeeded"],
            result["extraction_failed"],
            result["mentions"],
            result.get("error_message"),
            run_id,
        ),
    )
    conn.commit()


def upsert_crawl_state(conn, account_result):
    now = utc_now_iso()
    last_success_at = now if account_result["status"] == "success" else None
    conn.execute(
        """
        INSERT INTO crawl_state (
            account_name, status, last_run_at, last_success_at, last_article_time,
            articles_collected, articles_imported, articles_new, mentions,
            last_error, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(account_name) DO UPDATE SET
            status = excluded.status,
            last_run_at = excluded.last_run_at,
            last_success_at = COALESCE(excluded.last_success_at, crawl_state.last_success_at),
            last_article_time = COALESCE(excluded.last_article_time, crawl_state.last_article_time),
            articles_collected = excluded.articles_collected,
            articles_imported = excluded.articles_imported,
            articles_new = excluded.articles_new,
            mentions = excluded.mentions,
            last_error = excluded.last_error,
            updated_at = excluded.updated_at
        """,
        (
            account_result["account_name"],
            account_result["status"],
            now,
            last_success_at,
            account_result.get("last_article_time"),
            account_result["articles_collected"],
            account_result["articles_imported"],
            account_result["articles_new"],
            account_result["mentions"],
            account_result.get("error"),
            now,
        ),
    )
    conn.commit()


def account_window(account, today=None):
    today = today or datetime.now(CHINA_TZ).date()
    crawl_days = max(1, int(account.get("crawl_days") or DEFAULT_CRAWL_DAYS))
    start_day = today - timedelta(days=crawl_days - 1)
    return (
        datetime.combine(start_day, time(0, 0, 0)),
        datetime.combine(today, time(23, 59, 59)),
    )


def account_jsonl_path(config, account):
    base = Path(account.get("output_dir") or config["default_output_dir"])
    return base / account["name"] / "articles.jsonl"


def count_articles(conn):
    return conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]


def latest_article_time(conn, account_name):
    row = conn.execute(
        """
        SELECT MAX(publish_time)
        FROM articles
        WHERE account_name = ?
        """,
        (account_name,),
    ).fetchone()
    return row[0] if row and row[0] else None


def select_articles_for_extraction(conn, account_name, start_dt, end_dt, model, force=False):
    start_date = start_dt.date().isoformat()
    end_date = end_dt.date().isoformat()
    params = [account_name, start_date, end_date]
    filter_sql = ""
    if not force:
        filter_sql = """
            AND (
                er.article_db_id IS NULL
                OR er.status != 'success'
                OR er.content_hash IS NOT a.content_hash
                OR er.model != ?
            )
        """
        params.append(model)
    rows = conn.execute(
        f"""
        SELECT a.*
        FROM articles a
        LEFT JOIN extraction_runs er ON er.article_db_id = a.id
        WHERE a.account_name = ?
          AND substr(a.publish_time, 1, 10) BETWEEN ? AND ?
          {filter_sql}
        ORDER BY a.publish_time DESC, a.id DESC
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def default_extract_articles(conn, articles, model, force=False):
    if not articles:
        return {"articles": 0, "succeeded": 0, "failed": 0, "mentions": 0}
    client = stock_extractor.OpenAIStyleClient()
    return stock_extractor.run_articles_extraction(
        conn,
        articles,
        client,
        model=model,
        force=force,
        stop_on_error=False,
    )


def run_pipeline(
    config_path=DEFAULT_CONFIG,
    today=None,
    account_name=None,
    days=None,
    force=False,
    env_file=".env",
    crawler=wechat_crawler,
    extract_articles=default_extract_articles,
):
    config = load_config(config_path)
    if env_file:
        stock_extractor.load_env_file(env_file)
    if hasattr(crawler, "WEWE_RSS_URL"):
        crawler.WEWE_RSS_URL = config["wewe_rss_url"]

    accounts = [
        account
        for account in config["accounts"]
        if account["enabled"] and (not account_name or account["name"] == account_name)
    ]
    if days:
        accounts = [{**account, "crawl_days": int(days)} for account in accounts]

    result = empty_result(accounts_total=len(accounts))
    with closing(wechat_db.connect(config["db_path"])) as conn:
        initialize_all_tables(conn)
        run_id = create_pipeline_run(conn)
        try:
            for account in accounts:
                account_result = run_account(
                    conn,
                    config,
                    account,
                    today=today,
                    force=force,
                    crawler=crawler,
                    extract_articles=extract_articles,
                )
                merge_account_result(result, account_result)
            result["status"] = result_status(result)
        except Exception as exc:
            result["status"] = "failed"
            result["error_message"] = str(exc)
            raise
        finally:
            finish_pipeline_run(conn, run_id, result)
    return result


def empty_result(accounts_total=0):
    return {
        "status": "running",
        "accounts_total": accounts_total,
        "accounts_succeeded": 0,
        "accounts_failed": 0,
        "articles_collected": 0,
        "articles_imported": 0,
        "articles_new": 0,
        "extraction_articles": 0,
        "extraction_succeeded": 0,
        "extraction_failed": 0,
        "mentions": 0,
        "error_message": None,
    }


def result_status(result):
    if result["accounts_failed"] == 0:
        return "success"
    if result["accounts_succeeded"] > 0:
        return "partial_failed"
    return "failed"


def merge_account_result(result, account_result):
    if account_result["status"] == "success":
        result["accounts_succeeded"] += 1
    else:
        result["accounts_failed"] += 1
    for key in (
        "articles_collected",
        "articles_imported",
        "articles_new",
        "extraction_articles",
        "extraction_succeeded",
        "extraction_failed",
        "mentions",
    ):
        result[key] += account_result[key]


def empty_account_result(account_name):
    return {
        "account_name": account_name,
        "status": "running",
        "articles_collected": 0,
        "articles_imported": 0,
        "articles_new": 0,
        "extraction_articles": 0,
        "extraction_succeeded": 0,
        "extraction_failed": 0,
        "mentions": 0,
        "last_article_time": None,
        "error": None,
    }


def run_account(conn, config, account, today, force, crawler, extract_articles):
    account_result = empty_account_result(account["name"])
    start_dt, end_dt = account_window(account, today=today)
    try:
        feed_url = crawler.get_feed_url(
            account["name"],
            seed_urls=account.get("seed_urls") or [],
            limit=account.get("limit") or DEFAULT_LIMIT,
        )
        if not feed_url:
            raise PipelineAccountError("feed not found or login required")

        jsonl_path = account_jsonl_path(config, account)
        before_count = count_articles(conn)
        records = crawler.fetch_articles(
            feed_url,
            start_dt,
            end_dt,
            jsonl_path.parent,
            account_id=crawler.account_id_from_feed_url(feed_url, account["name"]),
            account_name=account["name"],
            jsonl_path=jsonl_path,
        )
        if not records:
            records = fetch_with_platform_fallback(crawler, account, start_dt, end_dt, jsonl_path)

        account_result["articles_collected"] = len(records)
        if jsonl_path.exists():
            account_result["articles_imported"] = wechat_db.import_jsonl_file(
                conn,
                jsonl_path,
                account_name=account["name"],
            )
        account_result["articles_new"] = max(0, count_articles(conn) - before_count)
        account_result["last_article_time"] = latest_article_time(conn, account["name"])

        extraction = {"articles": 0, "succeeded": 0, "failed": 0, "mentions": 0}
        if account.get("extract", True):
            articles = select_articles_for_extraction(
                conn,
                account["name"],
                start_dt,
                end_dt,
                config["llm_model"],
                force=force,
            )
            extraction = extract_articles(conn, articles, config["llm_model"], force=force)
        account_result["extraction_articles"] = extraction["articles"]
        account_result["extraction_succeeded"] = extraction["succeeded"]
        account_result["extraction_failed"] = extraction["failed"]
        account_result["mentions"] = extraction["mentions"]
        account_result["status"] = "success"
    except Exception as exc:
        account_result["status"] = "failed"
        account_result["error"] = str(exc)
    upsert_crawl_state(conn, account_result)
    return account_result


def fetch_with_platform_fallback(crawler, account, start_dt, end_dt, jsonl_path):
    if not all(hasattr(crawler, name) for name in ("find_feed", "fetch_platform_entries", "fetch_articles_from_entries")):
        return []
    feed = crawler.find_feed(account["name"])
    if not feed:
        return []
    entries = crawler.fetch_platform_entries(feed["id"], limit=account.get("limit") or DEFAULT_LIMIT)
    if not entries:
        return []
    return crawler.fetch_articles_from_entries(
        entries,
        start_dt,
        end_dt,
        jsonl_path.parent,
        account_id=feed["id"],
        account_name=account["name"],
        jsonl_path=jsonl_path,
    )


def get_latest_pipeline_run(conn):
    initialize_pipeline_tables(conn)
    row = conn.execute(
        """
        SELECT *
        FROM pipeline_runs
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    return dict(row) if row else None


def get_crawl_states(conn):
    initialize_pipeline_tables(conn)
    rows = conn.execute(
        """
        SELECT *
        FROM crawl_state
        ORDER BY account_name
        """
    ).fetchall()
    return [dict(row) for row in rows]


def command_init_db(args):
    config = load_config(args.config)
    with closing(wechat_db.connect(config["db_path"])) as conn:
        initialize_all_tables(conn)
    print(f"Initialized pipeline database: {Path(config['db_path']).resolve()}")


def command_run(args):
    result = run_pipeline(
        config_path=args.config,
        account_name=args.account,
        days=args.days,
        force=args.force,
        env_file=args.env_file,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def command_status(args):
    config = load_config(args.config)
    with closing(wechat_db.connect(config["db_path"])) as conn:
        initialize_all_tables(conn)
        payload = {
            "latest_run": get_latest_pipeline_run(conn),
            "accounts": get_crawl_states(conn),
        }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def build_parser():
    parser = argparse.ArgumentParser(description="Run the WeChat article automation pipeline.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--env-file", default=".env")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init-db", help="Create pipeline status tables.")
    init_parser.set_defaults(func=command_init_db)

    run_parser = subparsers.add_parser("run", help="Crawl, import, and extract configured accounts.")
    run_parser.add_argument("--account", help="Run only one account from the config.")
    run_parser.add_argument("--days", type=int, help="Override crawl_days for this run.")
    run_parser.add_argument("--force", action="store_true", help="Re-extract stocks even when unchanged.")
    run_parser.set_defaults(func=command_run)

    status_parser = subparsers.add_parser("status", help="Show latest run and account status.")
    status_parser.set_defaults(func=command_status)
    return parser


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
