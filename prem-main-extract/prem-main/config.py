"""Konfigurasi bot. Nilai sensitif dibaca dari environment / file .env (jangan hardcode token)."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_env_file(path: Path) -> None:
    """Loader .env sederhana (KEY=VALUE), tanpa dependency tambahan."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env_file(BASE_DIR / ".env")


def _int_list(raw: str) -> list:
    out = []
    for p in raw.replace(";", ",").split(","):
        p = p.strip()
        if p.lstrip("-").isdigit():
            out.append(int(p))
    return out


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


# ── Telegram ───────────────────────────────────────────────
TOKEN_TELEGRAM = os.environ.get("BOT_TOKEN", "8592901951:AAHzd2PKkGI96a1nM1q6LurEva7E1VOOgIQ").strip()
# Owner (bisa lebih dari satu, pisahkan koma): OWNER_IDS=8684272520,123456
OWNER_IDS = set(_int_list(os.environ.get("OWNER_IDS", "8684272520")))
FOTO_DASHBOARD = os.environ.get("FOTO_DASHBOARD") or "https://cdn.nekohime.site/file/7yek4tzg.jpg"
TZ_NAME = os.environ.get("BOT_TZ", "Asia/Makassar")  # untuk reset hitungan "hari ini"

# Username bot (untuk link tombol Salin @username?text=…). Kosong → dideteksi otomatis saat bot menyala.
BOT_USERNAME = os.environ.get("BOT_USERNAME", "").strip()

# ── WhatsApp client yang ditiru ────────────────────────────
VERSI_MARK = os.environ.get("versi_mark") or "2.26.33.73"
WA_MARK = os.environ.get("WA_mark") or "0a1mLfGUIBVrMKF1RdvLI5lkRBvof6vn0fD2QRSM"
MEREK = ["Apple-iPhone_13", "Apple-iPhone_14_Pro", "Apple-iPhone_15", "Apple-iPhone_SE_3"]
IOS_VERSI = ["17.5.1", "18.0", "18.1.1"]

# ── Kecepatan & keamanan pengecekan ─────────────────────────
# Diatur "agak pelan tapi stabil": batch kecil + jeda sehat + retry lebih banyak,
# supaya hasil cek konsisten dan jumlah nomor yang GAGAL ("gagal dicek") diminimalkan.
# Naikkan CEK_LEBAR / turunkan JEDA_BATCH hanya kalau server WA terbukti stabil.
CEK_LEBAR = int(_float("CEK_LEBAR", 3))          # nomor dicek berengan dalam satu jalur
JEDA_NOMOR = _float("JEDA_NOMOR", 0.6)           # jeda antar-permintaan per jalur (detik)
JEDA_BATCH = _float("JEDA_BATCH", 1.5)           # jeda antar gelombang batch (detik)
MAX_COBA = int(_float("MAX_COBA", 4))            # percobaan ulang per nomor saat error/timeout
TUNDA_COBA = _float("TUNDA_COBA", 1.2)           # jeda dasar sebelum coba lagi (dikalikan nomor percobaan)
TIMEOUT_CEK = _float("TIMEOUT_CEK", 20.0)        # batas tunggu satu request ke server WA
AUTO_ULANG = int(_float("AUTO_ULANG", 3))        # putaran cek-ulang otomatis untuk nomor yang gagal
CACHE_DETIK = _float("CACHE_DETIK", 45)          # nomor yang sama dalam rentang ini tidak dikirim ulang ke WA (0 = mati)
# Jeda minimum antar-request PER jalur lama (dipakai bila suatu saat pakai proxy tunggal).
REQ_GAP = _float("REQ_GAP", 3.0)
REQ_GAP_MAX = _float("REQ_GAP_MAX", 15.0)        # batas atas pelebaran jeda otomatis
COOLOFF_AWAL = _float("COOLOFF_AWAL", 10.0)      # istirahat jalur saat pertama kena limit (lalu 20s, 40s, …)
COOLOFF_MAX = _float("COOLOFF_MAX", 120.0)       # istirahat terlama
MAX_LIMIT_HIT = int(_float("MAX_LIMIT_HIT", 8))  # berapa kali satu nomor boleh kena limit sebelum menyerah
# Proxy opsional (pisahkan koma). Tiap proxy = 1 jalur tambahan = kecepatan bertambah.
# Contoh: PROXIES=http://user:pass@1.2.3.4:8080,http://5.6.7.8:3128
PROXIES = [p.strip() for p in (os.environ.get("PROXIES") or os.environ.get("PROXY") or "").split(",") if p.strip()]
VERIFY_SSL = os.environ.get("VERIFY_SSL", "1").strip() not in ("0", "false", "False", "no")

# ── Tampilan real-time di Telegram ─────────────────────────
LIVE_INTERVAL = _float("LIVE_INTERVAL", 1.5)     # tiap sekian detik pesan hasil diedit ulang
LIVE_MAX_BARIS = int(_float("LIVE_MAX_BARIS", 40))  # maks baris daftar per kategori di pesan live

# ── Role & batas ───────────────────────────────────────────
# max      = jumlah nomor maksimal per sekali /cek
# cooldown = jeda antar /cek (detik), 0 = tanpa jeda
# conc     = berapa nomor diproses bersamaan dalam satu batch (per jalur)
# ingat    = maksimal pengingat cooldown aktif
LIMIT = {
    "owner": {"max": 200, "cooldown": 0, "conc": 4, "ingat": 100},
    "user":  {"max": 5,   "cooldown": 10, "conc": 2, "ingat": 3},
}

# ═══════════════════════════════════════════════
#   💎 PAKET TOPUP (VIP / XVIP / VVIP)
#   Struktur datanya sama dengan LIMIT, kunci = kode paket.
# ═══════════════════════════════════════════════
PAKET = {
    "vip":  {"label": "VIP",  "emoji": "💎", "max": 20,  "cooldown": 0, "conc": 3, "ingat": 20,
             "desc": "Detek 20 nomor, limit 5 nomor"},
    "xvip": {"label": "XVIP", "emoji": "🔥", "max": 50,  "cooldown": 0, "conc": 4, "ingat": 20,
             "desc": "Detek 50 nomor, limit 10 nomor"},
    "vvip": {"label": "VVIP", "emoji": "👑", "max": 100, "cooldown": 0, "conc": 5, "ingat": 50,
             "desc": "Detek 100 nomor, limit unlimited nomor"},
}

# Harga per paket (Rp) berdasarkan durasi (hari)
HARGA = {
    "vip":  {1: 2500,  3: 5500,  7: 12000, 30: 50000},
    "xvip": {1: 5000,  3: 15000, 7: 30000, 30: 80000},
    "vvip": {1: 8000,  3: 20000, 7: 50000, 30: 110000},
}

DURASI = [1, 3, 7, 30]  # pilihan durasi (hari)


def harga(kode_paket: str, hari: int):
    return HARGA.get(kode_paket, {}).get(hari)


def role_limit(role: str) -> dict:
    """Batas teknis untuk role owner/user maupun kode paket (vip/xvip/vvip)."""
    if role in LIMIT:
        return LIMIT[role]
    return PAKET.get(role, LIMIT["user"])


# ── Pembayaran QRIS (Open API BuatQris — lihat konfigurasi-open-api.txt) ──
# Kredensial dibaca dari environment / file .env — JANGAN menaruh secret_token
# di kode, repo, atau frontend.
#   BUATQRIS_BASE_URL, BUATQRIS_ACCOUNT_ID, BUATQRIS_SECRET_TOKEN,
#   BUATQRIS_SIGNING_SECRET (verifikasi webhook), BUATQRIS_TEST (1 = sandbox)
BUATQRIS = {
    "base_url": (os.environ.get("BUATQRIS_BASE_URL") or "https://api.buatqris.site").rstrip("/"),
    "account_id": os.environ.get("BUATQRIS_ACCOUNT_ID", "").strip(),
    "secret_token": os.environ.get("BUATQRIS_SECRET_TOKEN", "").strip(),
    "signing_secret": os.environ.get("BUATQRIS_SIGNING_SECRET", "").strip(),
    "test": os.environ.get("BUATQRIS_TEST", "").strip().lower() in ("1", "true", "yes", "test"),
    "cek_status_mulai": _float("BUATQRIS_CEK_MULAI", 15),   # detik sebelum polling status pertama
    "cek_status_jeda": _float("BUATQRIS_CEK_JEDA", 25),     # ≥21 dtk (API membatasi 1 cek/20 dtk per transaksi)
    "cek_status_max": int(_float("BUATQRIS_CEK_MAX", 40)),  # maksimal jumlah cek status per transaksi
}
