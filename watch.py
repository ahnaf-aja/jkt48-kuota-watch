#!/usr/bin/env python3
"""JKT48 Exclusive quota watcher — versi CLOUD (GitHub Actions).

Bedanya sama versi lokal (D:\\notif kuota):
  * jalan di runner GitHub tiap ~1 menit, jadi laptop gak perlu nyala;
  * alert dikirim langsung ke Discord lewat secret repo DISCORD_WEBHOOK_URL;
  * state disimpan di state.json repo ini (di-commit otomatis oleh workflow),
    jadi tidak ada alert dobel antar run.

Aturan: kirim notifikasi kalau ADA perubahan apa pun pada kuota / member / harga /
sesi baru / jalur baru-hilang, KECUALI member di config `ignore_members`.

Selalu exit 0 supaya workflow tidak dianggap gagal (kecuali error tak terduga).
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(__file__).resolve().parent
STATE_PATH = HOME / "state.json"
CONFIG_PATH = HOME / "config.json"
TEST_PATH = HOME / "test_alert.txt"

API = "https://jkt48.com/api/v1/exclusives/{code}/bonus?lang=id"
BASE = "https://jkt48.com"
# proxy pembaca teks: dipakai kalau Cloudflare nolak IP runner langsung
JINA = "https://r.jina.ai/"

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "id-ID,id;q=0.9,en;q=0.8",
    "Referer": BASE + "/",
    "Origin": BASE,
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
    ),
    "sec-ch-ua": '"Chromium";v="141", "Not?A_Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

DEFAULT_CONFIG = {
    "events": [{"code": "EX5B99", "label": "JKT48 Exclusive EX5B99"}],
    "ignore_members": ["Desy Natalia"],
    "fail_alert_after": 3,
    "max_lines": 25,
}


def log(line: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}Z] {line}", flush=True)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def take_test_alert() -> str:
    try:
        return TEST_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def config() -> dict:
    cfg = load_json(CONFIG_PATH, None)
    merged = dict(DEFAULT_CONFIG)
    if isinstance(cfg, dict):
        merged.update(cfg)
    return merged


def ignored(member: str, ignore_list) -> bool:
    name = (member or "").strip().casefold()
    return any(name == str(i).strip().casefold() for i in ignore_list)


def _get(url: str) -> tuple[str, str]:
    """(body, error). Tanpa cookie jar — memang cuma butuh header browser."""
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", "replace"), ""
    except urllib.error.HTTPError as exc:
        return "", f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return "", f"{type(exc).__name__}: {exc}"


def _json_or_none(body: str):
    """(payload, error). Browser kadang bungkus JSON jadi HTML — ambil dari '{' pertama."""
    if not body:
        return None, "body kosong"
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        return None, f"bukan JSON ({len(body)} bytes)"
    try:
        return json.loads(body[start:end + 1]), ""
    except ValueError as exc:
        return None, f"JSON rusak: {exc}"


def _stealth_json(url: str):
    """Scrapling StealthyFetcher — browser beneran (Camoufox) buat nembus managed
    challenge Cloudflare yang nolak client HTTP biasa dari IP datacenter.
    Dikasih batas waktu keras: di runner GitHub pernah ngunci >10 menit."""
    try:
        from scrapling.fetchers import StealthyFetcher
    except Exception as exc:  # noqa: BLE001
        return None, f"scrapling tidak tersedia ({type(exc).__name__})"

    def _ambil():
        try:
            page = StealthyFetcher.fetch(url, headless=True, solve_cloudflare=True,
                                         timeout=45000, network_idle=False)
        except TypeError:  # versi lama gak kenal sebagian kwarg
            page = StealthyFetcher.fetch(url, headless=True, timeout=45000)
        raw = getattr(page, "body", None) or getattr(page, "text", "") or ""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        return raw

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            raw = pool.submit(_ambil).result(timeout=90)
    except concurrent.futures.TimeoutError:
        return None, "stealth timeout >90s"
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"
    payload, err = _json_or_none(raw)
    return (payload, "") if payload is not None else (None, err)


def _proxy_json(url: str):
    """Lewat proxy pembaca teks (r.jina.ai) — dipakai kalau Cloudflare nolak IP
    datacenter secara langsung. Balikannya JSON asli yang dibungkus teks, jadi
    diambil dari '{' pertama sampai '}' terakhir."""
    req = urllib.request.Request(JINA + url, headers={
        "User-Agent": HEADERS["User-Agent"], "Accept": "text/plain, */*"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return None, f"proxy: {type(exc).__name__}: {exc}"
    payload, err = _json_or_none(body)
    if payload is None:
        return None, f"proxy: {err}"
    if not payload.get("status"):
        return None, f"proxy: status={payload.get('status')!r}"
    if not any(s.get("session_members") for s in payload.get("data") or []):
        return None, "proxy: data sesi kosong"
    return payload, ""


def fetch(code: str) -> tuple[dict | None, str]:
    """Rantai: langsung -> proxy teks -> browser stealth (kalau USE_SCRAPLING=1).
    Tanpa mode stealth, ditutup fallback retry + curl."""
    url = API.format(code=code)
    stealth = (os.environ.get("USE_SCRAPLING") or "").strip().lower() in {"1", "true", "yes"}

    body, err = _get(url)
    payload, jerr = _json_or_none(body)
    if payload is not None:
        return payload, ""
    err = jerr or err
    log(f"cara langsung gagal untuk {code} ({err})")

    payload, perr = _proxy_json(url)
    if payload is not None:
        log("berhasil lewat proxy r.jina.ai")
        return payload, ""
    log(f"proxy gagal untuk {code} ({perr})")

    if stealth:
        payload, serr = _stealth_json(url)
        if payload is not None:
            log("berhasil lewat StealthyFetcher (browser)")
            return payload, ""
        log(f"stealth gagal: {serr}")
        return None, f"langsung: {err}; {perr}; scrapling: {serr}"

    for attempt, wait in enumerate((3, 6, 10), start=2):
        time.sleep(wait)
        body, err = _get(url)
        payload, jerr = _json_or_none(body)
        if payload is not None:
            return payload, ""
        err = jerr or err
        log(f"percobaan {attempt} untuk {code} gagal ({err})")
    warm, _ = _get(BASE + "/")
    log(f"warm-up halaman utama: {'ok' if warm else 'gagal'}")

    log(f"urllib habis percobaan untuk {code} ({err}); fallback ke curl")
    argv = ["curl", "-sS", "--max-time", "30", "-w", "\n%{http_code}"]
    for key, value in HEADERS.items():
        argv += ["-H", f"{key}: {value}"]
    argv.append(url)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except Exception as exc:  # noqa: BLE001
        return None, f"urllib: {err}; curl: {type(exc).__name__}: {exc}"
    out = proc.stdout or ""
    body, _, status = out.rpartition("\n")
    if proc.returncode != 0 or not status.strip().startswith("2"):
        return None, f"urllib: {err}; curl HTTP {status.strip() or proc.returncode}"
    try:
        return json.loads(body), ""
    except ValueError:
        return None, f"urllib: {err}; curl: non-JSON response"


def normalize(payload: dict) -> dict:
    sessions: dict[str, dict] = {}
    lanes: dict[str, dict] = {}
    for sess in payload.get("data") or []:
        scode = str(sess.get("exclusive_session_code") or "")
        if not scode:
            continue
        sessions[scode] = {
            "date": sess.get("date"),
            "label": sess.get("label"),
            "start": sess.get("start_time"),
            "end": sess.get("end_time"),
            "lanes": len(sess.get("session_members") or []),
        }
        for lane in sess.get("session_members") or []:
            lcode = str(lane.get("session_detail_code") or "")
            if not lcode:
                continue
            lanes[lcode] = {
                "s": scode,
                "j": lane.get("label"),
                "m": lane.get("member_name"),
                "q": lane.get("available_quota"),
                "p": lane.get("price"),
            }
    return {"sessions": sessions, "lanes": lanes}


def session_title(ev_state: dict, scode: str) -> str:
    sess = ev_state["sessions"].get(scode) or {}
    label = sess.get("label") or scode
    start = (sess.get("start") or "")[:5]
    return f"{label} {start}".strip()


def diff_event(old: dict, new: dict, ignore_list, max_lines: int) -> list[str]:
    lines: list[str] = []
    old_s, new_s = old.get("sessions", {}), new["sessions"]
    old_l, new_l = old.get("lanes", {}), new["lanes"]

    for scode in sorted(new_s):
        if scode not in old_s:
            lane_codes = [c for c in new_l if new_l[c]["s"] == scode]
            kept = [c for c in lane_codes if not ignored(new_l[c].get("m"), ignore_list)]
            quota = sum((new_l[c].get("q") or 0) for c in kept)
            lines.append(
                f"🆕 SESI BARU: {session_title(new, scode)} — {len(lane_codes)} jalur, "
                f"kuota tersisa {quota}"
            )
    for scode in sorted(old_s):
        if scode not in new_s:
            lines.append(f"❌ SESI HILANG: {session_title(old, scode)}")

    for lcode in sorted(new_l):
        lane = new_l[lcode]
        if ignored(lane.get("m"), ignore_list):
            continue
        before = old_l.get(lcode)
        where = f"{session_title(new, lane['s'])} • {lane.get('j')} — {lane.get('m')}"
        if before is None:
            lines.append(f"🆕 JALUR BARU: {where} (kuota {lane.get('q')})")
            continue
        oldq, newq = before.get("q"), lane.get("q")
        if oldq != newq:
            delta = ""
            mark = "🔁"
            if isinstance(oldq, int) and isinstance(newq, int):
                diff = newq - oldq
                delta = f"  ({'+' if diff > 0 else ''}{diff})"
                mark = "➕" if diff > 0 else ("➖" if diff < 0 else "•")
            lines.append(f"{mark} {where}: kuota {oldq} → {newq}{delta}")
        if before.get("m") != lane.get("m"):
            lines.append(f"🔁 {where}: member {before.get('m')} → {lane.get('m')}")
        if before.get("p") != lane.get("p"):
            lines.append(f"💰 {where}: harga {before.get('p')} → {lane.get('p')}")

    for lcode in sorted(old_l):
        if lcode not in new_l:
            lane = old_l[lcode]
            if ignored(lane.get("m"), ignore_list):
                continue
            lines.append(
                f"❌ JALUR HILANG: {session_title(old, lane['s'])} • {lane.get('j')} — "
                f"{lane.get('m')} (kuota terakhir {lane.get('q')})"
            )

    if len(lines) > max_lines:
        extra = len(lines) - max_lines
        lines = lines[:max_lines] + [f"…dan {extra} perubahan lain lagi"]
    return lines


def send_discord(text: str) -> bool:
    """Kirim ke Discord via webhook (secret repo). True kalau sukses."""
    url = (os.environ.get("DISCORD_WEBHOOK_URL") or "").strip()
    if not url:
        log("DISCORD_WEBHOOK_URL kosong — tidak bisa kirim notifikasi")
        return False
    payload = json.dumps({"content": text[:1900]}).encode()
    req = urllib.request.Request(url, data=payload, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "jkt48-quota-watch/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            ok = 200 <= resp.status < 300
            log(f"kirim Discord: HTTP {resp.status}")
            return ok
    except urllib.error.HTTPError as exc:
        log(f"kirim Discord GAGAL: HTTP {exc.code} {exc.read()[:200]!r}")
    except Exception as exc:  # noqa: BLE001
        log(f"kirim Discord GAGAL: {type(exc).__name__}: {exc}")
    return False


def save_state(prev: dict, events: dict, fail_streak: int, fail_notified: bool) -> bool:
    """Tulis state.json HANYA kalau isi pentingnya berubah. Kalau tidak, workflow bakal
    commit tiap menit cuma karena timestamp — repo jadi sampah 1.400 commit/hari."""
    if all(prev.get(k) == v for k, v in (("events", events), ("fail_streak", fail_streak),
                                        ("fail_notified", fail_notified))):
        log("state tidak berubah — tidak ditulis ulang")
        return False
    save_json(STATE_PATH, {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fail_streak": fail_streak, "fail_notified": fail_notified, "events": events})
    log("state diperbarui")
    return True


def main() -> int:
    cfg = config()
    ignore_list = cfg.get("ignore_members") or []
    max_lines = int(cfg.get("max_lines") or 25)
    fail_alert_after = int(cfg.get("fail_alert_after") or 3)

    prev = load_json(STATE_PATH, None)
    first_run = not isinstance(prev, dict) or "events" not in (prev or {})
    prev = prev if isinstance(prev, dict) else {}
    prev_events = prev.get("events") or {}
    prev_streak = int(prev.get("fail_streak") or 0)

    now = datetime.now(timezone.utc)
    test_alert = take_test_alert()

    new_events: dict[str, dict] = {}
    blocks: list[tuple[str, dict, list[str]]] = []
    errors: list[str] = []

    for entry in cfg.get("events") or []:
        code = str(entry.get("code") or "").strip()
        if not code:
            continue
        label = entry.get("label") or code
        payload, err = fetch(code)
        if payload is None or not payload.get("status"):
            errors.append(f"{label}: {err or 'status=false'}")
            continue
        snap = normalize(payload)
        changes = diff_event(prev_events.get(code) or {}, snap, ignore_list, max_lines)
        new_events[code] = snap
        if changes and not first_run:
            blocks.append((label, snap, changes))
        log(f"OK {code}: {len(snap['lanes'])} jalur, "
            f"{sum((l.get('q') or 0) for l in snap['lanes'].values())} kuota, "
            f"{len(changes)} perubahan")

    for code, snap in prev_events.items():
        new_events.setdefault(code, snap)

    if errors and not new_events:
        fail_streak = prev_streak + 1
    elif errors:
        fail_streak = prev_streak
    else:
        fail_streak = 0

    if first_run:
        total = sum(sum((l.get("q") or 0) for l in s["lanes"].values()) for s in new_events.values())
        log(f"baseline pertama: total kuota {total} (tidak ada notifikasi)")
        if test_alert and send_discord(test_alert):
            TEST_PATH.unlink(missing_ok=True)
        save_state(prev, new_events, fail_streak, False)
        return 0

    messages: list[str] = []
    if test_alert:
        messages.append(test_alert)

    if errors and fail_streak >= fail_alert_after and not prev.get("fail_notified"):
        messages.append("⚠️ Watcher kuota JKT48 gagal ambil data "
                        f"{fail_streak}x berturut-turut: {'; '.join(errors)}")
        prev["fail_notified"] = True

    for idx, (label, snap, changes) in enumerate(blocks):
        head = f"🔔 *{label}* — ada perubahan kuota!\n🕒 {now.strftime('%d %b %Y %H:%M')} UTC\n"
        if idx > 0:
            head = f"— {label} —"
        total = sum((l.get("q") or 0) for l in snap["lanes"].values())
        tail = f"\nTotal kuota tersisa sekarang: {total}"
        messages.append(head + "\n" + "\n".join(changes) + tail)

    delivered = True
    if messages:
        delivered = send_discord("\n\n".join(messages))
        if delivered and test_alert:
            TEST_PATH.unlink(missing_ok=True)

    # State hanya dimajukan kalau notifikasi benar-benar terkirim (atau tidak ada notif),
    # supaya alert yang gagal kirim tetap dicoba lagi di run berikutnya.
    if delivered or not messages:
        if not errors and prev.get("fail_notified"):
            prev["fail_notified"] = False
        save_state(prev, new_events, fail_streak, bool(prev.get("fail_notified")))
    else:
        log("state TIDAK dimajukan karena pengiriman gagal")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        log(f"UNCAUGHT {type(exc).__name__}: {exc}")
        sys.exit(0)
