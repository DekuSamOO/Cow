"""共用工具：handler.components 內各 sub-tab 共享的小函數"""
import io
import pandas as pd


def df_to_csv_bytes(df: pd.DataFrame) -> bytes:
    """將 DataFrame 轉換為 UTF-8 BOM 編碼的 CSV bytes，供 st.download_button 使用"""
    buffer = io.StringIO()
    df.to_csv(buffer, index=True, encoding='utf-8-sig')
    return buffer.getvalue().encode('utf-8-sig')
