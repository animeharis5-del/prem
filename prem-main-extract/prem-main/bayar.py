"""Integrasi Open API BuatQris untuk fitur Topup (VIP/XVIP/VVIP).

Mengikuti dokumen `konfigurasi-open-api.txt`:
  • Satu URL, dibedakan parameter `action` (POST form-urlencoded).
  • Autentikasi: account_id + secret_token (dibaca dari .env / environment —
    JANGAN pernah di frontend / repo).
  • Pencocokan nominal SELALU memakai `total_amount` (sudah termasuk kode unik),
    bukan `amount`.
  • api_check_status dibatasi 1 cek / 20 detik per transaksi → jeda polling
    default 25 detik (config BUATQRIS_CEK_JEDA); webhook lebih disarankan.
  • Webhook diverifikasi dengan HMAC-SHA256 (`X-BuatQris-Signature`) memakai
    BODY MENTAH, dan pemrosesannya idempoten (order yang sudah lunas diabaikan).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from typing import Optional, Tuple

import httpx

import config

log = logging.getLogger("bayar")


class BayarError(Exception):
    """Gagal memanggil API BuatQris (pesan bahasa Indonesia dari server)."""


async def _post(action: str, **params) -> dict:
    cfg = config.BUATQRIS
    if not cfg["account_id"] or not cfg["secret_token"]:
        raise BayarError(
            "Kredensial BuatQris belum diisi (BUATQRIS_ACCOUNT_ID / BUATQRIS_SECRET_TOKEN di file .env)."
        )
    data = {"action": action, "account_id": cfg["account_id"], "secret_token": cfg["secret_token"]}
    data.update({k: v for k, v in params.items() if v is not None and v != ""})
    async with httpx.AsyncClient(timeout=30.0, verify=config.VERIFY_SSL) as client:
        res = await client.post(cfg["base_url"], data=data)
    try:
        body = res.json()
    except ValueError:
        raise BayarError(f"Respons BuatQris bukan JSON (HTTP {res.status_code}).") from None
    if not isinstance(body, dict):
        raise BayarError(f"Respons BuatQris tidak dikenali (HTTP {res.status_code}).")
    if not body.get("success"):
        raise BayarError(str(body.get("message") or f"HTTP {res.status_code}"))
    return body.get("data") or {}


async def buat_qris(amount: int, description: str = "", fee_by: str = "buyer") -> dict:
    """action=api_create_qris → buat transaksi QRIS.

    fee_by="buyer": biaya admin + kode unik ditanggung pembeli (nominal paket utuh),
    pencocokan tetap pakai total_amount dari respons.
    Return dict: transaction_id, amount, total_amount, qr_url, payment_url, expired_at, status.
    """
    params = {"amount": int(amount), "description": (description or "Pembayaran via API")[:100], "fee_by": fee_by}
    if config.BUATQRIS["test"]:
        params["test"] = "1"
    return await _post("api_create_qris", **params)


async def cek_status(transaction_id: str) -> Tuple[str, dict]:
    """action=api_check_status → (status, data).

    status: pending | success | expired | failed.
    HTTP 429 (rate_limited) dianggap "masih pending", tunggu retry_after.
    """
    cfg = config.BUATQRIS
    data = {"action": "api_check_status", "account_id": cfg["account_id"],
            "secret_token": cfg["secret_token"], "transaction_id": transaction_id}
    async with httpx.AsyncClient(timeout=30.0, verify=config.VERIFY_SSL) as client:
        res = await client.post(cfg["base_url"], data=data)
    try:
        body = res.json()
    except ValueError:
        return "pending", {}
    if res.status_code == 429 or (isinstance(body, dict) and body.get("error") == "rate_limited"):
        return "pending", {"retry_after": int((body or {}).get("retry_after") or cfg["cek_status_jeda"])}
    if not isinstance(body, dict) or not body.get("success"):
        return "pending", {}
    d = body.get("data") or {}
    return str(d.get("status") or "pending"), d


def verifikasi_webhook(body: bytes, signature_header: str) -> bool:
    """Cocokkan X-BuatQris-Signature = sha256=<hmac(body mentah, signing_secret)>."""
    secret = config.BUATQRIS["signing_secret"]
    if not secret or not signature_header:
        return False
    calc = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(calc, signature_header)


async def pantau_pembayaran(transaction_id: str, stop: Optional[asyncio.Event] = None) -> str:
    """Polling status sampai lunas/kedaluwarsa/gagal atau batas cek tercapai.

    Kepatuhan rate-limit: jeda antar-cek ≥ cek_status_jeda (default 25 dtk > 20 dtk).
    Return: "success" | "expired" | "failed" | "timeout".
    """
    cfg = config.BUATQRIS
    await asyncio.sleep(max(5.0, cfg["cek_status_mulai"]))
    for _ in range(int(cfg["cek_status_max"])):
        if stop is not None and stop.is_set():
            return "timeout"
        try:
            status, extra = await cek_status(transaction_id)
        except Exception as e:
            log.warning("cek status gagal: %s", e)
            status, extra = "pending", {}
        if status in ("success", "expired", "failed"):
            return status
        jeda = max(cfg["cek_status_jeda"], float(extra.get("retry_after") or 0) + 2)
        await asyncio.sleep(jeda)
    return "timeout"
