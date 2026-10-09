"""
hulkAbsen.py
============
Script otomatis absensi menggunakan esensi.exe.
Dijalankan oleh web_absensi.py (dashboard) atau bisa langsung:

    python hulkAbsen.py

Logika:
  - Cek status absensi hari ini (status-today).
  - Tunggu sampai jam masuk -> absen masuk.
  - Tunggu sampai jam pulang -> absen pulang.
  - Catat semua ke absensi.log.
"""

import os
import sys
import subprocess
import time
import random
import logging
from datetime import datetime, time as dtime
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Konfigurasi ──────────────────────────────────────────────────────────────
ESENSI_PATH  = os.getenv("ESENSI_PATH",  str(Path(__file__).parent / "esensi.exe"))
LOG_FILE     = os.getenv("LOG_FILE",     str(Path(__file__).parent / "absensi.log"))
JAM_MASUK    = os.getenv("JAM_MASUK",   "08:00")   # format HH:MM
JAM_PULANG   = os.getenv("JAM_PULANG",  "17:00")   # format HH:MM
DELAY_MENIT  = int(os.getenv("DELAY_MENIT",  "30")) # random delay ±N menit
POLL_DETIK   = int(os.getenv("POLL_DETIK",  "30")) # cek jam setiap N detik
POLL_MALAM   = 300  # polling interval (detik) setelah absen selesai

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ── Helper ───────────────────────────────────────────────────────────────────

def parse_jam(s: str) -> dtime:
    """Parse string 'HH:MM' menjadi datetime.time."""
    h, m = map(int, s.strip().split(":"))
    return dtime(h, m)


def random_delay(menit: int) -> int:
    """Return detik delay acak antara 0 hingga menit*60."""
    return random.randint(0, menit * 60)


def jalankan(args: list[str], timeout: int = 30) -> tuple[bool, str]:
    """
    Jalankan esensi.exe dengan argumen tertentu.
    Return (sukses, output_string).
    """
    if not Path(ESENSI_PATH).exists():
        return False, f"esensi.exe tidak ditemukan di: {ESENSI_PATH}"

    cmd = [ESENSI_PATH] + args
    try:
        hasil = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = (hasil.stdout + hasil.stderr).strip()
        return hasil.returncode == 0, out
    except subprocess.TimeoutExpired:
        return False, "Timeout: esensi.exe tidak merespons."
    except Exception as e:
        return False, str(e)



def _parse_status_output(bersih: str) -> dict:
    """
    Parse output status dari esensi.exe (sudah di-strip ANSI).
    Coba JSON dulu (lebih andal), fallback ke pencocokan teks.

    Dari log nyata response esensi.exe, struktur JSON-nya:
      { "today": { "is_absent": true, "is_completed": true, "status": 4,
                   "message": "Anda telah selesai bekerja hari ini..." } }

    Frasa teks yang diketahui dari log nyata:
      - Sudah masuk saja : "telah presensi masuk"
      - Sudah pulang     : "telah selesai bekerja" / "selesai bekerja hari ini"
                           (BUKAN "telah menyelesaikan presensi" — frasa lama belum terkonfirmasi)
    """
    import json, re as _re

    # ── Coba parse JSON ──────────────────────────────────────────────────────
    json_match = _re.search(r'\{.*\}', bersih, _re.DOTALL)
    if json_match:
        try:
            data  = json.loads(json_match.group())
            today = data.get("today", {})
            if today:
                sudah_masuk  = bool(today.get("is_absent",    False))
                sudah_pulang = bool(today.get("is_completed", False))
                return {"masuk": sudah_masuk, "pulang": sudah_pulang}
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
            pass

    # ── Fallback: pencocokan teks ────────────────────────────────────────────
    out_lower = bersih.lower()
    sudah_pulang = (
        "telah selesai bekerja"       in out_lower or
        "selesai bekerja hari ini"    in out_lower or
        "telah menyelesaikan presensi" in out_lower or
        "presensi pulang berhasil"    in out_lower
    )
    sudah_masuk = sudah_pulang or ("telah presensi masuk" in out_lower)
    return {"masuk": sudah_masuk, "pulang": sudah_pulang}


def cek_status_hari_ini() -> dict:
    """
    Return dict {'masuk': bool, 'pulang': bool} dari endpoint status-today.

    Dari log nyata response esensi.exe:
      { "today": { "is_absent": true, "is_completed": true, "status": 4,
                   "message": "Anda telah selesai bekerja hari ini..." } }
    """
    import re as _re
    ok, out = jalankan(["-endpoint", "status-today"])
    log.info(f"[status-today] ok={ok} -> {out[:300]}")
    if not ok:
        return {"masuk": False, "pulang": False}

    bersih = _re.sub(r'\x1b\[[0-9;]*m', '', out)
    hasil  = _parse_status_output(bersih)
    log.info(f"[status-today] parsed -> masuk={hasil['masuk']}, pulang={hasil['pulang']}")
    return hasil


def absen(tipe: str) -> bool:
    """
    Lakukan absen masuk atau pulang.
    tipe: 'masuk' | 'pulang'
    Return True jika berhasil.
    """
    delay = random_delay(DELAY_MENIT)
    log.info(f"Menunggu delay acak {delay} detik sebelum absen {tipe}…")
    time.sleep(delay)

    log.info(f"Mengirim absen {tipe}…")
    ok, out = jalankan([tipe])

    if ok:
        log.info(f"Absen {tipe} BERHASIL -> {out[:200]}")
    else:
        log.error(f"Absen {tipe} GAGAL -> {out[:200]}")

    return ok


def sudah_absen_log(tipe: str) -> bool:
    """
    Cek log hari ini apakah sudah absen tipe tertentu.
    Ini backup cross-check selain status-today.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = Path(LOG_FILE)
    if not log_path.exists():
        return False
    lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    return any(
        today in l and f"Absen {tipe} BERHASIL" in l
        for l in lines
    )


# ── Loop Utama ───────────────────────────────────────────────────────────────

# FIX Bug 1: state harian disimpan di module-level, bukan di function attribute
_state = {
    "last_date":       None,
    "sudah_masuk":     False,
    "sudah_pulang":    False,
    "log_selesai":     False,  # supaya log "selesai" tidak spam tiap iterasi
}


def _reset_state_harian(hari_ini):
    """Reset semua state harian saat hari berubah."""
    _state["last_date"]    = hari_ini
    _state["sudah_masuk"]  = False
    _state["sudah_pulang"] = False
    _state["log_selesai"]  = False
    log.info(f"Hari baru: {hari_ini}. Reset status absensi.")


def main():
    log.info("=" * 60)
    log.info("Hulk Absen mulai berjalan.")
    log.info(f"  esensi.exe : {ESENSI_PATH}")
    log.info(f"  Jam masuk  : {JAM_MASUK}")
    log.info(f"  Jam pulang : {JAM_PULANG}")
    log.info(f"  Delay maks : ±{DELAY_MENIT} menit")
    log.info("=" * 60)

    jam_masuk_obj  = parse_jam(JAM_MASUK)
    jam_pulang_obj = parse_jam(JAM_PULANG)

    while True:
        sekarang = datetime.now()
        waktu    = sekarang.time().replace(second=0, microsecond=0)
        hari_ini = sekarang.date()

        # Reset flag setiap hari baru
        if _state["last_date"] != hari_ini:
            _reset_state_harian(hari_ini)

        # Cek hari Minggu — skip absensi
        if sekarang.weekday() == 6:  # 6 = Minggu
            if not _state["log_selesai"]:
                log.info("Hari ini Minggu. Tidak ada absensi.")
                _state["log_selesai"] = True
            time.sleep(POLL_MALAM)
            continue

        # ── Absen masuk ──────────────────────────────────────────────
        if not _state["sudah_masuk"] and waktu >= jam_masuk_obj:
            if sudah_absen_log("masuk"):
                log.info("Sudah absen masuk (dari log). Lewati.")
                _state["sudah_masuk"] = True
            else:
                st = cek_status_hari_ini()
                if st["masuk"]:
                    log.info("Sudah absen masuk (dari status-today). Lewati.")
                    _state["sudah_masuk"] = True
                else:
                    ok = absen("masuk")
                    _state["sudah_masuk"] = ok

        # ── Jam pulang tiba tapi sudah_masuk masih False ─────────────
        # Cek sekali ke server — mungkin sudah tercatat masuk di sana.
        if not _state["sudah_masuk"] and waktu >= jam_pulang_obj:
            log.warning(
                "Jam pulang sudah tiba tapi sudah_masuk=False. "
                "Cek ulang status ke server sebelum absen pulang..."
            )
            st = cek_status_hari_ini()
            if st["masuk"]:
                log.info("Konfirmasi server: sudah masuk. Lanjut proses absen pulang.")
                _state["sudah_masuk"] = True
            else:
                log.error(
                    "Server konfirmasi belum masuk. Absen pulang dilewati untuk keamanan."
                )

        # ── Absen pulang ─────────────────────────────────────────────
        if _state["sudah_masuk"] and not _state["sudah_pulang"] and waktu >= jam_pulang_obj:
            if sudah_absen_log("pulang"):
                log.info("Sudah absen pulang (dari log). Lewati.")
                _state["sudah_pulang"] = True
            else:
                st = cek_status_hari_ini()
                if st["pulang"]:
                    log.info("Sudah absen pulang (dari status-today). Lewati.")
                    _state["sudah_pulang"] = True
                else:
                    ok = absen("pulang")
                    _state["sudah_pulang"] = ok
                    if not ok:
                        log.warning(
                            f"Absen pulang gagal. Akan dicoba lagi dalam {POLL_DETIK} detik..."
                        )

            # Setelah selesai, polling lebih jarang + log hanya sekali
            if _state["sudah_masuk"] and _state["sudah_pulang"]:
                if not _state["log_selesai"]:
                    log.info("Absen masuk & pulang hari ini selesai. Tunggu hari berikutnya...")
                    _state["log_selesai"] = True
                time.sleep(POLL_MALAM)
                continue

        time.sleep(POLL_DETIK)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Dihentikan oleh pengguna (Ctrl+C).")
    except Exception as e:
        log.exception(f"Error tidak terduga: {e}")
        sys.exit(1)