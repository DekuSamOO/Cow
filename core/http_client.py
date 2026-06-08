import requests
import time
import logging
import urllib3
from config import SSL_VERIFY
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

if not SSL_VERIFY:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DEFAULT_TIMEOUT = 10
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"

def safe_get(
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = 2,
    backoff_factor: float = 0.5,
    verify: Optional[bool] = None
) -> requests.Response:
    """
    統一的 GET 請求封裝，包含自動重試、超時控制與假裝 User-Agent。
    """
    _verify = verify if verify is not None else SSL_VERIFY
    _headers = headers or {}
    if "User-Agent" not in _headers:
        _headers["User-Agent"] = DEFAULT_USER_AGENT
    # 不請求 brotli：部分環境 urllib3 的 br streaming 解碼器有 bug
    # （"decoder process called with data when can_accept_more_data() is False"），
    # 會讓 DeFiLlama 等回 br 的端點整包失敗。限定 gzip/deflate 從源頭避開。
    if "Accept-Encoding" not in _headers:
        _headers["Accept-Encoding"] = "gzip, deflate"

    last_exception = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(
                url,
                headers=_headers,
                params=params,
                timeout=timeout,
                verify=_verify
            )
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as e:
            last_exception = e
            if attempt < retries:
                sleep_time = backoff_factor * (2 ** attempt)
                logger.warning(f"[http_client] Request to {url} failed: {e}. Retrying in {sleep_time}s...")
                time.sleep(sleep_time)
            else:
                logger.error(f"[http_client] Request to {url} failed after {retries} retries: {e}")
                
    if last_exception:
        raise last_exception
    raise RuntimeError("Unknown error")

def safe_post(
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    json: Optional[Dict[str, Any]] = None,
    data: Optional[Any] = None,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = 2,
    backoff_factor: float = 0.5,
    verify: Optional[bool] = None
) -> requests.Response:
    """
    統一的 POST 請求封裝。
    """
    _verify = verify if verify is not None else SSL_VERIFY
    _headers = headers or {}
    
    last_exception = None
    for attempt in range(retries + 1):
        try:
            resp = requests.post(
                url,
                headers=_headers,
                json=json,
                data=data,
                timeout=timeout,
                verify=_verify
            )
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as e:
            last_exception = e
            if attempt < retries:
                sleep_time = backoff_factor * (2 ** attempt)
                logger.warning(f"[http_client] POST to {url} failed: {e}. Retrying in {sleep_time}s...")
                time.sleep(sleep_time)
            else:
                logger.error(f"[http_client] POST to {url} failed after {retries} retries: {e}")
                
    if last_exception:
        raise last_exception
    raise RuntimeError("Unknown error")
