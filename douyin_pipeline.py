#!/usr/bin/env python3
import argparse
import hashlib
import json
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import asr_client
import douyin_crawler
import stock_extractor
import wechat_db


DEFAULT_CONFIG = Path("./config/douyin_accounts.json")
DEFAULT_OUTPUT_DIR = Path("./douyin_data")
DEFAULT_DB = Path("./wechat_articles/articles.sqlite")
DEFAULT_CRAWL_DAYS = 3
DEFAULT_LIMIT = 5
CHINA_TZ = timezone(timedelta(hours=8))


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_config(config_path=DEFAULT_CONFIG):
    config_path = Path(config_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    accounts = [normalize_account(item) for item in data.get("accounts", [])]
    if not accounts:
        raise ValueError("config must contain at least one douyin account")
    return {
        "config_path": str(config_path),
        "accounts": accounts,
        "default_output_dir": data.get("default_output_dir", str(DEFAULT_OUTPUT_DIR)),
        "db_path": data.get("db_path", str(DEFAULT_DB)),
        "llm_model": data.get("llm_model", stock_extractor.DEFAULT_MODEL),
    }


def normalize_account(item):
    name = str(item.get("name", "")).strip()
    douyin_id = str(item.get("douyin_id", "")).strip()
    if not name:
        raise ValueError("douyin account name is required")
    if not douyin_id:
        raise ValueError(f"douyin_id is required for {name}")
    profile_url = str(item.get("profile_url", "")).strip()
    return {
        "name": name,
        "douyin_id": douyin_id,
        "profile_url": profile_url,
        "enabled": bool(item.get("enabled", True)),
        "crawl_days": int(item.get("crawl_days", DEFAULT_CRAWL_DAYS)),
        "limit": int(item.get("limit", DEFAULT_LIMIT)),
        "extract": bool(item.get("extract", True)),
        "cookies_file": item.get("cookies_file", ""),
        "setup_hint": "" if profile_url else douyin_crawler.profile_setup_hint(name, douyin_id),
    }


def account_output_dir(config, account):
    return Path(config["default_output_dir"]) / account["name"]


def articles_jsonl_path(config, account):
    return account_output_dir(config, account) / "articles.jsonl"


def transcript_path(output_dir, video):
    return Path(output_dir) / "transcripts" / f"{video['video_id']}.txt"


def account_since_date(account, today=None):
    today = today or datetime.now(CHINA_TZ).date()
    days = max(1, int(account.get("crawl_days") or DEFAULT_CRAWL_DAYS))
    return today - timedelta(days=days - 1)


def content_hash(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def video_to_article(account, video, transcript):
    publish_time = video.get("publish_time") or utc_now_iso()
    return {
        "account_id": account["douyin_id"],
        "article_id": video["video_id"],
        "author": video.get("author") or account["name"],
        "title": video.get("title") or f"{account['name']} 抖音视频",
        "url": video["url"],
        "publish_time": publish_time,
        "collected_at": utc_now_iso(),
        "cover_url": video.get("cover_url", ""),
        "digest": video.get("description", ""),
        "body_text": transcript,
        "content_hash": content_hash(transcript),
        "source_type": "douyin_video",
    }


def write_articles_jsonl(path, articles):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for article in articles:
            f.write(json.dumps(article, ensure_ascii=False) + "\n")
    return path


def transcribe_video(account, video, output_dir, crawler, asr):
    path = transcript_path(output_dir, video)
    if path.exists() and path.read_text(encoding="utf-8").strip():
        return path.read_text(encoding="utf-8").strip(), False

    audio_path = crawler.download_audio(video, account, output_dir)
    transcript = asr.transcribe(audio_path).strip()
    if not transcript:
        raise RuntimeError(f"empty transcript for {video['url']}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(transcript, encoding="utf-8")
    return transcript, True


def initialize_all_tables(conn):
    wechat_db.initialize_db(conn)
    stock_extractor.initialize_stock_tables(conn)


def select_articles_for_extraction(conn, account, since_date, model, force=False):
    params = [account["name"], since_date.isoformat()]
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
          AND substr(a.publish_time, 1, 10) >= ?
          AND a.source_type = 'douyin_video'
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


def empty_result(accounts_total=0):
    return {
        "status": "running",
        "accounts_total": accounts_total,
        "accounts_succeeded": 0,
        "accounts_failed": 0,
        "videos_collected": 0,
        "transcripts_created": 0,
        "articles_imported": 0,
        "articles_new": 0,
        "extraction_articles": 0,
        "extraction_succeeded": 0,
        "extraction_failed": 0,
        "mentions": 0,
        "errors": [],
    }


def merge_extract_result(result, extract_result):
    result["extraction_articles"] += extract_result["articles"]
    result["extraction_succeeded"] += extract_result["succeeded"]
    result["extraction_failed"] += extract_result["failed"]
    result["mentions"] += extract_result["mentions"]


def run_account(conn, config, account, today, crawler, asr, extract_articles, force=False):
    since_date = account_since_date(account, today=today)
    output_dir = account_output_dir(config, account)
    videos = crawler.fetch_recent_videos(account, since_date, account["limit"])
    asr = asr or asr_client.GroqASRClient()
    articles = []
    transcripts_created = 0
    for video in videos:
        transcript, created = transcribe_video(account, video, output_dir, crawler, asr)
        transcripts_created += int(created)
        articles.append(video_to_article(account, video, transcript))

    jsonl_path = write_articles_jsonl(articles_jsonl_path(config, account), articles)
    before = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    imported = wechat_db.import_jsonl_file(conn, jsonl_path, account_name=account["name"])
    after = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    target_articles = select_articles_for_extraction(
        conn, account, since_date, config["llm_model"], force=force
    )
    extract_result = (
        extract_articles(conn, target_articles, config["llm_model"], force=force)
        if account.get("extract", True)
        else {"articles": 0, "succeeded": 0, "failed": 0, "mentions": 0}
    )
    return {
        "status": "success",
        "videos_collected": len(videos),
        "transcripts_created": transcripts_created,
        "articles_imported": imported,
        "articles_new": max(0, after - before),
        "extract_result": extract_result,
    }


def result_status(result):
    if result["accounts_failed"] and result["accounts_succeeded"]:
        return "partial_failed"
    if result["accounts_failed"]:
        return "failed"
    return "success"


def run_pipeline(
    config_path=DEFAULT_CONFIG,
    today=None,
    account_name=None,
    days=None,
    force=False,
    env_file=".env",
    crawler=None,
    asr=None,
    extract_articles=default_extract_articles,
):
    if env_file:
        asr_client.load_env_file(env_file)
        stock_extractor.load_env_file(env_file)
    config = load_config(config_path)
    accounts = [
        account
        for account in config["accounts"]
        if account["enabled"] and (not account_name or account["name"] == account_name)
    ]
    if days:
        accounts = [{**account, "crawl_days": int(days)} for account in accounts]
    today = today or datetime.now(CHINA_TZ).date()
    crawler = crawler or douyin_crawler.DouyinCrawler()

    result = empty_result(accounts_total=len(accounts))
    with closing(wechat_db.connect(config["db_path"])) as conn:
        initialize_all_tables(conn)
        for account in accounts:
            try:
                account_result = run_account(
                    conn,
                    config,
                    account,
                    today=today,
                    crawler=crawler,
                    asr=asr,
                    extract_articles=extract_articles,
                    force=force,
                )
                result["accounts_succeeded"] += 1
                result["videos_collected"] += account_result["videos_collected"]
                result["transcripts_created"] += account_result["transcripts_created"]
                result["articles_imported"] += account_result["articles_imported"]
                result["articles_new"] += account_result["articles_new"]
                merge_extract_result(result, account_result["extract_result"])
            except Exception as exc:
                result["accounts_failed"] += 1
                result["errors"].append({"account_name": account["name"], "error": str(exc)})
        result["status"] = result_status(result)
    return result


def command_run(args):
    result = run_pipeline(
        config_path=args.config,
        account_name=args.account,
        days=args.days,
        force=args.force,
        env_file=args.env_file,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def command_init_db(args):
    with closing(wechat_db.connect(args.db)) as conn:
        initialize_all_tables(conn)
    print(f"Douyin/stock tables initialized: {Path(args.db).resolve()}")


def main():
    parser = argparse.ArgumentParser(description="Run the Douyin video stock analysis pipeline.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Crawl Douyin videos, transcribe, import, and extract stocks.")
    run_parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    run_parser.add_argument("--account", help="Run only one Douyin account from the config.")
    run_parser.add_argument("--days", type=int, help="Override crawl_days for this run.")
    run_parser.add_argument("--force", action="store_true", help="Re-run GPT extraction for matched transcripts.")
    run_parser.add_argument("--env-file", default=".env")
    run_parser.set_defaults(func=command_run)

    init_parser = subparsers.add_parser("init-db", help="Create required database tables.")
    init_parser.add_argument("--db", default=str(DEFAULT_DB))
    init_parser.set_defaults(func=command_init_db)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
