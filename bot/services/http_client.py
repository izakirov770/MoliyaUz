import os
import httpx

WEB_BASE = os.getenv("WEB_BASE", "").rstrip("/")

async def ping_return(invoice_id: str) -> tuple[bool, str, int]:
    """
    GET {WEB_BASE}/payments/return?invoice_id=... → (ok, text, status_code)
    Used as fallback if CLICK didn't redirect back automatically.
    """
    if not WEB_BASE:
        return False, "WEB_BASE env not set", 0
    url = f"{WEB_BASE}/payments/return"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, params={"invoice_id": invoice_id})
            return (r.status_code == 200, r.text, r.status_code)
    except Exception as e:
        return False, str(e), 0
