import streamlit as st

st.set_page_config(
    page_title="スロ屋データベース",
    page_icon="🎰",
    layout="wide"
)

st.title("🎰 スロ屋データベース")

st.success("アプリの初期設定が完了しました。")

st.write("BIGディッパー新橋1号店などのスロット実績データを蓄積・分析するアプリです。")

st.subheader("これから追加する機能")
st.write("""
- アナスロJSONのインポート
- 日付・店舗別のデータベース保存
- 台番号別分析
- 機種別分析
- 日別総差枚
- 曜日別傾向
- 過去の凹み台・上げ狙い分析
- 特定日分析
- 狙い台候補の抽出
""")
