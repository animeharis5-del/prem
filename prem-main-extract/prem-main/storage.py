"""Database JSON (cache di memori, simpan atomik) + sistem role:
owner / paket VIP-XVIP-VVIP (menggantikan "premium") / user biasa + order topup QRIS."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import config

try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo(config.TZ_NAME)
except Exception:  # tzdata tidak ada (mis. Windows) → WITA tetap UTC+8
    _TZ = timezone(timedelta(hours=8))

DB_FILE = config.BASE_DIR / "stats.json"

OWNER, USER = "owner", "user"
PREMIUM = "premium"  # kode generik untuk SEMUA paket aktif (vip/xvip/vvip)
KODE_PAKET = ("vip", "xvip", "vvip")

_db: Optional[dict] = None


def _hari_ini() -> str:
    return datetime.now(_TZ).strftime("%Y-%m-%d")


def _default() -> dict:
    return {
        "global": {"total_detek": 0, "total_ready": 0, "total_coldown": 0, "total_banned": 0,
                   "total_nosupport": 0, "maintenance": False},
        "users": {},
        "premium": {},
        "order": {},
        "blokir": {},
        "pengingat": [],
    }


def _migrate(d: dict) -> dict:
    """Lengkapi struktur lama dengan field baru supaya tidak KeyError saat ++.

    Penting: stats.json yang dibuat sebelum ada kategori 'nosupport' tidak punya
    kolom total_nosupport sama sekali — dulu hal ini memicu KeyError di catat_hasil.
    """
    base = _default()
    d.setdefault("global", {})
    for k, v in base["global"].items():
        if not isinstance(d["global"].get(k), int) or isinstance(d["global"].get(k), bool):
            d["global"][k] = v
    d.setdefault("users", {})
    d.setdefault("premium", {})
    d.setdefault("order", {})
    d.setdefault("blokir", {})
    d.setdefault("pengingat", [])
    # migrasi record user lama: tambahkan kolom statistik yang belum ada
    for u in d["users"].values():
        if not isinstance(u, dict):
            continue
        for k, v in _user_default().items():
            if not isinstance(u.get(k), int) or isinstance(u.get(k), bool):
                u[k] = v
    return d


def db() -> dict:
    global _db
    if _db is None:
        data = None
        if DB_FILE.exists():
            try:
                data = json.loads(DB_FILE.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"[DB] stats.json rusak, dibackup lalu dibuat baru: {e}")
                try:
                    os.replace(DB_FILE, DB_FILE.with_suffix(".json.rusak"))
                except OSError:
                    pass
        _db = _migrate(data if isinstance(data, dict) else _default())
        save()
    return _db


def save() -> None:
    """Tulis atomik (file sementara → rename) supaya tidak korup kalau bot mati mendadak."""
    if _db is None:
        return
    tmp = DB_FILE.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(_db, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, DB_FILE)
    except Exception as e:
        print(f"[DB ERROR] {e}")


# ═══════════════════════════════════════════════
#   👤 USER & STATISTIK
# ═══════════════════════════════════════════════

def _user_default() -> dict:
    return {"nama": "", "username": "", "total_detek": 0, "total_ready": 0,
            "total_coldown": 0, "total_banned": 0, "total_nosupport": 0, "hari_ini": 0, "hari_tgl": ""}


def get_user(user_id: int) -> dict:
    d = db()
    uid = str(user_id)
    u = d["users"].get(uid)
    if u is None:
        u = d["users"][uid] = _user_default()
        save()
    else:
        # jaminan kolom baru (mis. total_nosupport) ada di record lama → cegah KeyError
        perlu = False
        for k, v in _user_default().items():
            if not isinstance(u.get(k), int) or isinstance(u.get(k), bool):
                u[k] = v
                perlu = True
        if perlu:
            save()
    if u.get("hari_tgl") != _hari_ini():  # reset hitungan harian
        u["hari_tgl"] = _hari_ini()
        u["hari_ini"] = 0
    return u


RIWAYAT_MAX = 30


def _naik(d: dict, col: str) -> None:
    """d[col] += 1 dengan jaminan kolom selalu ada (anti KeyError untuk field baru)."""
    v = d.get(col)
    d[col] = (v if isinstance(v, int) and not isinstance(v, bool) else 0) + 1


def catat_hasil(user_id: int, nama: str, username: str, hasil: List[Tuple[str, str, int]]) -> None:
    """hasil: [(kind, nomor, wait)] — 'ready'/'cooldown'/'banned'/'nosupport' dihitung & masuk riwayat."""
    d = db()
    u = get_user(user_id)
    u["nama"], u["username"] = nama, username
    now = int(time.time())
    rw = u.setdefault("riwayat", [])
    harian = u.setdefault("harian", {})
    tgl = _hari_ini()
    for kind, nomor, wait in hasil:
        if kind not in ("ready", "cooldown", "banned", "nosupport"):
            continue
        col = "total_coldown" if kind == "cooldown" else f"total_{kind}"
        _naik(d["global"], "total_detek")
        _naik(u, "total_detek")
        _naik(u, "hari_ini")
        harian[tgl] = max(harian.get(tgl, 0), u["hari_ini"] - 1) + 1
        _naik(d["global"], col)
        _naik(u, col)
        rw.append({"t": now, "n": nomor, "k": kind, "w": wait})
    del rw[:-RIWAYAT_MAX]
    for lama in sorted(harian)[:-14]:      # simpan 14 hari terakhir saja
        del harian[lama]
    save()


def get_riwayat(user_id: int, n: int = 12) -> List[dict]:
    return list(reversed(get_user(user_id).get("riwayat", [])[-n:]))


def waktu_str(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, _TZ).strftime("%d/%m %H:%M")


PERIODE = {"hari": "Hari Ini", "minggu": "7 Hari", "total": "Sepanjang Waktu"}


def _skor(u: dict, periode: str) -> int:
    if periode == "total":
        return u.get("total_detek", 0)
    today = _hari_ini()
    hari_ini = u.get("hari_ini", 0) if u.get("hari_tgl") == today else 0
    if periode == "hari":
        return hari_ini
    h = u.get("harian", {})
    tgl = [(datetime.now(_TZ) - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    total = sum(h.get(t, 0) for t in tgl[1:])
    return total + max(h.get(today, 0), hari_ini)       # hari ini bisa lebih besar dari catatan harian (data lama)


def leaderboard(periode: str, n: int = 10, user_id: Optional[int] = None):
    """Return (baris, peringkat_user, skor_user). baris = [(uid, nama, skor, tersembunyi)] urut turun."""
    rows = []
    for uid, u in db()["users"].items():
        sk = _skor(u, periode)
        if sk > 0 and uid not in db()["blokir"]:
            rows.append((int(uid), u.get("nama") or "Tanpa Nama", sk, bool(u.get("sembunyi"))))
    rows.sort(key=lambda r: (-r[2], r[0]))
    rank = skor = None
    if user_id is not None:
        for i, r in enumerate(rows, 1):
            if r[0] == user_id:
                rank, skor = i, r[2]
                break
    return rows[:n], rank, skor


def toggle_sembunyi(user_id: int) -> bool:
    u = get_user(user_id)
    u["sembunyi"] = not u.get("sembunyi", False)
    save()
    return u["sembunyi"]


def top_users(n: int = 5) -> List[Tuple[str, str, int]]:
    return [(str(uid), nama, sk) for uid, nama, sk, _ in leaderboard("total", n)[0]]


def detek_hari_ini_global() -> int:
    tgl = _hari_ini()
    return sum(u.get("hari_ini", 0) for u in db()["users"].values() if u.get("hari_tgl") == tgl)


# ═══════════════════════════════════════════════
#   ⛔ BLOKIR USER
# ═══════════════════════════════════════════════

def is_blokir(user_id: int) -> bool:
    return str(user_id) in db()["blokir"] and not is_owner(user_id)


def blokir(user_id: int, oleh: int) -> None:
    db()["blokir"][str(user_id)] = {"t": int(time.time()), "oleh": oleh}
    save()


def unblokir(user_id: int) -> bool:
    if db()["blokir"].pop(str(user_id), None) is not None:
        save()
        return True
    return False


# ═══════════════════════════════════════════════
#   🔔 PENGINGAT COOLDOWN
# ═══════════════════════════════════════════════

def jumlah_pengingat(user_id: int) -> int:
    return sum(1 for e in db()["pengingat"] if e["uid"] == user_id)


def daftar_pengingat(user_id: int) -> List[dict]:
    return sorted((e for e in db()["pengingat"] if e["uid"] == user_id), key=lambda e: e["at"])


def tambah_pengingat(user_id: int, items: List[Tuple[str, int]], maks: int) -> Tuple[int, int]:
    """items: [(nomor, detik_lagi)]. Return (ditambah, ditolak_karena_penuh). Nomor sama → waktu diperbarui."""
    p = db()["pengingat"]
    now = time.time()
    ok = tolak = 0
    for nomor, detik in items:
        ada = next((e for e in p if e["uid"] == user_id and e["n"] == nomor), None)
        if ada:
            ada["at"] = now + detik + 15
            ok += 1
        elif jumlah_pengingat(user_id) >= maks:
            tolak += 1
        else:
            p.append({"uid": user_id, "n": nomor, "at": now + detik + 15})
            ok += 1
    save()
    return ok, tolak


def ambil_jatuh_tempo() -> List[dict]:
    p = db()["pengingat"]
    now = time.time()
    due = [e for e in p if e["at"] <= now]
    if due:
        p[:] = [e for e in p if e["at"] > now]
        save()
    return due


def maintenance_aktif() -> bool:
    return bool(db()["global"].get("maintenance"))


def set_maintenance(on: bool) -> None:
    db()["global"]["maintenance"] = on
    save()


def semua_user_id() -> List[int]:
    return [int(u) for u in db()["users"].keys()]


# ═══════════════════════════════════════════════
#   🎖️ ROLE
# ═══════════════════════════════════════════════

def is_owner(user_id: int) -> bool:
    return user_id in config.OWNER_IDS


def _premium_entry(user_id: int) -> Optional[dict]:
    """Entry paket yang masih aktif (yang kadaluarsa dibuang otomatis).

    Format lama: {"sampai":…} tanpa kode paket → dianggap "premium" biasa.
    """
    p = db()["premium"]
    e = p.get(str(user_id))
    if not e:
        return None
    sampai = e.get("sampai", 0)
    if sampai and sampai <= time.time():
        del p[str(user_id)]
        save()
        return None
    return e


def role_of(user_id: int) -> str:
    """owner | vip | xvip | vvip | premium | user — kunci untuk config.role_limit()."""
    if is_owner(user_id):
        return OWNER
    e = _premium_entry(user_id)
    if not e:
        return USER
    kode = str(e.get("paket") or "").lower()
    return kode if kode in KODE_PAKET else PREMIUM


def limit_of(user_id: int) -> dict:
    return config.role_limit(role_of(user_id))


def sisa_paket_hari(user_id: int) -> Optional[int]:
    """None = permanen / bukan paket aktif."""
    e = _premium_entry(user_id)
    if not e or not e.get("sampai"):
        return None
    return max(1, int((e["sampai"] - time.time() + 86399) // 86400))


def label_role(user_id: int) -> str:
    r = role_of(user_id)
    if r == OWNER:
        return "👑 Owner / Admin"
    if r in KODE_PAKET:
        pk = config.PAKET[r]
        sisa = sisa_paket_hari(user_id)
        return f"{pk['emoji']} {pk['label']}" + (f" ({sisa} hari lagi)" if sisa else " (permanen)")
    if r == PREMIUM:
        sisa = sisa_paket_hari(user_id)
        return "⭐ Premium" + (f" ({sisa} hari lagi)" if sisa else "")
    return "🙂 Biasa"


def tambah_paket(user_id: int, kode: str, hari: Optional[int], oleh: int) -> Optional[float]:
    """Aktifkan/perpanjang paket `kode` (vip/xvip/vvip/premium). hari=None → permanen.

    Return waktu kadaluarsa (epoch) atau None kalau permanen.
    """
    now = time.time()
    if hari is None:
        sampai = 0
    else:
        e = _premium_entry(user_id)
        base = e["sampai"] if (e and e.get("sampai", 0) > now) else now  # perpanjang kalau masih aktif
        sampai = base + hari * 86400
    db()["premium"][str(user_id)] = {"sampai": sampai, "paket": kode, "ditambah": int(now), "oleh": oleh}
    save()
    return sampai or None


# ── kompatibilitas nama lama ────────────────────────────────
tambah_premium = tambah_paket
sisa_premium_hari = sisa_paket_hari


def hapus_premium(user_id: int) -> bool:
    p = db()["premium"]
    if str(user_id) in p:
        del p[str(user_id)]
        save()
        return True
    return False


def daftar_premium() -> List[Tuple[int, Optional[int], str, str]]:
    """[(id, sisa_hari | None=permanen, nama, kode_paket)]"""
    out = []
    for uid in list(db()["premium"].keys()):
        e = _premium_entry(int(uid))
        if e is None:
            continue
        nama = db()["users"].get(uid, {}).get("nama") or ""
        out.append((int(uid), sisa_paket_hari(int(uid)), nama, str(e.get("paket") or PREMIUM).lower()))
    return out


# ═══════════════════════════════════════════════
#   🧾 ORDER TOPUP (pembayaran QRIS)
# ═══════════════════════════════════════════════
ORDER_MAX_HISTORIS = 200


def random4() -> str:
    import random as _r
    return "".join(_r.choice("0123456789ABCDEF") for _ in range(4))


def buat_order(user_id: int, kode: str, hari: int, nominal: int) -> dict:
    """Simpan order baru (status pending). Return dict order."""
    d = db()
    oid = f"DTK{int(time.time())}{random4()}-{user_id}"
    o = {
        "id": oid, "uid": user_id, "paket": kode, "hari": hari, "amount": nominal,
        "status": "pending", "trx": "", "qr_url": "", "payment_url": "",
        "total_amount": 0, "created": int(time.time()), "paid": 0,
    }
    d["order"][oid] = o
    hist = [k for k, v in d["order"].items() if v.get("status") != "pending"]
    for lama in sorted(hist, key=lambda k: d["order"][k].get("created", 0))[:-ORDER_MAX_HISTORIS]:
        d["order"].pop(lama, None)
    save()
    return o


def get_order(oid: str) -> Optional[dict]:
    return db()["order"].get(oid)


def order_uid_pending(user_id: int) -> Optional[dict]:
    for o in db()["order"].values():
        if o.get("uid") == user_id and o.get("status") == "pending":
            return o
    return None


def order_trx(trx_id: str) -> Optional[dict]:
    for o in db()["order"].values():
        if o.get("trx") == str(trx_id):
            return o
    return None


def update_order(oid: str, **fields) -> Optional[dict]:
    o = db()["order"].get(oid)
    if not o:
        return None
    o.update(fields)
    save()
    return o


def _owner_yang_mengaktifkan() -> int:
    ids = sorted(config.OWNER_IDS)
    return ids[0] if ids else 0


def lunasi_order(oid: str, total_amount: int = 0) -> Tuple[bool, str]:
    """Tandai order lunas (idempoten) + aktifkan paketnya. Return (berhasil, pesan)."""
    o = db()["order"].get(oid)
    if not o:
        return False, "Order tidak ditemukan."
    if o.get("status") == "paid":
        return False, "sudah-lunas"
    tambah_paket(o["uid"], o["paket"], o["hari"], _owner_yang_mengaktifkan())
    o["status"] = "paid"
    o["paid"] = int(time.time())
    if total_amount:
        o["total_amount"] = total_amount
    save()
    return True, "ok"


def batalkan_order(oid: str) -> bool:
    o = db()["order"].get(oid)
    if not o or o.get("status") != "pending":
        return False
    o["status"] = "cancelled"
    save()
    return True


def daftar_order(status: Optional[str] = None, n: int = 30) -> List[dict]:
    rows = [o for o in db()["order"].values() if (status is None or o.get("status") == status)]
    rows.sort(key=lambda o: o.get("created", 0), reverse=True)
    return rows[:n]


def ringkas_order() -> dict:
    out = {"pending": 0, "paid": 0, "expired": 0, "failed": 0, "cancelled": 0, "om": 0}
    for o in db()["order"].values():
        st = o.get("status", "pending")
        out[st] = out.get(st, 0) + 1
        if st == "paid":
            out["om"] += int(o.get("total_amount") or o.get("amount") or 0)
    return out
