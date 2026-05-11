#!/usr/bin/env python3
import argparse
import base64
import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import time
from contextlib import closing
from datetime import datetime, timedelta
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import stock_extractor
import wechat_db


DEFAULT_DB = Path("./wechat_articles/articles.sqlite")
WEB_DIR = Path("./web")
SESSION_COOKIE = "stock_session"
SESSION_TTL_SECONDS = 7 * 24 * 60 * 60
PASSWORD_ITERATIONS = 240000
STOCK_WINDOWS = {7, 14, 30}
BULLISH_SQL = "(sm.mention_type LIKE '%推荐%' OR sm.mention_type LIKE '%重点%')"
BEARISH_SQL = "(sm.mention_type LIKE '%风险%')"
NEUTRAL_SQL = f"(NOT {BULLISH_SQL} AND NOT {BEARISH_SQL})"


def rows_to_dicts(cursor):
    return [dict(row) for row in cursor.fetchall()]


def utc_ts():
    return int(time.time())


def initialize_auth_tables(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS web_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin', 'user')),
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS web_sessions (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            FOREIGN KEY(user_id) REFERENCES web_users(id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_web_sessions_user_id ON web_sessions(user_id)"
    )
    conn.commit()


def password_hash(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    )
    return "pbkdf2_sha256${}${}${}".format(
        PASSWORD_ITERATIONS,
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def verify_password(password, stored_hash):
    try:
        scheme, iterations, salt_b64, digest_b64 = stored_hash.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            base64.urlsafe_b64decode(salt_b64.encode("ascii")),
            int(iterations),
        )
        expected = base64.urlsafe_b64decode(digest_b64.encode("ascii"))
        return hmac.compare_digest(digest, expected)
    except Exception:
        return False


def public_user(row):
    if not row:
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "is_active": bool(row["is_active"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def create_user(conn, username, password, role="user"):
    initialize_auth_tables(conn)
    username = (username or "").strip()
    password = password or ""
    if not username:
        raise ValueError("username is required")
    if len(password) < 6:
        raise ValueError("password must be at least 6 characters")
    if role not in {"admin", "user"}:
        raise ValueError("invalid role")
    now = utc_ts()
    try:
        cursor = conn.execute(
            """
            INSERT INTO web_users (
                username, password_hash, role, is_active, created_at, updated_at
            )
            VALUES (?, ?, ?, 1, ?, ?)
            """,
            (username, password_hash(password), role, now, now),
        )
        conn.commit()
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            raise ValueError("username already exists") from exc
        raise
    return get_user_by_id(conn, cursor.lastrowid)


def get_user_by_id(conn, user_id):
    row = conn.execute(
        """
        SELECT id, username, role, is_active, created_at, updated_at
        FROM web_users
        WHERE id = ?
        """,
        (user_id,),
    ).fetchone()
    return public_user(row)


def list_users(conn):
    initialize_auth_tables(conn)
    rows = conn.execute(
        """
        SELECT id, username, role, is_active, created_at, updated_at
        FROM web_users
        ORDER BY role, username
        """
    ).fetchall()
    return [public_user(row) for row in rows]


def authenticate_user(conn, username, password):
    initialize_auth_tables(conn)
    row = conn.execute(
        """
        SELECT *
        FROM web_users
        WHERE username = ? AND is_active = 1
        """,
        ((username or "").strip(),),
    ).fetchone()
    if not row or not verify_password(password or "", row["password_hash"]):
        return None
    return public_user(row)


def session_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(conn, user_id):
    initialize_auth_tables(conn)
    token = secrets.token_urlsafe(32)
    now = utc_ts()
    conn.execute(
        """
        INSERT INTO web_sessions (token_hash, user_id, created_at, expires_at)
        VALUES (?, ?, ?, ?)
        """,
        (session_hash(token), user_id, now, now + SESSION_TTL_SECONDS),
    )
    conn.commit()
    return token


def user_from_session(conn, token):
    if not token:
        return None
    initialize_auth_tables(conn)
    row = conn.execute(
        """
        SELECT u.id, u.username, u.role, u.is_active, u.created_at, u.updated_at
        FROM web_sessions s
        JOIN web_users u ON u.id = s.user_id
        WHERE s.token_hash = ? AND s.expires_at > ? AND u.is_active = 1
        """,
        (session_hash(token), utc_ts()),
    ).fetchone()
    return public_user(row)


def delete_session(conn, token):
    if not token:
        return
    initialize_auth_tables(conn)
    conn.execute("DELETE FROM web_sessions WHERE token_hash = ?", (session_hash(token),))
    conn.commit()


def ensure_default_admin(conn):
    initialize_auth_tables(conn)
    count = conn.execute("SELECT COUNT(*) FROM web_users").fetchone()[0]
    if count:
        return None
    username = os.environ.get("STOCK_WEB_ADMIN_USER", "admin")
    password = os.environ.get("STOCK_WEB_ADMIN_PASSWORD")
    if not password:
        password = secrets.token_urlsafe(18)
        print(
            f"Created default admin user '{username}' with generated password: {password}",
            flush=True,
        )
        print(
            "Set STOCK_WEB_ADMIN_PASSWORD before first start to choose a stable password.",
            flush=True,
        )
    return create_user(conn, username, password, role="admin")


def get_dates(conn):
    rows = conn.execute(
        """
        SELECT DISTINCT trade_date
        FROM stock_mentions
        WHERE trade_date IS NOT NULL AND trade_date != ''
        ORDER BY trade_date DESC
        """
    ).fetchall()
    return [row[0] for row in rows]


def get_channels(conn):
    rows = conn.execute(
        """
        SELECT DISTINCT channel
        FROM stock_mentions
        WHERE channel IS NOT NULL AND channel != ''
        ORDER BY channel
        """
    ).fetchall()
    return [row[0] for row in rows]


def selected_date_or_latest(conn, date):
    if date:
        return date
    dates = get_dates(conn)
    return dates[0] if dates else ""


def channel_filter_sql(channel, params):
    if not channel:
        return ""
    params.append(channel)
    return " AND sm.channel = ?"


def normalize_stock_window(value):
    try:
        window = int(value)
    except (TypeError, ValueError):
        return 7
    return window if window in STOCK_WINDOWS else 7


def stock_window_bounds(end_date, window_days):
    if not end_date:
        return "", ""
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    start = end - timedelta(days=window_days - 1)
    return start.isoformat(), end.isoformat()


def stock_keyword_filter_sql(keyword, params):
    keyword = (keyword or "").strip()
    if not keyword:
        return ""
    pattern = f"%{keyword}%"
    params.extend([pattern, pattern])
    return " AND (sm.stock_name LIKE ? OR sm.stock_code LIKE ?)"


def get_summary(conn, date, channel=None):
    params = [date]
    extra = channel_filter_sql(channel, params)
    row = conn.execute(
        f"""
        SELECT
            COUNT(DISTINCT sm.article_db_id) AS article_count,
            COUNT(*) AS mention_count,
            COUNT(DISTINCT sm.stock_name) AS stock_count,
            COUNT(DISTINCT sm.theme) AS theme_count
        FROM stock_mentions sm
        WHERE sm.trade_date = ? {extra}
        """,
        params,
    ).fetchone()
    return {
        "article_count": row["article_count"] or 0,
        "mention_count": row["mention_count"] or 0,
        "stock_count": row["stock_count"] or 0,
        "theme_count": row["theme_count"] or 0,
    }


def get_stock_groups(conn, date, channel=None):
    params = [date]
    extra = channel_filter_sql(channel, params)
    return rows_to_dicts(
        conn.execute(
            f"""
            SELECT
                sm.stock_name,
                COALESCE(NULLIF(sm.stock_code, ''), '') AS stock_code,
                COALESCE(NULLIF(sm.market, ''), '未知') AS market,
                COUNT(*) AS mention_count,
                ROUND(MAX(COALESCE(sm.confidence, 0)), 3) AS max_confidence,
                GROUP_CONCAT(DISTINCT sm.theme) AS themes,
                GROUP_CONCAT(DISTINCT sm.mention_type) AS mention_types,
                GROUP_CONCAT(DISTINCT sm.channel) AS channels,
                MAX(sm.evidence) AS evidence
            FROM stock_mentions sm
            WHERE sm.trade_date = ? {extra}
            GROUP BY sm.stock_name, sm.stock_code, sm.market
            ORDER BY mention_count DESC, max_confidence DESC, sm.stock_name
            """,
            params,
        )
    )


def get_theme_groups(conn, date, channel=None):
    params = [date]
    extra = channel_filter_sql(channel, params)
    return rows_to_dicts(
        conn.execute(
            f"""
            SELECT
                COALESCE(NULLIF(sm.theme, ''), '未分类') AS theme,
                COUNT(*) AS mention_count,
                COUNT(DISTINCT sm.stock_name) AS stock_count
            FROM stock_mentions sm
            WHERE sm.trade_date = ? {extra}
            GROUP BY COALESCE(NULLIF(sm.theme, ''), '未分类')
            ORDER BY mention_count DESC, stock_count DESC, theme
            LIMIT 20
            """,
            params,
        )
    )


def get_mentions(conn, date, channel=None):
    params = [date]
    extra = channel_filter_sql(channel, params)
    return rows_to_dicts(
        conn.execute(
            f"""
            SELECT
                sm.id,
                sm.stock_name,
                sm.stock_code,
                sm.market,
                sm.theme,
                sm.reason,
                sm.evidence,
                sm.mention_type,
                ROUND(COALESCE(sm.confidence, 0), 3) AS confidence,
                sm.channel,
                a.id AS article_id,
                a.title AS article_title,
                a.publish_time,
                a.url
            FROM stock_mentions sm
            JOIN articles a ON a.id = sm.article_db_id
            WHERE sm.trade_date = ? {extra}
            ORDER BY a.publish_time DESC, sm.confidence DESC, sm.stock_name
            """,
            params,
        )
    )


def get_stock_view_summary(conn, start_date, end_date, channel=None, keyword=None):
    params = [start_date, end_date]
    extra = channel_filter_sql(channel, params)
    extra += stock_keyword_filter_sql(keyword, params)
    row = conn.execute(
        f"""
        SELECT
            COUNT(DISTINCT sm.stock_name) AS stock_count,
            COUNT(*) AS mention_count,
            COUNT(DISTINCT sm.trade_date) AS active_days,
            SUM(CASE WHEN {BULLISH_SQL} THEN 1 ELSE 0 END) AS bullish_count,
            SUM(CASE WHEN {BEARISH_SQL} THEN 1 ELSE 0 END) AS bearish_count,
            SUM(CASE WHEN {NEUTRAL_SQL} THEN 1 ELSE 0 END) AS neutral_count
        FROM stock_mentions sm
        WHERE sm.trade_date BETWEEN ? AND ? {extra}
        """,
        params,
    ).fetchone()
    return {
        "stock_count": row["stock_count"] or 0,
        "mention_count": row["mention_count"] or 0,
        "active_days": row["active_days"] or 0,
        "bullish_count": row["bullish_count"] or 0,
        "bearish_count": row["bearish_count"] or 0,
        "neutral_count": row["neutral_count"] or 0,
    }


def get_stock_view_groups(conn, start_date, end_date, channel=None, keyword=None):
    params = [start_date, end_date]
    extra = channel_filter_sql(channel, params)
    extra += stock_keyword_filter_sql(keyword, params)
    return rows_to_dicts(
        conn.execute(
            f"""
            SELECT
                sm.stock_name,
                COALESCE(NULLIF(sm.stock_code, ''), '') AS stock_code,
                COALESCE(NULLIF(sm.market, ''), '未知') AS market,
                COUNT(*) AS mention_count,
                COUNT(DISTINCT sm.trade_date) AS active_days,
                SUM(CASE WHEN {BULLISH_SQL} THEN 1 ELSE 0 END) AS bullish_count,
                SUM(CASE WHEN {BEARISH_SQL} THEN 1 ELSE 0 END) AS bearish_count,
                SUM(CASE WHEN {NEUTRAL_SQL} THEN 1 ELSE 0 END) AS neutral_count,
                ROUND(MAX(COALESCE(sm.confidence, 0)), 3) AS max_confidence,
                MIN(sm.trade_date) AS first_date,
                MAX(sm.trade_date) AS latest_date,
                GROUP_CONCAT(DISTINCT sm.theme) AS themes,
                GROUP_CONCAT(DISTINCT sm.mention_type) AS mention_types,
                GROUP_CONCAT(DISTINCT sm.channel) AS channels,
                MAX(sm.evidence) AS evidence
            FROM stock_mentions sm
            WHERE sm.trade_date BETWEEN ? AND ? {extra}
            GROUP BY sm.stock_name, sm.stock_code, sm.market
            ORDER BY mention_count DESC, active_days DESC, bullish_count DESC,
                     bearish_count DESC, max_confidence DESC, sm.stock_name
            LIMIT 120
            """,
            params,
        )
    )


def get_stock_view_mentions(conn, start_date, end_date, channel=None, keyword=None):
    params = [start_date, end_date]
    extra = channel_filter_sql(channel, params)
    extra += stock_keyword_filter_sql(keyword, params)
    return rows_to_dicts(
        conn.execute(
            f"""
            SELECT
                sm.id,
                sm.stock_name,
                sm.stock_code,
                sm.market,
                sm.theme,
                sm.reason,
                sm.evidence,
                sm.mention_type,
                CASE
                    WHEN {BULLISH_SQL} THEN 'bullish'
                    WHEN {BEARISH_SQL} THEN 'bearish'
                    ELSE 'neutral'
                END AS sentiment,
                ROUND(COALESCE(sm.confidence, 0), 3) AS confidence,
                sm.channel,
                sm.trade_date,
                a.id AS article_id,
                a.title AS article_title,
                a.publish_time,
                a.url
            FROM stock_mentions sm
            JOIN articles a ON a.id = sm.article_db_id
            WHERE sm.trade_date BETWEEN ? AND ? {extra}
            ORDER BY sm.trade_date DESC, sm.confidence DESC, sm.stock_name
            LIMIT 120
            """,
            params,
        )
    )


def get_stock_view(conn, end_date, channel=None, window_days=7, keyword=None):
    window_days = normalize_stock_window(window_days)
    start_date, end_date = stock_window_bounds(end_date, window_days)
    if not start_date:
        return {
            "window_days": window_days,
            "start_date": "",
            "end_date": "",
            "keyword": keyword or "",
            "summary": {
                "stock_count": 0,
                "mention_count": 0,
                "active_days": 0,
                "bullish_count": 0,
                "bearish_count": 0,
                "neutral_count": 0,
            },
            "stocks": [],
            "mentions": [],
        }
    return {
        "window_days": window_days,
        "start_date": start_date,
        "end_date": end_date,
        "keyword": keyword or "",
        "summary": get_stock_view_summary(conn, start_date, end_date, channel, keyword),
        "stocks": get_stock_view_groups(conn, start_date, end_date, channel, keyword),
        "mentions": get_stock_view_mentions(conn, start_date, end_date, channel, keyword),
    }


def get_dashboard(conn, date=None, channel=None, stock_window=7, stock_keyword=None):
    stock_extractor.initialize_stock_tables(conn)
    selected_date = selected_date_or_latest(conn, date)
    return {
        "selected_date": selected_date,
        "selected_channel": channel or "",
        "dates": get_dates(conn),
        "channels": get_channels(conn),
        "summary": get_summary(conn, selected_date, channel) if selected_date else {
            "article_count": 0,
            "mention_count": 0,
            "stock_count": 0,
            "theme_count": 0,
        },
        "stocks": get_stock_groups(conn, selected_date, channel) if selected_date else [],
        "themes": get_theme_groups(conn, selected_date, channel) if selected_date else [],
        "mentions": get_mentions(conn, selected_date, channel) if selected_date else [],
        "stock_view": get_stock_view(
            conn,
            selected_date,
            channel=channel,
            window_days=stock_window,
            keyword=stock_keyword,
        ),
    }


class StockDashboardHandler(BaseHTTPRequestHandler):
    db_path = DEFAULT_DB
    web_dir = WEB_DIR

    def log_message(self, format, *args):
        print(f"{self.address_string()} - {format % args}")

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/me":
            self.handle_me()
            return
        if parsed.path == "/api/users":
            self.handle_users_list()
            return
        if parsed.path == "/api/dashboard":
            self.handle_dashboard(parsed)
            return
        self.handle_static(parsed.path)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/login":
            self.handle_login()
            return
        if parsed.path == "/api/logout":
            self.handle_logout()
            return
        if parsed.path == "/api/users":
            self.handle_users_create()
            return
        self.send_json({"error": "not found"}, status=404)

    def init_db(self, conn):
        wechat_db.initialize_db(conn)
        stock_extractor.initialize_stock_tables(conn)
        initialize_auth_tables(conn)
        ensure_default_admin(conn)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        body = self.rfile.read(length).decode("utf-8")
        return json.loads(body or "{}")

    def session_token(self):
        raw_cookie = self.headers.get("Cookie", "")
        if not raw_cookie:
            return ""
        jar = cookies.SimpleCookie()
        try:
            jar.load(raw_cookie)
        except cookies.CookieError:
            return ""
        morsel = jar.get(SESSION_COOKIE)
        return morsel.value if morsel else ""

    def current_user(self, conn):
        return user_from_session(conn, self.session_token())

    def require_user(self, conn):
        user = self.current_user(conn)
        if not user:
            self.send_json({"error": "unauthorized"}, status=401)
            return None
        return user

    def require_admin(self, conn):
        user = self.require_user(conn)
        if not user:
            return None
        if user["role"] != "admin":
            self.send_json({"error": "forbidden"}, status=403)
            return None
        return user

    def handle_login(self):
        try:
            payload = self.read_json()
        except json.JSONDecodeError:
            self.send_json({"error": "invalid json"}, status=400)
            return
        with closing(wechat_db.connect(self.db_path)) as conn:
            self.init_db(conn)
            user = authenticate_user(
                conn,
                payload.get("username", ""),
                payload.get("password", ""),
            )
            if not user:
                self.send_json({"error": "invalid username or password"}, status=401)
                return
            token = create_session(conn, user["id"])
        self.send_json(
            {"user": user},
            headers=[self.session_cookie_header(token)],
        )

    def handle_logout(self):
        with closing(wechat_db.connect(self.db_path)) as conn:
            self.init_db(conn)
            delete_session(conn, self.session_token())
        self.send_json(
            {"ok": True},
            headers=[self.expired_session_cookie_header()],
        )

    def handle_me(self):
        with closing(wechat_db.connect(self.db_path)) as conn:
            self.init_db(conn)
            user = self.require_user(conn)
            if not user:
                return
        self.send_json({"user": user})

    def handle_users_list(self):
        with closing(wechat_db.connect(self.db_path)) as conn:
            self.init_db(conn)
            if not self.require_admin(conn):
                return
            payload = {"users": list_users(conn)}
        self.send_json(payload)

    def handle_users_create(self):
        try:
            payload = self.read_json()
        except json.JSONDecodeError:
            self.send_json({"error": "invalid json"}, status=400)
            return
        with closing(wechat_db.connect(self.db_path)) as conn:
            self.init_db(conn)
            if not self.require_admin(conn):
                return
            try:
                user = create_user(
                    conn,
                    payload.get("username", ""),
                    payload.get("password", ""),
                    role="user",
                )
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
        self.send_json({"user": user}, status=201)

    def handle_dashboard(self, parsed):
        query = parse_qs(parsed.query)
        date = first_query_value(query, "date")
        channel = first_query_value(query, "channel")
        stock_window = first_query_value(query, "stock_window")
        stock_keyword = first_query_value(query, "stock_keyword")
        with closing(wechat_db.connect(self.db_path)) as conn:
            self.init_db(conn)
            if not self.require_user(conn):
                return
            payload = get_dashboard(
                conn,
                date=date,
                channel=channel,
                stock_window=stock_window,
                stock_keyword=stock_keyword,
            )
        self.send_json(payload)

    def handle_static(self, path):
        if path in {"", "/"}:
            path = "/index.html"
        relative = path.lstrip("/")
        target = (self.web_dir / relative).resolve()
        web_root = self.web_dir.resolve()
        if not str(target).startswith(str(web_root)) or not target.exists() or target.is_dir():
            self.send_error(404)
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def session_cookie_header(self, token):
        return (
            "Set-Cookie",
            f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; "
            f"Max-Age={SESSION_TTL_SECONDS}",
        )

    def expired_session_cookie_header(self):
        return (
            "Set-Cookie",
            f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0",
        )

    def send_json(self, payload, status=200, headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in headers or []:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


def first_query_value(query, key):
    value = query.get(key, [""])[0].strip()
    return value or None


def run_server(host="127.0.0.1", port=8088, db_path=DEFAULT_DB, web_dir=WEB_DIR):
    handler = type(
        "ConfiguredStockDashboardHandler",
        (StockDashboardHandler,),
        {
            "db_path": Path(db_path),
            "web_dir": Path(web_dir),
        },
    )
    httpd = ThreadingHTTPServer((host, port), handler)
    print(f"股票汇总网页已启动: http://{host}:{port}")
    print(f"数据库: {Path(db_path).resolve()}")
    httpd.serve_forever()


def build_parser():
    parser = argparse.ArgumentParser(description="启动微信公众号股票汇总网页。")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--web-dir", default=str(WEB_DIR))
    return parser


def main():
    args = build_parser().parse_args()
    run_server(host=args.host, port=args.port, db_path=args.db, web_dir=args.web_dir)


if __name__ == "__main__":
    main()
