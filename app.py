import streamlit as st
import psycopg

st.set_page_config(
    page_title="スロ屋データベース",
    page_icon="🎰",
    layout="wide"
)

st.title("🎰 スロ屋データベース")

st.subheader("データベース接続確認")

try:
    database_url = st.secrets["DATABASE_URL"]

    with psycopg.connect(database_url, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()

    st.success("✅ Neonデータベース接続成功")

except Exception as e:
    st.error("❌ Neonデータベースに接続できません")
    st.code(str(e))
