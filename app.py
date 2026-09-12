import hashlib
import json
import os
import re
import sqlite3
import tempfile
from datetime import datetime

import pandas as pd
import psycopg
import streamlit as st
from psycopg.rows import dict_row


st.set_page_config(
    page_title="スロ屋データベース",
    page_icon="🎰",
    layout="wide",
)

DB_URL = st.secrets["DATABASE_URL"]


def get_conn():
    return psycopg.connect(DB_URL, connect_timeout=15)


def init_db():
    ddl = """
    CREATE TABLE IF NOT EXISTS slot_stores (
        store_id BIGSERIAL PRIMARY KEY,
        store_name TEXT NOT NULL UNIQUE
    );

    CREATE TABLE IF NOT EXISTS slot_daily_store_summary (
        date DATE NOT NULL,
        store_id BIGINT NOT NULL REFERENCES slot_stores(store_id) ON DELETE CASCADE,
        weekday TEXT,
        total_diff_medals INTEGER,
        avg_diff_medals INTEGER,
        avg_games INTEGER,
        win_rate NUMERIC,
        win_count INTEGER,
        machine_count INTEGER,
        source_url TEXT,
        PRIMARY KEY (date, store_id)
    );

    CREATE TABLE IF NOT EXISTS slot_machine_results (
        date DATE NOT NULL,
        store_id BIGINT NOT NULL REFERENCES slot_stores(store_id) ON DELETE CASCADE,
        machine_no INTEGER NOT NULL,
        machine_name TEXT,
        games INTEGER,
        diff_medals INTEGER,
        bb INTEGER,
        rb INTEGER,
        combined_rate NUMERIC,
        bb_rate NUMERIC,
        rb_rate NUMERIC,
        combined_rate_text TEXT,
        bb_rate_text TEXT,
        rb_rate_text TEXT,
        source_url TEXT,
        PRIMARY KEY (date, store_id, machine_no)
    );

    CREATE TABLE IF NOT EXISTS slot_import_log (
        import_id BIGSERIAL PRIMARY KEY,
        imported_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        target_date DATE,
        store_name TEXT,
        source_url TEXT,
        source_title TEXT,
        exported_at TEXT,
        source_table_count INTEGER,
        imported_machine_rows INTEGER,
        source_sha256 TEXT UNIQUE
    );

    CREATE INDEX IF NOT EXISTS idx_slot_machine_results_store_date
        ON slot_machine_results(store_id, date);

    CREATE INDEX IF NOT EXISTS idx_slot_machine_results_machine_name
        ON slot_machine_results(machine_name);

    CREATE INDEX IF NOT EXISTS idx_slot_machine_results_machine_no
        ON slot_machine_results(machine_no);
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)
        conn.commit()


@st.cache_data(ttl=30)
def query_df(sql, params=()):
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    return pd.DataFrame(rows)


def clear_cache():
    st.cache_data.clear()


def get_or_create_store_id(cur, store_name):
    cur.execute(
        """
        INSERT INTO slot_stores(store_name)
        VALUES (%s)
        ON CONFLICT(store_name) DO UPDATE SET store_name = EXCLUDED.store_name
        RETURNING store_id
        """,
        (store_name,),
    )
    return cur.fetchone()[0]


def clean_int(value):
    if value is None:
        return None
    s = str(value).strip().replace(",", "").replace("+", "")
    if s in ("", "-", "None", "nan"):
        return None
    try:
        return int(float(s))
    except Exception:
        return None


def rate_num(value):
    s = str(value or "").strip()
    if s.startswith("1/"):
        try:
            return float(s[2:])
        except Exception:
            return None
    return None


def parse_page_title(raw):
    title = raw.get("page_title", "")
    m = re.match(r"(\d{4})/(\d{2})/(\d{2})\s+(.+?)\s+データまとめ", title)
    if not m:
        raise ValueError(f"ページタイトルから日付・店舗名を判定できません: {title}")
    date_str = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    store_name = m.group(4)
    return date_str, store_name


def weekday_jp(date_str):
    return ["月", "火", "水", "木", "金", "土", "日"][
        datetime.strptime(date_str, "%Y-%m-%d").weekday()
    ]


def parse_anaslo_json(raw):
    date_str, store_name = parse_page_title(raw)
    source_url = raw.get("page_url", "")

    all_table = next(
        (t for t in raw.get("tables", []) if t.get("id") == "all_data_table"),
        None,
    )
    if not all_table:
        raise ValueError('id="all_data_table" が見つかりません。')

    machine_rows = []
    for r in all_table.get("rows", [])[1:]:
        if len(r) < 9:
            continue
        machine_no = clean_int(r[1])
        if machine_no is None:
            continue
        machine_rows.append(
            {
                "machine_name": r[0],
                "machine_no": machine_no,
                "games": clean_int(r[2]),
                "diff_medals": clean_int(r[3]),
                "bb": clean_int(r[4]),
                "rb": clean_int(r[5]),
                "combined_rate": rate_num(r[6]),
                "bb_rate": rate_num(r[7]),
                "rb_rate": rate_num(r[8]),
                "combined_rate_text": str(r[6]),
                "bb_rate_text": str(r[7]),
                "rb_rate_text": str(r[8]),
            }
        )

    summary_table = next(
        (
            t
            for t in raw.get("tables", [])
            if "total_get_medals_table" in str(t.get("class") or "")
        ),
        None,
    )

    if summary_table and len(summary_table.get("rows", [])) >= 2:
        vals = summary_table["rows"][1]
        total_diff = clean_int(vals[0])
        avg_diff = clean_int(vals[1])
        avg_games = clean_int(vals[2])
        m = re.search(r"([\d.]+)%\((\d+)/(\d+)\)", str(vals[3]))
        win_rate = float(m.group(1)) if m else None
        win_count = int(m.group(2)) if m else None
        machine_count = int(m.group(3)) if m else len(machine_rows)
    else:
        machine_count = len(machine_rows)
        total_diff = sum((r["diff_medals"] or 0) for r in machine_rows)
        avg_diff = round(total_diff / machine_count) if machine_count else None
        avg_games = (
            round(sum((r["games"] or 0) for r in machine_rows) / machine_count)
            if machine_count
            else None
        )
        win_count = sum(1 for r in machine_rows if (r["diff_medals"] or 0) > 0)
        win_rate = round(win_count / machine_count * 100, 1) if machine_count else None

    calc_sum = sum((r["diff_medals"] or 0) for r in machine_rows)

    return {
        "date": date_str,
        "store_name": store_name,
        "weekday": weekday_jp(date_str),
        "total_diff_medals": total_diff,
        "avg_diff_medals": avg_diff,
        "avg_games": avg_games,
        "win_rate": win_rate,
        "win_count": win_count,
        "machine_count": machine_count,
        "source_url": source_url,
        "source_title": raw.get("page_title", ""),
        "exported_at": raw.get("exported_at", ""),
        "source_table_count": raw.get("table_count"),
        "machine_rows": machine_rows,
        "calc_sum": calc_sum,
    }


def import_parsed_json(parsed, source_sha256):
    with get_conn() as conn:
        with conn.cursor() as cur:
            store_id = get_or_create_store_id(cur, parsed["store_name"])

            cur.execute(
                """
                INSERT INTO slot_daily_store_summary
                (date, store_id, weekday, total_diff_medals, avg_diff_medals,
                 avg_games, win_rate, win_count, machine_count, source_url)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(date, store_id) DO UPDATE SET
                    weekday = EXCLUDED.weekday,
                    total_diff_medals = EXCLUDED.total_diff_medals,
                    avg_diff_medals = EXCLUDED.avg_diff_medals,
                    avg_games = EXCLUDED.avg_games,
                    win_rate = EXCLUDED.win_rate,
                    win_count = EXCLUDED.win_count,
                    machine_count = EXCLUDED.machine_count,
                    source_url = EXCLUDED.source_url
                """,
                (
                    parsed["date"],
                    store_id,
                    parsed["weekday"],
                    parsed["total_diff_medals"],
                    parsed["avg_diff_medals"],
                    parsed["avg_games"],
                    parsed["win_rate"],
                    parsed["win_count"],
                    parsed["machine_count"],
                    parsed["source_url"],
                ),
            )

            machine_sql = """
                INSERT INTO slot_machine_results
                (date, store_id, machine_no, machine_name, games, diff_medals,
                 bb, rb, combined_rate, bb_rate, rb_rate, combined_rate_text,
                 bb_rate_text, rb_rate_text, source_url)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(date, store_id, machine_no) DO UPDATE SET
                    machine_name = EXCLUDED.machine_name,
                    games = EXCLUDED.games,
                    diff_medals = EXCLUDED.diff_medals,
                    bb = EXCLUDED.bb,
                    rb = EXCLUDED.rb,
                    combined_rate = EXCLUDED.combined_rate,
                    bb_rate = EXCLUDED.bb_rate,
                    rb_rate = EXCLUDED.rb_rate,
                    combined_rate_text = EXCLUDED.combined_rate_text,
                    bb_rate_text = EXCLUDED.bb_rate_text,
                    rb_rate_text = EXCLUDED.rb_rate_text,
                    source_url = EXCLUDED.source_url
            """
            params = []
            for r in parsed["machine_rows"]:
                params.append(
                    (
                        parsed["date"],
                        store_id,
                        r["machine_no"],
                        r["machine_name"],
                        r["games"],
                        r["diff_medals"],
                        r["bb"],
                        r["rb"],
                        r["combined_rate"],
                        r["bb_rate"],
                        r["rb_rate"],
                        r["combined_rate_text"],
                        r["bb_rate_text"],
                        r["rb_rate_text"],
                        parsed["source_url"],
                    )
                )
            cur.executemany(machine_sql, params)

            cur.execute(
                """
                INSERT INTO slot_import_log
                (target_date, store_name, source_url, source_title, exported_at,
                 source_table_count, imported_machine_rows, source_sha256)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(source_sha256) DO NOTHING
                """,
                (
                    parsed["date"],
                    parsed["store_name"],
                    parsed["source_url"],
                    parsed["source_title"],
                    parsed["exported_at"],
                    parsed["source_table_count"],
                    len(parsed["machine_rows"]),
                    source_sha256,
                ),
            )
        conn.commit()


def admin_gate():
    secret = st.secrets.get("ADMIN_PASSWORD", "")
    if not secret:
        st.warning(
            '書き込み機能を使うには Streamlit Secrets に '
            'ADMIN_PASSWORD = "任意の管理パスワード" を追加してください。'
        )
        return False

    if st.session_state.get("admin_ok"):
        return True

    st.info("この画面はデータ登録用です。管理パスワードを入力してください。")
    pwd = st.text_input("管理パスワード", type="password")
    if st.button("ログイン", type="primary"):
        if pwd == secret:
            st.session_state["admin_ok"] = True
            st.rerun()
        else:
            st.error("管理パスワードが違います。")
    return False


def batch_executemany(cur, sql, rows, batch_size=1000):
    total = len(rows)
    for i in range(0, total, batch_size):
        cur.executemany(sql, rows[i : i + batch_size])


def import_sqlite_bytes(file_bytes):
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite") as tf:
            tf.write(file_bytes)
            temp_path = tf.name

        src = sqlite3.connect(temp_path)
        src.row_factory = sqlite3.Row

        tables = {
            r[0]
            for r in src.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        required = {"stores", "daily_store_summary", "machine_results"}
        missing = required - tables
        if missing:
            raise ValueError(
                "SQLiteに必要なテーブルがありません: " + ", ".join(sorted(missing))
            )

        src_stores = {
            int(r["store_id"]): r["store_name"]
            for r in src.execute("SELECT store_id, store_name FROM stores").fetchall()
        }

        daily_rows = src.execute(
            """
            SELECT date, store_id, weekday, total_diff_medals, avg_diff_medals,
                   avg_games, win_rate, win_count, machine_count, source_url
            FROM daily_store_summary
            ORDER BY date
            """
        ).fetchall()

        machine_rows = src.execute(
            """
            SELECT date, store_id, machine_no, machine_name, games, diff_medals,
                   bb, rb, combined_rate, bb_rate, rb_rate, combined_rate_text,
                   bb_rate_text, rb_rate_text, source_url
            FROM machine_results
            ORDER BY date, machine_no
            """
        ).fetchall()

        with get_conn() as conn:
            with conn.cursor() as cur:
                neon_store_ids = {}
                for old_id, name in src_stores.items():
                    neon_store_ids[old_id] = get_or_create_store_id(cur, name)

                daily_params = [
                    (
                        r["date"],
                        neon_store_ids[int(r["store_id"])],
                        r["weekday"],
                        r["total_diff_medals"],
                        r["avg_diff_medals"],
                        r["avg_games"],
                        r["win_rate"],
                        r["win_count"],
                        r["machine_count"],
                        r["source_url"],
                    )
                    for r in daily_rows
                ]

                daily_sql = """
                    INSERT INTO slot_daily_store_summary
                    (date, store_id, weekday, total_diff_medals, avg_diff_medals,
                     avg_games, win_rate, win_count, machine_count, source_url)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(date, store_id) DO UPDATE SET
                        weekday = EXCLUDED.weekday,
                        total_diff_medals = EXCLUDED.total_diff_medals,
                        avg_diff_medals = EXCLUDED.avg_diff_medals,
                        avg_games = EXCLUDED.avg_games,
                        win_rate = EXCLUDED.win_rate,
                        win_count = EXCLUDED.win_count,
                        machine_count = EXCLUDED.machine_count,
                        source_url = EXCLUDED.source_url
                """
                batch_executemany(cur, daily_sql, daily_params, 500)

                machine_params = [
                    (
                        r["date"],
                        neon_store_ids[int(r["store_id"])],
                        r["machine_no"],
                        r["machine_name"],
                        r["games"],
                        r["diff_medals"],
                        r["bb"],
                        r["rb"],
                        r["combined_rate"],
                        r["bb_rate"],
                        r["rb_rate"],
                        r["combined_rate_text"],
                        r["bb_rate_text"],
                        r["rb_rate_text"],
                        r["source_url"],
                    )
                    for r in machine_rows
                ]

                machine_sql = """
                    INSERT INTO slot_machine_results
                    (date, store_id, machine_no, machine_name, games, diff_medals,
                     bb, rb, combined_rate, bb_rate, rb_rate, combined_rate_text,
                     bb_rate_text, rb_rate_text, source_url)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(date, store_id, machine_no) DO UPDATE SET
                        machine_name = EXCLUDED.machine_name,
                        games = EXCLUDED.games,
                        diff_medals = EXCLUDED.diff_medals,
                        bb = EXCLUDED.bb,
                        rb = EXCLUDED.rb,
                        combined_rate = EXCLUDED.combined_rate,
                        bb_rate = EXCLUDED.bb_rate,
                        rb_rate = EXCLUDED.rb_rate,
                        combined_rate_text = EXCLUDED.combined_rate_text,
                        bb_rate_text = EXCLUDED.bb_rate_text,
                        rb_rate_text = EXCLUDED.rb_rate_text,
                        source_url = EXCLUDED.source_url
                """
                batch_executemany(cur, machine_sql, machine_params, 750)

            conn.commit()

        src.close()
        return len(daily_rows), len(machine_rows)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


def store_selector():
    stores = query_df(
        "SELECT store_id, store_name FROM slot_stores ORDER BY store_name"
    )
    if stores.empty:
        return None, None
    store_name = st.selectbox("店舗", stores["store_name"].tolist())
    store_id = int(
        stores.loc[stores["store_name"] == store_name, "store_id"].iloc[0]
    )
    return store_id, store_name


def date_range_selector(store_id, key_prefix):
    bounds = query_df(
        """
        SELECT MIN(date) AS min_date, MAX(date) AS max_date
        FROM slot_daily_store_summary
        WHERE store_id = %s
        """,
        (store_id,),
    )
    if bounds.empty or pd.isna(bounds.iloc[0]["min_date"]):
        return None

    min_date = bounds.iloc[0]["min_date"]
    max_date = bounds.iloc[0]["max_date"]

    selected = st.date_input(
        "期間",
        value=(min_date, max_date),
        min_value=min_date,
        max_value=max_date,
        key=f"{key_prefix}_dates",
    )
    if isinstance(selected, (tuple, list)) and len(selected) == 2:
        return selected[0], selected[1]
    return min_date, max_date


init_db()

st.title("🎰 スロ屋データベース")

menu = st.sidebar.radio(
    "メニュー",
    [
        "ダッシュボード",
        "初期DB取込",
        "JSON追加",
        "日別集計",
        "機種別分析",
        "台番号別分析",
    ],
)

if menu == "ダッシュボード":
    st.subheader("ダッシュボード")

    store_id, store_name = store_selector()
    if store_id is None:
        st.info("まだデータがありません。「初期DB取込」または「JSON追加」から登録してください。")
        st.stop()

    summary = query_df(
        """
        SELECT
            MIN(date) AS start_date,
            MAX(date) AS end_date,
            COUNT(*) AS registered_days,
            SUM(total_diff_medals) AS total_diff,
            SUM(CASE WHEN total_diff_medals > 0 THEN 1 ELSE 0 END) AS plus_days,
            SUM(CASE WHEN total_diff_medals < 0 THEN 1 ELSE 0 END) AS minus_days
        FROM slot_daily_store_summary
        WHERE store_id = %s
        """,
        (store_id,),
    ).iloc[0]

    machine_count = query_df(
        """
        SELECT COUNT(*) AS cnt
        FROM slot_machine_results
        WHERE store_id = %s
        """,
        (store_id,),
    ).iloc[0]["cnt"]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("登録期間", f"{summary['start_date']} ～ {summary['end_date']}")
    c2.metric("登録日数", f"{int(summary['registered_days']):,}日")
    c3.metric("台別データ", f"{int(machine_count):,}件")
    c4.metric("期間総差枚", f"{int(summary['total_diff'] or 0):+,}枚")

    c5, c6 = st.columns(2)
    c5.metric("プラス日", f"{int(summary['plus_days'] or 0)}日")
    c6.metric("マイナス日", f"{int(summary['minus_days'] or 0)}日")

    daily = query_df(
        """
        SELECT date, total_diff_medals, avg_games, win_rate
        FROM slot_daily_store_summary
        WHERE store_id = %s
        ORDER BY date
        """,
        (store_id,),
    )
    if not daily.empty:
        daily["date"] = pd.to_datetime(daily["date"])
        st.subheader("日別 総差枚")
        st.bar_chart(
            daily.set_index("date")[["total_diff_medals"]],
            use_container_width=True,
        )
        st.dataframe(
            daily.sort_values("date", ascending=False),
            use_container_width=True,
            hide_index=True,
        )

elif menu == "初期DB取込":
    st.subheader("初期DB取込")
    st.write(
        "ChatGPTで作成したSQLiteデータベースを1回アップロードして、"
        "Neonへまとめて移行します。同じデータを再度入れても重複しません。"
    )

    if not admin_gate():
        st.stop()

    uploaded = st.file_uploader(
        "📂 ここにSQLiteファイルをドラッグ＆ドロップ",
        type=["sqlite", "db", "sqlite3"],
        accept_multiple_files=False,
        help="スロ屋データベースの .sqlite ファイルをこの枠の中へドラッグしてください。",
    )

    if uploaded is not None:
        st.info(f"選択中: {uploaded.name}")
        if st.button("Neonへ取り込む", type="primary"):
            with st.spinner("データを移行しています。少しお待ちください..."):
                try:
                    days, rows = import_sqlite_bytes(uploaded.getvalue())
                    clear_cache()
                    st.success(
                        f"取込完了：日別 {days:,}件 / 台別 {rows:,}件"
                    )
                except Exception as e:
                    st.error("取込に失敗しました。")
                    st.exception(e)

elif menu == "JSON追加":
    st.subheader("アナスロJSON追加")
    st.write(
        "「アナスロ保存」で取得したJSONを複数まとめて登録できます。"
        "日付 × 店舗 × 台番号で更新するため、同じ日を再登録しても二重計上しません。"
    )

    if not admin_gate():
        st.stop()

    files = st.file_uploader(
        "📂 ここにJSONファイルをまとめてドラッグ＆ドロップ",
        type=["json"],
        accept_multiple_files=True,
        help="複数のJSONファイルを一度にドラッグ＆ドロップできます。",
    )

    if files:
        previews = []
        parsed_files = []
        for f in files:
            try:
                file_bytes = f.getvalue()
                raw = json.loads(file_bytes.decode("utf-8"))
                parsed = parse_anaslo_json(raw)
                sha = hashlib.sha256(file_bytes).hexdigest()
                ok = (
                    len(parsed["machine_rows"]) == parsed["machine_count"]
                    and parsed["calc_sum"] == parsed["total_diff_medals"]
                )
                previews.append(
                    {
                        "ファイル": f.name,
                        "日付": parsed["date"],
                        "店舗": parsed["store_name"],
                        "台数": len(parsed["machine_rows"]),
                        "総差枚": parsed["total_diff_medals"],
                        "台別合計": parsed["calc_sum"],
                        "照合": "OK" if ok else "要確認",
                    }
                )
                parsed_files.append((parsed, sha))
            except Exception as e:
                previews.append(
                    {
                        "ファイル": f.name,
                        "日付": "",
                        "店舗": "",
                        "台数": "",
                        "総差枚": "",
                        "台別合計": "",
                        "照合": f"エラー: {e}",
                    }
                )

        st.dataframe(pd.DataFrame(previews), use_container_width=True, hide_index=True)

        if st.button("表示中のJSONを登録", type="primary"):
            success = 0
            failed = []
            with st.spinner("Neonへ登録しています..."):
                for parsed, sha in parsed_files:
                    try:
                        import_parsed_json(parsed, sha)
                        success += 1
                    except Exception as e:
                        failed.append(f"{parsed.get('date', '')}: {e}")
            clear_cache()

            if success:
                st.success(f"{success}ファイルを登録しました。")
            if failed:
                st.error("一部の登録に失敗しました。")
                for msg in failed:
                    st.write(msg)

elif menu == "日別集計":
    st.subheader("日別集計")
    store_id, store_name = store_selector()
    if store_id is None:
        st.info("データがありません。")
        st.stop()

    date_range = date_range_selector(store_id, "daily")
    if not date_range:
        st.stop()
    start_date, end_date = date_range

    df = query_df(
        """
        SELECT date, weekday, total_diff_medals, avg_diff_medals,
               avg_games, win_rate, win_count, machine_count
        FROM slot_daily_store_summary
        WHERE store_id = %s AND date BETWEEN %s AND %s
        ORDER BY date
        """,
        (store_id, start_date, end_date),
    )
    if df.empty:
        st.info("該当データがありません。")
    else:
        total = int(df["total_diff_medals"].fillna(0).sum())
        avg = round(df["total_diff_medals"].fillna(0).mean())
        c1, c2, c3 = st.columns(3)
        c1.metric("期間総差枚", f"{total:+,}枚")
        c2.metric("1日平均差枚", f"{avg:+,}枚")
        c3.metric("対象日数", f"{len(df)}日")

        chart_df = df.copy()
        chart_df["date"] = pd.to_datetime(chart_df["date"])
        st.bar_chart(
            chart_df.set_index("date")[["total_diff_medals"]],
            use_container_width=True,
        )
        st.dataframe(df, use_container_width=True, hide_index=True)

elif menu == "機種別分析":
    st.subheader("機種別分析")
    store_id, store_name = store_selector()
    if store_id is None:
        st.info("データがありません。")
        st.stop()

    date_range = date_range_selector(store_id, "machine")
    if not date_range:
        st.stop()
    start_date, end_date = date_range

    df = query_df(
        """
        SELECT
            machine_name,
            COUNT(*) AS records,
            COUNT(DISTINCT date) AS days,
            COUNT(DISTINCT machine_no) AS machines,
            SUM(diff_medals) AS total_diff_medals,
            ROUND(AVG(diff_medals), 1) AS avg_diff_medals,
            ROUND(AVG(games), 1) AS avg_games,
            SUM(CASE WHEN diff_medals > 0 THEN 1 ELSE 0 END) AS win_count,
            ROUND(
                100.0 * SUM(CASE WHEN diff_medals > 0 THEN 1 ELSE 0 END)
                / NULLIF(COUNT(*), 0),
                1
            ) AS win_rate
        FROM slot_machine_results
        WHERE store_id = %s AND date BETWEEN %s AND %s
        GROUP BY machine_name
        ORDER BY total_diff_medals DESC
        """,
        (store_id, start_date, end_date),
    )

    search = st.text_input("機種名検索")
    if search:
        df = df[df["machine_name"].astype(str).str.contains(search, case=False, na=False)]

    st.dataframe(df, use_container_width=True, hide_index=True)

elif menu == "台番号別分析":
    st.subheader("台番号別分析")
    store_id, store_name = store_selector()
    if store_id is None:
        st.info("データがありません。")
        st.stop()

    date_range = date_range_selector(store_id, "number")
    if not date_range:
        st.stop()
    start_date, end_date = date_range

    df = query_df(
        """
        SELECT
            machine_no,
            COUNT(*) AS days,
            MIN(machine_name) AS machine_name_example,
            SUM(diff_medals) AS total_diff_medals,
            ROUND(AVG(diff_medals), 1) AS avg_diff_medals,
            ROUND(AVG(games), 1) AS avg_games,
            SUM(CASE WHEN diff_medals > 0 THEN 1 ELSE 0 END) AS win_count,
            ROUND(
                100.0 * SUM(CASE WHEN diff_medals > 0 THEN 1 ELSE 0 END)
                / NULLIF(COUNT(*), 0),
                1
            ) AS win_rate
        FROM slot_machine_results
        WHERE store_id = %s AND date BETWEEN %s AND %s
        GROUP BY machine_no
        ORDER BY total_diff_medals DESC
        """,
        (store_id, start_date, end_date),
    )

    c1, c2 = st.columns(2)
    min_days = c1.number_input(
        "最低データ日数",
        min_value=1,
        max_value=max(1, len(pd.date_range(start_date, end_date))),
        value=1,
    )
    sort_choice = c2.selectbox(
        "並び順",
        ["総差枚が高い順", "平均差枚が高い順", "勝率が高い順", "台番号順"],
    )

    df = df[df["days"] >= min_days]

    if sort_choice == "総差枚が高い順":
        df = df.sort_values("total_diff_medals", ascending=False)
    elif sort_choice == "平均差枚が高い順":
        df = df.sort_values("avg_diff_medals", ascending=False)
    elif sort_choice == "勝率が高い順":
        df = df.sort_values("win_rate", ascending=False)
    else:
        df = df.sort_values("machine_no")

    st.dataframe(df, use_container_width=True, hide_index=True)
