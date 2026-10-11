"""SLOT EDGE ミニゲーム（NeonのDBを読み取るだけ）。"""
from __future__ import annotations

import json
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components


SQL = """
WITH latest AS (
    SELECT store_id, MAX(date) AS latest_date
    FROM slot_machine_results
    GROUP BY store_id
)
SELECT s.store_id, s.store_name, latest.latest_date, r.machine_name
FROM latest
JOIN slot_stores AS s ON s.store_id = latest.store_id
JOIN slot_machine_results AS r
    ON r.store_id = latest.store_id AND r.date = latest.latest_date
WHERE r.machine_name IS NOT NULL AND BTRIM(r.machine_name) <> ''
GROUP BY s.store_id, s.store_name, latest.latest_date, r.machine_name
ORDER BY s.store_name, r.machine_name
"""


def build_stores(dataframe):
    """DBの登録済み店舗（台別データあり）だけ抽出。機種は最新登録日のみ。"""
    stores = {}
    for row in dataframe.to_dict(orient="records"):
        try:
            store_id = int(row["store_id"])
        except (ValueError, TypeError, KeyError):
            continue
        name = str(row.get("store_name") or "").strip()
        machine = str(row.get("machine_name") or "").strip()
        if not name or not machine:
            continue
        if store_id not in stores:
            date = row.get("latest_date")
            stores[store_id] = {
                "id": store_id,
                "name": name,
                "latest_date": str(date) if date is not None else None,
                "machines": [],
            }
        if machine not in stores[store_id]["machines"]:
            stores[store_id]["machines"].append(machine)
    return list(stores.values())


def build_html(stores):
    """DBの店舗リストをHTMLへ安全に渡す。固定の架空店舗にはフォールバックしない。"""
    html = Path(__file__).with_name("slot_edge_minigame.html").read_text(encoding="utf-8")
    payload = json.dumps(stores, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    return html.replace("/* SLOT_EDGE_STORES_INJECTION */", "window.SLOT_EDGE_STORES="+payload+";")


def render_slot_edge_minigame(query_df):
    st.subheader("🎮 ミニゲーム｜PUSH PICK")
    st.caption("行くお店はNeonに台別データを登録済みの店舗だけ表示。登録した最新日の機種から抽選します。")
    try:
        df = query_df(SQL)
    except Exception as exc:
        st.error("店舗データを読み込めませんでした。Neonへの接続を確認してください。")
        st.caption(str(exc))
        return
    stores = build_stores(df)
    if not stores:
        st.info("対象店舗がありません。『初期DB取込』または『JSON追加』から台別データを登録してください。")
        return
    st.caption(f"DB登録済み店舗：{len(stores)}店。店舗一覧は自動更新されます。")
    components.html(build_html(stores), height=1210, scrolling=True)
    st.caption("※ 現在は遊び用のランダム抽選です。店舗別の予測・設定判定・的中率モデルは未接続です。")
