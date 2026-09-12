import hashlib
import json
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import psycopg
import streamlit as st
from psycopg.rows import dict_row
from pykakasi import kakasi


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


_kakasi = kakasi()


def kana_sort_key(value):
    """機種名を読み仮名ベースで並べるためのキーを返す。"""
    text = str(value or "")
    converted = _kakasi.convert(text)
    reading = "".join(
        item.get("hira") or item.get("kana") or item.get("orig") or ""
        for item in converted
    )
    return reading.casefold()


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


def rank_score(series, higher_is_better=True):
    """0～100の相対点へ変換。高い方が良いか、低い方が良いかを指定する。"""
    s = pd.to_numeric(series, errors="coerce")
    valid_count = int(s.notna().sum())

    if valid_count <= 1:
        return pd.Series([50.0] * len(s), index=s.index)

    ranks = s.rank(pct=True, method="average") * 100.0

    if higher_is_better:
        return ranks.fillna(50.0)

    reverse = 100.0 - ranks + (100.0 / valid_count)
    return reverse.clip(0, 100).fillna(50.0)


def exact_machine_windows(machine_nos, block_size):
    """台番号が完全な連番になっている窓だけを作る。"""
    nums = sorted(
        set(
            int(x)
            for x in machine_nos
            if pd.notna(x)
        )
    )

    windows = []

    for i in range(len(nums) - block_size + 1):
        window = nums[i : i + block_size]

        if all(
            window[j + 1] - window[j] == 1
            for j in range(len(window) - 1)
        ):
            windows.append(tuple(window))

    return windows


def prepare_strategy_cache(df, block_size=3):
    """狙い分析用の集計を一度だけ作る。"""
    data = df.copy()
    data["date"] = pd.to_datetime(data["date"])

    numeric_cols = [
        "machine_no",
        "games",
        "diff_medals",
        "bb",
        "rb",
        "combined_rate",
        "bb_rate",
        "rb_rate",
    ]

    for col in numeric_cols:
        if col in data.columns:
            data[col] = pd.to_numeric(data[col], errors="coerce")

    data = data.dropna(subset=["date", "machine_no"])
    data["machine_no"] = data["machine_no"].astype(int)
    data = data.sort_values(["date", "machine_no"]).reset_index(drop=True)

    machine_daily = (
        data.groupby(["date", "machine_name"])
        .agg(
            machine_count=("machine_no", "nunique"),
            avg_diff=("diff_medals", "mean"),
            avg_games=("games", "mean"),
            wins=("diff_medals", lambda s: (s > 0).sum()),
            rows=("machine_no", "size"),
        )
        .reset_index()
    )

    machine_daily["win_rate"] = (
        machine_daily["wins"]
        / machine_daily["rows"].replace(0, np.nan)
        * 100
    )

    suffix_source = data.copy()
    suffix_source["suffix"] = suffix_source["machine_no"] % 10

    suffix_daily = (
        suffix_source.groupby(["date", "suffix"])
        .agg(
            avg_diff=("diff_medals", "mean"),
            wins=("diff_medals", lambda s: (s > 0).sum()),
            rows=("machine_no", "size"),
        )
        .reset_index()
    )

    suffix_daily["win_rate"] = (
        suffix_daily["wins"]
        / suffix_daily["rows"].replace(0, np.nan)
        * 100
    )

    all_machine_nos = sorted(data["machine_no"].unique().tolist())
    windows = exact_machine_windows(all_machine_nos, block_size)

    diff_pivot = data.pivot_table(
        index="date",
        columns="machine_no",
        values="diff_medals",
        aggfunc="first",
    )

    block_frames = []

    for window in windows:
        if any(no not in diff_pivot.columns for no in window):
            continue

        block_values = diff_pivot[list(window)]
        valid = block_values.notna().all(axis=1)

        if not valid.any():
            continue

        block_values = block_values.loc[valid]

        block_frames.append(
            pd.DataFrame(
                {
                    "date": block_values.index,
                    "start_no": window[0],
                    "end_no": window[-1],
                    "block": f"{window[0]}～{window[-1]}",
                    "avg_diff": block_values.mean(axis=1).values,
                    "win_rate": (
                        block_values.gt(0).mean(axis=1) * 100
                    ).values,
                }
            )
        )

    if block_frames:
        block_daily = pd.concat(block_frames, ignore_index=True)
    else:
        block_daily = pd.DataFrame(
            columns=[
                "date",
                "start_no",
                "end_no",
                "block",
                "avg_diff",
                "win_rate",
            ]
        )

    sequence = data.sort_values(["machine_no", "date"]).copy()
    grouped = sequence.groupby("machine_no", group_keys=False)

    sequence["prior3_sum"] = grouped["diff_medals"].transform(
        lambda s: s.shift(1).rolling(3, min_periods=3).sum()
    )

    sequence["roll3_sum"] = grouped["diff_medals"].transform(
        lambda s: s.rolling(3, min_periods=3).sum()
    )

    sequence["roll5_sum"] = grouped["diff_medals"].transform(
        lambda s: s.rolling(5, min_periods=5).sum()
    )

    sequence["previous_result"] = grouped["diff_medals"].shift(1)

    return {
        "data": data,
        "machine_daily": machine_daily,
        "suffix_daily": suffix_daily,
        "block_daily": block_daily,
        "sequence": sequence,
        "windows": windows,
        "block_size": block_size,
    }


def strategy_active_info(cache, target_date):
    target_ts = pd.Timestamp(target_date)
    data = cache["data"]

    history = data[data["date"] < target_ts].copy()

    if history.empty:
        return history, None, pd.DataFrame()

    latest_date = history["date"].max()
    current = history[history["date"] == latest_date].copy()

    return history, latest_date, current


def score_all_machine_cached(
    cache,
    target_date,
    avg_threshold=500,
    win_threshold=60,
):
    """全台系候補を機種単位で採点する。"""
    _, _, current = strategy_active_info(cache, target_date)

    if current.empty:
        return pd.DataFrame()

    active_counts = current.groupby("machine_name")["machine_no"].nunique()
    active_names = active_counts[active_counts >= 2].index

    daily = cache["machine_daily"]
    daily = daily[
        (daily["date"] < pd.Timestamp(target_date))
        & (daily["machine_name"].isin(active_names))
    ].copy()

    if daily.empty:
        return pd.DataFrame()

    daily["strong"] = (
        (daily["machine_count"] >= 2)
        & (daily["avg_diff"] >= avg_threshold)
        & (daily["win_rate"] >= win_threshold)
    )

    daily["weekday_num"] = daily["date"].dt.weekday
    target_weekday = pd.Timestamp(target_date).weekday()

    summary = (
        daily.groupby("machine_name")
        .agg(
            observed_days=("date", "nunique"),
            strong_days=("strong", "sum"),
            strong_rate=("strong", "mean"),
            recent3_avg_diff=(
                "avg_diff",
                lambda s: s.tail(3).mean(),
            ),
        )
        .reset_index()
    )

    summary["strong_rate"] = summary["strong_rate"] * 100

    same_weekday = (
        daily[daily["weekday_num"] == target_weekday]
        .groupby("machine_name")["avg_diff"]
        .mean()
        .rename("same_weekday_avg_diff")
    )

    last_strong = (
        daily[daily["strong"]]
        .groupby("machine_name")["date"]
        .max()
        .rename("last_strong_date")
    )

    first_seen = daily.groupby("machine_name")["date"].min()

    summary = (
        summary.merge(
            same_weekday,
            on="machine_name",
            how="left",
        )
        .merge(
            last_strong,
            on="machine_name",
            how="left",
        )
    )

    summary["machine_count"] = (
        summary["machine_name"]
        .map(active_counts)
        .fillna(0)
        .astype(int)
    )

    def calc_days_since(row):
        if pd.notna(row["last_strong_date"]):
            return (
                pd.Timestamp(target_date)
                - row["last_strong_date"]
            ).days

        return (
            pd.Timestamp(target_date)
            - first_seen[row["machine_name"]]
        ).days

    summary["days_since_strong"] = summary.apply(
        calc_days_since,
        axis=1,
    )

    summary["_freq"] = rank_score(
        summary["strong_rate"],
        True,
    )
    summary["_weekday"] = rank_score(
        summary["same_weekday_avg_diff"],
        True,
    )
    summary["_overdue"] = rank_score(
        summary["days_since_strong"],
        True,
    )
    summary["_dip"] = rank_score(
        summary["recent3_avg_diff"],
        False,
    )

    summary["score"] = (
        0.35 * summary["_freq"]
        + 0.25 * summary["_weekday"]
        + 0.25 * summary["_overdue"]
        + 0.15 * summary["_dip"]
    ).round(1)

    return summary.sort_values(
        ["score", "strong_rate"],
        ascending=[False, False],
    ).reset_index(drop=True)


def score_suffix_cached(
    cache,
    target_date,
    avg_threshold=300,
    win_threshold=55,
):
    """台番号の末尾0～9を採点する。"""
    _, _, current = strategy_active_info(cache, target_date)

    if current.empty:
        return pd.DataFrame()

    current = current.copy()
    current["suffix"] = current["machine_no"] % 10

    active_counts = current.groupby("suffix")["machine_no"].nunique()

    daily = cache["suffix_daily"]
    daily = daily[
        daily["date"] < pd.Timestamp(target_date)
    ].copy()

    if daily.empty:
        return pd.DataFrame()

    daily["strong"] = (
        (daily["avg_diff"] >= avg_threshold)
        & (daily["win_rate"] >= win_threshold)
    )

    daily["weekday_num"] = daily["date"].dt.weekday
    target_weekday = pd.Timestamp(target_date).weekday()

    summary = (
        daily.groupby("suffix")
        .agg(
            strong_days=("strong", "sum"),
            strong_rate=("strong", "mean"),
            recent3_avg_diff=(
                "avg_diff",
                lambda s: s.tail(3).mean(),
            ),
        )
        .reset_index()
    )

    summary["strong_rate"] = summary["strong_rate"] * 100

    same_weekday = (
        daily[daily["weekday_num"] == target_weekday]
        .groupby("suffix")["avg_diff"]
        .mean()
        .rename("same_weekday_avg_diff")
    )

    last_strong = (
        daily[daily["strong"]]
        .groupby("suffix")["date"]
        .max()
        .rename("last_strong_date")
    )

    first_seen = daily.groupby("suffix")["date"].min()

    summary = (
        summary.merge(
            same_weekday,
            on="suffix",
            how="left",
        )
        .merge(
            last_strong,
            on="suffix",
            how="left",
        )
    )

    summary["machine_count"] = (
        summary["suffix"]
        .map(active_counts)
        .fillna(0)
        .astype(int)
    )

    def calc_days_since(row):
        if pd.notna(row["last_strong_date"]):
            return (
                pd.Timestamp(target_date)
                - row["last_strong_date"]
            ).days

        return (
            pd.Timestamp(target_date)
            - first_seen[row["suffix"]]
        ).days

    summary["days_since_strong"] = summary.apply(
        calc_days_since,
        axis=1,
    )

    summary["_freq"] = rank_score(
        summary["strong_rate"],
        True,
    )
    summary["_weekday"] = rank_score(
        summary["same_weekday_avg_diff"],
        True,
    )
    summary["_overdue"] = rank_score(
        summary["days_since_strong"],
        True,
    )
    summary["_dip"] = rank_score(
        summary["recent3_avg_diff"],
        False,
    )

    summary["score"] = (
        0.35 * summary["_freq"]
        + 0.25 * summary["_weekday"]
        + 0.20 * summary["_overdue"]
        + 0.20 * summary["_dip"]
    ).round(1)

    return summary.sort_values(
        "score",
        ascending=False,
    ).reset_index(drop=True)


def score_blocks_cached(
    cache,
    target_date,
    avg_threshold=500,
    win_threshold=66.7,
):
    """連番の並び候補を採点する。"""
    _, _, current = strategy_active_info(cache, target_date)

    if current.empty:
        return pd.DataFrame()

    active = set(current["machine_no"].astype(int).tolist())

    daily = cache["block_daily"]

    if daily.empty:
        return pd.DataFrame()

    daily = daily[
        daily["date"] < pd.Timestamp(target_date)
    ].copy()

    valid_pairs = set()

    for window in cache["windows"]:
        if all(no in active for no in window):
            valid_pairs.add((window[0], window[-1]))

    pair_mask = [
        (int(start), int(end)) in valid_pairs
        for start, end in zip(
            daily["start_no"],
            daily["end_no"],
        )
    ]

    daily = daily.loc[pair_mask].copy()

    if daily.empty:
        return pd.DataFrame()

    daily["strong"] = (
        (daily["avg_diff"] >= avg_threshold)
        & (daily["win_rate"] >= win_threshold)
    )

    daily["weekday_num"] = daily["date"].dt.weekday
    target_weekday = pd.Timestamp(target_date).weekday()

    keys = ["block", "start_no", "end_no"]

    summary = (
        daily.groupby(keys)
        .agg(
            observed_days=("date", "nunique"),
            strong_days=("strong", "sum"),
            strong_rate=("strong", "mean"),
            recent3_avg_diff=(
                "avg_diff",
                lambda s: s.tail(3).mean(),
            ),
        )
        .reset_index()
    )

    summary["strong_rate"] = summary["strong_rate"] * 100

    same_weekday = (
        daily[daily["weekday_num"] == target_weekday]
        .groupby(keys)["avg_diff"]
        .mean()
        .rename("same_weekday_avg_diff")
        .reset_index()
    )

    last_strong = (
        daily[daily["strong"]]
        .groupby(keys)["date"]
        .max()
        .rename("last_strong_date")
        .reset_index()
    )

    first_seen = (
        daily.groupby(keys)["date"]
        .min()
        .rename("first_seen_date")
        .reset_index()
    )

    summary = (
        summary.merge(
            same_weekday,
            on=keys,
            how="left",
        )
        .merge(
            last_strong,
            on=keys,
            how="left",
        )
        .merge(
            first_seen,
            on=keys,
            how="left",
        )
    )

    summary["days_since_strong"] = summary.apply(
        lambda row: (
            (
                pd.Timestamp(target_date)
                - row["last_strong_date"]
            ).days
            if pd.notna(row["last_strong_date"])
            else (
                pd.Timestamp(target_date)
                - row["first_seen_date"]
            ).days
        ),
        axis=1,
    )

    summary["machine_nos"] = summary.apply(
        lambda row: tuple(
            range(
                int(row["start_no"]),
                int(row["end_no"]) + 1,
            )
        ),
        axis=1,
    )

    summary["_freq"] = rank_score(
        summary["strong_rate"],
        True,
    )
    summary["_weekday"] = rank_score(
        summary["same_weekday_avg_diff"],
        True,
    )
    summary["_overdue"] = rank_score(
        summary["days_since_strong"],
        True,
    )
    summary["_dip"] = rank_score(
        summary["recent3_avg_diff"],
        False,
    )

    summary["score"] = (
        0.35 * summary["_freq"]
        + 0.20 * summary["_weekday"]
        + 0.25 * summary["_overdue"]
        + 0.20 * summary["_dip"]
    ).round(1)

    return summary.sort_values(
        "score",
        ascending=False,
    ).reset_index(drop=True)


def score_dip_cached(cache, target_date):
    """直近3日・5日の凹みと、過去の戻り方から台単位で採点する。"""
    _, latest_date, current = strategy_active_info(
        cache,
        target_date,
    )

    if current.empty:
        return pd.DataFrame()

    sequence = cache["sequence"]
    target_ts = pd.Timestamp(target_date)

    current_features = sequence[
        sequence["date"] == latest_date
    ][
        [
            "machine_no",
            "machine_name",
            "roll3_sum",
            "roll5_sum",
        ]
    ].copy()

    recovery_rows = sequence[
        (sequence["date"] < target_ts)
        & (sequence["prior3_sum"] < 0)
    ].copy()

    recovery = (
        recovery_rows.groupby("machine_no")
        .agg(
            recovery_samples=("date", "size"),
            recovery_rate=(
                "diff_medals",
                lambda s: (s > 0).mean() * 100,
            ),
            recovery_avg_diff=("diff_medals", "mean"),
        )
        .reset_index()
    )

    result = current_features.merge(
        recovery,
        on="machine_no",
        how="left",
    )

    result = result.rename(
        columns={
            "roll3_sum": "last3_sum",
            "roll5_sum": "last5_sum",
        }
    )

    result["recovery_samples"] = (
        result["recovery_samples"]
        .fillna(0)
        .astype(int)
    )

    result["_dip3"] = rank_score(
        result["last3_sum"],
        False,
    )
    result["_dip5"] = rank_score(
        result["last5_sum"],
        False,
    )
    result["_rate"] = rank_score(
        result["recovery_rate"],
        True,
    )
    result["_avg"] = rank_score(
        result["recovery_avg_diff"],
        True,
    )

    result["score"] = (
        0.40 * result["_dip3"]
        + 0.20 * result["_dip5"]
        + 0.25 * result["_rate"]
        + 0.15 * result["_avg"]
    ).round(1)

    result["is_dip"] = result["last3_sum"] < 0

    result.loc[
        ~result["is_dip"],
        "score",
    ] = (
        result.loc[
            ~result["is_dip"],
            "score",
        ]
        * 0.45
    ).round(1)

    return result.sort_values(
        "score",
        ascending=False,
    ).reset_index(drop=True)


def score_uphold_cached(cache, target_date):
    """前日マイナス後の上昇傾向、前日プラス後の継続傾向を採点する。"""
    _, _, current = strategy_active_info(
        cache,
        target_date,
    )

    if current.empty:
        return pd.DataFrame()

    sequence = cache["sequence"]
    target_ts = pd.Timestamp(target_date)

    current_previous = current[
        [
            "machine_no",
            "machine_name",
            "diff_medals",
        ]
    ].rename(
        columns={
            "diff_medals": "previous_diff",
        }
    )

    history_rows = sequence[
        sequence["date"] < target_ts
    ].copy()

    after_negative = (
        history_rows[
            history_rows["previous_result"] < 0
        ]
        .groupby("machine_no")
        .agg(
            neg_samples=("date", "size"),
            neg_rate=(
                "diff_medals",
                lambda s: (s > 0).mean() * 100,
            ),
            neg_avg=("diff_medals", "mean"),
        )
    )

    after_positive = (
        history_rows[
            history_rows["previous_result"] > 0
        ]
        .groupby("machine_no")
        .agg(
            pos_samples=("date", "size"),
            pos_rate=(
                "diff_medals",
                lambda s: (s > 0).mean() * 100,
            ),
            pos_avg=("diff_medals", "mean"),
        )
    )

    result = (
        current_previous.join(
            after_negative,
            on="machine_no",
        )
        .join(
            after_positive,
            on="machine_no",
        )
    )

    result["type"] = np.where(
        result["previous_diff"] < 0,
        "上げ候補",
        np.where(
            result["previous_diff"] > 0,
            "据え候補",
            "不明",
        ),
    )

    result["transition_samples"] = np.where(
        result["type"] == "上げ候補",
        result["neg_samples"],
        result["pos_samples"],
    )

    result["next_positive_rate"] = np.where(
        result["type"] == "上げ候補",
        result["neg_rate"],
        result["pos_rate"],
    )

    result["next_avg_diff"] = np.where(
        result["type"] == "上げ候補",
        result["neg_avg"],
        result["pos_avg"],
    )

    result["transition_samples"] = (
        pd.to_numeric(
            result["transition_samples"],
            errors="coerce",
        )
        .fillna(0)
        .astype(int)
    )

    result["_magnitude_raw"] = np.where(
        result["type"] == "上げ候補",
        -result["previous_diff"],
        result["previous_diff"],
    )

    result["_magnitude"] = rank_score(
        result["_magnitude_raw"],
        True,
    )
    result["_rate"] = rank_score(
        result["next_positive_rate"],
        True,
    )
    result["_avg"] = rank_score(
        result["next_avg_diff"],
        True,
    )

    result["score"] = (
        0.35 * result["_magnitude"]
        + 0.40 * result["_rate"]
        + 0.25 * result["_avg"]
    ).round(1)

    return result.sort_values(
        "score",
        ascending=False,
    ).reset_index(drop=True)


def score_rotation_cached(
    cache,
    target_date,
    avg_threshold=500,
    win_threshold=60,
):
    """機種が強く使われる間隔からローテーション候補を採点する。"""
    _, _, current = strategy_active_info(
        cache,
        target_date,
    )

    if current.empty:
        return pd.DataFrame()

    active_counts = current.groupby(
        "machine_name"
    )["machine_no"].nunique()

    active_counts = active_counts[
        active_counts >= 2
    ]

    daily = cache["machine_daily"]
    daily = daily[
        (daily["date"] < pd.Timestamp(target_date))
        & (
            daily["machine_name"].isin(
                active_counts.index
            )
        )
    ].copy()

    if daily.empty:
        return pd.DataFrame()

    daily["strong"] = (
        (daily["machine_count"] >= 2)
        & (daily["avg_diff"] >= avg_threshold)
        & (daily["win_rate"] >= win_threshold)
    )

    rows = []

    for machine_name, machine_count in active_counts.items():
        machine_data = daily[
            daily["machine_name"] == machine_name
        ].sort_values("date")

        if machine_data.empty:
            continue

        strong_dates = (
            machine_data.loc[
                machine_data["strong"],
                "date",
            ]
            .sort_values()
        )

        if len(strong_dates):
            last_strong = strong_dates.max()
            days_since = (
                pd.Timestamp(target_date)
                - last_strong
            ).days
        else:
            last_strong = pd.NaT
            days_since = (
                pd.Timestamp(target_date)
                - machine_data["date"].min()
            ).days

        intervals = (
            strong_dates.diff()
            .dt.days
            .dropna()
        )

        if len(intervals):
            typical_interval = float(
                intervals.median()
            )
        else:
            typical_interval = np.nan

        observed_span = max(
            1,
            (
                pd.Timestamp(target_date)
                - machine_data["date"].min()
            ).days,
        )

        if (
            pd.notna(typical_interval)
            and typical_interval > 0
        ):
            due_ratio = (
                days_since
                / typical_interval
            )
        else:
            due_ratio = (
                days_since
                / observed_span
            )

        rows.append(
            {
                "machine_name": machine_name,
                "machine_count": int(machine_count),
                "strong_days": int(
                    machine_data["strong"].sum()
                ),
                "strong_rate": float(
                    machine_data["strong"].mean()
                    * 100
                ),
                "last_strong_date": last_strong,
                "days_since_strong": days_since,
                "typical_interval": typical_interval,
                "due_ratio": due_ratio,
                "recent3_avg_diff": (
                    machine_data
                    .tail(3)["avg_diff"]
                    .mean()
                ),
            }
        )

    result = pd.DataFrame(rows)

    if result.empty:
        return result

    result["_due"] = rank_score(
        result["due_ratio"],
        True,
    )
    result["_dip"] = rank_score(
        result["recent3_avg_diff"],
        False,
    )
    result["_freq"] = rank_score(
        result["strong_rate"],
        True,
    )

    result["score"] = (
        0.50 * result["_due"]
        + 0.30 * result["_dip"]
        + 0.20 * result["_freq"]
    ).round(1)

    return result.sort_values(
        "score",
        ascending=False,
    ).reset_index(drop=True)


def build_combined_ranking(
    cache,
    target_date,
    all_machine_df,
    suffix_df,
    blocks_df,
    dip_df,
    uphold_df,
    rotation_df,
):
    """6種類の狙い点を台番号ごとにまとめて総合点を作る。"""
    _, _, current = strategy_active_info(
        cache,
        target_date,
    )

    if current.empty:
        return pd.DataFrame()

    base = (
        current[
            [
                "machine_no",
                "machine_name",
            ]
        ]
        .drop_duplicates("machine_no")
        .copy()
    )

    base["suffix"] = base["machine_no"] % 10

    if not all_machine_df.empty:
        base = base.merge(
            all_machine_df[
                ["machine_name", "score"]
            ].rename(
                columns={"score": "all_score"}
            ),
            on="machine_name",
            how="left",
        )
    else:
        base["all_score"] = 0.0

    if not suffix_df.empty:
        base = base.merge(
            suffix_df[
                ["suffix", "score"]
            ].rename(
                columns={"score": "suffix_score"}
            ),
            on="suffix",
            how="left",
        )
    else:
        base["suffix_score"] = 0.0

    block_score_map = {}

    if not blocks_df.empty:
        for _, row in blocks_df.iterrows():
            for machine_no in row["machine_nos"]:
                block_score_map[machine_no] = max(
                    block_score_map.get(
                        machine_no,
                        0.0,
                    ),
                    float(row["score"]),
                )

    base["block_score"] = (
        base["machine_no"]
        .map(block_score_map)
        .fillna(0.0)
    )

    if not dip_df.empty:
        base = base.merge(
            dip_df[
                ["machine_no", "score"]
            ].rename(
                columns={"score": "dip_score"}
            ),
            on="machine_no",
            how="left",
        )
    else:
        base["dip_score"] = 0.0

    if not uphold_df.empty:
        base = base.merge(
            uphold_df[
                [
                    "machine_no",
                    "score",
                    "type",
                ]
            ].rename(
                columns={
                    "score": "uphold_score",
                    "type": "uphold_type",
                }
            ),
            on="machine_no",
            how="left",
        )
    else:
        base["uphold_score"] = 0.0
        base["uphold_type"] = ""

    if not rotation_df.empty:
        base = base.merge(
            rotation_df[
                ["machine_name", "score"]
            ].rename(
                columns={
                    "score": "rotation_score",
                }
            ),
            on="machine_name",
            how="left",
        )
    else:
        base["rotation_score"] = 0.0

    score_columns = [
        "all_score",
        "suffix_score",
        "block_score",
        "dip_score",
        "uphold_score",
        "rotation_score",
    ]

    for col in score_columns:
        base[col] = (
            pd.to_numeric(
                base[col],
                errors="coerce",
            )
            .fillna(0.0)
        )

    weights = {
        "all_score": 0.20,
        "suffix_score": 0.15,
        "block_score": 0.15,
        "dip_score": 0.20,
        "uphold_score": 0.15,
        "rotation_score": 0.15,
    }

    base["total_score"] = sum(
        base[col] * weight
        for col, weight in weights.items()
    ).round(1)

    labels = {
        "all_score": "全台",
        "suffix_score": "末尾",
        "block_score": "並び",
        "dip_score": "凹み",
        "uphold_score": "上げ/据え",
        "rotation_score": "ローテ",
    }

    def build_reason(row):
        contributions = sorted(
            [
                (
                    float(row[col]) * weights[col],
                    labels[col],
                )
                for col in score_columns
            ],
            reverse=True,
        )

        return "・".join(
            item[1]
            for item in contributions[:3]
        )

    base["reason"] = base.apply(
        build_reason,
        axis=1,
    )

    base = base.sort_values(
        "total_score",
        ascending=False,
    ).reset_index(drop=True)

    base.insert(
        0,
        "rank",
        range(1, len(base) + 1),
    )

    return base


def evaluate_strategy_candidates(
    cache,
    target_date,
    strategy,
    candidates,
    top_n,
):
    """予測対象日の実績を使い、過去時点の候補を後から採点する。"""
    actual = cache["data"][
        cache["data"]["date"]
        == pd.Timestamp(target_date)
    ].copy()

    if actual.empty or candidates.empty:
        return None

    selected = pd.DataFrame()

    if strategy in ["全台系", "機種ローテ"]:
        names = (
            candidates
            .head(top_n)["machine_name"]
            .tolist()
        )

        selected = actual[
            actual["machine_name"].isin(names)
        ]

    elif strategy == "末尾":
        suffixes = set(
            candidates
            .head(top_n)["suffix"]
            .astype(int)
            .tolist()
        )

        selected = actual[
            actual["machine_no"]
            .astype(int)
            .mod(10)
            .isin(suffixes)
        ]

    elif strategy == "並び":
        machine_nos = set()

        for _, row in (
            candidates.head(top_n).iterrows()
        ):
            machine_nos.update(
                int(no)
                for no in row["machine_nos"]
            )

        selected = actual[
            actual["machine_no"]
            .astype(int)
            .isin(machine_nos)
        ]

    elif strategy == "凹み":
        dip_candidates = candidates[
            candidates["is_dip"]
        ]

        machine_nos = set(
            dip_candidates
            .head(top_n)["machine_no"]
            .astype(int)
            .tolist()
        )

        selected = actual[
            actual["machine_no"]
            .astype(int)
            .isin(machine_nos)
        ]

    elif strategy == "上げ/据え":
        machine_nos = set(
            candidates
            .head(top_n)["machine_no"]
            .astype(int)
            .tolist()
        )

        selected = actual[
            actual["machine_no"]
            .astype(int)
            .isin(machine_nos)
        ]

    if selected.empty:
        return None

    avg_diff = float(
        selected["diff_medals"].mean()
    )

    return {
        "avg_diff": avg_diff,
        "win_rate": float(
            (selected["diff_medals"] > 0)
            .mean()
            * 100
        ),
        "selected_rows": int(len(selected)),
        "positive": avg_diff > 0,
    }


def run_strategy_backtest(
    cache,
    test_days,
    all_avg_threshold,
    all_win_threshold,
    suffix_avg_threshold,
    suffix_win_threshold,
    block_avg_threshold,
    block_win_threshold,
):
    """未来データを見ずに各日の候補を出し、その日の実績で検証する。"""
    dates = sorted(
        pd.to_datetime(
            cache["data"]["date"]
            .dropna()
            .unique()
        )
    )

    if len(dates) < 10:
        return pd.DataFrame(), pd.DataFrame()

    eligible_dates = dates[7:]

    target_dates = eligible_dates[
        -min(
            test_days,
            len(eligible_dates),
        ):
    ]

    rows = []

    for target_date in target_dates:
        strategies = [
            (
                "全台系",
                score_all_machine_cached(
                    cache,
                    target_date,
                    all_avg_threshold,
                    all_win_threshold,
                ),
                3,
            ),
            (
                "末尾",
                score_suffix_cached(
                    cache,
                    target_date,
                    suffix_avg_threshold,
                    suffix_win_threshold,
                ),
                2,
            ),
            (
                "並び",
                score_blocks_cached(
                    cache,
                    target_date,
                    block_avg_threshold,
                    block_win_threshold,
                ),
                5,
            ),
            (
                "凹み",
                score_dip_cached(
                    cache,
                    target_date,
                ),
                10,
            ),
            (
                "上げ/据え",
                score_uphold_cached(
                    cache,
                    target_date,
                ),
                10,
            ),
            (
                "機種ローテ",
                score_rotation_cached(
                    cache,
                    target_date,
                    all_avg_threshold,
                    all_win_threshold,
                ),
                3,
            ),
        ]

        for (
            strategy,
            candidates,
            top_n,
        ) in strategies:
            result = evaluate_strategy_candidates(
                cache,
                target_date,
                strategy,
                candidates,
                top_n,
            )

            if result is None:
                continue

            rows.append(
                {
                    "strategy": strategy,
                    "date": target_date,
                    **result,
                }
            )

    detail = pd.DataFrame(rows)

    if detail.empty:
        return pd.DataFrame(), detail

    summary = (
        detail.groupby("strategy")
        .agg(
            tests=("date", "nunique"),
            plus_rate=("positive", "mean"),
            avg_diff=("avg_diff", "mean"),
            avg_win_rate=("win_rate", "mean"),
            avg_selected_rows=(
                "selected_rows",
                "mean",
            ),
        )
        .reset_index()
    )

    summary["plus_rate"] = (
        summary["plus_rate"] * 100
    ).round(1)

    summary["avg_diff"] = (
        summary["avg_diff"]
        .round(0)
    )

    summary["avg_win_rate"] = (
        summary["avg_win_rate"]
        .round(1)
    )

    summary["avg_selected_rows"] = (
        summary["avg_selected_rows"]
        .round(1)
    )

    strategy_order = [
        "全台系",
        "末尾",
        "並び",
        "凹み",
        "上げ/据え",
        "機種ローテ",
    ]

    summary["strategy"] = pd.Categorical(
        summary["strategy"],
        categories=strategy_order,
        ordered=True,
    )

    summary = summary.sort_values(
        "strategy"
    ).reset_index(drop=True)

    summary["strategy"] = (
        summary["strategy"].astype(str)
    )

    return summary, detail


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
        "狙い分析",
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
        daily_display = daily.sort_values("date", ascending=False).rename(
            columns={
                "date": "日付",
                "total_diff_medals": "総差枚",
                "avg_games": "平均G数",
                "win_rate": "勝率(%)",
            }
        )

        st.dataframe(
            daily_display,
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
        df_display = df.rename(
            columns={
                "date": "日付",
                "weekday": "曜日",
                "total_diff_medals": "総差枚",
                "avg_diff_medals": "平均差枚",
                "avg_games": "平均G数",
                "win_rate": "勝率(%)",
                "win_count": "勝ち台数",
                "machine_count": "設置台数",
            }
        )

        st.dataframe(
            df_display,
            use_container_width=True,
            hide_index=True,
        )

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

    st.markdown("#### 機種検索")

    search = st.text_input(
        "① 機種名を入力して検索",
        placeholder="例：ジャグラー、北斗、モンキー など",
    )

    machine_choices = sorted(
        df["machine_name"].dropna().astype(str).unique().tolist(),
        key=kana_sort_key,
    )

    selected_machines = st.multiselect(
        "② 機種を複数選択（カナ順）",
        options=machine_choices,
        placeholder="機種を選択してください（複数選択可）",
    )

    filtered_df = df.copy()

    if search:
        filtered_df = filtered_df[
            filtered_df["machine_name"]
            .astype(str)
            .str.contains(search, case=False, na=False)
        ]

    if selected_machines:
        filtered_df = filtered_df[
            filtered_df["machine_name"].isin(selected_machines)
        ]

    df_display = filtered_df.rename(
        columns={
            "machine_name": "機種名",
            "records": "データ件数",
            "days": "データ日数",
            "machines": "台数",
            "total_diff_medals": "総差枚",
            "avg_diff_medals": "平均差枚",
            "avg_games": "平均G数",
            "win_count": "勝ち回数",
            "win_rate": "勝率(%)",
        }
    )

    st.dataframe(
        df_display,
        use_container_width=True,
        hide_index=True,
    )

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

            SUM(COALESCE(bb, 0)) AS bb_total,
            SUM(COALESCE(rb, 0)) AS rb_total,

            ROUND(
                SUM(COALESCE(games, 0))::numeric
                / NULLIF(
                    SUM(COALESCE(bb, 0)) + SUM(COALESCE(rb, 0)),
                    0
                ),
                1
            ) AS combined_rate_period,

            ROUND(
                SUM(COALESCE(games, 0))::numeric
                / NULLIF(SUM(COALESCE(bb, 0)), 0),
                1
            ) AS bb_rate_period,

            ROUND(
                SUM(COALESCE(games, 0))::numeric
                / NULLIF(SUM(COALESCE(rb, 0)), 0),
                1
            ) AS rb_rate_period,

            SUM(CASE WHEN diff_medals > 0 THEN 1 ELSE 0 END) AS win_count,

            ROUND(
                100.0 * SUM(CASE WHEN diff_medals > 0 THEN 1 ELSE 0 END)
                / NULLIF(COUNT(*), 0),
                1
            ) AS win_rate

        FROM slot_machine_results

        WHERE
            store_id = %s
            AND date BETWEEN %s AND %s

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

    df_display = df.copy()

    df_display["combined_rate_period"] = df_display["combined_rate_period"].apply(
        lambda x: f"1/{float(x):.1f}" if pd.notna(x) else "-"
    )
    df_display["bb_rate_period"] = df_display["bb_rate_period"].apply(
        lambda x: f"1/{float(x):.1f}" if pd.notna(x) else "-"
    )
    df_display["rb_rate_period"] = df_display["rb_rate_period"].apply(
        lambda x: f"1/{float(x):.1f}" if pd.notna(x) else "-"
    )

    df_display = df_display.rename(
        columns={
            "machine_no": "台番号",
            "days": "データ日数",
            "machine_name_example": "機種名",
            "total_diff_medals": "総差枚",
            "avg_diff_medals": "平均差枚",
            "avg_games": "平均G数",
            "bb_total": "BB",
            "rb_total": "RB",
            "combined_rate_period": "合算",
            "bb_rate_period": "BB確率",
            "rb_rate_period": "RB確率",
            "win_count": "勝ち回数",
            "win_rate": "勝率(%)",
        }
    )

    st.dataframe(
        df_display,
        use_container_width=True,
        hide_index=True,
    )

elif menu == "狙い分析":
    st.subheader("🎯 狙い分析")
    st.write(
        "過去データから、全台系・末尾・並び・凹み・上げ/据え・"
        "機種ローテの6方向で候補を点数化します。"
    )
    st.caption(
        "点数は設定を断定する数字ではなく、過去傾向の相対評価です。"
        "狙う日より後のデータは計算に使いません。"
    )

    store_id, store_name = store_selector()

    if store_id is None:
        st.info("データがありません。")
        st.stop()

    analysis_data = query_df(
        """
        SELECT
            date,
            store_id,
            machine_no,
            machine_name,
            games,
            diff_medals,
            bb,
            rb,
            combined_rate,
            bb_rate,
            rb_rate
        FROM slot_machine_results
        WHERE store_id = %s
        ORDER BY date, machine_no
        """,
        (store_id,),
    )

    if analysis_data.empty:
        st.info("台別データがありません。")
        st.stop()

    analysis_data["date"] = pd.to_datetime(
        analysis_data["date"]
    )

    min_data_date = analysis_data["date"].min()
    max_data_date = analysis_data["date"].max()

    default_target = (
        max_data_date
        + pd.Timedelta(days=1)
    ).date()

    target_date = st.date_input(
        "狙う日",
        value=default_target,
        min_value=min_data_date.date(),
        key="strategy_target_date",
    )

    history_before_target = analysis_data[
        analysis_data["date"]
        < pd.Timestamp(target_date)
    ]

    if history_before_target.empty:
        st.warning(
            "この日より前のデータがないため分析できません。"
        )
        st.stop()

    latest_used = history_before_target["date"].max()

    st.info(
        f"分析対象：{target_date} ／ "
        f"使用する最新データ：{latest_used.date()}まで"
    )

    with st.expander(
        "判定条件を変更する",
        expanded=False,
    ):
        c1, c2 = st.columns(2)

        all_avg_threshold = c1.number_input(
            "全台系・ローテ 強日：平均差枚",
            min_value=0,
            max_value=5000,
            value=500,
            step=100,
        )

        all_win_threshold = c2.slider(
            "全台系・ローテ 強日：勝率(%)",
            min_value=50,
            max_value=100,
            value=60,
            step=1,
        )

        c3, c4 = st.columns(2)

        suffix_avg_threshold = c3.number_input(
            "末尾 強日：平均差枚",
            min_value=0,
            max_value=3000,
            value=300,
            step=100,
        )

        suffix_win_threshold = c4.slider(
            "末尾 強日：勝率(%)",
            min_value=50,
            max_value=100,
            value=55,
            step=1,
        )

        c5, c6, c7 = st.columns(3)

        block_size = c5.selectbox(
            "並び台数",
            [3, 4, 5],
            index=0,
        )

        block_avg_threshold = c6.number_input(
            "並び 強判定：平均差枚",
            min_value=0,
            max_value=5000,
            value=500,
            step=100,
        )

        block_win_threshold = c7.slider(
            "並び 強判定：勝率(%)",
            min_value=50,
            max_value=100,
            value=67,
            step=1,
        )

    with st.spinner(
        "狙い候補を計算しています..."
    ):
        strategy_cache = prepare_strategy_cache(
            analysis_data,
            block_size,
        )

        all_machine_candidates = (
            score_all_machine_cached(
                strategy_cache,
                target_date,
                all_avg_threshold,
                all_win_threshold,
            )
        )

        suffix_candidates = score_suffix_cached(
            strategy_cache,
            target_date,
            suffix_avg_threshold,
            suffix_win_threshold,
        )

        block_candidates = score_blocks_cached(
            strategy_cache,
            target_date,
            block_avg_threshold,
            block_win_threshold,
        )

        dip_candidates = score_dip_cached(
            strategy_cache,
            target_date,
        )

        uphold_candidates = score_uphold_cached(
            strategy_cache,
            target_date,
        )

        rotation_candidates = (
            score_rotation_cached(
                strategy_cache,
                target_date,
                all_avg_threshold,
                all_win_threshold,
            )
        )

        combined_ranking = build_combined_ranking(
            strategy_cache,
            target_date,
            all_machine_candidates,
            suffix_candidates,
            block_candidates,
            dip_candidates,
            uphold_candidates,
            rotation_candidates,
        )

    tabs = st.tabs(
        [
            "総合ランキング",
            "全台系",
            "末尾",
            "並び",
            "凹み",
            "上げ/据え",
            "機種ローテ",
            "バックテスト",
        ]
    )

    with tabs[0]:
        st.markdown("#### 総合狙いランキング")
        st.caption(
            "全台20%・末尾15%・並び15%・凹み20%・"
            "上げ/据え15%・機種ローテ15%で総合点を計算しています。"
        )

        if combined_ranking.empty:
            st.info("候補を計算できませんでした。")
        else:
            top_row = combined_ranking.iloc[0]

            m1, m2, m3 = st.columns(3)
            m1.metric(
                "1位 台番号",
                str(int(top_row["machine_no"])),
            )
            m2.metric(
                "機種",
                str(top_row["machine_name"]),
            )
            m3.metric(
                "総合点",
                f"{float(top_row['total_score']):.1f}点",
            )

            combined_display = (
                combined_ranking.head(50).copy()
            )

            combined_display = combined_display.rename(
                columns={
                    "rank": "順位",
                    "machine_no": "台番号",
                    "machine_name": "機種名",
                    "total_score": "総合点",
                    "all_score": "全台点",
                    "suffix_score": "末尾点",
                    "block_score": "並び点",
                    "dip_score": "凹み点",
                    "uphold_score": "上げ/据え点",
                    "rotation_score": "ローテ点",
                    "uphold_type": "上げ/据え種別",
                    "reason": "主な根拠",
                }
            )

            combined_display = combined_display[
                [
                    "順位",
                    "台番号",
                    "機種名",
                    "総合点",
                    "全台点",
                    "末尾点",
                    "並び点",
                    "凹み点",
                    "上げ/据え点",
                    "ローテ点",
                    "上げ/据え種別",
                    "主な根拠",
                ]
            ]

            st.dataframe(
                combined_display,
                use_container_width=True,
                hide_index=True,
            )

    with tabs[1]:
        st.markdown("#### 全台系候補")

        if all_machine_candidates.empty:
            st.info("全台系候補を計算できませんでした。")
        else:
            display = all_machine_candidates.copy()
            display.insert(
                0,
                "順位",
                range(1, len(display) + 1),
            )

            display = display.rename(
                columns={
                    "machine_name": "機種名",
                    "machine_count": "現在台数",
                    "observed_days": "データ日数",
                    "strong_days": "強日回数",
                    "strong_rate": "強日率(%)",
                    "last_strong_date": "前回強日",
                    "days_since_strong": "前回からの日数",
                    "recent3_avg_diff": "直近3回平均差枚",
                    "same_weekday_avg_diff": "同曜日平均差枚",
                    "score": "狙い点",
                }
            )

            columns = [
                "順位",
                "機種名",
                "現在台数",
                "狙い点",
                "強日回数",
                "強日率(%)",
                "前回強日",
                "前回からの日数",
                "直近3回平均差枚",
                "同曜日平均差枚",
                "データ日数",
            ]

            st.dataframe(
                display[columns].head(30),
                use_container_width=True,
                hide_index=True,
            )

    with tabs[2]:
        st.markdown("#### 末尾候補")

        if suffix_candidates.empty:
            st.info("末尾候補を計算できませんでした。")
        else:
            display = suffix_candidates.copy()
            display.insert(
                0,
                "順位",
                range(1, len(display) + 1),
            )

            display = display.rename(
                columns={
                    "suffix": "末尾",
                    "machine_count": "対象台数",
                    "strong_days": "強日回数",
                    "strong_rate": "強日率(%)",
                    "days_since_strong": "前回からの日数",
                    "recent3_avg_diff": "直近3回平均差枚",
                    "same_weekday_avg_diff": "同曜日平均差枚",
                    "score": "狙い点",
                }
            )

            st.dataframe(
                display[
                    [
                        "順位",
                        "末尾",
                        "狙い点",
                        "対象台数",
                        "強日回数",
                        "強日率(%)",
                        "前回からの日数",
                        "直近3回平均差枚",
                        "同曜日平均差枚",
                    ]
                ],
                use_container_width=True,
                hide_index=True,
            )

    with tabs[3]:
        st.markdown(
            f"#### {block_size}台並び候補"
        )
        st.caption(
            "台番号が完全に連番になっている場所を対象にしています。"
        )

        if block_candidates.empty:
            st.info("並び候補を計算できませんでした。")
        else:
            display = block_candidates.copy()
            display.insert(
                0,
                "順位",
                range(1, len(display) + 1),
            )

            display = display.rename(
                columns={
                    "block": "台番号範囲",
                    "observed_days": "データ日数",
                    "strong_days": "強日回数",
                    "strong_rate": "強日率(%)",
                    "days_since_strong": "前回からの日数",
                    "recent3_avg_diff": "直近3回平均差枚",
                    "same_weekday_avg_diff": "同曜日平均差枚",
                    "score": "狙い点",
                }
            )

            st.dataframe(
                display[
                    [
                        "順位",
                        "台番号範囲",
                        "狙い点",
                        "強日回数",
                        "強日率(%)",
                        "前回からの日数",
                        "直近3回平均差枚",
                        "同曜日平均差枚",
                        "データ日数",
                    ]
                ].head(50),
                use_container_width=True,
                hide_index=True,
            )

    with tabs[4]:
        st.markdown("#### 凹み狙い候補")

        only_dip = st.checkbox(
            "直近3回累計がマイナスの台だけ表示",
            value=True,
            key="only_dip_candidates",
        )

        display = dip_candidates.copy()

        if only_dip and not display.empty:
            display = display[
                display["is_dip"]
            ].copy()

        if display.empty:
            st.info("凹み候補がありません。")
        else:
            display.insert(
                0,
                "順位",
                range(1, len(display) + 1),
            )

            display = display.rename(
                columns={
                    "machine_no": "台番号",
                    "machine_name": "機種名",
                    "last3_sum": "直近3回累計差枚",
                    "last5_sum": "直近5回累計差枚",
                    "recovery_samples": "過去凹み回数",
                    "recovery_rate": "凹み後プラス率(%)",
                    "recovery_avg_diff": "凹み後平均差枚",
                    "score": "狙い点",
                }
            )

            st.dataframe(
                display[
                    [
                        "順位",
                        "台番号",
                        "機種名",
                        "狙い点",
                        "直近3回累計差枚",
                        "直近5回累計差枚",
                        "過去凹み回数",
                        "凹み後プラス率(%)",
                        "凹み後平均差枚",
                    ]
                ].head(50),
                use_container_width=True,
                hide_index=True,
            )

    with tabs[5]:
        st.markdown("#### 上げ・据え候補")

        state_filter = st.selectbox(
            "表示する候補",
            [
                "すべて",
                "上げ候補",
                "据え候補",
            ],
            key="uphold_filter",
        )

        display = uphold_candidates.copy()

        if (
            state_filter != "すべて"
            and not display.empty
        ):
            display = display[
                display["type"] == state_filter
            ].copy()

        if display.empty:
            st.info("候補がありません。")
        else:
            display.insert(
                0,
                "順位",
                range(1, len(display) + 1),
            )

            display = display.rename(
                columns={
                    "machine_no": "台番号",
                    "machine_name": "機種名",
                    "type": "種別",
                    "previous_diff": "前回差枚",
                    "transition_samples": "過去該当回数",
                    "next_positive_rate": "次回プラス率(%)",
                    "next_avg_diff": "次回平均差枚",
                    "score": "狙い点",
                }
            )

            st.dataframe(
                display[
                    [
                        "順位",
                        "台番号",
                        "機種名",
                        "種別",
                        "狙い点",
                        "前回差枚",
                        "過去該当回数",
                        "次回プラス率(%)",
                        "次回平均差枚",
                    ]
                ].head(50),
                use_container_width=True,
                hide_index=True,
            )

    with tabs[6]:
        st.markdown("#### 機種ローテーション候補")

        if rotation_candidates.empty:
            st.info(
                "機種ローテ候補を計算できませんでした。"
            )
        else:
            display = rotation_candidates.copy()
            display.insert(
                0,
                "順位",
                range(1, len(display) + 1),
            )

            display = display.rename(
                columns={
                    "machine_name": "機種名",
                    "machine_count": "現在台数",
                    "strong_days": "強日回数",
                    "strong_rate": "強日率(%)",
                    "last_strong_date": "前回強日",
                    "days_since_strong": "経過日数",
                    "typical_interval": "強日間隔中央値",
                    "due_ratio": "ローテ到来度",
                    "recent3_avg_diff": "直近3回平均差枚",
                    "score": "狙い点",
                }
            )

            st.dataframe(
                display[
                    [
                        "順位",
                        "機種名",
                        "現在台数",
                        "狙い点",
                        "前回強日",
                        "経過日数",
                        "強日間隔中央値",
                        "ローテ到来度",
                        "強日回数",
                        "強日率(%)",
                        "直近3回平均差枚",
                    ]
                ].head(30),
                use_container_width=True,
                hide_index=True,
            )

    with tabs[7]:
        st.markdown("#### 過去データでバックテスト")
        st.write(
            "各日について、その日より前のデータだけで候補を作り、"
            "実際の当日結果と照合します。"
        )

        available_test_days = max(
            1,
            len(
                sorted(
                    analysis_data["date"]
                    .dropna()
                    .unique()
                )
            )
            - 7,
        )

        test_options = [
            days
            for days in [5, 10, 20, 30]
            if days <= available_test_days
        ]

        if not test_options:
            test_options = [available_test_days]

        default_index = min(
            1,
            len(test_options) - 1,
        )

        test_days = st.selectbox(
            "検証する直近日数",
            test_options,
            index=default_index,
        )

        st.caption(
            "検証時の候補数：全台系3機種、末尾2個、"
            "並び5か所、凹み10台、上げ/据え10台、"
            "機種ローテ3機種。"
        )

        if st.button(
            "バックテストを実行",
            type="primary",
            key="run_strategy_backtest",
        ):
            with st.spinner(
                "未来データを見ない形で検証しています..."
            ):
                (
                    backtest_summary,
                    backtest_detail,
                ) = run_strategy_backtest(
                    strategy_cache,
                    test_days,
                    all_avg_threshold,
                    all_win_threshold,
                    suffix_avg_threshold,
                    suffix_win_threshold,
                    block_avg_threshold,
                    block_win_threshold,
                )

            if backtest_summary.empty:
                st.warning(
                    "バックテストできるデータが足りません。"
                )
            else:
                summary_display = (
                    backtest_summary.rename(
                        columns={
                            "strategy": "狙い方",
                            "tests": "検証日数",
                            "plus_rate": "候補全体プラス率(%)",
                            "avg_diff": "候補1台平均差枚",
                            "avg_win_rate": "候補台勝率(%)",
                            "avg_selected_rows": "1日平均候補台数",
                        }
                    )
                )

                st.dataframe(
                    summary_display[
                        [
                            "狙い方",
                            "検証日数",
                            "候補全体プラス率(%)",
                            "候補1台平均差枚",
                            "候補台勝率(%)",
                            "1日平均候補台数",
                        ]
                    ],
                    use_container_width=True,
                    hide_index=True,
                )

                best = backtest_summary.sort_values(
                    "avg_diff",
                    ascending=False,
                ).iloc[0]

                st.success(
                    "今回の検証で候補1台平均差枚が最も高かった狙い方："
                    f"{best['strategy']} "
                    f"（{int(best['avg_diff']):+,}枚）"
                )

                with st.expander(
                    "日ごとの検証結果を見る"
                ):
                    detail_display = (
                        backtest_detail.copy()
                    )

                    detail_display["date"] = (
                        pd.to_datetime(
                            detail_display["date"]
                        ).dt.date
                    )

                    detail_display = (
                        detail_display.rename(
                            columns={
                                "strategy": "狙い方",
                                "date": "日付",
                                "avg_diff": "候補平均差枚",
                                "win_rate": "候補台勝率(%)",
                                "selected_rows": "候補台数",
                                "positive": "候補全体プラス",
                            }
                        )
                    )

                    detail_display[
                        "候補全体プラス"
                    ] = detail_display[
                        "候補全体プラス"
                    ].map(
                        {
                            True: "○",
                            False: "×",
                        }
                    )

                    st.dataframe(
                        detail_display[
                            [
                                "日付",
                                "狙い方",
                                "候補平均差枚",
                                "候補台勝率(%)",
                                "候補台数",
                                "候補全体プラス",
                            ]
                        ].sort_values(
                            ["日付", "狙い方"],
                            ascending=[False, True],
                        ),
                        use_container_width=True,
                        hide_index=True,
                    )

