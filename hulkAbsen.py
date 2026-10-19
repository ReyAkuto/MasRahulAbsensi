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
import json
import subprocess
import time
import random
import logging
from datetime import datetime, time as dtime
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:
    pass

def get_esensi_path() -> str:
    p = os.getenv("ESENSI_PATH")
    if p and Path(p).exists():
        return str(Path(p).resolve())
    lokal = Path(__file__).parent / "esensi.exe"
    if lokal.exists():
        return str(lokal.resolve())
    return "esensi.exe"

ESENSI_PATH  = get_esensi_path()
LOG_FILE     = os.getenv("LOG_FILE",     str(Path(__file__).parent / "absensi.log"))
JADWAL_FILE  = Path(__file__).parent / "jadwal.json"
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
    return random.randint(0, max(0, menit * 60))


def baca_jadwal() -> tuple[dtime, dtime, int, str, str]:
    """
    Membaca jadwal secara dinamis dari jadwal.json.
    Mengembalikan (jam_masuk_obj, jam_pulang_obj, delay_menit, masuk_str, pulang_str).
    """
    default = {
        "jam_masuk": "07:18",
        "jam_pulang": "16:40",
        "delay_menit": 15
    }
    if JADWAL_FILE.exists():
        try:
            data = json.loads(JADWAL_FILE.read_text(encoding="utf-8"))
            default.update(data)
        except Exception as e:
            log.warning(f"Gagal membaca {JADWAL_FILE.name}: {e}. Memakai default.")

    masuk_str = default.get("jam_masuk", "07:18")
    pulang_str = default.get("jam_pulang", "16:40")
    try:
        delay = int(default.get("delay_menit", 15))
    except (ValueError, TypeError):
        delay = 15

    return parse_jam(masuk_str), parse_jam(pulang_str), delay, masuk_str, pulang_str


def jalankan(args: list[str], timeout: int = 30) -> tuple[bool, str]:
    """
    Jalankan esensi.exe dengan argumen tertentu (otomatis pakai wine jika di Linux).
    Return (sukses, output_string).
    """
    esensi_path = get_esensi_path()
    if not Path(esensi_path).exists():
        return False, f"esensi.exe tidak ditemukan di: {esensi_path}"

    import platform
    env = os.environ.copy()
    if platform.system() != "Windows":
        env.setdefault("DISPLAY", ":1")
        xdg = env.get("XDG_RUNTIME_DIR", "")
        if not xdg or not Path(xdg).exists():
            xdg_dir = Path("/run/user/0")
            xdg_dir.mkdir(parents=True, exist_ok=True)
            env["XDG_RUNTIME_DIR"] = str(xdg_dir)
        cmd = ["wine", esensi_path] + args
    else:
        cmd = [esensi_path] + args

    try:
        hasil = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            env=env
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


def absen(tipe: str, delay_menit: int = 15) -> bool:
    """
    Lakukan absen masuk atau pulang dengan delay acak.
    tipe: 'masuk' | 'pulang'
    Return True jika berhasil.
    """
    delay = random_delay(delay_menit)
    log.info(f"Menunggu delay acak {delay} detik (maks ±{delay_menit} menit) sebelum absen {tipe}…")
    time.sleep(delay)

    # Cek sekali lagi setelah delay (siapa tahu sudah absen manual di sela delay)
    st2 = cek_status_hari_ini()
    if tipe == "masuk" and st2.get("masuk"):
        log.info(f"[{tipe}] Sudah absen masuk saat menunggu delay. Lewati.")
        return True
    if tipe == "pulang" and st2.get("pulang"):
        log.info(f"[{tipe}] Sudah absen pulang saat menunggu delay. Lewati.")
        return True

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
    log.info(f"  esensi.exe   : {ESENSI_PATH}")
    log.info(f"  File jadwal  : {JADWAL_FILE.name}")
    log.info("=" * 60)

    last_sched_str = None

    while True:
        jam_masuk_obj, jam_pulang_obj, delay_menit, masuk_str, pulang_str = baca_jadwal()
        curr_sched = f"{masuk_str}|{pulang_str}|{delay_menit}"
        if curr_sched != last_sched_str:
            log.info(f"Jadwal aktif: Jam masuk {masuk_str} | Jam pulang {pulang_str} | Delay acak ±{delay_menit} menit")
            last_sched_str = curr_sched

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
                    ok = absen("masuk", delay_menit)
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
                    ok = absen("pulang", delay_menit)
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