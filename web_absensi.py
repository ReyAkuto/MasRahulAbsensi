"""
web_absensi.py
==============
Dashboard web lokal untuk monitoring & kontrol absensi otomatis.
Jalankan: python web_absensi.py
Buka browser: http://localhost:5000
"""

import os
import sys
import json
import subprocess
import time
import logging as _logging
from datetime import datetime
from pathlib import Path

try:
    from flask import Flask, render_template, jsonify, request
except ImportError:
    print("Flask belum terinstall. Jalankan: pip install flask")
    sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:
    pass

# ── Config ────────────────────────────────────────────────────────────────────
ESENSI_PATH = os.getenv("ESENSI_PATH", str(Path(__file__).parent / "esensi.exe"))
LOG_FILE    = os.getenv("LOG_FILE",    str(Path(__file__).parent / "absensi.log"))
JADWAL_FILE = Path(__file__).parent / "jadwal.json"
WEB_PORT    = int(os.getenv("WEB_PORT", "5000"))
WEB_HOST    = os.getenv("WEB_HOST", "0.0.0.0")

def baca_jadwal_config() -> dict:
    default = {
        "jam_masuk": "07:18",
        "jam_pulang": "16:40",
        "delay_menit": 15
    }
    if JADWAL_FILE.exists():
        try:
            data = json.loads(JADWAL_FILE.read_text(encoding="utf-8"))
            default.update(data)
            return default
        except Exception:
            pass
    simpan_jadwal_config(default)
    return default

def simpan_jadwal_config(cfg: dict):
    JADWAL_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

# ── State ─────────────────────────────────────────────────────────────────────
absen_proses   = None
_cfg_init      = baca_jadwal_config()
jam_masuk_rt   = _cfg_init["jam_masuk"]
jam_pulang_rt  = _cfg_init["jam_pulang"]
delay_menit_rt = int(_cfg_init.get("delay_menit", 15))

# Cache hasil cek status live dari server (agar card kiri sinkron)
_status_live_cache = {"masuk": False, "pulang": False, "tanggal": None}

# ── Logger ───────────────────────────────────────────────────────────────────
# FIX Bug 9: tambahkan StreamHandler agar log tampil di stdout juga
_logger = _logging.getLogger("web_absensi")
if not _logger.handlers:
    _fmt = _logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    _fh  = _logging.FileHandler(LOG_FILE, encoding="utf-8")
    _fh.setFormatter(_fmt)
    _sh  = _logging.StreamHandler(sys.stdout)
    _sh.setFormatter(_fmt)
    _logger.addHandler(_fh)
    _logger.addHandler(_sh)
    _logger.setLevel(_logging.INFO)

# ── Helpers ───────────────────────────────────────────────────────────────────

def jalankan(args: list, timeout: int = 30):
    if not Path(ESENSI_PATH).exists():
        return False, f"esensi.exe tidak ditemukan: {ESENSI_PATH}"
    try:
        hasil = subprocess.run(
            [ESENSI_PATH] + args,
            capture_output=True, text=True, timeout=timeout
        )
        out = (hasil.stdout + hasil.stderr).strip()
        return hasil.returncode == 0, out
    except subprocess.TimeoutExpired:
        return False, "Timeout: esensi.exe tidak merespons."
    except Exception as e:
        return False, str(e)


def strip_ansi(s: str) -> str:
    import re
    return re.sub(r'\x1b\[[0-9;]*m', '', s)


def cek_status_live() -> dict:
    """Jalankan status-today dan parse hasilnya.

    Dari log nyata response esensi.exe:
      { "today": { "is_absent": true, "is_completed": true,
                   "message": "Anda telah selesai bekerja hari ini..." } }
    """
    import json, re as _re
    _logger.info("[cek-status] Memulai pengecekan status dari server...")
    ok, raw = jalankan(["-endpoint", "status-today"])
    if not ok:
        _logger.error(f"[cek-status] GAGAL: {raw}")
        return {"ok": False, "masuk": False, "pulang": False, "pesan": raw, "raw": raw}

    bersih    = strip_ansi(raw)
    out_lower = bersih.lower()

    sudah_masuk  = False
    sudah_pulang = False

    # Coba parse JSON dulu (lebih andal daripada pencocokan teks)
    json_match = _re.search(r'\{.*\}', bersih, _re.DOTALL)
    if json_match:
        try:
            data  = json.loads(json_match.group())
            today = data.get("today", {})
            if today:
                sudah_masuk  = bool(today.get("is_absent",    False))
                sudah_pulang = bool(today.get("is_completed", False))
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
            pass

    # Fallback teks jika JSON tidak tersedia
    if not sudah_masuk and not sudah_pulang:
        sudah_pulang = (
            "telah selesai bekerja"        in out_lower or
            "selesai bekerja hari ini"     in out_lower or
            "telah menyelesaikan presensi" in out_lower or
            "presensi pulang berhasil"     in out_lower
        )
        sudah_masuk = sudah_pulang or ("telah presensi masuk" in out_lower)

    if sudah_masuk and sudah_pulang:
        pesan = "Absen masuk & pulang sudah selesai hari ini."
    elif sudah_masuk:
        pesan = "Sudah absen masuk. Belum absen pulang."
    else:
        pesan = "Belum absen masuk hari ini."

    _logger.info(f"[cek-status] masuk={sudah_masuk}, pulang={sudah_pulang} -> {pesan}")

    # Simpan ke cache agar card STATUS HARI INI (LOG) ikut sinkron
    today = datetime.now().strftime("%Y-%m-%d")
    _status_live_cache["masuk"]    = sudah_masuk
    _status_live_cache["pulang"]   = sudah_pulang
    _status_live_cache["tanggal"]  = today

    return {"ok": True, "masuk": sudah_masuk, "pulang": sudah_pulang, "pesan": pesan, "raw": bersih}

def absen_manual(tipe: str, force: bool = False):
    # FIX Bug 8: parameter `force` sekarang diteruskan ke esensi.exe jika True
    if not Path(ESENSI_PATH).exists():
        return False, f"esensi.exe tidak ditemukan: {ESENSI_PATH}"
    try:
        cmd = [ESENSI_PATH, tipe]
        if force:
            cmd.append("--force")
        hasil = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=30
        )
        out = (hasil.stdout + hasil.stderr).strip()
        ok  = hasil.returncode == 0
        if ok:
            _logger.info(f"[absen-manual] Absen {tipe} BERHASIL → {out[:200]}")
        else:
            _logger.error(f"[absen-manual] Absen {tipe} GAGAL → {out[:200]}")
        return ok, out
    except Exception as e:
        return False, str(e)


def get_status_log():
    today = datetime.now().strftime("%Y-%m-%d")

    # Baca dari file log
    log_path = Path(LOG_FILE)
    masuk_log  = False
    pulang_log = False
    if log_path.exists():
        lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        today_lines = [l for l in lines if l.startswith(today)]
        masuk_log  = any("Absen masuk BERHASIL"  in l for l in today_lines)
        pulang_log = any("Absen pulang BERHASIL" in l for l in today_lines)

    # Gabungkan dengan cache hasil cek live (OR: kalau salah satu True, dianggap sudah)
    cache_hari_ini = _status_live_cache.get("tanggal") == today
    masuk_live  = cache_hari_ini and _status_live_cache.get("masuk",  False)
    pulang_live = cache_hari_ini and _status_live_cache.get("pulang", False)

    return {
        "masuk":   masuk_log  or masuk_live,
        "pulang":  pulang_log or pulang_live,
        "log_ada": log_path.exists(),
        "tanggal": today,
    }


def baca_log(n=200):
    log_path = Path(LOG_FILE)
    if not log_path.exists():
        return []
    lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    return lines[-n:]


def is_proses_jalan():
    global absen_proses
    return absen_proses is not None and absen_proses.poll() is None


def mulai_absen_auto():
    global absen_proses
    if is_proses_jalan():
        return False, "Proses sudah berjalan."
    script = Path(__file__).parent / "hulkAbsen.py"
    if not script.exists():
        return False, "hulkAbsen.py tidak ditemukan."
    absen_proses = subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return True, f"Proses dimulai (PID {absen_proses.pid})"


def hentikan_absen_auto():
    # FIX Bug 6: tunggu proses benar-benar mati sebelum set None
    global absen_proses
    if not is_proses_jalan():
        return False, "Tidak ada proses yang berjalan."
    absen_proses.terminate()
    try:
        absen_proses.wait(timeout=5)
    except subprocess.TimeoutExpired:
        absen_proses.kill()
        absen_proses.wait()
    absen_proses = None
    return True, "Proses dihentikan."


# ═══════════════════════════════════════════════════════════════════════════════
#  FLASK — template HTML ada di templates/index.html
# ═══════════════════════════════════════════════════════════════════════════════


app = Flask(__name__)

@app.route("/")
def index():
    cfg = baca_jadwal_config()
    return render_template("dashboard.html", jam_masuk=cfg["jam_masuk"], jam_pulang=cfg["jam_pulang"], delay_menit=cfg.get("delay_menit", 15))

@app.route("/config")
def config():
    cfg = baca_jadwal_config()
    return render_template("index.html", jam_masuk=cfg["jam_masuk"], jam_pulang=cfg["jam_pulang"], delay_menit=cfg.get("delay_menit", 15))


@app.route("/api/status")
def api_status():
    st = get_status_log()
    st["proses_jalan"] = is_proses_jalan()
    cfg = baca_jadwal_config()
    st["jam_masuk"] = cfg["jam_masuk"]
    st["jam_pulang"] = cfg["jam_pulang"]
    st["delay_menit"] = cfg.get("delay_menit", 15)
    return jsonify(st)

@app.route("/api/log")
def api_log():
    return jsonify({"lines": baca_log(200)})

@app.route("/api/cek-status")
def api_cek_status():
    return jsonify(cek_status_live())

@app.route("/api/mulai", methods=["POST"])
def api_mulai():
    ok, msg = mulai_absen_auto()
    return jsonify({"ok": ok, "pesan": msg})

@app.route("/api/hentikan", methods=["POST"])
def api_hentikan():
    ok, msg = hentikan_absen_auto()
    return jsonify({"ok": ok, "pesan": msg})

@app.route("/api/absen", methods=["POST"])
def api_absen():
    data  = request.get_json(force=True) or {}
    tipe  = data.get("tipe", "masuk")
    force = bool(data.get("force", False))
    if tipe not in ("masuk", "pulang"):
        return jsonify({"ok": False, "pesan": "tipe tidak valid"})
    ok, msg = absen_manual(tipe, force=force)
    return jsonify({"ok": ok, "pesan": f"Absen {tipe}: {msg}"})

@app.route("/api/set-jam", methods=["POST"])
def api_set_jam():
    global jam_masuk_rt, jam_pulang_rt, delay_menit_rt
    data = request.get_json(force=True) or {}
    cfg = baca_jadwal_config()
    jam_masuk_rt   = data.get("masuk",  cfg["jam_masuk"])
    jam_pulang_rt  = data.get("pulang", cfg["jam_pulang"])
    try:
        delay_menit_rt = int(data.get("delay_menit", cfg.get("delay_menit", 15)))
    except (ValueError, TypeError):
        delay_menit_rt = 15

    cfg["jam_masuk"]   = jam_masuk_rt
    cfg["jam_pulang"]  = jam_pulang_rt
    cfg["delay_menit"] = delay_menit_rt

    try:
        simpan_jadwal_config(cfg)
        _logger.info(f"[set-jam] Jadwal tersimpan ke {JADWAL_FILE.name}: Masuk {jam_masuk_rt}, Pulang {jam_pulang_rt}, Delay ±{delay_menit_rt}m")
        return jsonify({
            "ok": True,
            "pesan": f"Jadwal disimpan: masuk {jam_masuk_rt}, pulang {jam_pulang_rt} (delay acak ±{delay_menit_rt} menit). Otomatis aktif!"
        })
    except Exception as e:
        return jsonify({"ok": False, "pesan": f"Gagal menyimpan jadwal: {e}"})

# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print(f"""
╔══════════════════════════════════════════╗
║       Hulk Absen — Web Dashboard         ║
╠══════════════════════════════════════════╣
║  Buka browser: http://localhost:{WEB_PORT:<5}   ║
║  Ctrl+C untuk menghentikan               ║
╚══════════════════════════════════════════╝
""")
    app.run(host=WEB_HOST, port=WEB_PORT, debug=False)