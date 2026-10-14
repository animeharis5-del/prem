import asyncio
import base64
import hashlib
import json
import os
import random
import re
import sys
import uuid
from pathlib import Path

import httpx
import phonenumbers
from phonenumbers.phonenumberutil import COUNTRY_CODE_TO_REGION_CODE

BASE_TAI = Path(__file__).resolve().parent

# VERSI KONTOL
versi_mark = os.environ.get("versi_mark") or "2.26.33.73"
WA_mark = "0a1mLfGUIBVrMKF1RdvLI5lkRBvof6vn0fD2QRSM"

# HP JELEK BUAT NIPU
merek_jelek = [
    "Apple-iPhone_13",
    "Apple-iPhone_13_Pro",
    "Apple-iPhone_14",
    "Apple-iPhone_14_Pro",
    "Apple-iPhone_15",
    "Apple-iPhone_15_Pro",
    "Apple-iPhone_12",
    "Apple-iPhone_SE_3",
]
ios_jelek = ["17.5.1", "17.6.1", "18.0", "18.1.1", "16.7.8"]


def buat_ua() -> str:
    return f"WhatsApp/{versi_mark} iOS/{random.choice(ios_jelek)} Device/{random.choice(merek_jelek)}"

# MATEMATIKA ED25519 NGENTOT
_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_BX = 0x216936D3CD6E53FEC0A4E231FDD6DC5C692CC7609525A7B2C9562D608F25D51A
_BY = (4 * pow(5, _P - 2, _P)) % _P


def _clamp_ibulu(sk: bytearray) -> bytearray:
    sk[0] &= 248
    sk[31] &= 127
    sk[31] |= 64
    return sk


def _perkalian_ibulu(k: bytes, u: bytes) -> bytes:
    k_int = int.from_bytes(k, "little")
    k_int &= ~7
    k_int &= ~(128 << 248)
    k_int |= 64 << 248
    x1 = int.from_bytes(u, "little") & ((1 << 255) - 1)
    x1 %= _P
    x2, z2, x3, z3, swap = 1, 0, x1, 1, 0
    for t in range(254, -1, -1):
        k_t = (k_int >> t) & 1
        swap ^= k_t
        if swap:
            x2, x3 = x3, x2
            z2, z3 = z3, z2
        swap = k_t
        a = (x2 + z2) % _P
        aa = a * a % _P
        b = (x2 - z2) % _P
        bb = b * b % _P
        e = (aa - bb) % _P
        c = (x3 + z3) % _P
        d = (x3 - z3) % _P
        da = d * a % _P
        cb = c * b % _P
        x3 = (da + cb) % _P
        x3 = x3 * x3 % _P
        z3 = (da - cb) % _P
        z3 = z3 * z3 * x1 % _P
        x2 = aa * bb % _P
        z2 = e * ((aa + 121665 * e) % _P) % _P
    if swap:
        x2, x3 = x3, x2
        z2, z3 = z3, z2
    return ((x2 * pow(z2, _P - 2, _P)) % _P).to_bytes(32, "little")


def _ed_bokaps(p, q):
    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = q
    a = (y1 - x1) * (y2 - x2) % _P
    b = (x1 + y1) * (x2 + y2) % _P
    c = 2 * t1 * t2 * _D % _P
    d = 2 * z1 * z2 % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


_tai_kontol = (0, 1, 1, 0)
_tai_memek = (_BX, _BY, 1, _BX * _BY % _P)


def _ed_ibulu(s: int):
    p = _tai_kontol
    for i in range(255, -1, -1):
        p = _ed_bokaps(p, p)
        if (s >> i) & 1:
            p = _ed_bokaps(p, _tai_memek)
    return p


def _ed_bokap(p) -> bytes:
    x, y, z, _ = p
    zi = pow(z, _P - 2, _P)
    x = x * zi % _P
    y = y * zi % _P
    r = bytearray(y.to_bytes(32, "little"))
    r[31] ^= (x & 1) << 7
    return bytes(r)


def buat_bapaklu(seed: bytes) -> dict:
    if len(seed) != 32:
        raise ValueError("panjang seed salah kontol")
    sk = _clamp_ibulu(bytearray(seed))
    pk = bytearray(_perkalian_ibulu(bytes(sk), b"\x09" + b"\x00" * 31))
    pk[31] &= 127
    return {"public": bytes(pk), "private": bytes(sk)}


def sigma(private_key: bytes, msg: bytes) -> bytes:
    if len(private_key) != 32:
        raise ValueError("panjang private key salah anjing")
    sk = _clamp_ibulu(bytearray(private_key))
    a = int.from_bytes(sk, "little")
    a_pub = _ed_bokap(_ed_ibulu(a))
    sigma_bit = a_pub[31] & 0x80
    r_int = int.from_bytes(hashlib.sha512(bytes(sk) + msg).digest(), "little") % _L
    rr = _ed_bokap(_ed_ibulu(r_int))
    h_int = int.from_bytes(hashlib.sha512(rr + a_pub + msg).digest(), "little") % _L
    s_int = (r_int + h_int * a) % _L
    sig = bytearray(rr + s_int.to_bytes(32, "little"))
    sig[63] |= sigma_bit
    return bytes(sig)


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def buat_token(nomor: str) -> str:
    return _md5(WA_mark + _md5(versi_mark) + nomor)


def buat_key_pair() -> dict:
    return buat_bapaklu(os.urandom(32))


def pub_key_sigmaal(p: bytes) -> bytes:
    return p if len(p) == 33 else b"\x05" + p


def ibulugacor_pre_key(idk: dict) -> dict:
    pre_key = buat_key_pair()
    return {
        "key_pair": pre_key,
        "sigmaature": sigma(idk["private"], pub_key_sigmaal(pre_key["public"])),
    }


def pisah_nomor(raw: str) -> dict:
    digits = re.sub(r"\D", "", str(raw))
    try:
        parsed = phonenumbers.parse("+" + digits, None)
        if phonenumbers.is_valid_number(parsed):
            return {
                "cc": str(parsed.country_code),
                "nomor": str(parsed.national_number),
                "sumber": "phonenumbers",
            }
    except phonenumbers.NumberParseException:
        pass

    cc_env = re.sub(r"\D", "", os.environ.get("WA_CC", ""))
    if cc_env and digits.startswith(cc_env):
        return {"cc": cc_env, "nomor": digits[len(cc_env):], "sumber": "WA_CC"}

    for panjang in (3, 2, 1):
        if len(digits) > panjang:
            cc = digits[:panjang]
            if int(cc) in COUNTRY_CODE_TO_REGION_CODE:
                return {"cc": cc, "nomor": digits[panjang:], "sumber": "prefiks cc"}
    cc = digits[:3]
    return {
        "cc": cc,
        "nomor": digits[len(cc):],
        "sumber": "tebakan (set WA_CC tai)",
    }


class CekBapaklu(Exception):
    pass

Api_Mark_Anj = "https://v.whatsapp.net/v2/code"

# ── HTTP client bersama (connection pooling) ──────────────────────────
# Membuat satu AsyncClient untuk SEMUA pengecekan membuat setiap request tidak
# perlu handshake TLS dari awal → pengecekan jadi jauh lebih cepat saat paralel.
_client: httpx.AsyncClient | None = None


def get_client(timeout: float = 15.0) -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            verify=False,
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
        )
    return _client


async def tutup_client() -> None:
    """Tutup pool saat bot mati."""
    global _client
    if _client is not None and not _client.is_closed:
        try:
            await _client.aclose()
        except Exception:
            pass
    _client = None

def _bapaklu_data_anjing(cc: str, nomor: str, method: str, ua_seed: dict | None = None) -> dict:
    key_lasolu = (ua_seed or {}).get("key_lasolu") or buat_key_pair()
    noise_lasolu = (ua_seed or {}).get("noise_lasolu") or buat_key_pair()
    idk_lasolu = (ua_seed or {}).get("idk") or buat_key_pair()
    ibulugacor = ibulugacor_pre_key(idk_lasolu)
    reg_tolol = ((ua_seed or {}).get("reg") if isinstance((ua_seed or {}).get("reg"), int)
              else int.from_bytes(os.urandom(2), "big") & 16383).to_bytes(4, "big")
    data_anjing = {
        "cc": cc,
        "in": nomor,
        "lg": "en",
        "lc": "GB",
        "mistyped": "6",
        "authkey": _b64url(noise_lasolu["public"]),
        "e_regid": _b64url(reg_tolol),
        "e_keytype": "BQ",
        "e_ident": _b64url(idk_lasolu["public"]),
        "e_skey_id": _b64url(b"\x00\x01\x00"),
        "e_skey_val": _b64url(ibulugacor["key_pair"]["public"]),
        "e_skey_sig": _b64url(ibulugacor["sigmaature"]),
        "fdid": (ua_seed or {}).get("fdid") or str(uuid.uuid4()),
        "expid": _b64url((ua_seed or {}).get("expid") or os.urandom(16)),
        "network_radio_type": "1",
        "simnum": "1",
        "hasinrc": "1",
        "pid": str(random.randint(1000, 9999)),
        "rc": "0",
        "id": _b64url(os.urandom(20)),
        "token": buat_token(nomor),
        "method": method,
    }
    return data_anjing


async def cek(number: str, method: str = "wa_old", *, proxy: str | None = None,
             timeout: float = 15.0, ua: str | None = None) -> dict:
    digits = re.sub(r"\D", "", str(number))
    if len(digits) < 8:
        return {"status": "invalid", "reason": "NOMOR KEPENDEKAN KONTOL"}
    hasil = pisah_nomor(number)
    cc, nomor = hasil["cc"], hasil["nomor"]
    if not cc or not nomor:
        return {"status": "invalid", "reason": "GAK BISA PISAH KODE NEGARA BANGSAT"}
    if os.environ.get("WA_DEBUG"):
        print(f"[debug] cc={cc} nomor={nomor} (via {hasil['sumber']})")

    data_anjing = _bapaklu_data_anjing(cc, nomor, method)
    url = Api_Mark_Anj + "?" + _urlencode(data_anjing)
    headers = {"User-Agent": ua or buat_ua(), "Accept": "text/json"}
    if proxy:
        # proxy jarang dipakai → client khusus singkat
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                     verify=False, proxy=proxy) as client:
            res = await client.get(url, headers=headers)
    else:
        client = get_client(timeout)
        res = await client.get(url, headers=headers)
    try:
        data = res.json()
    except ValueError:
        raise CekBapaklu(f"HTTP {res.status_code}: RESPON BUKAN JSON ANJING") from None
    if not isinstance(data, dict):
        raise CekBapaklu(f"HTTP {res.status_code}: RESPON BUKAN JSON OBJECT TAI")
    return data


def _urlencode(data_anjing: dict) -> str:
    from urllib.parse import urlencode
    return urlencode(data_anjing)


sumber_mamaklu = [
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
]


async def mamaklu_list(maksimal: int = 200) -> list:
    kumpulan = []
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for url in sumber_mamaklu:
            try:
                res = await client.get(url)
                for baris in res.text.splitlines():
                    p = baris.strip()
                    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}:\d+", p):
                        kumpulan.append("http://" + p)
            except Exception:
                pass
            if len(kumpulan) >= maksimal * 3:
                break

    random.shuffle(kumpulan)
    return kumpulan[:maksimal]


def mamaklu_proxy() -> list:
    filename = os.environ.get("PROXY_FILE") or "proxies.txt"
    candidates = [Path(filename), BASE_TAI / filename]
    content = None

    for f in candidates:
        try:
            content = f.read_text(encoding="utf-8")
            break
        except (OSError, UnicodeDecodeError):
            continue

    if content is None:
        return []

    proxies = []
    for line in content.splitlines():
        proxy = line.strip()
        if not proxy or proxy.startswith("#"):
            continue
        proxies.append(proxy)

    return proxies


def _respons_bagus(r) -> bool:
    return bool(
        isinstance(r, dict)
        and r.get("reason") not in ("no_routes", "blocked")
        and r.get("status") != "invalid"
        and isinstance(r.get("sms_wait"), (int, float))
        and not isinstance(r.get("sms_wait"), bool)
    )


async def cek_balapan(number: str, method: str, daftar_proxy: list, lebar: int = 25,
                      timeout: float = 5.0):
    terakhir = None
    menang = None

    async def satu(px):
        nonlocal terakhir, menang
        try:
            r = await cek(number, method, proxy=px, timeout=timeout)
            terakhir = dict(r, _proxy=px)
            if _respons_bagus(r) and menang is None:
                menang = terakhir
        except Exception as e:
            if os.environ.get("WA_DEBUG"):
                print(f"[balap] {px} gagal: {type(e).__name__}: {e}")

    antrian = list(daftar_proxy)
    idx = 0
    aktif = set()
    try:
        while True:
            while idx < len(antrian) and len(aktif) < lebar and menang is None:
                t = asyncio.ensure_future(satu(antrian[idx]))
                idx += 1
                aktif.add(t)
            if not aktif:
                break
            if os.environ.get("WA_DEBUG") and aktif:
                pass
            done, _ = await asyncio.wait(aktif, return_when=asyncio.FIRST_COMPLETED)
            aktif -= done
            if menang is not None:
                break
    finally:
        for t in aktif:
            t.cancel()
        if aktif:
            await asyncio.gather(*aktif, return_exceptions=True)
    return menang if menang is not None else terakhir


async def detect(number: str, method: str = "wa_old") -> tuple:
    ampas = mamaklu_proxy() if os.environ.get("WA_ampas") else []
    if ampas:
        lebar = int(os.environ.get("WA_tai_LEBAR") or 10)
        r = await cek_balapan(number, method, ampas, lebar)
        info = "langsung (tanpa proxy)"
        if r and r.get("_proxy"):
            info = "ampas " + re.sub(r"//[^@]+@", "//***@", r["_proxy"])
        else:
            info = "ampas (semua gagal)"
        if not r:
            r = {"status": "error", "reason": "semua_ampas_gagal"}
        return r, info

    if os.environ.get("WA_PROXY"):
        px = os.environ["WA_PROXY"]
        try:
            r = await cek(number, method, proxy=px, timeout=15.0)
        except Exception as e:
            r = {"status": "error", "reason": "tai_gagal",
                 "detail": f"{type(e).__name__}: {e}"}
        return r, "manual " + px

    if os.environ.get("WA_USE_PROXY"):
        pool_size = int(os.environ.get("WA_tai_POOL") or 300)
        lebar = int(os.environ.get("WA_tai_LEBAR") or 50)
        max_anjing = int(os.environ.get("WA_tai_PUTARAN") or 3)

        def asli(x):
            return bool(x) and x.get("reason") in ("too_recent", "incorrect")

        r = None
        info = "langsung (tanpa proxy)"
        for putaran in range(1, max_anjing + 1):
            daftar = await mamaklu_list(pool_size)
            hasil = await cek_balapan(number, method, daftar, lebar)
            if hasil:
                r = hasil
                if hasil.get("_proxy"):
                    info = "proxy " + hasil["_proxy"]
            if asli(hasil):
                break
        if not r:
            r = {"status": "error", "reason": "semua_tai_gagal"}
        return r, info

    r = await cek(number, method, timeout=15.0)
    return r, "langsung (tanpa proxy)"


async def tunggu_tolol(number: str, method: str = "wa_old") -> dict:
    r, _info = await detect(number, method)
    return r


def fmt(d) -> str:
    if d is None:
        return "0 (BISA MINTA OTP KONTOL)"
    try:
        d = int(d)
    except (TypeError, ValueError):
        return "-"
    if d < 0:
        return "GAK TERSEDIA (METHOD DINONAKTIFKAN SERVER BANGSAT)"
    if not d:
        return "0 (BISA MINTA OTP TAI)"
    return f"{d // 3600} JAM {(d % 3600) // 60} MENIT ({d} DETIK)"


ARTI_REASON = {
    "too_recent": "NOMOR BARU AJA REQUEST OTP, MASIH COOLDOWN (INI TUJUAN KITA).",
    "no_routes": "SERVER GAK BISA KIRIM OTP KE NOMOR INI, FORMATNYA SALAH ATAU OPERATOR GAK DIKENAL.",
    "blocked": "IP/NOMOR KENA BANNED! GANTI IP LU KALO GAK MAU KENA TAMBAH LAMA.",
    "old_version": "VERSI LU UDAH KADALUARSA, UPDATE!",
    "bad_token": "TOKEN GAK MATCH SAMA VERSI, SETEL ULANG VERSI_MARK.",
    "incorrect": "NOMOR INI GAK TERDAFTAR DI WHATSAPP ATAU FORMATNYA SALAH.",
}


async def _cli():
    nomor = sys.argv[1] if len(sys.argv) > 1 else "+94710954319"
    method = sys.argv[2] if len(sys.argv) > 2 else "wa_old"

    try:
        r, info_proxy = await detect(nomor, method)
    except CekBapaklu as e:
        print("NOMOR  :", nomor, "| VERSI:", versi_mark, "| VIA: LANGSUNG (TANPA PROXY)")
        print("ERROR  :", e)
        return

    print("NOMOR  :", nomor, "| VERSI:", versi_mark, "| VIA:", info_proxy)
    print("RESPONS:", json.dumps(r, indent=2, ensure_ascii=False))
    if r.get("reason") and r["reason"] in ARTI_REASON:
        print("CATATAN:", ARTI_REASON[r["reason"]])

    if r.get("status") == "fail" and (r.get("appeal_token") or r.get("reason") == "blocked"):
        print("---")
        print("HASIL: NOMOR/IP KENA BLOKIR SERVER KONTOL, BUKAN COOLDOWN OTP.")
        print("COOLDOWN ASLI GAK BISA DIBACA DARI IP INI. PINDAH KE IP BERSIH (NON-DATACENTER) BANGSAT.")
        return

    if isinstance(r.get("sms_wait"), (int, float)) and not isinstance(r.get("sms_wait"), bool):
        waits = [r.get(k) for k in ("sms_wait", "voice_wait", "flash_wait",
                                    "email_otp_wait", "send_sms_wait", "wa_old_wait")]
        waits = [v for v in waits if isinstance(v, (int, float)) and not isinstance(v, bool)]
        semua_sama = len(waits) >= 3 and waits[0] > 0 and all(v == waits[0] for v in waits)
        kemungkinan_rate_limit = (
            r.get("reason") == "no_routes"
            or (semua_sama and r.get("reason") not in ("too_recent", "incorrect"))
        )

        print("---")
        print("SMS_WAIT   :", fmt(r.get("sms_wait")))
        print("VOICE_WAIT :", fmt(r.get("voice_wait")))
        if r.get("retry_after"):
            print("RETRY_AFTER:", fmt(r.get("retry_after")))

        if kemungkinan_rate_limit:
            print("---")
            print(f"PERINGATAN KONTOL: reason={r.get('reason')} DENGAN FIELD WAIT SERAGAM.")
            print("INI NILAI PLACEHOLDER RATE-LIMIT IP, BUKAN COOLDOWN ASLI NOMOR TAI.")
            print("COBA DARI IP BERSIH / PAKAI WA_USE_PROXY=1 BANGSAT.")


if __name__ == "__main__":
    asyncio.run(_cli())
