#!/usr/bin/env python3
import argparse
import sqlite3
from pathlib import Path


def table_columns(conn, table_name, include_id=False):
    columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table_name})")]
    if include_id:
        return columns
    return [column for column in columns if column != "id"]


def placeholders(columns):
    return ", ".join("?" for _ in columns)


def merge_new_data(target_db, source_db):
    target_db = Path(target_db)
    source_db = Path(source_db)
    conn = sqlite3.connect(target_db, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ? AS src", (str(source_db),))

    remote_max = conn.execute(
        "SELECT COALESCE(MAX(publish_time), '') FROM articles"
    ).fetchone()[0]
    article_cols = table_columns(conn, "articles")
    mention_cols = table_columns(conn, "stock_mentions")
    run_cols = table_columns(conn, "extraction_runs")

    source_articles = conn.execute(
        """
        SELECT *
        FROM src.articles
        WHERE publish_time > ?
        ORDER BY publish_time, id
        """,
        (remote_max,),
    ).fetchall()

    article_count = 0
    mention_count = 0
    run_count = 0
    for article in source_articles:
        updates = ", ".join(
            f"{column}=excluded.{column}" for column in article_cols if column != "url"
        )
        conn.execute(
            f"""
            INSERT INTO articles ({", ".join(article_cols)})
            VALUES ({placeholders(article_cols)})
            ON CONFLICT(url) DO UPDATE SET {updates}
            """,
            [article[column] for column in article_cols],
        )
        remote_id = conn.execute(
            "SELECT id FROM articles WHERE url = ?",
            (article["url"],),
        ).fetchone()[0]
        local_id = article["id"]

        conn.execute("DELETE FROM stock_mentions WHERE article_db_id = ?", (remote_id,))
        for mention in conn.execute(
            "SELECT * FROM src.stock_mentions WHERE article_db_id = ?",
            (local_id,),
        ).fetchall():
            row = {column: mention[column] for column in mention_cols}
            row["article_db_id"] = remote_id
            conn.execute(
                f"""
                INSERT INTO stock_mentions ({", ".join(mention_cols)})
                VALUES ({placeholders(mention_cols)})
                """,
                [row[column] for column in mention_cols],
            )
            mention_count += 1

        conn.execute("DELETE FROM extraction_runs WHERE article_db_id = ?", (remote_id,))
        for run in conn.execute(
            "SELECT * FROM src.extraction_runs WHERE article_db_id = ?",
            (local_id,),
        ).fetchall():
            row = {column: run[column] for column in run_cols}
            row["article_db_id"] = remote_id
            conn.execute(
                f"""
                INSERT INTO extraction_runs ({", ".join(run_cols)})
                VALUES ({placeholders(run_cols)})
                """,
                [row[column] for column in run_cols],
            )
            run_count += 1
        article_count += 1

    conn.commit()
    summary = {
        "remote_max_before": remote_max,
        "articles_merged": article_count,
        "mentions_merged": mention_count,
        "runs_merged": run_count,
    }
    print(summary)
    print("latest_articles")
    for row in conn.execute(
        """
        SELECT account_name, title, publish_time
        FROM articles
        ORDER BY publish_time DESC, id DESC
        LIMIT 5
        """
    ):
        print(dict(row))
    print("mention_dates")
    for row in conn.execute(
        """
        SELECT trade_date, COUNT(*) AS c
        FROM stock_mentions
        GROUP BY trade_date
        ORDER BY trade_date DESC
        LIMIT 5
        """
    ):
        print(dict(row))
    conn.close()
    return summary


def main():
    parser = argparse.ArgumentParser(
        description="Merge newer article and stock data from one SQLite DB into another."
    )
    parser.add_argument("target_db")
    parser.add_argument("source_db")
    args = parser.parse_args()
    merge_new_data(args.target_db, args.source_db)


if __name__ == "__main__":
    main()
