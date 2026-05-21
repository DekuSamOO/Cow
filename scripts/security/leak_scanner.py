import os
import re
import subprocess
import urllib3

# =================================================================
# 1. 環境初始化與 SSL 處理
# =================================================================
from config import SSL_VERIFY

# 由於公司內網 SSL 憑證問題，本地端執行時關閉驗證。
# 詳細註解：這能防止腳本在未來擴充網路功能時，因為 SSL 憑證檢查失敗而中斷。
cert_reqs = 'CERT_REQUIRED' if SSL_VERIFY else 'CERT_NONE'
http = urllib3.PoolManager(cert_reqs=cert_reqs)
if not SSL_VERIFY:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# =================================================================
# 2. 設定檢查參數與遮蔽邏輯
# =================================================================
TARGET_EXTENSIONS = ['.log', '.txt', '.env', '.json', '.csv']
SENSITIVE_PATTERNS = [
    r"api[-_]?key", r"secret", r"password", r"token", 
    r"access[-_]?key", r"sk-", r"db_url", r"birthday", r"address"
]

def mask_sensitive_data(text):
    """
    將敏感字串進行遮蔽處理，防止在終端機顯示完整金鑰。
    例如: 'sk-1234567890abcdef' -> 'sk-12...bcdef'
    """
    if len(text) <= 8:
        return "*******"
    # 顯示前 4 碼與最後 4 碼，中間以星號替代
    return f"{text[:4]}...{text[-4:]}"

def check_git_ignore_status(target_file=".env"):
    """
    檢查檔案是否已列入 .gitignore，避免被 Git 追蹤。
    """
    if not os.path.exists(target_file):
        return True
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-v", target_file],
            capture_output=True, text=True
        )
        return result.returncode == 0
    except Exception:
        return False

def scan_content_for_leaks(file_path):
    """
    掃描檔案內容，若發現敏感資訊則回傳「已遮蔽」的內容。
    """
    leaks = []
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            for line_num, line in enumerate(f, 1):
                clean_line = line.strip()
                for pattern in SENSITIVE_PATTERNS:
                    if re.search(pattern, clean_line, re.IGNORECASE):
                        # 進行遮蔽處理，確保終端機輸出的不是明文
                        masked_text = mask_sensitive_data(clean_line)
                        leaks.append((line_num, masked_text))
                        break
    except Exception as e:
        print(f"  [!] 無法讀取檔案 {file_path}: {e}")
    return leaks

def run_security_audit(project_dir):
    """
    執行資安審計流程。
    """
    print("="*60)
    print(f"安全審查中: {project_dir}")
    print("="*60)

    # 第一階段：檢查 .env 是否安全
    is_env_safe = check_git_ignore_status(".env")
    if os.path.exists(".env"):
        status = "[✓] 安全" if is_env_safe else "[✘] 危險 (尚未忽略)"
        print(f"1. .env 隔離狀態: {status}")
    
    print("\n2. 開始內容深度掃描 (已啟動自動遮蔽機制):")
    total_leaks = 0
    
    for subdir, _, files in os.walk(project_dir):
        if any(ignored in subdir for ignored in ['.git', '__pycache__', 'venv', '.venv']):
            continue
            
        for file in files:
            file_path = os.path.join(subdir, file)
            if any(file.endswith(ext) for ext in TARGET_EXTENSIONS):
                # 如果 .env 已經被 Git 忽略，就跳過掃描，因為那本來就是放 Key 的地方
                if file == ".env" and is_env_safe:
                    continue
                
                leaks = scan_content_for_leaks(file_path)
                if leaks:
                    print(f"\n[!] 偵測到洩漏風險: {file_path}")
                    for line_no, masked_text in leaks:
                        print(f"    行 {line_no}: {masked_text}")
                    total_leaks += len(leaks)

    print("\n" + "="*60)
    if total_leaks == 0 and is_env_safe:
        print("恭喜！未發現明顯資安風險。")
    else:
        print(f"注意：共發現 {total_leaks} 處潛在問題，請檢查上述檔案。")
    print("="*60)

if __name__ == "__main__":
    run_security_audit(os.getcwd())