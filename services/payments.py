import time
from urllib.parse import quote


def create_invoice_id(user_id: int) -> str:
    return f"INV-{user_id}-{int(time.time())}"


def build_miniapp_url(web_base: str, amount: int, invoice_id: str, return_url: str) -> str:
    base = (web_base or "").rstrip("/")
    url = f"{base}/clickpay/pay?amount={amount}&invoice_id={invoice_id}"
    if return_url:
        url += f"&return_url={quote(return_url, safe='')}"
    return url
