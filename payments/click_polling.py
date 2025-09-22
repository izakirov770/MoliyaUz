# [SUBSCRIPTION-POLLING-BEGIN]
"""Click polling helpers for subscription payments."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from typing import Any, Dict, Optional

import httpx


logger = logging.getLogger(__name__)

CLICK_POLLING_ENABLED = os.getenv("ENABLE_CLICK_POLLING", "true").lower() in {"1", "true", "yes"}

CLICK_BASE_PAY_URL = os.getenv("CLICK_BASE_PAY_URL", "https://my.click.uz/services/pay")
CLICK_STATUS_URL = os.getenv(
    "CLICK_STATUS_URL",
    "https://merchant.click.uz/api/v2/invoice/status/",
)
CLICK_SERVICE_ID = os.getenv("CLICK_SERVICE_ID", "")
CLICK_MERCHANT_ID = os.getenv("CLICK_MERCHANT_ID", "")
CLICK_MERCHANT_USER_ID = os.getenv("CLICK_MERCHANT_USER_ID", "")
CLICK_SECRET_KEY = os.getenv("CLICK_SECRET_KEY", "")

PLAN_WEEK_KEY = os.getenv("CLICK_PLAN_WEEKLY_KEY", "weekly_1")
PLAN_MONTH_KEY = os.getenv("CLICK_PLAN_MONTHLY_KEY", "monthly_1")

RAW_WEEK_AMOUNT = os.getenv("AMOUNT_WEEK_SOM", "7900.00")
RAW_MONTH_AMOUNT = os.getenv("AMOUNT_MONTH_SOM", "19900.00")


def get_plan_amount(plan: str) -> str:
    if plan == PLAN_WEEK_KEY:
        return _format_amount(RAW_WEEK_AMOUNT)
    if plan == PLAN_MONTH_KEY:
        return _format_amount(RAW_MONTH_AMOUNT)
    return "0.00"


def _format_amount(amount: str | int | float) -> str:
    try:
        return f"{float(amount):.2f}"
    except Exception:
        return str(amount)


def build_click_pay_url(merchant_trans_id: str, amount: str) -> str:
    """Build CLICK pay URL for polling flow."""

    formatted_amount = _format_amount(amount)
    params = {
        "service_id": CLICK_SERVICE_ID,
        "merchant_id": CLICK_MERCHANT_ID,
        "transaction_param": merchant_trans_id,
        "amount": formatted_amount,
    }
    if CLICK_MERCHANT_USER_ID:
        params["merchant_user_id"] = CLICK_MERCHANT_USER_ID
    query = "&".join(f"{k}={v}" for k, v in params.items() if v)
    return f"{CLICK_BASE_PAY_URL}?{query}" if query else CLICK_BASE_PAY_URL


async def check_click_status(merchant_trans_id: str) -> Dict[str, Any]:
    """Poll CLICK merchant API for current payment status."""

    if not CLICK_POLLING_ENABLED:
        return {"ok": False, "reason": "polling_disabled"}

    if not merchant_trans_id:
        return {"ok": False, "reason": "missing_id"}

    timestamp = int(time.time())
    signature = hashlib.sha1(f"{timestamp}{CLICK_SECRET_KEY}".encode()).hexdigest()
    headers = {
        "merchant_user_id": CLICK_MERCHANT_USER_ID or "",
        "sign": signature,
        "sign_time": str(timestamp),
    }
    params = {
        "service_id": CLICK_SERVICE_ID,
        "merchant_id": CLICK_MERCHANT_ID,
        "merchant_trans_id": merchant_trans_id,
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(CLICK_STATUS_URL, params=params, headers=headers)
            try:
                data = response.json()
            except Exception:
                data = {"raw": response.text}

            status_raw = data.get("status") or data.get("payment_status")
            note_raw = data.get("status_note") or data.get("error_note")
            status_value = str(status_raw or note_raw or "").strip().lower()
            is_paid = False
            if status_value in {"paid", "success", "successfully_pay", "completed", "200"}:
                is_paid = True
            elif any(token in status_value for token in ["оплачен", "успешн", "оплата прошла"]):
                is_paid = True

            logger.info(
                "click-status merchant_trans_id=%s status=%s code=%s",
                merchant_trans_id,
                status_value,
                response.status_code,
            )
            logger.info("click-status payload=%s", data)
            return {
                "paid": is_paid,
                "payload": data,
                "status_code": response.status_code,
                "status": status_value,
            }
        except httpx.RequestError as exc:
            logger.warning(
                "click-status-error",
                extra={"merchant_trans_id": merchant_trans_id, "error": str(exc)},
            )
            return {"paid": False, "error": str(exc), "status": "error", "status_code": None, "payload": {}}


# Small helper for synchronous contexts (e.g., tests)
def check_click_status_sync(merchant_trans_id: str) -> Dict[str, Any]:
    return asyncio.get_event_loop().run_until_complete(check_click_status(merchant_trans_id))


__all__ = [
    "CLICK_POLLING_ENABLED",
    "PLAN_WEEK_KEY",
    "PLAN_MONTH_KEY",
    "get_plan_amount",
    "build_click_pay_url",
    "check_click_status",
]

# [SUBSCRIPTION-POLLING-END]
