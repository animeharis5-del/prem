import asyncio
import html
import logging
import re
import time
from datetime import datetime

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    MenuButtonCommands,
    Update,
)
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import phonenumbers

import config
import storage
from cek_wait import (
    detect,
    versi_mark,
    CekBapaklu,
)

try:  # integrasi pembayaran opsional — bot tetap jalan tanpa kredensial QRIS
    import bayar
except Exception as _e:  # pragma: no cover
    bayar = None
    log0 = logging.getLogger("detekbot")

# Status hasil pengecekan (dipakai di seluruh bot)
BATAL = "batal"        # dibatalkan user sebelum dicek
READY = "ready"        # siap OTP
COOLDOWN = "cooldown"  # masih cooldown
BANNED = "banned"      # akun terbanned WA
NOSUPPORT = "nosupport"  # nomor tidak support clone
INVALID = "invalid"    # nomor tidak valid
ERROR = "error"        # gagal dicek (server error dll.)


class CekResult:
    """Hasil pengecekan satu nomor."""

    def __init__(self, nomor: str, kind: str, wait: int = 0, detail: str = "", selesai: float = 0.0):
        self.nomor = nomor
        self.kind = kind
        self.wait = wait
        self.detail = detail
        self.selesai = selesai or time.time()  # kapan hasil didapat (untuk urutan live)


def extract_numbers(teks: str):
    """Ambil semua nomor dari teks. Return (valid, invalid)."""
    valid, invalid = [], []
    for baris in teks.splitlines():
        for token in re.findall(r"\+?\d[\d\s\-().]{5,}", baris):
            digits = re.sub(r"\D", "", token)
            if len(digits) < 8:
                continue
            if not token.strip().startswith("+"):
                if digits.startswith("0"):
                    digits = "62" + digits.lstrip("0")  # 08xxx -> 628xxx
                elif not digits.startswith("1"):
                    digits = "1" + digits  # 62xxx -> +62xxx
            nomor = "+" + digits
            if nomor not in valid and nomor not in invalid:
                try:
                    parsed = phonenumbers.parse(nomor, None)
                    ok = phonenumbers.is_valid_number(parsed)
                except phonenumbers.NumberParseException:
                    ok = False
                (valid if ok else invalid).append(nomor)
    return valid, invalid

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.WARNING)
log = logging.getLogger("detekbot")

user_cooldowns: dict = {}    # user_id -> waktu selesai cek terakhir
sedang_cek: dict = {}        # user_id -> asyncio.Event (set = batalkan)
terakhir: dict = {}          # user_id -> {"error": [...], "cooldown": [(nomor, wait)], "t": ts}
_owner_menu_ok: set = set()
pemantau: dict = {}          # order_id -> asyncio.Task polling pembayaran QRIS

esc = html.escape
EMOJI_ROLE = {storage.OWNER: "👑", storage.PREMIUM: "⭐", storage.USER: "🙂"}


def emoji_role(role: str) -> str:
    if role in config.PAKET:
        return config.PAKET[role]["emoji"]
    return EMOJI_ROLE.get(role, "🙂")


# ═══════════════════════════════════════════════
#   🧰 HELPER TAMPILAN
# ═══════════════════════════════════════════════

def format_waktu(detik) -> str:
    """Durasi ringkas real-time: `12j 31m 41s`, `1m 5s`, `44m`."""
    try:
        d = int(detik)
    except (ValueError, TypeError):
        return "Tidak diketahui"
    if d < 0:
        return "Dinonaktifkan"
    if d == 0:
        return "Siap OTP"
    jam, sisa = divmod(d, 3600)
    menit, det = divmod(sisa, 60)
    bagian = []
    if jam:
        bagian.append(f"{jam}j")
    if menit or jam:
        bagian.append(f"{menit}m")
    if det or not bagian:
        bagian.append(f"{det}s")
    return " ".join(bagian)


def _is_banned_detail(detail: str) -> bool:
    d = (detail or "").lower()
    return "banned" in d or "blocked" in d or "terbanned" in d


def klasifikasi(res: dict) -> tuple:
    """Petakan respons engine cek_wait -> (kind, wait, detail)."""
    st = res.get("status")
    reason = res.get("reason")
    sms_wait = res.get("sms_wait")
    detail = str(reason or st or "")

    if st == "invalid" or reason in ("no_routes", "incorrect"):
        return INVALID, 0, detail
    if reason == "blocked":
        # Bedakan akun terbanned vs nomor tidak support clone
        if _is_banned_detail(str(res.get("msg") or res.get("message") or "")):
            return BANNED, 0, "Banned"
        return NOSUPPORT, 0, detail
    if isinstance(sms_wait, (int, float)) and not isinstance(sms_wait, bool):
        if sms_wait > 0:
            return COOLDOWN, int(sms_wait), detail
        return READY, 0, detail
    if st == "sent":
        return READY, 0, detail
    return ERROR, 0, detail


def fmt_durasi(detik: float) -> str:
    d = int(max(0, detik))
    if d < 60:
        return f"{d} dtk"
    m, s = divmod(d, 60)
    if m < 60:
        return f"{m} mnt {s} dtk" if s else f"{m} mnt"
    h, m = divmod(m, 60)
    return f"{h} jam {m} mnt"


def bar(frac: float, n: int = 10) -> str:
    f = max(0, min(n, int(round(frac * n))))
    return "▰" * f + "▱" * (n - f)


def rp(n) -> str:
    """Rp 110.000"""
    try:
        return f"Rp {int(n):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "-"


def salin_btn(teks: str, label: str = "📋 Salin Semua Nomor"):
    """Tombol yang menyalin teks ke input Telegram saat di-tap (t.me link).

    `?text=` mengisi kolom pesan; `&aftertext=` menampilkan sisa teks di bawah
    tombol supaya tidak terlihat seperti terpotong.
    """
    from urllib.parse import quote
    ctx = getattr(salin_btn, "_ctx", None)
    uname = config.BOT_USERNAME or (ctx.bot.username if ctx else "")
    if not uname:
        return None
    prefix = f"https://t.me/{uname}?text="
    budget = 4096 - len(prefix) - len("&aftertext=") - len(label) - 8
    isi = quote(teks[:budget])
    sisa_teks = teks[budget:].lstrip("\n")
    url = prefix + isi
    if sisa_teks:
        url += "&aftertext=" + quote("\n" + sisa_teks[:2000])
    return InlineKeyboardButton(label, url=url)


def keyboard_salin(daftar: list, label: str = "📋 Salin Semua Nomor"):
    """Keyboard dengan tombol tap-to-copy untuk daftar nomor."""
    rows = []
    btn = salin_btn("\n".join(daftar), label)
    if btn:
        rows.append([btn])
    return rows


def kotak(*baris: str) -> str:
    return "<blockquote>" + "\n".join(baris) + "</blockquote>"


def status_server() -> str:
    return "🟢 Normal"


def teks_limit(uid: int) -> str:
    lim = storage.limit_of(uid)
    cd = f"{lim['cooldown']} detik" if lim["cooldown"] else "tanpa cooldown"
    return f"{lim['max']} nomor/cek · {cd}"


# ═══════════════════════════════════════════════
#   🎨 KEYBOARD & HALAMAN
# ═══════════════════════════════════════════════

def keyboard_menu_utama(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🔍 Detek Nomor", callback_data="menu_detek")],
        [InlineKeyboardButton("💎 Topup / Upgrade", callback_data="topup_home"),
         InlineKeyboardButton("📡 Status", callback_data="menu_status")],
        [InlineKeyboardButton("📖 Panduan", callback_data="menu_katalog"),
         InlineKeyboardButton("📜 Riwayat", callback_data="menu_riwayat")],
        [InlineKeyboardButton("🔔 Pengingat", callback_data="menu_ingat"),
         InlineKeyboardButton("🏆 Leaderboard", callback_data="menu_lb")],
    ]
    if storage.is_owner(user_id):
        rows.append([InlineKeyboardButton("👑 Panel Owner", callback_data="adm_home")])
    return InlineKeyboardMarkup(rows)


def keyboard_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data="menu_home")]])


def teks_dashboard(user) -> str:
    uid = user.id
    nama = esc(user.first_name or "Tanpa Nama")
    username = f"@{esc(user.username)}" if user.username else "<i>(tidak ada)</i>"
    u = storage.get_user(uid)
    d = storage.db()
    role = storage.role_of(uid)
    return (
        "<b>✦ D E T E K B O T ✦</b>\n"
        "<i>Cek status nomor WhatsApp · aman &amp; stabil</i>\n\n"
        f"{emoji_role(role)} <b>{nama}</b>\n"
        + kotak(
            f"👤 {username}",
            f"🆔 <code>{uid}</code>",
            f"🎖 {storage.label_role(uid)}",
        )
        + "\n📊 <b>Aktivitasmu</b>\n"
        + kotak(
            f"🔍 Total detek : <b>{u['total_detek']}</b>",
            f"📅 Hari ini    : <b>{u['hari_ini']}</b>",
            f"📦 Batas       : {teks_limit(uid)}",
        )
        + "\n🌐 <b>Global</b>\n"
        + kotak(
            f"👥 Pengguna : <b>{len(d['users'])}</b>",
            f"🔎 Detek    : <b>{d['global']['total_detek']}</b>",
            f"📡 Server   : {status_server()}",
        )
    )


def teks_panduan(uid: int) -> str:
    L = config.LIMIT
    P = config.PAKET
    return (
        "<b>📖 PANDUAN</b>\n\n"
        "<b>Cara detek</b>\n"
        + kotak(
            "/detek +62812xxxx",
            "/detek +62812xxxx +60123xxxx",
            "atau kirim langsung nomor (satu per baris)",
        )
        + "\n<b>Perintah</b>\n"
        + kotak(
            "/detek – detek nomor",
            "/ulang – cek ulang nomor yang gagal",
            "/riwayat – 12 hasil terakhirmu",
            "/leaderboard – peringkat pendetek (/sembunyi = anonim)",
            "/status – kondisi server &amp; kecepatan",
            "/topup – beli paket VIP/XVIP/VVIP",
            "/bantuan – halaman ini",
        )
        + "\n<b>Role &amp; batas</b>\n"
        + kotak(
            f"🙂 Biasa : {L['user']['max']} nomor, cooldown {L['user']['cooldown']} dtk",
            f"{P['vip']['emoji']} VIP   : {P['vip']['desc']}, tanpa cooldown",
            f"{P['xvip']['emoji']} XVIP  : {P['xvip']['desc']}, tanpa cooldown",
            f"{P['vvip']['emoji']} VVIP  : {P['vvip']['desc']}, tanpa cooldown",
            f"👑 Owner  : {L['owner']['max']} nomor, tanpa cooldown",
        )
        + "\n<i>Pengecekan dibuat pelan &amp; teratur supaya tidak kena rate limit WhatsApp.</i>"
    )


def teks_status(uid: int) -> str:
    baris = [
        f"📡 Server    : {status_server()}",
        f"🚦 Jalur     : Proxy / Direct aktif",
        f"🧮 Antrian   : {len(sedang_cek)} batch berjalan",
    ]
    teks = "<b>📡 STATUS SERVER</b>\n" + kotak(*baris)
    return teks


def teks_riwayat(uid: int) -> str:
    rw = storage.get_riwayat(uid, 12)
    if not rw:
        return "<b>📜 RIWAYAT</b>\n" + kotak("Belum ada riwayat. Mulai dengan /detek ✨")
    ikon = {"ready": "✅", "cooldown": "⏳", "banned": "❌", "nosupport": "👥", "invalid": "🚫", "error": "🔁"}
    baris = []
    for e in rw:
        ket = f" · {format_waktu(e['w'])}" if e["k"] == "cooldown" else ""
        baris.append(f"{ikon.get(e['k'], '•')} <code>+{e['n']}</code>{ket}  <i>{storage.waktu_str(e['t'])}</i>")
    return "<b>📜 RIWAYAT TERAKHIR</b>\n" + kotak(*baris)


def teks_pengingat(uid: int) -> str:
    items = storage.daftar_pengingat(uid)
    maks = storage.limit_of(uid)["ingat"]
    if not items:
        return ("<b>🔔 PENGINGAT</b>\n"
                + kotak("Belum ada pengingat.", "Setelah /detek, tekan “🔔 Ingatkan saat siap OTP” "
                        "dan bot akan mengabarimu begitu cooldown selesai."))
    now = time.time()
    baris = [f"<code>+{e['n']}</code> ➜ <b>{fmt_durasi(e['at'] - now)}</b>" for e in items[:30]]
    return f"<b>🔔 PENGINGAT ({len(items)}/{maks})</b>\n" + kotak(*baris)


MEDALI = ["🥇", "🥈", "🥉"]


def _nama_lb(nama: str, tersembunyi: bool, uid: int, me: int) -> str:
    if tersembunyi:
        return "Anonim" + (" (kamu)" if uid == me else "")
    nama = esc(nama if len(nama) <= 16 else nama[:15] + "…")
    return f"<b>{nama}</b>" + (" 👈" if uid == me else "")


def teks_leaderboard(periode: str, me: int) -> str:
    rows, rank, skor = storage.leaderboard(periode, 10, me)
    judul = f"<b>🏆 LEADERBOARD · {storage.PERIODE[periode]}</b>"
    if not rows:
        return judul + "\n" + kotak("Belum ada data. Jadilah yang pertama dengan /detek ✨")
    baris = []
    for i, (uid, nama, sk, hide) in enumerate(rows):
        lencana = MEDALI[i] if i < 3 else f"{i + 1}."
        baris.append(f"{lencana} {_nama_lb(nama, hide, uid, me)} — <b>{sk:,}</b> detek")
    teks = judul + "\n" + kotak(*baris)
    if rank and rank > 10:
        teks += f"\n📍 Peringkatmu: <b>#{rank}</b> ({skor:,} detek)"
    elif not rank:
        teks += "\n📍 Kamu belum masuk daftar — mulai /detek untuk naik peringkat!"
    return teks + "\n<i>/sembunyi untuk tampil sebagai Anonim</i>"


def keyboard_lb(periode: str) -> InlineKeyboardMarkup:
    tab = lambda kode, label: InlineKeyboardButton(("• " if kode == periode else "") + label, callback_data=f"lb_{kode}")
    return InlineKeyboardMarkup([
        [tab("hari", "📅 Hari Ini"), tab("minggu", "🗓 7 Hari"), tab("total", "🌟 Total")],
        [InlineKeyboardButton("🏠 Menu Utama", callback_data="menu_home")],
    ])


def teks_stats_owner() -> str:
    d = storage.db()
    g = d["global"]
    tot = max(1, g["total_detek"])
    top = [f"{i + 1}. {esc(n)} — <b>{c}</b>" for i, (_, n, c) in enumerate(storage.top_users(5))] or ["-"]
    return (
        "<b>👑 PANEL ADMIN</b>\n"
        + kotak(
            f"👥 Pengguna      : <b>{len(d['users']):,}</b>",
            f"🔎 Total detek   : <b>{g['total_detek']:,}</b>",
            f"📅 Detek hari ini: <b>{storage.detek_hari_ini_global():,}</b>",
            f"✅ Siap OTP      : {g['total_ready']:,} ({g['total_ready'] * 100 // tot}%)",
            f"⏳ Cooldown      : {g['total_coldown']:,} ({g['total_coldown'] * 100 // tot}%)",
            f"❌ Terbanned     : {g['total_banned']:,} ({g['total_banned'] * 100 // tot}%)",
            f"👥 Gak Support   : {g.get('total_nosupport', 0):,} ({g.get('total_nosupport', 0) * 100 // tot}%)",
            f"⭐ Premium aktif : <b>{len(storage.daftar_premium())}</b>",
            f"🚫 Diblokir      : <b>{len(d['blokir'])}</b>",
        )
        + "\n<b>🏆 Top pengguna</b>\n" + kotak(*top)
    )


HEADER = (
    "<b>🔎 HASIL DETEKSI</b>\n"
    "<blockquote>⚠️ Waktu cooldown bisa berubah sewaktu-waktu.\n"
    "Segera login ke nomor yang telah melakukan detek.</blockquote>"
)
BATAS_BLOK = 3000
BATAS_PESAN = 3900


def _blok(luar: str, dalam: str, baris: list) -> list:
    chunks, cur, n = [], [], 0
    for b in baris:
        if cur and n + len(b) + 1 > BATAS_BLOK:
            chunks.append(cur)
            cur, n = [], 0
        cur.append(b)
        n += len(b) + 1
    if cur:
        chunks.append(cur)
    out = []
    for i, chunk in enumerate(chunks):
        isi = (dalam + "\n\n" if (dalam and i == 0) else "") + "\n".join(chunk)
        out.append(f"{luar if i == 0 else ''}<blockquote>{isi}</blockquote>")
    return out


def _kolom(kiri: list, kanan: list, judul_l: str, judul_r: str) -> list:
    """Bikin baris dua kolom rata monospace: `Nomor   Sisa`."""
    w = max([len(x) for x in kiri] + [len(judul_l), 6])
    p = max([len(x) for x in kanan] + [len(judul_r), 4]) if kanan else 0
    baris = []
    if p:
        baris.append(judul_l.ljust(w) + "  " + judul_r.ljust(p))
    else:
        baris.append(judul_l)
    for i, k in enumerate(kiri):
        if p:
            rr = kanan[i] if i < len(kanan) else ""
            baris.append(k.ljust(w) + "  " + rr.ljust(p))
        else:
            baris.append(k)
    return baris


def _blok_tabel(luar: str, dalam: str, baris: list) -> list:
    """Pecah daftar baris tabel jadi beberapa chunk agar tidak melebihi batas Telegram.

    Judul kolom diulang di tiap halaman supaya tetap terbaca.
    """
    if not baris:
        return []
    header = baris[0] if ("Nomor" in baris[0]) else ""
    isi = baris[1:] if header else baris
    # perkirakan panjang tag pembungkus
    selubung = len(luar) + len(dalam) + 60
    chunks, cur, n = [], [], 0
    for b in isi:
        tambah = len(b) + 1
        if cur and selubung + n + tambah > BATAS_BLOK:
            chunks.append(cur)
            cur, n = [], 0
        cur.append(b)
        n += tambah
    if cur:
        chunks.append(cur)
    out = []
    for i, chunk in enumerate(chunks):
        # judul kolom diulang di tiap halaman supaya tetap terbaca
        isi = (header + "\n" if header else "") + "\n".join(chunk)
        intro = (dalam + "\n\n") if (dalam and i == 0) else ""
        out.append(f"{luar if i == 0 else ''}{intro}<blockquote><pre>{esc(isi)}</pre></blockquote>")
    return out


def susun_blok(results: list, maks_baris: int = 0) -> list:
    """Blok hasil deteksi: dipisah per kategori & diurutkan real-time.

    - Siap OTP     : urut nomor paling cepat selesai duluan
    - Cooldown     : urut dari sisa waktu TERPENDEK
    - Terbanned    : daftar akun yang kena banned
    - Gak Support  : daftar nomor yang tidak support clone

    maks_baris > 0 → tiap kategori dipotong sebatas itu (dipakai pesan live agar
    tetap muat dalam satu pesan); sisanya ditandai `… +N lainnya`.
    """
    pilih = lambda k: [r for r in results if r and r.kind == k]
    cd = sorted(pilih(COOLDOWN), key=lambda r: (r.wait, r.selesai, r.nomor))
    ban, ns, inv, err = pilih(BANNED), pilih(NOSUPPORT), pilih(INVALID), pilih(ERROR)

    def potong(items, kunci):
        if maks_baris and len(items) > maks_baris:
            return items[:maks_baris], f"… +{len(items) - maks_baris} lainnya"
        return items, ""

    blok = []
    if (ready := pilih(READY)):
        # Nomor yang lebih dulu selesai dideteksi dianggap lebih dulu siap
        items, extra = potong(sorted(ready, key=lambda r: r.selesai), READY)
        kiri = [f"{r.nomor}" for r in items] + ([extra] if extra else [])
        blok += _blok_tabel(f"✅ <b>Nomor siap OTP</b> · {len(ready)}\n", "", _kolom(kiri, [], "Nomor", ""))
    if cd:
        items, extra = potong(cd, COOLDOWN)
        kiri = [f"{r.nomor}" for r in items] + ([extra] if extra else [])
        kanan = [f": {format_waktu(r.wait)}" for r in items] + ([""] if extra else [])
        blok += _blok_tabel(f"⏱ <b>Nomor yang sedang cooldown</b> · {len(cd)}\n",
                            "<i>urut dari sisa waktu paling pendek</i>",
                            _kolom(kiri, kanan, "Nomor", "Sisa"))
    if ban:
        items, extra = potong(ban, BANNED)
        kiri = [f"{r.nomor}" for r in items] + ([extra] if extra else [])
        kanan = [": ❌ Banned" for _ in items] + ([""] if extra else [])
        blok += _blok_tabel(f"❌ <b>Nomor yang tidak dapat menerima OTP — Terbanned WA</b> · {len(ban)}\n", "",
                            _kolom(kiri, kanan, "Nomor", "Status"))
    if ns:
        items, extra = potong(ns, NOSUPPORT)
        kiri = [f"{r.nomor}" for r in items] + ([extra] if extra else [])
        kanan = [": 👥 Gak Support" for _ in items] + ([""] if extra else [])
        blok += _blok_tabel(f"👥 <b>Gak Support Clone</b> · {len(ns)}\n", "", _kolom(kiri, kanan, "Nomor", "Status"))
    if inv:
        items, extra = potong(inv, INVALID)
        kiri = [f"{r.nomor}" for r in items] + ([extra] if extra else [])
        blok += _blok_tabel(f"🚫 <b>Nomor tidak valid</b> · {len(inv)}\n", "", _kolom(kiri, [], "Nomor", ""))
    if err:
        items, extra = potong(err, ERROR)
        kiri = [f"{r.nomor}" for r in items] + ([extra] if extra else [])
        blok += _blok_tabel(f"🔁 <b>Gagal dicek</b> · {len(err)}\n",
                            "<i>si mark ngambek jir, silahkan cek ulang</i>",
                            _kolom(kiri, [], "Nomor", ""))
    return blok


def susun_pesan(blok: list, footer: str) -> list:
    pages, cur = [], HEADER
    for b in blok + [footer]:
        if len(cur) + len(b) + 2 > BATAS_PESAN and cur:
            pages.append(cur)
            cur = b
        else:
            cur = f"{cur}\n\n{b}"
    pages.append(cur)
    return pages


def footer_hasil(durasi: float, total: int, hasil: list, catatan: str = "") -> str:
    batal = sum(1 for r in hasil if r and r.kind == BATAL)
    baris = [f"⏱ <b>{durasi}s</b> · 📦 {total} nomor · 🏷 <code>v{versi_mark}</code>"]
    if batal:
        baris.append(f"⏹ Dibatalkan: {batal} nomor belum sempat dicek")
    if catatan:
        baris.append(catatan)
    return kotak(*baris)


def keyboard_hasil(uid: int, n_error: int, n_cd: int) -> InlineKeyboardMarkup:
    rows = []
    if n_error:
        rows.append([InlineKeyboardButton(f"🔁 Cek ulang yang gagal ({n_error})", callback_data=f"ulang:{uid}")])
    if n_cd:
        rows.append([InlineKeyboardButton("🔔 Ingatkan saat siap OTP", callback_data=f"ingat:{uid}")])
    rows.append([InlineKeyboardButton("🏠 Menu Utama", callback_data="menu_home")])
    return InlineKeyboardMarkup(rows)


def keyboard_hasil_salin(uid: int, n_error: int, n_cd: int, daftar_nomor: list) -> InlineKeyboardMarkup:
    """Keyboard hasil + tombol tap-to-copy semua nomor terdeteksi."""
    rows = keyboard_salin(daftar_nomor)
    rows += keyboard_hasil(uid, n_error, n_cd).inline_keyboard
    return InlineKeyboardMarkup(rows)


def file_hasil(hasil: list) -> bytes:
    now = datetime.now(storage._TZ).strftime("%Y-%m-%d %H:%M")
    bag = lambda k: [r for r in hasil if r.kind == k]
    out = [f"DETEKBOT — {now}", ""]
    for judul, k in (("SIAP OTP", READY), ("COOLDOWN", COOLDOWN), ("TERBANNED WA", BANNED),
                     ("GAK SUPPORT CLONE", NOSUPPORT), ("TIDAK VALID", INVALID), ("GAGAL", ERROR)):
        items = bag(k)
        if not items:
            continue
        out.append(f"== {judul} ({len(items)}) ==")
        out += [f"{r.nomor}" + (f" | {format_waktu(r.wait)}" if k == COOLDOWN else "") for r in items]
        out.append("")
    return "\n".join(out).encode("utf-8")


async def izinkan(msg, user, cek_cooldown: bool = True) -> bool:
    uid = user.id
    role = storage.role_of(uid)
    if storage.is_blokir(uid):
        await msg.reply_text("⛔ Akunmu diblokir dari bot ini.")
        return False
    if storage.maintenance_aktif() and role != storage.OWNER:
        await msg.reply_text("🛠️ <b>Bot sedang maintenance!</b>", parse_mode="HTML")
        return False
    if uid in sedang_cek:
        await msg.reply_text("⏳ Pengecekanmu sebelumnya masih berjalan. Tunggu selesai (atau tekan ⏹ Batalkan).")
        return False
    cd = config.LIMIT[role]["cooldown"]
    if cek_cooldown and cd > 0 and uid in user_cooldowns:
        sisa = int(cd - (time.time() - user_cooldowns[uid]))
        if sisa > 0:
            await msg.reply_text(f"⏳ Tunggu <b>{sisa} detik</b> lagi sebelum detek berikutnya.", parse_mode="HTML")
            return False
    return True


async def proses_batch(msg, user, valid: list, invalid: list):
    uid = user.id
    lim = storage.limit_of(uid)
    kepotong = len(valid) > lim["max"]
    if kepotong:
        valid = valid[: lim["max"]]
    total = len(valid)

    stop = asyncio.Event()
    sedang_cek[uid] = stop
    kb_stop = InlineKeyboardMarkup([[InlineKeyboardButton("⏹ Batalkan", callback_data=f"stop:{uid}")]])

    try:
        status_msg = await msg.reply_text(
            f"<b>🔎 Memulai deteksi…</b>\n" + kotak(f"📦 {total} nomor"),
            parse_mode="HTML", reply_markup=kb_stop,
        )
    except TelegramError:
        sedang_cek.pop(uid, None)
        return

    mulai = time.time()
    hasil: list = []                 # diisi [None]*total, lalu tiap slot diganti CekResult
    selesai_hit = 0                  # berapa nomor sudah tercek (untuk progress bar)

    # ── Tampilan real-time: SEMUA nomor yang sudah selesai dicek selalu ditampilkan
    #    dan pesannya diedit otomatis setiap ada nomor baru yang selesai. ──────────
    batas_edit = 0.0

    def sisa(r: CekResult) -> int:
        """Sisa cooldown dihitung mundur real-time dari saat hasil didapat."""
        return max(0, int(r.wait - (time.time() - r.selesai))) if r and r.wait > 0 else 0

    def render_live():
        now = time.time()
        done = sum(1 for r in hasil if r is not None and r.kind != BATAL)
        pct = int(done * 100 / total) if total else 0
        prog = "▰" * (pct // 10) + "▱" * (10 - pct // 10)
        n_cd = n_ok = n_ns = n_err = 0
        for r in hasil:
            if r is None or r.kind == BATAL:
                continue
            if r.kind == COOLDOWN:
                n_cd += 1
            elif r.kind in (READY, BANNED, NOSUPPORT):
                n_ok += 1
                if r.kind == NOSUPPORT:
                    n_ns += 1
            elif r.kind == ERROR:
                n_err += 1
        head = (
            f"<b>🔎 HASIL DETEKSI</b> · ⚡ Real-time\n"
            + kotak(
                f"{prog} <b>{done}/{total}</b> · ⏱ {fmt_durasi(now - mulai)}",
                f"✅ Aman: <b>{n_ok}</b>" + (f" (👥 gak support {n_ns})" if n_ns else "")
                + f" · ⏳ Cooldown: <b>{n_cd}</b>" + (f" · 🔁 Gagal: <b>{n_err}</b>" if n_err else ""),
            )
        )
        tampil = [r for r in hasil if r is not None and r.kind != BATAL] \
            + [CekResult(n, INVALID) for n in invalid]
        blok = susun_blok(tampil, maks_baris=config.LIVE_MAX_BARIS)
        isi = "\n\n".join(blok) if blok else "<blockquote><i>Menunggu hasil pertama…</i></blockquote>"
        teks = head + "\n\n" + isi
        if len(teks) > BATAS_PESAN:          # jaga agar edit tidak gagal karena kepanjangan
            teks = teks[:BATAS_PESAN - 20] + "\n…"
        return teks

    async def refresh(force=False):
        """Edit pesan live; dipanggil saat ada nomor selesai & oleh ticker (countdown jalan)."""
        nonlocal batas_edit
        now = time.time()
        if not force and now < batas_edit:
            return
        batas_edit = now + config.LIVE_INTERVAL
        try:
            await status_msg.edit_text(render_live(), parse_mode="HTML", reply_markup=kb_stop)
        except RetryAfter as e:
            batas_edit = time.time() + float(e.retry_after) + 1
        except TelegramError as e:
            log.debug("edit live dilewati: %s", e)

    async def ticker():
        """Update otomatis tiap LIVE_INTERVAL supaya sisa waktu cooldown ikut berjalan."""
        while True:
            try:
                await asyncio.sleep(max(0.6, config.LIVE_INTERVAL))
                await refresh()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.debug("ticker live berhenti: %s", e)
                break

    async def cek_satuan(i: int, nomor: str):
        """Cek satu nomor dengan retry ringan; hasil langsung memicu update pesan live."""
        nonlocal selesai_hit
        if stop.is_set():
            hasil[i] = CekResult(nomor, BATAL)
            return
        res = None
        for percobaan in range(max(1, config.MAX_COBA)):
            try:
                res, _info = await detect(nomor, method="wa_old")
                break
            except Exception as e:
                if percobaan + 1 < max(1, config.MAX_COBA) and not stop.is_set():
                    await asyncio.sleep(0.4 * (percobaan + 1))   # coba lagi cepat
                    continue
                res = {"status": "error", "reason": str(e)[:80]}
        kind, wait_val, detail = klasifikasi(res)
        hasil[i] = CekResult(nomor, kind, wait=wait_val, detail=detail)
        selesai_hit += 1
        await refresh(force=True)                # ← text di-update begitu nomor ini selesai

    lebar = max(1, min(int(lim.get("conc") or config.CEK_LEBAR), total or 1, config.CEK_LEBAR * 2))
    hasil = [None] * total

    async def jalur(idx: int):
        while idx < total:
            if stop.is_set():
                break
            await cek_satuan(idx, valid[idx])
            idx += lebar
            if idx < total and config.JEDA_NOMOR > 0:
                await asyncio.sleep(config.JEDA_NOMOR)

    jaga = asyncio.create_task(ticker())
    try:
        await asyncio.gather(*(jalur(i) for i in range(lebar)))
    except Exception as e:                     # jangan sampai ada batch menggantung
        log.exception("batch terhenti mendadak: %s", e)
    finally:
        jaga.cancel()
        for i, r in enumerate(hasil):
            if r is None:
                hasil[i] = CekResult(valid[i], BATAL if stop.is_set() else ERROR)
        sedang_cek.pop(uid, None)

    await refresh(force=True)   # snapshot terakhir sebelum hasil final dikirim
    durasi = round(time.time() - mulai, 2)
    try:
        storage.catat_hasil(
            uid, user.first_name or "Tanpa Nama", user.username or "",
            [(r.kind, r.nomor.lstrip("+"), r.wait) for r in hasil
             if r and r.kind in (READY, COOLDOWN, BANNED, NOSUPPORT)],
        )
    except Exception as e:
        log.exception("gagal mencatat hasil: %s", e)
    err = [r for r in hasil if r.kind == ERROR]
    cd = [r for r in hasil if r.kind == COOLDOWN]
    terakhir[uid] = {"error": [r.nomor for r in err], "cooldown": [(r.nomor, r.wait) for r in cd], "t": time.time()}
    if lim["cooldown"] > 0:
        user_cooldowns[uid] = time.time()

    catatan = ""
    if kepotong:
        catatan += f"⚠️ Hanya {lim['max']} nomor pertama diproses (batas role {lim['max']}/cek).\n"
    tampil = [r for r in hasil if r.kind != BATAL] + [CekResult(n, INVALID) for n in invalid]
    for r in tampil:                      # sinkronkan sisa waktu dengan detik ini
        if r.kind == COOLDOWN:
            r.wait = max(0, int(r.wait - (time.time() - r.selesai)))
    pages = susun_pesan(susun_blok(tampil), footer_hasil(durasi, total, hasil, catatan.strip()))
    await _kirim_hasil(msg, status_msg, pages, keyboard_hasil(uid, len(err), len(cd)))

    if total > 20:
        try:
            await msg.reply_document(document=file_hasil(tampil), filename="hasil_detek.txt",
                                     caption="📄 Hasil lengkap (mudah di-copy)")
        except TelegramError as e:
            log.warning("kirim file hasil gagal: %s", e)


async def _kirim_hasil(msg, status_msg, pages: list, kb_akhir):
    for i, page in enumerate(pages):
        kb = kb_akhir if i == len(pages) - 1 else None
        if i == 0:
            try:
                await status_msg.edit_text(page, parse_mode="HTML", reply_markup=kb)
                continue
            except TelegramError as e:
                log.warning("edit hasil gagal, kirim sebagai pesan baru: %s", e)
        try:
            await msg.reply_text(page, parse_mode="HTML", reply_markup=kb)
        except TelegramError as e:
            log.warning("kirim hasil gagal: %s", e)


async def handle_detek(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return
    user = update.effective_user
    if not await izinkan(msg, user):
        return
    valid, invalid = extract_numbers(msg.text)
    if not valid and not invalid:
        if re.fullmatch(r"/\w+(@\w+)?\s*", msg.text):
            await msg.reply_text(
                "🔍 <b>Kirim nomor yang ingin dideteksi</b>\nContoh: <code>/detek +6281234567890</code>\n"
                "Boleh banyak nomor (satu per baris).",
                parse_mode="HTML",
                reply_markup=ForceReply(selective=True, input_field_placeholder="+6281234567890"),
            )
        else:
            await msg.reply_text("⚠️ <b>Format salah!</b>\nContoh: <code>/detek +6281234567890</code>", parse_mode="HTML")
        return
    if not valid:
        daftar = "\n".join(f"<code>{esc(n)}</code>" for n in invalid[:20])
        await msg.reply_text(
            f"🚫 <b>Nomor tidak valid</b>\n<blockquote>{daftar}</blockquote>\n"
            "Pakai kode negara, contoh: <code>/detek +6281234567890</code>", parse_mode="HTML")
        return
    await proses_batch(msg, user, valid, invalid)


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = update.effective_user
    doc = msg.document if msg else None
    if not doc or not (doc.file_name or "").lower().endswith((".txt", ".csv")):
        return
    if storage.role_of(user.id) == storage.USER:
        await msg.reply_text("⭐ Upload file .txt khusus <b>Premium</b>. Kirim nomor lewat teks biasa ya.", parse_mode="HTML")
        return
    if (doc.file_size or 0) > 200_000:
        await msg.reply_text("⚠️ File terlalu besar (maks 200 KB).")
        return
    if not await izinkan(msg, user):
        return
    try:
        f = await doc.get_file()
        teks = bytes(await f.download_as_bytearray()).decode("utf-8", errors="ignore")
    except TelegramError:
        await msg.reply_text("⚠️ Gagal membaca file, coba kirim ulang.")
        return
    valid, invalid = extract_numbers(teks)
    if not valid:
        await msg.reply_text("🚫 Tidak ada nomor valid di file itu (pakai kode negara, mis. +62812…).")
        return
    await proses_batch(msg, user, valid, invalid)


async def ulang_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, user = update.message, update.effective_user
    if not await izinkan(msg, user):
        return
    await _ulang(msg, user)


async def _ulang(msg, user):
    data = terakhir.get(user.id)
    if not data or not data["error"]:
        await msg.reply_text("🎉 Tidak ada nomor gagal dari deteksi terakhirmu.")
        return
    await proses_batch(msg, user, list(data["error"]), [])


async def riwayat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if storage.is_blokir(update.effective_user.id):
        return
    await update.message.reply_text(teks_riwayat(update.effective_user.id), parse_mode="HTML", reply_markup=keyboard_back())


async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if storage.is_blokir(uid):
        return
    await update.message.reply_text(teks_leaderboard("hari", uid), parse_mode="HTML", reply_markup=keyboard_lb("hari"))


async def sembunyi_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if storage.is_blokir(uid):
        return
    hide = storage.toggle_sembunyi(uid)
    await update.message.reply_text(
        "🕶 Namamu sekarang <b>disembunyikan</b> di leaderboard (tampil sebagai Anonim)." if hide
        else "👀 Namamu sekarang <b>tampil</b> di leaderboard.", parse_mode="HTML")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(teks_status(update.effective_user.id), parse_mode="HTML", reply_markup=keyboard_back())


async def bantuan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(teks_panduan(update.effective_user.id), parse_mode="HTML", reply_markup=keyboard_back())


async def maintenance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not storage.is_owner(update.effective_user.id):
        return
    if not context.args:
        status = "Aktif 🔴" if storage.maintenance_aktif() else "Mati 🟢"
        await update.message.reply_text(
            f"🛠️ <b>Status Maintenance</b>: {status}\n"
            + kotak("/maintenance on – aktifkan", "/maintenance off – matikan"),
            parse_mode="HTML",
        )
        return
    param = context.args[0].lower()
    if param == "on":
        storage.set_maintenance(True)
        await update.message.reply_text("🛠️ Mode Maintenance <b>DIAKTIFKAN</b>.", parse_mode="HTML")
    elif param == "off":
        storage.set_maintenance(False)
        await update.message.reply_text("✅ Mode Maintenance <b>DIMATIKAN</b>.", parse_mode="HTML")


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not storage.is_owner(update.effective_user.id):
        return
    pesan = " ".join(context.args)
    if not pesan and update.message.reply_to_message:
        pesan = update.message.reply_to_message.text or update.message.reply_to_message.caption
    if not pesan:
        await update.message.reply_text("⚠️ Format salah! Contoh: <code>/broadcast Halo</code>", parse_mode="HTML")
        return
    users = storage.semua_user_id()
    if not users:
        await update.message.reply_text("⚠️ Belum ada user di database.")
        return
    status_msg = await update.message.reply_text(f"📢 Memulai broadcast ke {len(users)} pengguna...")
    ok = gagal = 0
    teks = f"📢 <b>PENGUMUMAN</b>\n<blockquote>{esc(pesan)}</blockquote>"
    for uid in users:
        try:
            await context.bot.send_message(chat_id=uid, text=teks, parse_mode="HTML")
            ok += 1
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            gagal += 1
        except Exception:
            gagal += 1
        await asyncio.sleep(0.1)
    await status_msg.edit_text(f"✅ Broadcast selesai!\n• Berhasil: {ok}\n• Gagal: {gagal}")


def _target_dan_hari(update: Update, context: ContextTypes.DEFAULT_TYPE, butuh_hari: bool):
    args = list(context.args)
    reply = update.message.reply_to_message
    if reply and reply.from_user:
        target = reply.from_user.id
    elif args and args[0].lstrip("-").isdigit():
        target = int(args.pop(0))
    else:
        return None, None, "Sebutkan ID user atau reply pesan user-nya."
    hari = None
    if butuh_hari and args:
        if not args[0].isdigit() or int(args[0]) <= 0:
            return None, None, "Jumlah hari harus angka lebih dari 0."
        hari = int(args[0])
    return target, hari, None


async def addprem_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner = update.effective_user.id
    if not storage.is_owner(owner):
        return
    target, hari, err = _target_dan_hari(update, context, True)
    if err:
        await update.message.reply_text(f"⚠️ {err}", parse_mode="HTML")
        return
    if storage.is_owner(target):
        await update.message.reply_text("Owner sudah punya akses penuh.")
        return
    sampai = storage.tambah_premium(target, hari, owner)
    info = f"{hari} hari" if sampai else "permanen"
    await update.message.reply_text(f"✅ <code>{target}</code> sekarang <b>Premium</b> ({info}).", parse_mode="HTML")


async def delprem_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not storage.is_owner(update.effective_user.id):
        return
    target, _, err = _target_dan_hari(update, context, False)
    if err:
        await update.message.reply_text(f"⚠️ {err}", parse_mode="HTML")
        return
    if storage.hapus_premium(target):
        await update.message.reply_text(f"✅ Premium <code>{target}</code> dicabut.", parse_mode="HTML")
    else:
        await update.message.reply_text("User itu memang bukan premium.")


async def listprem_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not storage.is_owner(update.effective_user.id):
        return
    data = storage.daftar_premium()
    if not data:
        await update.message.reply_text("Belum ada user premium.")
        return
    baris = [f"<code>{uid}</code> {esc(nama) or '-'} — {f'{sisa} hari lagi' if sisa else 'permanen'}" for uid, sisa, nama in data[:60]]
    await update.message.reply_text(f"⭐ <b>Premium aktif ({len(data)})</b>\n" + kotak(*baris), parse_mode="HTML")


async def blokir_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner = update.effective_user.id
    if not storage.is_owner(owner):
        return
    target, _, err = _target_dan_hari(update, context, False)
    if err:
        await update.message.reply_text(f"⚠️ {err}", parse_mode="HTML")
        return
    if storage.is_owner(target):
        await update.message.reply_text("Owner tidak bisa diblokir.")
        return
    storage.blokir(target, owner)
    await update.message.reply_text(f"⛔ <code>{target}</code> diblokir.", parse_mode="HTML")


async def unblokir_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not storage.is_owner(update.effective_user.id):
        return
    target, _, err = _target_dan_hari(update, context, False)
    if err:
        await update.message.reply_text(f"⚠️ {err}", parse_mode="HTML")
        return
    ok = storage.unblokir(target)
    await update.message.reply_text("✅ Blokir dicabut." if ok else "User itu tidak sedang diblokir.")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not storage.is_owner(update.effective_user.id):
        return
    await update.message.reply_text(teks_stats_owner(), parse_mode="HTML", reply_markup=keyboard_back())


async def kirim_dashboard(bot, chat_id: int, user):
    try:
        await bot.send_photo(chat_id=chat_id, photo=config.FOTO_DASHBOARD, caption=teks_dashboard(user),
                             parse_mode="HTML", reply_markup=keyboard_menu_utama(user.id))
    except TelegramError:
        await bot.send_message(chat_id=chat_id, text=teks_dashboard(user), parse_mode="HTML",
                               reply_markup=keyboard_menu_utama(user.id))


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if storage.is_blokir(user.id):
        await update.message.reply_text("⛔ Akunmu diblokir dari bot ini.")
        return
    if storage.maintenance_aktif() and not storage.is_owner(user.id):
        await update.message.reply_text("🛠️ <b>Bot sedang maintenance!</b>", parse_mode="HTML")
        return
    storage.get_user(user.id)
    await kirim_dashboard(context.bot, update.effective_chat.id, user)


async def _ganti_halaman(query, context, teks: str, kb):
    try:
        await query.message.delete()
    except TelegramError:
        pass
    try:
        await context.bot.send_message(chat_id=query.message.chat_id, text=teks, parse_mode="HTML", reply_markup=kb)
    except TelegramError:
        pass


async def callback_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    uid = user.id
    data = query.data or ""

    async def jawab(teks=None, alert=False):
        try:
            await query.answer(teks, show_alert=alert)
        except TelegramError:
            pass

    if storage.is_blokir(uid):
        await jawab("⛔ Akunmu diblokir.", True)
        return
    if storage.maintenance_aktif() and not storage.is_owner(uid):
        await jawab("🛠️ Bot sedang dalam pemeliharaan!", True)
        return

    if ":" in data:
        aksi, _, pemilik = data.partition(":")
        if pemilik != str(uid):
            await jawab("Tombol ini bukan untukmu.", True)
            return
        if aksi == "stop":
            ev = sedang_cek.get(uid)
            if ev:
                ev.set()
                await jawab("⏹ Membatalkan…")
            else:
                await jawab("Tidak ada pengecekan berjalan.")
        elif aksi == "ulang":
            await jawab("🔁 Mengulang nomor yang gagal…")
            if await izinkan(query.message, user):
                await _ulang(query.message, user)
        elif aksi == "ingat":
            data_t = terakhir.get(uid)
            if not data_t or not data_t["cooldown"]:
                await jawab("Tidak ada nomor cooldown dari deteksi terakhir.", True)
                return
            umur = time.time() - data_t["t"]
            items = [(n, max(1, int(w - umur))) for n, w in data_t["cooldown"]]
            ok, tolak = storage.tambah_pengingat(uid, items, storage.limit_of(uid)["ingat"])
            teks = f"🔔 {ok} pengingat dipasang."
            await jawab(teks, True)
        else:
            await jawab()
        return

    await jawab()
    chat_id = query.message.chat_id

    if data.startswith("lb_") and data[3:] in storage.PERIODE:
        try:
            await query.message.edit_text(teks_leaderboard(data[3:], uid), parse_mode="HTML",
                                          reply_markup=keyboard_lb(data[3:]))
        except TelegramError:
            pass
        return
    if data in ("menu_home", "menu_back"):
        try:
            await query.message.delete()
        except TelegramError:
            pass
        await kirim_dashboard(context.bot, chat_id, user)
        return
    if data == "menu_detek":
        await context.bot.send_message(
            chat_id=chat_id,
            text="🔍 <b>Kirim nomor yang ingin dideteksi</b>\n"
                 + kotak("Contoh: +6281234567890", "Boleh banyak nomor, satu per baris."),
            parse_mode="HTML",
            reply_markup=ForceReply(selective=True, input_field_placeholder="+6281234567890"),
        )
        return
    if data == "menu_katalog":
        teks = teks_panduan(uid)
    elif data == "menu_status":
        teks = teks_status(uid)
    elif data == "menu_riwayat":
        teks = teks_riwayat(uid)
    elif data == "menu_ingat":
        teks = teks_pengingat(uid)
    elif data == "menu_lb":
        await _ganti_halaman(query, context, teks_leaderboard("hari", uid), keyboard_lb("hari"))
        return
    elif data == "menu_admin":
        if not storage.is_owner(uid):
            await jawab("⚠️ Khusus Owner!", True)
            return
        teks = teks_stats_owner()
    else:
        teks = "❓ Menu tidak dikenal."
    await _ganti_halaman(query, context, teks, keyboard_back())


async def loop_pengingat(app: Application):
    while True:
        await asyncio.sleep(20)
        try:
            for e in storage.ambil_jatuh_tempo():
                try:
                    await app.bot.send_message(
                        chat_id=e["uid"],
                        text="🔔 <b>Pengingat cooldown</b>\n"
                             + kotak(f"<code>+{e['n']}</code>", "Cooldown seharusnya sudah selesai — saatnya login!")
                             + f"\nVerifikasi: <code>/detek +{e['n']}</code>",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
                await asyncio.sleep(0.05)
        except Exception:
            pass


CMD_UMUM = [
    BotCommand("start", "🏠 Menu utama"),
    BotCommand("detek", "🔍 Detek nomor WhatsApp"),
    BotCommand("ulang", "🔁 Cek ulang yang gagal"),
    BotCommand("riwayat", "📜 Riwayat detek"),
    BotCommand("leaderboard", "🏆 Peringkat pendetek"),
    BotCommand("status", "📡 Status server"),
    BotCommand("bantuan", "📖 Panduan"),
]


async def post_init(app: Application):
    try:
        await app.bot.set_my_commands(CMD_UMUM)
        await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except TelegramError:
        pass
    app.bot_data["bg"] = [asyncio.create_task(loop_pengingat(app))]
    print(f"🚀 Bot siap menggunakan engine dari cek_wait.py!")


async def post_shutdown(app: Application):
    for t in app.bot_data.get("bg", []):
        t.cancel()


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("Error handler: %s", context.error, exc_info=context.error)


def main():
    if not config.TOKEN_TELEGRAM:
        print("ERROR: BOT_TOKEN belum diisi.")
        return

    app = (
        ApplicationBuilder()
        .token(config.TOKEN_TELEGRAM)
        .concurrent_updates(True)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    for nama, fn in (
        ("start", start_command), ("detek", handle_detek), ("cek", handle_detek),
        ("ulang", ulang_command), ("riwayat", riwayat_command), ("status", status_command),
        ("leaderboard", leaderboard_command), ("top", leaderboard_command), ("sembunyi", sembunyi_command),
        ("bantuan", bantuan_command), ("help", bantuan_command),
        ("stats", stats_command), ("maintenance", maintenance_command), ("broadcast", broadcast_command),
        ("addprem", addprem_command), ("delprem", delprem_command), ("listprem", listprem_command),
        ("blokir", blokir_command), ("unblokir", unblokir_command),
    ):
        app.add_handler(CommandHandler(nama, fn))
    app.add_handler(CallbackQueryHandler(callback_menu))
    app.add_handler(MessageHandler(filters.Document.ALL & filters.ChatType.PRIVATE, handle_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_detek))
    app.add_error_handler(on_error)

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
