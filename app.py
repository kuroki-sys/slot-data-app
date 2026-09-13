import hashlib
import json
import math
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
        art INTEGER,
        combined_rate NUMERIC,
        bb_rate NUMERIC,
        rb_rate NUMERIC,
        art_rate NUMERIC,
        combined_rate_text TEXT,
        bb_rate_text TEXT,
        rb_rate_text TEXT,
        art_rate_text TEXT,
        source_url TEXT,
        PRIMARY KEY (date, store_id, machine_no)
    );

    -- 既存DBを壊さず、多店舗でART/AT系も保存できるように列を追加する。
    ALTER TABLE slot_machine_results ADD COLUMN IF NOT EXISTS art INTEGER;
    ALTER TABLE slot_machine_results ADD COLUMN IF NOT EXISTS art_rate NUMERIC;
    ALTER TABLE slot_machine_results ADD COLUMN IF NOT EXISTS art_rate_text TEXT;

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
        source_sha256 TEXT UNIQUE,
        source_headers TEXT,
        import_notes TEXT
    );

    ALTER TABLE slot_import_log ADD COLUMN IF NOT EXISTS source_headers TEXT;
    ALTER TABLE slot_import_log ADD COLUMN IF NOT EXISTS import_notes TEXT;

    CREATE INDEX IF NOT EXISTS idx_slot_machine_results_store_date
        ON slot_machine_results(store_id, date);
    CREATE INDEX IF NOT EXISTS idx_slot_machine_results_machine_name
        ON slot_machine_results(machine_name);
    CREATE INDEX IF NOT EXISTS idx_slot_machine_results_machine_no
        ON slot_machine_results(machine_no);

    -- 旧テーブルは互換性のため残す。新規イベント管理は slot_store_events を使用する。
    CREATE TABLE IF NOT EXISTS slot_special_events (
        date DATE NOT NULL,
        store_id BIGINT NOT NULL REFERENCES slot_stores(store_id) ON DELETE CASCADE,
        event_name TEXT NOT NULL,
        event_tags TEXT,
        source_label TEXT,
        confidence TEXT,
        note TEXT,
        PRIMARY KEY (date, store_id)
    );

    CREATE TABLE IF NOT EXISTS slot_store_events (
        event_id BIGSERIAL PRIMARY KEY,
        date DATE NOT NULL,
        store_id BIGINT NOT NULL REFERENCES slot_stores(store_id) ON DELETE CASCADE,
        event_name TEXT NOT NULL,
        event_tags TEXT,
        full_machine_names TEXT,
        half_machine_names TEXT,
        tail_targets TEXT,
        line_targets TEXT,
        other_features TEXT,
        source_label TEXT,
        confidence TEXT,
        note TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (store_id, date, event_name)
    );

    CREATE INDEX IF NOT EXISTS idx_slot_store_events_store_date
        ON slot_store_events(store_id, date);
    CREATE INDEX IF NOT EXISTS idx_slot_store_events_name
        ON slot_store_events(store_id, event_name);

    -- ジャグラー公式スペックの共通マスタ。店舗には依存せず全店舗で共有する。
    CREATE TABLE IF NOT EXISTS slot_juggler_masters (
        spec_id BIGSERIAL PRIMARY KEY,
        machine_name TEXT NOT NULL UNIQUE,
        source_label TEXT,
        source_url TEXT,
        note TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS slot_juggler_spec_values (
        spec_id BIGINT NOT NULL REFERENCES slot_juggler_masters(spec_id) ON DELETE CASCADE,
        setting SMALLINT NOT NULL CHECK (setting BETWEEN 1 AND 6),
        bb_den NUMERIC NOT NULL,
        rb_den NUMERIC NOT NULL,
        combined_den NUMERIC NOT NULL,
        PRIMARY KEY (spec_id, setting)
    );

    CREATE TABLE IF NOT EXISTS slot_juggler_aliases (
        alias_name TEXT PRIMARY KEY,
        spec_id BIGINT NOT NULL REFERENCES slot_juggler_masters(spec_id) ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS idx_slot_juggler_alias_spec
        ON slot_juggler_aliases(spec_id);

    CREATE TABLE IF NOT EXISTS slot_schema_migrations (
        migration_key TEXT PRIMARY KEY,
        migrated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    -- 旧イベント表の内容は最初の1回だけ新テーブルへコピーする。
    INSERT INTO slot_store_events
        (date, store_id, event_name, event_tags, source_label, confidence, note)
    SELECT
        date, store_id, event_name, event_tags, source_label, confidence, note
    FROM slot_special_events
    WHERE NOT EXISTS (
        SELECT 1
        FROM slot_schema_migrations
        WHERE migration_key = 'special_events_to_store_events_v1'
    )
    ON CONFLICT (store_id, date, event_name) DO NOTHING;

    INSERT INTO slot_schema_migrations(migration_key)
    VALUES ('special_events_to_store_events_v1')
    ON CONFLICT (migration_key) DO NOTHING;
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)
        conn.commit()


SPECIAL_EVENT_STORE_NAME = "BIGディッパー新橋1号店"

# ユーザー提供の「仕掛け一覧」画像4枚から、判読に自信がある
# 特定日・イベント名だけを仮登録する。
# 読みにくい「機種」「その他仕掛け」は誤登録を避けるため自動転記しない。
SPECIAL_EVENT_SEEDS = [
    # 2026年6月
    ("2026-06-01", "東京大戦 第一章", "東京大戦 第一章"),
    ("2026-06-03", "スペシャルサンキュー", "スペシャルサンキュー"),
    ("2026-06-04", "スロパチ", "スロパチ"),
    ("2026-06-06", "月ゾロ目／裏天下無双", "月ゾロ目,裏天下無双"),
    ("2026-06-07", "7の付く日", "7の付く日"),
    ("2026-06-08", "ハーフテン", "ハーフテン"),
    ("2026-06-09", "スペシャルサンキュー", "スペシャルサンキュー"),
    ("2026-06-11", "ぶちアゲ無双", "ぶちアゲ無双"),
    ("2026-06-13", "スペシャルサンキュー", "スペシャルサンキュー"),
    ("2026-06-14", "九頭龍", "九頭龍"),
    ("2026-06-15", "一刀両断", "一刀両断"),
    ("2026-06-17", "7の付く日", "7の付く日"),
    ("2026-06-18", "ハーフテン", "ハーフテン"),
    ("2026-06-19", "スペシャルサンキュー", "スペシャルサンキュー"),
    ("2026-06-22", "ぶちアゲWeek", "ぶちアゲWeek"),
    ("2026-06-23", "ぶちアゲWeek／スペシャルサンキュー", "ぶちアゲWeek,スペシャルサンキュー"),
    ("2026-06-24", "ぶちアゲWeek／テッペンDASHリサーチ", "ぶちアゲWeek,テッペンDASHリサーチ"),
    ("2026-06-25", "ぶちアゲWeek", "ぶちアゲWeek"),
    ("2026-06-26", "ぶちアゲWeek", "ぶちアゲWeek"),
    ("2026-06-27", "ぶちアゲWeek／7の付く日", "ぶちアゲWeek,7の付く日"),
    ("2026-06-28", "ぶちアゲWeek／ハーフテン", "ぶちアゲWeek,ハーフテン"),
    ("2026-06-29", "スペシャルサンキュー", "スペシャルサンキュー"),
    ("2026-06-30", "転生", "転生"),

    # 2026年7月
    ("2026-07-01", "東京大戦 第一章", "東京大戦 第一章"),
    ("2026-07-03", "THANK YOU", "THANK YOU"),
    ("2026-07-04", "スロパチ", "スロパチ"),
    ("2026-07-07", "月ゾロ目／7の付く日", "月ゾロ目,7の付く日"),
    ("2026-07-08", "ハーフテン／パセリ", "ハーフテン,パセリ"),
    ("2026-07-09", "THANK YOU", "THANK YOU"),
    ("2026-07-11", "ぶちアゲ無双", "ぶちアゲ無双"),
    ("2026-07-17", "7の付く日", "7の付く日"),
    ("2026-07-18", "ハーフテン／パセリ", "ハーフテン,パセリ"),
    ("2026-07-19", "THANK YOU", "THANK YOU"),
    ("2026-07-23", "THANK YOU", "THANK YOU"),
    ("2026-07-27", "7の付く日", "7の付く日"),
    ("2026-07-28", "ハーフテン／パセリ", "ハーフテン,パセリ"),
    ("2026-07-29", "THANK YOU", "THANK YOU"),

    # 2026年8月
    ("2026-08-01", "東京大戦 第一章", "東京大戦 第一章"),
    ("2026-08-03", "THANK YOU", "THANK YOU"),
    ("2026-08-04", "スロパチ", "スロパチ"),
    ("2026-08-06", "アルマゲドン", "アルマゲドン"),
    ("2026-08-07", "7の付く日", "7の付く日"),
    ("2026-08-09", "クロロプレミアム", "クロロプレミアム"),
    ("2026-08-17", "7の付く日", "7の付く日"),
    ("2026-08-18", "ハーフテン／パセリ", "ハーフテン,パセリ"),
    ("2026-08-19", "THANK YOU", "THANK YOU"),
    ("2026-08-27", "7の付く日", "7の付く日"),
    ("2026-08-28", "ハーフテン／パセリ", "ハーフテン,パセリ"),

    # 2026年9月
    ("2026-09-01", "東京大戦 第一章", "東京大戦 第一章"),
    ("2026-09-02", "1号店・2号店合同取材", "1号店・2号店合同取材"),
    ("2026-09-03", "THANK YOU", "THANK YOU"),
    ("2026-09-04", "スロパチ", "スロパチ"),
    ("2026-09-05", "テッペンDASHリサーチ", "テッペンDASHリサーチ"),
    ("2026-09-06", "クロロプレミアム", "クロロプレミアム"),
    ("2026-09-07", "7の付く日", "7の付く日"),
    ("2026-09-08", "ハーフテン／パセリ", "ハーフテン,パセリ"),
    ("2026-09-09", "月ゾロ目／裏天下無双", "月ゾロ目,裏天下無双"),
    ("2026-09-10", "アルマゲドン", "アルマゲドン"),
    ("2026-09-11", "ぶちアゲ無双", "ぶちアゲ無双"),
]


def split_event_tags(value):
    if value is None:
        return []
    parts = re.split(r"[,、/／|]+", str(value))
    return [p.strip() for p in parts if p.strip()]


def text_or_blank(value):
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return str(value)


def seed_special_event_defaults():
    """新橋の画像由来の初期イベントは最初の1回だけ登録する。"""
    migration_key = "shimbashi_image_seed_v1"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM slot_schema_migrations WHERE migration_key = %s",
                (migration_key,),
            )
            if cur.fetchone():
                return

            cur.execute(
                "SELECT store_id FROM slot_stores WHERE store_name = %s",
                (SPECIAL_EVENT_STORE_NAME,),
            )
            row = cur.fetchone()
            if not row:
                return
            store_id = row[0]

            for date_str, event_name, event_tags in SPECIAL_EVENT_SEEDS:
                cur.execute(
                    """
                    INSERT INTO slot_store_events
                    (date, store_id, event_name, event_tags, source_label, confidence, note)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (store_id, date, event_name) DO NOTHING
                    """,
                    (
                        date_str,
                        store_id,
                        event_name,
                        event_tags,
                        "ぽこ独自調べ「仕掛け一覧」画像（ユーザー提供）",
                        "画像から判読できた範囲",
                        "イベント名のみ初期登録。細かい機種欄・その他仕掛けは誤読防止のため自動転記していません。",
                    ),
                )

            cur.execute(
                """
                INSERT INTO slot_schema_migrations(migration_key)
                VALUES (%s)
                ON CONFLICT (migration_key) DO NOTHING
                """,
                (migration_key,),
            )
        conn.commit()


def create_store_by_name(store_name):
    name = str(store_name or "").strip()
    if not name:
        raise ValueError("店舗名を入力してください。")
    with get_conn() as conn:
        with conn.cursor() as cur:
            store_id = get_or_create_store_id(cur, name)
        conn.commit()
    clear_cache()
    return store_id


def save_store_event(
    store_id,
    event_date,
    event_name,
    event_tags="",
    full_machine_names="",
    half_machine_names="",
    tail_targets="",
    line_targets="",
    other_features="",
    source_label="アプリ手入力",
    confidence="手入力",
    note="",
    event_id=None,
):
    name = str(event_name or "").strip()
    if not name:
        raise ValueError("イベント名を入力してください。")

    values = (
        event_date,
        store_id,
        name,
        str(event_tags or name).strip(),
        str(full_machine_names or "").strip(),
        str(half_machine_names or "").strip(),
        str(tail_targets or "").strip(),
        str(line_targets or "").strip(),
        str(other_features or "").strip(),
        str(source_label or "").strip(),
        str(confidence or "").strip(),
        str(note or "").strip(),
    )

    with get_conn() as conn:
        with conn.cursor() as cur:
            if event_id is None:
                cur.execute(
                    """
                    INSERT INTO slot_store_events
                    (date, store_id, event_name, event_tags, full_machine_names,
                     half_machine_names, tail_targets, line_targets, other_features,
                     source_label, confidence, note)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (store_id, date, event_name) DO UPDATE SET
                        event_tags = EXCLUDED.event_tags,
                        full_machine_names = EXCLUDED.full_machine_names,
                        half_machine_names = EXCLUDED.half_machine_names,
                        tail_targets = EXCLUDED.tail_targets,
                        line_targets = EXCLUDED.line_targets,
                        other_features = EXCLUDED.other_features,
                        source_label = EXCLUDED.source_label,
                        confidence = EXCLUDED.confidence,
                        note = EXCLUDED.note,
                        updated_at = NOW()
                    """,
                    values,
                )
            else:
                cur.execute(
                    """
                    UPDATE slot_store_events
                    SET date=%s,
                        store_id=%s,
                        event_name=%s,
                        event_tags=%s,
                        full_machine_names=%s,
                        half_machine_names=%s,
                        tail_targets=%s,
                        line_targets=%s,
                        other_features=%s,
                        source_label=%s,
                        confidence=%s,
                        note=%s,
                        updated_at=NOW()
                    WHERE event_id=%s
                    """,
                    values + (int(event_id),),
                )
        conn.commit()
    clear_cache()


def delete_store_event(store_id, event_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM slot_store_events WHERE store_id=%s AND event_id=%s",
                (store_id, int(event_id)),
            )
        conn.commit()
    clear_cache()


def get_special_event_tags(events_df):
    tags = set()
    if events_df is None or events_df.empty:
        return []
    for value in events_df["event_tags"].fillna(""):
        tags.update(split_event_tags(value))
    return sorted(tags, key=kana_sort_key)


def matching_special_event_dates(events_df, target_date, selected_tags):
    """同じ日に複数イベントがある場合は、その日のタグを合算して照合する。"""
    if events_df is None or events_df.empty or not selected_tags:
        return []

    target_ts = pd.Timestamp(target_date)
    grouped_tags = {}

    for _, row in events_df.iterrows():
        row_date = pd.Timestamp(row["date"])
        if row_date >= target_ts:
            continue
        grouped_tags.setdefault(row_date, set()).update(
            split_event_tags(row.get("event_tags", ""))
        )

    matched = [
        row_date
        for row_date, tags in grouped_tags.items()
        if all(tag in tags for tag in selected_tags)
    ]
    return sorted(set(matched))


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
    title = str(raw.get("page_title", "") or "").strip()
    m = re.search(
        r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})\s+(.+?)\s+データまとめ",
        title,
    )
    if not m:
        raise ValueError(f"ページタイトルから日付・店舗名を判定できません: {title}")
    date_str = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    store_name = m.group(4).strip()
    return date_str, store_name


def weekday_jp(date_str):
    return ["月", "火", "水", "木", "金", "土", "日"][
        datetime.strptime(date_str, "%Y-%m-%d").weekday()
    ]


def normalize_header(value):
    return re.sub(r"[\s　]+", "", str(value or "")).strip()


def build_header_map(header_row):
    return {
        normalize_header(name): idx
        for idx, name in enumerate(header_row or [])
        if normalize_header(name)
    }


def get_by_headers(row, header_map, *aliases):
    for alias in aliases:
        idx = header_map.get(normalize_header(alias))
        if idx is not None and idx < len(row):
            return row[idx]
    return None


def parse_anaslo_json(raw):
    """アナスロJSONをヘッダー名で解析する。店舗ごとの列順違いで誤読しない。"""
    date_str, store_name = parse_page_title(raw)
    source_url = raw.get("page_url", "")

    all_table = next(
        (t for t in raw.get("tables", []) if t.get("id") == "all_data_table"),
        None,
    )
    if not all_table:
        raise ValueError('id="all_data_table" が見つかりません。')

    rows = all_table.get("rows", [])
    if not rows:
        raise ValueError("all_data_table にデータがありません。")

    header_row = rows[0]
    header_map = build_header_map(header_row)
    detected_headers = [str(x) for x in header_row]

    required = ["台番号", "G数"]
    missing_required = [h for h in required if normalize_header(h) not in header_map]
    if missing_required:
        raise ValueError(
            "必須列が見つかりません: " + ", ".join(missing_required)
        )

    has_diff_column = any(
        normalize_header(x) in header_map
        for x in ["差枚", "差枚数", "差メダル", "差枚数(枚)"]
    )

    machine_rows = []
    for r in rows[1:]:
        machine_no = clean_int(get_by_headers(r, header_map, "台番号"))
        if machine_no is None:
            continue

        combined_text = get_by_headers(r, header_map, "合成確率", "合算", "合算確率")
        bb_rate_text = get_by_headers(r, header_map, "BB確率")
        rb_rate_text = get_by_headers(r, header_map, "RB確率")
        art_rate_text = get_by_headers(r, header_map, "ART確率", "AT確率")

        machine_rows.append(
            {
                "machine_name": str(
                    get_by_headers(r, header_map, "機種名") or ""
                ).strip(),
                "machine_no": machine_no,
                "games": clean_int(get_by_headers(r, header_map, "G数", "ゲーム数")),
                "diff_medals": clean_int(
                    get_by_headers(r, header_map, "差枚", "差枚数", "差メダル", "差枚数(枚)")
                ) if has_diff_column else None,
                "bb": clean_int(get_by_headers(r, header_map, "BB")),
                "rb": clean_int(get_by_headers(r, header_map, "RB")),
                "art": clean_int(get_by_headers(r, header_map, "ART", "AT")),
                "combined_rate": rate_num(combined_text),
                "bb_rate": rate_num(bb_rate_text),
                "rb_rate": rate_num(rb_rate_text),
                "art_rate": rate_num(art_rate_text),
                "combined_rate_text": str(combined_text or ""),
                "bb_rate_text": str(bb_rate_text or ""),
                "rb_rate_text": str(rb_rate_text or ""),
                "art_rate_text": str(art_rate_text or ""),
            }
        )

    if not machine_rows:
        raise ValueError("台別データを1件も取得できませんでした。")

    has_diff_data = has_diff_column and any(
        r["diff_medals"] is not None for r in machine_rows
    )

    summary_table = next(
        (
            t
            for t in raw.get("tables", [])
            if "total_get_medals_table" in str(t.get("class") or "")
            or t.get("id") == "total_get_medals_table"
        ),
        None,
    )

    total_diff = None
    avg_diff = None
    avg_games = None
    win_rate = None
    win_count = None
    machine_count = len(machine_rows)

    if summary_table and len(summary_table.get("rows", [])) >= 2:
        srows = summary_table["rows"]
        smap = build_header_map(srows[0])
        vals = srows[1]

        total_diff = clean_int(get_by_headers(vals, smap, "総差枚"))
        avg_diff = clean_int(get_by_headers(vals, smap, "平均差枚"))
        avg_games = clean_int(get_by_headers(vals, smap, "平均G数", "平均ゲーム数"))
        win_text = get_by_headers(vals, smap, "勝率")
        m = re.search(r"([\d.]+)%\((\d+)/(\d+)\)", str(win_text or ""))
        if m:
            win_rate = float(m.group(1))
            win_count = int(m.group(2))
            machine_count = int(m.group(3))

    if avg_games is None:
        games_values = [r["games"] for r in machine_rows if r["games"] is not None]
        avg_games = round(sum(games_values) / len(games_values)) if games_values else None

    calc_sum = None
    if has_diff_data:
        calc_sum = sum((r["diff_medals"] or 0) for r in machine_rows)
        if total_diff is None:
            total_diff = calc_sum
        if avg_diff is None:
            avg_diff = round(calc_sum / len(machine_rows)) if machine_rows else None
        if win_count is None:
            win_count = sum(1 for r in machine_rows if (r["diff_medals"] or 0) > 0)
        if win_rate is None:
            win_rate = round(win_count / len(machine_rows) * 100, 1) if machine_rows else None
    else:
        # 差枚列がない店舗では、BBなどを差枚として誤登録しない。
        total_diff = None
        avg_diff = None
        win_rate = None
        win_count = None

    notes = []
    if has_diff_data:
        notes.append("差枚列あり")
        if total_diff is not None and calc_sum is not None and total_diff != calc_sum:
            notes.append("店総差枚と台別差枚合計が不一致")
    else:
        notes.append("差枚列なし：差枚系項目は空欄で安全登録")

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
        "has_diff_data": has_diff_data,
        "detected_headers": detected_headers,
        "import_notes": " / ".join(notes),
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
                 bb, rb, art, combined_rate, bb_rate, rb_rate, art_rate,
                 combined_rate_text, bb_rate_text, rb_rate_text, art_rate_text,
                 source_url)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(date, store_id, machine_no) DO UPDATE SET
                    machine_name = EXCLUDED.machine_name,
                    games = EXCLUDED.games,
                    diff_medals = EXCLUDED.diff_medals,
                    bb = EXCLUDED.bb,
                    rb = EXCLUDED.rb,
                    art = EXCLUDED.art,
                    combined_rate = EXCLUDED.combined_rate,
                    bb_rate = EXCLUDED.bb_rate,
                    rb_rate = EXCLUDED.rb_rate,
                    art_rate = EXCLUDED.art_rate,
                    combined_rate_text = EXCLUDED.combined_rate_text,
                    bb_rate_text = EXCLUDED.bb_rate_text,
                    rb_rate_text = EXCLUDED.rb_rate_text,
                    art_rate_text = EXCLUDED.art_rate_text,
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
                        r.get("art"),
                        r["combined_rate"],
                        r["bb_rate"],
                        r["rb_rate"],
                        r.get("art_rate"),
                        r["combined_rate_text"],
                        r["bb_rate_text"],
                        r["rb_rate_text"],
                        r.get("art_rate_text", ""),
                        parsed["source_url"],
                    )
                )
            batch_executemany(cur, machine_sql, params, 1000)

            cur.execute(
                """
                INSERT INTO slot_import_log
                (target_date, store_name, source_url, source_title, exported_at,
                 source_table_count, imported_machine_rows, source_sha256,
                 source_headers, import_notes)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
                    " / ".join(parsed.get("detected_headers", [])),
                    parsed.get("import_notes", ""),
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



def is_juggler_name(value):
    text = str(value or "").strip()
    return ("ジャグラー" in text) or ("JUGGLER" in text.upper())


@st.cache_data(ttl=30)
def load_juggler_spec_map():
    """機種名・別名から設定1～6の公式スペックへ引ける辞書を作る。"""
    rows = query_df(
        """
        SELECT
            m.spec_id,
            m.machine_name,
            m.source_label,
            m.source_url,
            m.note,
            v.setting,
            v.bb_den,
            v.rb_den,
            v.combined_den
        FROM slot_juggler_masters m
        JOIN slot_juggler_spec_values v
          ON v.spec_id = m.spec_id
        ORDER BY m.machine_name, v.setting
        """
    )
    aliases = query_df(
        """
        SELECT a.alias_name, a.spec_id, m.machine_name
        FROM slot_juggler_aliases a
        JOIN slot_juggler_masters m ON m.spec_id = a.spec_id
        ORDER BY a.alias_name
        """
    )

    by_spec = {}
    if not rows.empty:
        for spec_id, group in rows.groupby("spec_id"):
            first = group.iloc[0]
            settings = {}
            for _, r in group.iterrows():
                settings[int(r["setting"])] = {
                    "bb_den": float(r["bb_den"]),
                    "rb_den": float(r["rb_den"]),
                    "combined_den": float(r["combined_den"]),
                }
            by_spec[int(spec_id)] = {
                "spec_id": int(spec_id),
                "machine_name": str(first["machine_name"]),
                "source_label": first.get("source_label"),
                "source_url": first.get("source_url"),
                "note": first.get("note"),
                "settings": settings,
            }

    name_map = {}
    for spec in by_spec.values():
        name_map[spec["machine_name"]] = spec

    if not aliases.empty:
        for _, r in aliases.iterrows():
            spec = by_spec.get(int(r["spec_id"]))
            if spec:
                name_map[str(r["alias_name"])] = spec

    return name_map, by_spec


def get_detected_juggler_names():
    df = query_df(
        """
        SELECT DISTINCT machine_name
        FROM slot_machine_results
        WHERE machine_name IS NOT NULL
          AND (machine_name ILIKE '%%ジャグラー%%' OR machine_name ILIKE '%%JUGGLER%%')
        ORDER BY machine_name
        """
    )
    if df.empty:
        return []
    names = [str(x) for x in df["machine_name"].dropna().tolist()]
    return sorted(set(names), key=kana_sort_key)


def calc_combined_den(bb_den, rb_den):
    try:
        bb = float(bb_den)
        rb = float(rb_den)
        if bb <= 0 or rb <= 0:
            return None
        return 1.0 / ((1.0 / bb) + (1.0 / rb))
    except Exception:
        return None


def poisson_logpmf(k, lam):
    if lam <= 0:
        return -1.0e18
    k = int(max(0, k))
    return k * math.log(lam) - lam - math.lgamma(k + 1)


def juggler_setting_fit(games, bb, rb, spec):
    """
    BB/RB回数を設定1～6の公式確率と比較する。
    独立Poisson近似の相対尤度を0～100%へ正規化する。
    これは実設定の確率や設定確定を意味しない。
    """
    try:
        games = int(games or 0)
        bb = int(bb or 0)
        rb = int(rb or 0)
    except Exception:
        return None

    if games <= 0 or not spec or len(spec.get("settings", {})) < 6:
        return None

    logs = {}
    for setting in range(1, 7):
        vals = spec["settings"].get(setting)
        if not vals:
            return None
        bb_den = float(vals["bb_den"])
        rb_den = float(vals["rb_den"])
        if bb_den <= 0 or rb_den <= 0:
            return None
        lam_bb = games / bb_den
        lam_rb = games / rb_den
        logs[setting] = poisson_logpmf(bb, lam_bb) + poisson_logpmf(rb, lam_rb)

    max_log = max(logs.values())
    weights = {s: math.exp(v - max_log) for s, v in logs.items()}
    denom = sum(weights.values()) or 1.0
    fits = {s: (weights[s] / denom) * 100.0 for s in range(1, 7)}
    best_setting = max(fits, key=fits.get)
    high_fit = fits[5] + fits[6]
    best_fit = fits[best_setting]

    if games >= 6000:
        data_confidence = "高"
        game_factor = 1.0
    elif games >= 3000:
        data_confidence = "中"
        game_factor = 0.75
    elif games >= 1000:
        data_confidence = "低"
        game_factor = 0.45
    else:
        data_confidence = "かなり低い"
        game_factor = 0.20

    if games < 1000 or best_fit < 30:
        judgement = "判別困難"
    else:
        judgement = f"設定{best_setting}寄り"

    total_bonus = bb + rb
    actual_combined = (games / total_bonus) if total_bonus > 0 else None
    aim_score = min(100.0, high_fit * game_factor)

    return {
        "best_setting": best_setting,
        "best_fit": best_fit,
        "high_fit": high_fit,
        "setting6_fit": fits[6],
        "data_confidence": data_confidence,
        "judgement": judgement,
        "actual_combined": actual_combined,
        "aim_score": aim_score,
        "fits": fits,
    }


def save_juggler_master(machine_name, source_label, source_url, note, aliases, spec_rows):
    machine_name = str(machine_name or "").strip()
    if not machine_name:
        raise ValueError("機種名を入力してください。")

    normalized = []
    for row in spec_rows:
        setting = int(row["setting"])
        bb_den = float(row["bb_den"])
        rb_den = float(row["rb_den"])
        combined_den = row.get("combined_den")
        if bb_den <= 0 or rb_den <= 0:
            raise ValueError(f"設定{setting}のBB/RB確率を確認してください。")
        if combined_den in (None, "") or pd.isna(combined_den):
            combined_den = calc_combined_den(bb_den, rb_den)
        combined_den = float(combined_den)
        if combined_den <= 0:
            raise ValueError(f"設定{setting}の合算確率を確認してください。")
        normalized.append((setting, bb_den, rb_den, combined_den))

    settings = {x[0] for x in normalized}
    if settings != set(range(1, 7)):
        raise ValueError("設定1～6をすべて入力してください。")

    aliases = [str(x).strip() for x in aliases if str(x).strip()]
    aliases = sorted(set(x for x in aliases if x != machine_name))

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO slot_juggler_masters
                    (machine_name, source_label, source_url, note, updated_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT(machine_name) DO UPDATE SET
                    source_label = EXCLUDED.source_label,
                    source_url = EXCLUDED.source_url,
                    note = EXCLUDED.note,
                    updated_at = NOW()
                RETURNING spec_id
                """,
                (machine_name, source_label, source_url, note),
            )
            spec_id = int(cur.fetchone()[0])

            cur.execute("DELETE FROM slot_juggler_spec_values WHERE spec_id = %s", (spec_id,))
            for setting, bb_den, rb_den, combined_den in normalized:
                cur.execute(
                    """
                    INSERT INTO slot_juggler_spec_values
                        (spec_id, setting, bb_den, rb_den, combined_den)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (spec_id, setting, bb_den, rb_den, combined_den),
                )

            cur.execute("DELETE FROM slot_juggler_aliases WHERE spec_id = %s", (spec_id,))
            for alias in aliases:
                cur.execute(
                    """
                    INSERT INTO slot_juggler_aliases(alias_name, spec_id)
                    VALUES (%s, %s)
                    ON CONFLICT(alias_name) DO UPDATE SET spec_id = EXCLUDED.spec_id
                    """,
                    (alias, spec_id),
                )
        conn.commit()
    clear_cache()
    return spec_id


def juggler_master_status_df():
    detected = get_detected_juggler_names()
    name_map, _ = load_juggler_spec_map()
    rows = []
    for name in detected:
        spec = name_map.get(name)
        rows.append(
            {
                "機種名": name,
                "登録状況": "登録済み" if spec else "未登録",
                "参照マスタ": spec["machine_name"] if spec else "-",
            }
        )
    return pd.DataFrame(rows)


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



def score_special_event_cached(
    cache,
    target_date,
    events_df,
    selected_tags,
):
    """同じ特定日・イベントタグの過去実績から機種候補を採点する。"""
    if not selected_tags:
        return pd.DataFrame(), []

    event_dates = matching_special_event_dates(
        events_df,
        target_date,
        selected_tags,
    )

    if not event_dates:
        return pd.DataFrame(), []

    _, _, current = strategy_active_info(cache, target_date)
    if current.empty:
        return pd.DataFrame(), event_dates

    active_names = set(
        current["machine_name"]
        .dropna()
        .astype(str)
        .unique()
        .tolist()
    )

    data = cache["data"]
    matched = data[
        data["date"].isin(event_dates)
        & data["machine_name"].astype(str).isin(active_names)
    ].copy()

    if matched.empty:
        return pd.DataFrame(), event_dates

    grouped = (
        matched.groupby("machine_name")
        .agg(
            event_days=("date", "nunique"),
            samples=("machine_no", "size"),
            avg_diff=("diff_medals", "mean"),
            avg_games=("games", "mean"),
            wins=("diff_medals", lambda s: (s > 0).sum()),
        )
        .reset_index()
    )

    grouped["win_rate"] = (
        grouped["wins"]
        / grouped["samples"].replace(0, np.nan)
        * 100
    )

    grouped["_diff"] = rank_score(grouped["avg_diff"], True)
    grouped["_win"] = rank_score(grouped["win_rate"], True)
    grouped["_days"] = rank_score(grouped["event_days"], True)

    grouped["score"] = (
        0.50 * grouped["_diff"]
        + 0.35 * grouped["_win"]
        + 0.15 * grouped["_days"]
    ).round(1)

    return (
        grouped.sort_values(
            ["score", "avg_diff", "win_rate"],
            ascending=[False, False, False],
        ).reset_index(drop=True),
        event_dates,
    )


def build_combined_ranking(
    cache,
    target_date,
    all_machine_df,
    suffix_df,
    blocks_df,
    dip_df,
    uphold_df,
    rotation_df,
    special_event_df=None,
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

    special_active = (
        special_event_df is not None
        and not special_event_df.empty
    )

    if special_active:
        base = base.merge(
            special_event_df[
                ["machine_name", "score"]
            ].rename(
                columns={"score": "special_score"}
            ),
            on="machine_name",
            how="left",
        )
    else:
        base["special_score"] = 0.0

    score_columns = [
        "all_score",
        "suffix_score",
        "block_score",
        "dip_score",
        "uphold_score",
        "rotation_score",
        "special_score",
    ]

    for col in score_columns:
        base[col] = (
            pd.to_numeric(
                base[col],
                errors="coerce",
            )
            .fillna(0.0)
        )

    if special_active:
        weights = {
            "all_score": 0.17,
            "suffix_score": 0.12,
            "block_score": 0.12,
            "dip_score": 0.17,
            "uphold_score": 0.12,
            "rotation_score": 0.10,
            "special_score": 0.20,
        }
    else:
        weights = {
            "all_score": 0.20,
            "suffix_score": 0.15,
            "block_score": 0.15,
            "dip_score": 0.20,
            "uphold_score": 0.15,
            "rotation_score": 0.15,
            "special_score": 0.0,
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
        "special_score": "特定日",
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
seed_special_event_defaults()

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
        "ジャグラー設定判別",
        "ジャグラー公式スペック管理",
        "店舗別イベント管理",
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
            COUNT(total_diff_medals) AS diff_days,
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
    if int(summary.get("diff_days") or 0) > 0:
        c4.metric("期間総差枚", f"{int(summary['total_diff'] or 0):+,}枚")
    else:
        c4.metric("期間総差枚", "差枚データなし")

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
        if daily["total_diff_medals"].notna().any():
            st.bar_chart(
                daily.set_index("date")[["total_diff_medals"]],
                use_container_width=True,
            )
        else:
            st.info("この店舗のJSONには差枚列がないため、総差枚グラフは表示しません。")
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
        "「アナスロ保存」で取得したJSONを複数店舗まとめて登録できます。"
        "ページタイトルから店舗名を自動判定し、日付 × 店舗 × 台番号で完全に分けて保存します。"
    )
    st.caption(
        "店舗によって列順や項目が違っても、列名で自動判定します。"
        "差枚列がない店舗はBB・RB・ART/AT・合算などだけを保存し、差枚を誤推測しません。"
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
                row_count_ok = len(parsed["machine_rows"]) == parsed["machine_count"]
                if parsed.get("has_diff_data"):
                    diff_ok = parsed["calc_sum"] == parsed["total_diff_medals"]
                    check_label = "OK" if row_count_ok and diff_ok else "要確認"
                else:
                    check_label = "差枚列なし（安全取込）" if row_count_ok else "要確認"

                previews.append(
                    {
                        "ファイル": f.name,
                        "日付": parsed["date"],
                        "店舗": parsed["store_name"],
                        "台数": len(parsed["machine_rows"]),
                        "総差枚": parsed["total_diff_medals"],
                        "台別合計": parsed["calc_sum"],
                        "検出列": " / ".join(parsed.get("detected_headers", [])),
                        "照合": check_label,
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
                        "検出列": "",
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
        c1, c2, c3 = st.columns(3)
        if df["total_diff_medals"].notna().any():
            total = int(df["total_diff_medals"].dropna().sum())
            avg = round(df["total_diff_medals"].dropna().mean())
            c1.metric("期間総差枚", f"{total:+,}枚")
            c2.metric("1日平均差枚", f"{avg:+,}枚")
        else:
            c1.metric("期間総差枚", "差枚データなし")
            c2.metric("1日平均差枚", "差枚データなし")
        c3.metric("対象日数", f"{len(df)}日")

        chart_df = df.copy()
        chart_df["date"] = pd.to_datetime(chart_df["date"])
        if chart_df["total_diff_medals"].notna().any():
            st.bar_chart(
                chart_df.set_index("date")[["total_diff_medals"]],
                use_container_width=True,
            )
        else:
            st.info("この店舗のJSONには差枚列がないため、差枚グラフは表示しません。")
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
            COUNT(diff_medals) AS diff_records,
            COUNT(DISTINCT date) AS days,
            COUNT(DISTINCT machine_no) AS machines,
            SUM(diff_medals) AS total_diff_medals,
            ROUND(AVG(diff_medals), 1) AS avg_diff_medals,
            ROUND(AVG(games), 1) AS avg_games,
            SUM(COALESCE(bb, 0)) AS bb_total,
            SUM(COALESCE(rb, 0)) AS rb_total,
            SUM(COALESCE(art, 0)) AS art_total,
            ROUND(
                SUM(COALESCE(games, 0))::numeric
                / NULLIF(
                    SUM(COALESCE(bb, 0))
                    + SUM(COALESCE(rb, 0))
                    + SUM(COALESCE(art, 0)),
                    0
                ),
                1
            ) AS combined_rate_period,
            CASE
                WHEN COUNT(diff_medals) = 0 THEN NULL
                ELSE SUM(CASE WHEN diff_medals > 0 THEN 1 ELSE 0 END)
            END AS win_count,
            ROUND(
                100.0 * SUM(CASE WHEN diff_medals > 0 THEN 1 ELSE 0 END)
                / NULLIF(COUNT(diff_medals), 0),
                1
            ) AS win_rate
        FROM slot_machine_results
        WHERE store_id = %s AND date BETWEEN %s AND %s
        GROUP BY machine_name
        ORDER BY total_diff_medals DESC NULLS LAST, machine_name
        """,
        (store_id, start_date, end_date),
    )

    if not df.empty and pd.to_numeric(df["diff_records"], errors="coerce").fillna(0).sum() == 0:
        st.info(
            "この店舗は差枚列がないため、総差枚・平均差枚・勝率は空欄です。"
            "G数・BB・RB・ART/AT・合算はそのまま確認できます。"
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

    df_display = filtered_df.copy()
    df_display["combined_rate_period"] = df_display["combined_rate_period"].apply(
        lambda x: f"1/{float(x):.1f}" if pd.notna(x) and float(x) > 0 else "-"
    )
    df_display = df_display.rename(
        columns={
            "machine_name": "機種名",
            "records": "データ件数",
            "diff_records": "差枚あり件数",
            "days": "データ日数",
            "machines": "台数",
            "total_diff_medals": "総差枚",
            "avg_diff_medals": "平均差枚",
            "avg_games": "平均G数",
            "bb_total": "BB",
            "rb_total": "RB",
            "art_total": "ART/AT",
            "combined_rate_period": "合算",
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
            COUNT(diff_medals) AS diff_days,
            MIN(machine_name) AS machine_name_example,
            SUM(diff_medals) AS total_diff_medals,
            ROUND(AVG(diff_medals), 1) AS avg_diff_medals,
            ROUND(AVG(games), 1) AS avg_games,

            SUM(COALESCE(bb, 0)) AS bb_total,
            SUM(COALESCE(rb, 0)) AS rb_total,
            SUM(COALESCE(art, 0)) AS art_total,

            ROUND(
                SUM(COALESCE(games, 0))::numeric
                / NULLIF(
                    SUM(COALESCE(bb, 0))
                    + SUM(COALESCE(rb, 0))
                    + SUM(COALESCE(art, 0)),
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

            ROUND(
                SUM(COALESCE(games, 0))::numeric
                / NULLIF(SUM(COALESCE(art, 0)), 0),
                1
            ) AS art_rate_period,

            CASE
                WHEN COUNT(diff_medals) = 0 THEN NULL
                ELSE SUM(CASE WHEN diff_medals > 0 THEN 1 ELSE 0 END)
            END AS win_count,

            ROUND(
                100.0 * SUM(CASE WHEN diff_medals > 0 THEN 1 ELSE 0 END)
                / NULLIF(COUNT(diff_medals), 0),
                1
            ) AS win_rate

        FROM slot_machine_results

        WHERE
            store_id = %s
            AND date BETWEEN %s AND %s

        GROUP BY machine_no

        ORDER BY total_diff_medals DESC NULLS LAST, machine_no
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

    has_diff_rows = (pd.to_numeric(df.get("diff_days"), errors="coerce").fillna(0) > 0).any()
    if has_diff_rows:
        sort_options = [
            "総差枚が高い順",
            "平均差枚が高い順",
            "勝率が高い順",
            "平均G数が高い順",
            "合算が良い順",
            "台番号順",
        ]
    else:
        sort_options = ["平均G数が高い順", "合算が良い順", "台番号順"]
        st.info(
            "この店舗は差枚列がないため、差枚・勝率の並び替えは表示していません。"
            "BB・RB・ART/AT・合算は確認できます。"
        )

    sort_choice = c2.selectbox("並び順", sort_options)

    df = df[df["days"] >= min_days]

    if sort_choice == "総差枚が高い順":
        df = df.sort_values("total_diff_medals", ascending=False, na_position="last")
    elif sort_choice == "平均差枚が高い順":
        df = df.sort_values("avg_diff_medals", ascending=False, na_position="last")
    elif sort_choice == "勝率が高い順":
        df = df.sort_values("win_rate", ascending=False, na_position="last")
    elif sort_choice == "平均G数が高い順":
        df = df.sort_values("avg_games", ascending=False, na_position="last")
    elif sort_choice == "合算が良い順":
        df = df.sort_values("combined_rate_period", ascending=True, na_position="last")
    else:
        df = df.sort_values("machine_no")

    df_display = df.copy()

    for col in [
        "combined_rate_period",
        "bb_rate_period",
        "rb_rate_period",
        "art_rate_period",
    ]:
        df_display[col] = df_display[col].apply(
            lambda x: f"1/{float(x):.1f}" if pd.notna(x) and float(x) > 0 else "-"
        )

    df_display = df_display.rename(
        columns={
            "machine_no": "台番号",
            "days": "データ日数",
            "diff_days": "差枚あり日数",
            "machine_name_example": "機種名",
            "total_diff_medals": "総差枚",
            "avg_diff_medals": "平均差枚",
            "avg_games": "平均G数",
            "bb_total": "BB",
            "rb_total": "RB",
            "art_total": "ART/AT",
            "combined_rate_period": "合算",
            "bb_rate_period": "BB確率",
            "rb_rate_period": "RB確率",
            "art_rate_period": "ART/AT確率",
            "win_count": "勝ち回数",
            "win_rate": "勝率(%)",
        }
    )

    st.dataframe(
        df_display,
        use_container_width=True,
        hide_index=True,
    )


elif menu == "ジャグラー設定判別":
    st.subheader("🤡 ジャグラー設定判別")
    st.write(
        "公式スペックマスタの設定1～6と、実際のG数・BB・RBを比較して、"
        "各設定への相対的な適合度を表示します。"
    )
    st.caption(
        "実設定を確定する機能ではありません。特にG数が少ない台は判別のブレが大きいため、"
        "『データ信頼度』と一緒に見てください。"
    )

    store_id, store_name = store_selector()
    if store_id is None:
        st.info("店舗データがありません。")
        st.stop()

    juggler_dates = query_df(
        """
        SELECT DISTINCT date
        FROM slot_machine_results
        WHERE store_id = %s
          AND machine_name IS NOT NULL
          AND (machine_name ILIKE '%%ジャグラー%%' OR machine_name ILIKE '%%JUGGLER%%')
        ORDER BY date
        """,
        (store_id,),
    )
    if juggler_dates.empty:
        st.info("この店舗にはジャグラー系のデータがまだありません。")
        st.stop()

    mode = st.radio(
        "判別方法",
        ["1日ごとに判別", "期間合計で判別"],
        horizontal=True,
        key="juggler_judge_mode",
    )

    available_dates = [x for x in juggler_dates["date"].tolist()]

    if mode == "1日ごとに判別":
        target_date = st.selectbox(
            "判別日",
            options=list(reversed(available_dates)),
            format_func=lambda x: str(x),
            key="juggler_single_date",
        )
        raw = query_df(
            """
            SELECT date, machine_no, machine_name, games, bb, rb,
                   combined_rate, combined_rate_text
            FROM slot_machine_results
            WHERE store_id = %s
              AND date = %s
              AND machine_name IS NOT NULL
              AND (machine_name ILIKE '%%ジャグラー%%' OR machine_name ILIKE '%%JUGGLER%%')
            ORDER BY machine_no
            """,
            (store_id, target_date),
        )
    else:
        min_date = min(available_dates)
        max_date = max(available_dates)
        selected_range = st.date_input(
            "集計期間",
            value=(min_date, max_date),
            min_value=min_date,
            max_value=max_date,
            key="juggler_period_dates",
        )
        if isinstance(selected_range, (tuple, list)) and len(selected_range) == 2:
            start_date, end_date = selected_range
        else:
            start_date, end_date = min_date, max_date

        raw = query_df(
            """
            SELECT
                machine_no,
                machine_name,
                SUM(COALESCE(games, 0)) AS games,
                SUM(COALESCE(bb, 0)) AS bb,
                SUM(COALESCE(rb, 0)) AS rb,
                COUNT(*) AS data_days
            FROM slot_machine_results
            WHERE store_id = %s
              AND date BETWEEN %s AND %s
              AND machine_name IS NOT NULL
              AND (machine_name ILIKE '%%ジャグラー%%' OR machine_name ILIKE '%%JUGGLER%%')
            GROUP BY machine_no, machine_name
            ORDER BY machine_no
            """,
            (store_id, start_date, end_date),
        )

    if raw.empty:
        st.info("対象期間にジャグラーデータがありません。")
        st.stop()

    machine_options = sorted(raw["machine_name"].dropna().astype(str).unique().tolist(), key=kana_sort_key)
    selected_machines = st.multiselect(
        "機種を絞り込み（複数選択可・カナ順）",
        options=machine_options,
        placeholder="未選択なら全ジャグラーを表示",
    )
    if selected_machines:
        raw = raw[raw["machine_name"].isin(selected_machines)].copy()

    name_map, _ = load_juggler_spec_map()
    display_rows = []
    missing_specs = set()

    for _, r in raw.iterrows():
        name = str(r.get("machine_name") or "")
        spec = name_map.get(name)
        if not spec:
            missing_specs.add(name)
            display_rows.append(
                {
                    "台番号": int(r["machine_no"]),
                    "機種名": name,
                    "G数": int(r.get("games") or 0),
                    "BB": int(r.get("bb") or 0),
                    "RB": int(r.get("rb") or 0),
                    "実績合算": "-",
                    "最適合": "公式スペック未登録",
                    "設定5以上適合度(%)": None,
                    "設定6適合度(%)": None,
                    "狙い参考点": None,
                    "データ信頼度": "-",
                    **{f"設定{s}適合度(%)": None for s in range(1, 7)},
                }
            )
            continue

        fit = juggler_setting_fit(r.get("games"), r.get("bb"), r.get("rb"), spec)
        if not fit:
            continue

        actual_combined = fit["actual_combined"]
        row = {
            "台番号": int(r["machine_no"]),
            "機種名": name,
            "G数": int(r.get("games") or 0),
            "BB": int(r.get("bb") or 0),
            "RB": int(r.get("rb") or 0),
            "実績合算": f"1/{actual_combined:.1f}" if actual_combined else "-",
            "最適合": fit["judgement"],
            "設定5以上適合度(%)": round(fit["high_fit"], 1),
            "設定6適合度(%)": round(fit["setting6_fit"], 1),
            "狙い参考点": round(fit["aim_score"], 1),
            "データ信頼度": fit["data_confidence"],
        }
        for s in range(1, 7):
            row[f"設定{s}適合度(%)"] = round(fit["fits"][s], 1)
        if "data_days" in r.index:
            row["データ日数"] = int(r.get("data_days") or 0)
        display_rows.append(row)

    result = pd.DataFrame(display_rows)
    if result.empty:
        st.info("判別できるデータがありません。")
        st.stop()

    if missing_specs:
        st.warning(
            "公式スペック未登録の機種があります："
            + "、".join(sorted(missing_specs, key=kana_sort_key))
        )
        st.caption("左メニューの『ジャグラー公式スペック管理』から設定1～6を登録すると判別できます。")

    c1, c2, c3 = st.columns(3)
    registered_mask = result["設定5以上適合度(%)"].notna()
    c1.metric("対象台数", f"{len(result):,}台")
    c2.metric("判別可能", f"{int(registered_mask.sum()):,}台")
    if registered_mask.any():
        top_score = result.loc[registered_mask, "狙い参考点"].max()
        c3.metric("最高 狙い参考点", f"{float(top_score):.1f}点")
    else:
        c3.metric("最高 狙い参考点", "-")

    sort_choice = st.selectbox(
        "並び順",
        ["狙い参考点が高い順", "設定5以上適合度が高い順", "G数が多い順", "台番号順"],
    )
    if sort_choice == "狙い参考点が高い順":
        result = result.sort_values(["狙い参考点", "G数"], ascending=[False, False], na_position="last")
    elif sort_choice == "設定5以上適合度が高い順":
        result = result.sort_values(["設定5以上適合度(%)", "G数"], ascending=[False, False], na_position="last")
    elif sort_choice == "G数が多い順":
        result = result.sort_values("G数", ascending=False)
    else:
        result = result.sort_values("台番号")

    base_cols = ["台番号", "機種名", "G数", "BB", "RB", "実績合算", "最適合",
                 "設定5以上適合度(%)", "設定6適合度(%)", "狙い参考点", "データ信頼度"]
    if "データ日数" in result.columns:
        base_cols.insert(2, "データ日数")
    st.dataframe(result[base_cols], use_container_width=True, hide_index=True)

    with st.expander("設定1～6の適合度を全部見る"):
        detail_cols = ["台番号", "機種名", "G数"] + [f"設定{s}適合度(%)" for s in range(1, 7)]
        st.dataframe(result[detail_cols], use_container_width=True, hide_index=True)

    st.caption(
        "狙い参考点は『設定5以上への相対適合度 × G数による信頼度補正』です。"
        "実際の設定や翌日の投入を保証する数字ではありません。"
    )


elif menu == "ジャグラー公式スペック管理":
    st.subheader("📘 ジャグラー公式スペック管理")
    st.write(
        "ジャグラーの設定1～6について、公式BB確率・RB確率・合算を登録します。"
        "一度登録すれば全店舗で共通利用できます。新台もここへ追加するだけで対応できます。"
    )

    if not admin_gate():
        st.stop()

    status_df = juggler_master_status_df()
    if not status_df.empty:
        st.markdown("#### DBで検出したジャグラー")
        st.dataframe(status_df, use_container_width=True, hide_index=True)
    else:
        st.info("まだDB内にジャグラー系機種がありません。手入力で先にマスタ登録することもできます。")

    detected = get_detected_juggler_names()
    options = ["（新しい機種名を手入力）"] + detected
    selected = st.selectbox(
        "登録・更新する機種",
        options=options,
        key="juggler_master_target",
    )

    name_map, by_spec = load_juggler_spec_map()
    existing_spec = None if selected == "（新しい機種名を手入力）" else name_map.get(selected)

    default_name = existing_spec["machine_name"] if existing_spec else ("" if selected.startswith("（") else selected)
    machine_name = st.text_input("正式な機種名", value=default_name)

    existing_aliases = []
    if existing_spec:
        alias_df = query_df(
            "SELECT alias_name FROM slot_juggler_aliases WHERE spec_id = %s ORDER BY alias_name",
            (existing_spec["spec_id"],),
        )
        if not alias_df.empty:
            existing_aliases = alias_df["alias_name"].astype(str).tolist()

    alias_text = st.text_area(
        "別名・表記揺れ（任意）",
        value="\n".join(existing_aliases),
        help="1行に1つ。別店舗で機種名表記が違う場合に同じ公式マスタへ紐づけます。",
    )
    source_label = st.text_input(
        "情報元名",
        value=(str(existing_spec.get("source_label") or "") if existing_spec else ""),
        placeholder="例：北電子公式",
    )
    source_url = st.text_input(
        "公式URL（任意）",
        value=(str(existing_spec.get("source_url") or "") if existing_spec else ""),
    )
    note = st.text_area(
        "メモ（任意）",
        value=(str(existing_spec.get("note") or "") if existing_spec else ""),
    )

    existing_values = {}
    if existing_spec:
        existing_values = existing_spec.get("settings", {})

    editor_rows = []
    for s in range(1, 7):
        vals = existing_values.get(s, {})
        editor_rows.append(
            {
                "設定": s,
                "BB確率分母": vals.get("bb_den"),
                "RB確率分母": vals.get("rb_den"),
                "合算分母": vals.get("combined_den"),
            }
        )
    editor_df = pd.DataFrame(editor_rows)

    st.markdown("#### 設定1～6 公式スペック")
    st.caption("例：BB 1/273.1 の場合は『273.1』と入力します。合算が空欄ならBB・RBから自動計算します。")
    edited = st.data_editor(
        editor_df,
        use_container_width=True,
        hide_index=True,
        disabled=["設定"],
        num_rows="fixed",
        key=f"juggler_spec_editor_{existing_spec['spec_id'] if existing_spec else 'new'}_{selected}",
        column_config={
            "設定": st.column_config.NumberColumn("設定", step=1),
            "BB確率分母": st.column_config.NumberColumn("BB確率分母", min_value=1.0, format="%.2f"),
            "RB確率分母": st.column_config.NumberColumn("RB確率分母", min_value=1.0, format="%.2f"),
            "合算分母": st.column_config.NumberColumn("合算分母", min_value=1.0, format="%.2f"),
        },
    )

    if st.button("この公式スペックを保存", type="primary"):
        try:
            spec_rows = []
            for _, r in edited.iterrows():
                spec_rows.append(
                    {
                        "setting": int(r["設定"]),
                        "bb_den": r["BB確率分母"],
                        "rb_den": r["RB確率分母"],
                        "combined_den": r["合算分母"],
                    }
                )
            aliases = re.split(r"[\n,、]+", alias_text or "")
            save_juggler_master(
                machine_name=machine_name,
                source_label=source_label,
                source_url=source_url,
                note=note,
                aliases=aliases,
                spec_rows=spec_rows,
            )
            st.success(f"{machine_name} の設定1～6スペックを保存しました。")
            st.rerun()
        except Exception as e:
            st.error(f"保存できませんでした：{e}")

    st.markdown("#### 登録済み公式スペック一覧")
    master_list = query_df(
        """
        SELECT
            m.machine_name AS "機種名",
            v.setting AS "設定",
            v.bb_den AS "BB確率分母",
            v.rb_den AS "RB確率分母",
            v.combined_den AS "合算分母",
            m.source_label AS "情報元"
        FROM slot_juggler_masters m
        JOIN slot_juggler_spec_values v ON v.spec_id = m.spec_id
        ORDER BY m.machine_name, v.setting
        """
    )
    if master_list.empty:
        st.info("公式スペックはまだ登録されていません。")
    else:
        st.dataframe(master_list, use_container_width=True, hide_index=True)


elif menu == "店舗別イベント管理":
    st.subheader("🏬 店舗別イベント管理")
    st.write(
        "イベントは店舗ごとに完全分離して保存します。"
        "今日のイベントだけでなく、過去の日付も後から何件でも登録できます。"
    )
    st.caption(
        "同じ店舗・同じ日に複数イベントが重なっていても別々に登録できます。"
        "新橋のイベントが三田など別店舗の分析へ混ざることはありません。"
    )

    is_admin = admin_gate()

    if is_admin:
        with st.expander("➕ まだ台データがない店舗を先に追加"):
            new_store_name = st.text_input(
                "新しい店舗名",
                placeholder="例：ピーアーク三田",
                key="event_new_store_name",
            )
            if st.button("店舗を追加", key="event_add_store"):
                try:
                    create_store_by_name(new_store_name)
                    st.success(f"店舗「{new_store_name.strip()}」を追加しました。")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

    store_id, store_name = store_selector()
    if store_id is None:
        st.info("店舗がありません。JSONを登録するか、上の『店舗を追加』から作成してください。")
        st.stop()

    st.success(f"現在の店舗：{store_name}")

    events = query_df(
        """
        SELECT
            e.event_id,
            e.date,
            e.event_name,
            e.event_tags,
            e.full_machine_names,
            e.half_machine_names,
            e.tail_targets,
            e.line_targets,
            e.other_features,
            e.source_label,
            e.confidence,
            e.note,
            d.total_diff_medals,
            d.avg_games,
            d.win_rate
        FROM slot_store_events e
        LEFT JOIN slot_daily_store_summary d
          ON d.store_id = e.store_id
         AND d.date = e.date
        WHERE e.store_id = %s
        ORDER BY e.date DESC, e.event_id DESC
        """,
        (store_id,),
    )

    st.markdown("#### 登録済みイベント履歴")
    if events.empty:
        st.info("この店舗のイベントはまだ登録されていません。")
    else:
        display = events.copy().rename(
            columns={
                "date": "日付",
                "event_name": "イベント名",
                "event_tags": "分析タグ",
                "full_machine_names": "全台系機種",
                "half_machine_names": "1/2系機種",
                "tail_targets": "末尾",
                "line_targets": "並び",
                "other_features": "その他仕掛け",
                "total_diff_medals": "実データ総差枚",
                "avg_games": "実データ平均G数",
                "win_rate": "実データ勝率(%)",
                "confidence": "確度",
                "source_label": "情報元",
                "note": "メモ",
            }
        )
        st.dataframe(
            display[
                [
                    "日付",
                    "イベント名",
                    "分析タグ",
                    "全台系機種",
                    "1/2系機種",
                    "末尾",
                    "並び",
                    "その他仕掛け",
                    "実データ総差枚",
                    "実データ平均G数",
                    "実データ勝率(%)",
                    "確度",
                    "情報元",
                    "メモ",
                ]
            ],
            use_container_width=True,
            hide_index=True,
        )

        st.markdown("#### イベント名ごとの店全体実績")
        stats_source = events.dropna(subset=["total_diff_medals"]).copy()
        if stats_source.empty:
            st.info("この店舗はイベント日と差枚データがまだ重なっていません。")
        else:
            stats = (
                stats_source.groupby("event_name")
                .agg(
                    開催回数=("date", "nunique"),
                    平均総差枚=("total_diff_medals", "mean"),
                    プラス回数=("total_diff_medals", lambda s: (s > 0).sum()),
                    平均勝率=("win_rate", "mean"),
                )
                .reset_index()
                .rename(columns={"event_name": "イベント名"})
            )
            stats["プラス率(%)"] = (
                stats["プラス回数"]
                / stats["開催回数"].replace(0, np.nan)
                * 100
            ).round(1)
            stats["平均総差枚"] = stats["平均総差枚"].round(0)
            stats["平均勝率"] = stats["平均勝率"].round(1)
            st.dataframe(
                stats[
                    ["イベント名", "開催回数", "平均総差枚", "プラス率(%)", "平均勝率"]
                ].sort_values(["開催回数", "平均総差枚"], ascending=[False, False]),
                use_container_width=True,
                hide_index=True,
            )

    if is_admin:
        st.markdown("---")
        st.markdown("#### ➕ 新しいイベントを登録（過去日も可）")
        with st.form("new_store_event_form", clear_on_submit=True):
            event_date = st.date_input(
                "日付",
                value=datetime.now().date(),
                key="new_event_date",
            )
            event_name = st.text_input(
                "イベント名",
                placeholder="例：7の付く日、スロパチ、THANK YOU",
                key="new_event_name",
            )
            event_tags = st.text_input(
                "分析タグ（複数はカンマ区切り）",
                placeholder="例：7の付く日,ぶちアゲWeek",
                key="new_event_tags",
            )

            c1, c2 = st.columns(2)
            full_machine_names = c1.text_area(
                "全台系機種",
                placeholder="分かる範囲で。複数は改行またはカンマ区切り",
                key="new_event_full",
            )
            half_machine_names = c2.text_area(
                "1/2系・半台系機種",
                placeholder="分かる範囲で入力",
                key="new_event_half",
            )

            c3, c4 = st.columns(2)
            tail_targets = c3.text_input(
                "末尾",
                placeholder="例：7、末尾99 など",
                key="new_event_tail",
            )
            line_targets = c4.text_input(
                "並び",
                placeholder="例：3台並び、4台並び複数",
                key="new_event_line",
            )

            other_features = st.text_area(
                "その他仕掛け",
                placeholder="角、塊、1/3、列など分かる内容",
                key="new_event_other",
            )

            c5, c6 = st.columns(2)
            source_label = c5.text_input(
                "情報元",
                value="手入力",
                key="new_event_source",
            )
            confidence = c6.selectbox(
                "確度",
                ["確定", "画像から判読", "手入力", "要確認"],
                index=2,
                key="new_event_confidence",
            )
            note = st.text_area("メモ", key="new_event_note")

            submitted = st.form_submit_button("このイベントを保存", type="primary")
            if submitted:
                try:
                    save_store_event(
                        store_id=store_id,
                        event_date=event_date,
                        event_name=event_name,
                        event_tags=event_tags or event_name,
                        full_machine_names=full_machine_names,
                        half_machine_names=half_machine_names,
                        tail_targets=tail_targets,
                        line_targets=line_targets,
                        other_features=other_features,
                        source_label=source_label,
                        confidence=confidence,
                        note=note,
                    )
                    st.success("イベントを保存しました。")
                    st.rerun()
                except Exception as e:
                    st.error(f"保存できませんでした: {e}")

        if not events.empty:
            st.markdown("---")
            st.markdown("#### ✏️ 登録済みイベントを修正・削除")

            event_options = events[["event_id", "date", "event_name"]].copy()
            event_options["label"] = event_options.apply(
                lambda r: f"{r['date']} ｜ {r['event_name']}", axis=1
            )
            label_to_id = dict(zip(event_options["label"], event_options["event_id"]))
            selected_label = st.selectbox(
                "修正するイベント",
                event_options["label"].tolist(),
                key="edit_event_selector",
            )
            selected_event_id = int(label_to_id[selected_label])
            current = events.loc[events["event_id"] == selected_event_id].iloc[0]

            edit_date = st.date_input(
                "日付（修正）",
                value=pd.Timestamp(current["date"]).date(),
                key=f"edit_event_date_{selected_event_id}",
            )
            edit_name = st.text_input(
                "イベント名（修正）",
                value=text_or_blank(current["event_name"]),
                key=f"edit_event_name_{selected_event_id}",
            )
            edit_tags = st.text_input(
                "分析タグ（修正）",
                value=text_or_blank(current["event_tags"]),
                key=f"edit_event_tags_{selected_event_id}",
            )

            e1, e2 = st.columns(2)
            edit_full = e1.text_area(
                "全台系機種（修正）",
                value=text_or_blank(current["full_machine_names"]),
                key=f"edit_event_full_{selected_event_id}",
            )
            edit_half = e2.text_area(
                "1/2系・半台系機種（修正）",
                value=text_or_blank(current["half_machine_names"]),
                key=f"edit_event_half_{selected_event_id}",
            )

            e3, e4 = st.columns(2)
            edit_tail = e3.text_input(
                "末尾（修正）",
                value=text_or_blank(current["tail_targets"]),
                key=f"edit_event_tail_{selected_event_id}",
            )
            edit_line = e4.text_input(
                "並び（修正）",
                value=text_or_blank(current["line_targets"]),
                key=f"edit_event_line_{selected_event_id}",
            )
            edit_other = st.text_area(
                "その他仕掛け（修正）",
                value=text_or_blank(current["other_features"]),
                key=f"edit_event_other_{selected_event_id}",
            )
            edit_source = st.text_input(
                "情報元（修正）",
                value=text_or_blank(current["source_label"]),
                key=f"edit_event_source_{selected_event_id}",
            )
            edit_confidence = st.text_input(
                "確度（修正）",
                value=text_or_blank(current["confidence"]),
                key=f"edit_event_confidence_{selected_event_id}",
            )
            edit_note = st.text_area(
                "メモ（修正）",
                value=text_or_blank(current["note"]),
                key=f"edit_event_note_{selected_event_id}",
            )

            b1, b2 = st.columns(2)
            if b1.button("変更を保存", type="primary", key="update_store_event"):
                try:
                    save_store_event(
                        store_id=store_id,
                        event_id=selected_event_id,
                        event_date=edit_date,
                        event_name=edit_name,
                        event_tags=edit_tags or edit_name,
                        full_machine_names=edit_full,
                        half_machine_names=edit_half,
                        tail_targets=edit_tail,
                        line_targets=edit_line,
                        other_features=edit_other,
                        source_label=edit_source,
                        confidence=edit_confidence,
                        note=edit_note,
                    )
                    st.success("変更を保存しました。")
                    st.rerun()
                except Exception as e:
                    st.error(f"変更できませんでした: {e}")

            if b2.button("このイベントを削除", key="delete_store_event_button"):
                delete_store_event(store_id, selected_event_id)
                st.success("イベントを削除しました。")
                st.rerun()


elif menu == "狙い分析":
    st.subheader("🎯 狙い分析")
    st.write(
        "過去データから、全台系・末尾・並び・凹み・上げ/据え・"
        "機種ローテに加えて、特定日・イベント傾向も候補点へ反映します。"
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
            art,
            combined_rate,
            bb_rate,
            rb_rate,
            art_rate
        FROM slot_machine_results
        WHERE store_id = %s
        ORDER BY date, machine_no
        """,
        (store_id,),
    )

    if analysis_data.empty:
        st.info("台別データがありません。")
        st.stop()

    diff_available_rows = pd.to_numeric(
        analysis_data["diff_medals"], errors="coerce"
    ).notna().sum()
    if diff_available_rows == 0:
        st.warning(
            "この店舗のアナスロJSONには差枚列がありません。"
            "BB・RB・ART/AT・合算などは保存されていますが、"
            "全台系・末尾・凹み・並びなど差枚を使う狙い分析は誤判定防止のため実行しません。"
        )
        st.info("台番号別分析ではBB・RBなどの実績を確認できます。")
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


    special_events = query_df(
        """
        SELECT
            date,
            event_name,
            event_tags,
            source_label,
            confidence,
            note
        FROM slot_store_events
        WHERE store_id = %s
        ORDER BY date
        """,
        (store_id,),
    )

    available_event_tags = get_special_event_tags(
        special_events
    )

    default_event_tags = []
    if not special_events.empty:
        exact_target = special_events[
            pd.to_datetime(special_events["date"]).dt.date
            == target_date
        ]
        if not exact_target.empty:
            tag_set = set()
            for value in exact_target["event_tags"].fillna(""):
                tag_set.update(split_event_tags(value))
            default_event_tags = sorted(tag_set, key=kana_sort_key)

    selected_event_tags = st.multiselect(
        "今回の特定日・イベント（複数選択可）",
        options=available_event_tags,
        default=[
            tag
            for tag in default_event_tags
            if tag in available_event_tags
        ],
        help=(
            "例：7の付く日＋ぶちアゲWeek。"
            "複数選んだ場合は、過去にそのタグがすべて重なった日だけで分析します。"
        ),
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

        special_event_candidates, matched_event_dates = (
            score_special_event_cached(
                strategy_cache,
                target_date,
                special_events,
                selected_event_tags,
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
            special_event_candidates,
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
            "特定日",
            "バックテスト",
        ]
    )

    with tabs[0]:
        st.markdown("#### 総合狙いランキング")
        if selected_event_tags and not special_event_candidates.empty:
            st.caption(
                "今回は特定日傾向を20%反映。"
                "全台17%・末尾12%・並び12%・凹み17%・"
                "上げ/据え12%・機種ローテ10%・特定日20%です。"
            )
        else:
            st.caption(
                "特定日指定なしの場合は、全台20%・末尾15%・並び15%・"
                "凹み20%・上げ/据え15%・機種ローテ15%です。"
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
                    "special_score": "特定日点",
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
                    "特定日点",
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
        st.markdown("#### 特定日・イベント機種候補")

        if not selected_event_tags:
            st.info(
                "上の「今回の特定日・イベント」からイベントを選ぶと、"
                "同じ特定日の過去実績を機種ごとに集計します。"
            )
        elif not matched_event_dates:
            st.info(
                "この組み合わせに一致する過去開催日がありません。"
            )
        else:
            st.write(
                "選択中："
                + " ＋ ".join(selected_event_tags)
            )
            st.caption(
                "過去の一致日："
                + "、".join(
                    pd.Timestamp(d).strftime("%Y/%m/%d")
                    for d in matched_event_dates
                )
            )

            if special_event_candidates.empty:
                st.info(
                    "一致する開催日はありますが、現在の台別DBと重なる実績がありません。"
                )
            else:
                display = special_event_candidates.copy()
                display.insert(
                    0,
                    "順位",
                    range(1, len(display) + 1),
                )
                display = display.rename(
                    columns={
                        "machine_name": "機種名",
                        "event_days": "一致開催日数",
                        "samples": "台別サンプル数",
                        "avg_diff": "一致日平均差枚",
                        "avg_games": "一致日平均G数",
                        "win_rate": "一致日勝率(%)",
                        "score": "特定日点",
                    }
                )

                st.dataframe(
                    display[
                        [
                            "順位",
                            "機種名",
                            "特定日点",
                            "一致開催日数",
                            "一致日平均差枚",
                            "一致日勝率(%)",
                            "一致日平均G数",
                            "台別サンプル数",
                        ]
                    ].head(40),
                    use_container_width=True,
                    hide_index=True,
                )

                st.caption(
                    "この特定日点は画像の機種名を推測したものではありません。"
                    "登録済みアナスロ台別実績から、同じイベント日に実際に強かった機種を集計しています。"
                )

    with tabs[8]:
        st.markdown("#### 過去データでバックテスト")
        st.write(
            "全台系・末尾・並び・凹み・上げ/据え・機種ローテの6種類について、"
            "各日より前のデータだけで候補を作り、実際の当日結果と照合します。"
        )
        st.caption(
            "特定日分析はイベント名が付いている日のみ母数が増えるため、"
            "現在は上の「特定日」タブで開催日数と実績を別表示しています。"
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

