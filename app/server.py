"""Webová služba a API pro čekací doby přípojů vlaků v ČR.

Bez externích závislostí (jen standardní knihovna Pythonu).

Proměnné prostředí:
  PORT            port HTTP serveru (Railway nastavuje sám), výchozí 8000
  GTFS_URL        odkud stahovat GTFS, výchozí https://motis.obehy.cz/get-feeds/cz-czptt-gtfs.zip
  GTFS_PATH       místo stahování lze použít lokální zip / adresář GTFS (např. pro vývoj)
  TRAINS_DB       kam uložit databázi vlaků, výchozí ./var/trains.db
  REFRESH_HOURS   jak často GTFS znovu stáhnout a přepočítat, výchozí 84 (2× týdně)
  HANDBOOK_URL    odkaz na Excel pomůcky „Přípoje mezi vlaky osobní dopravy“ (výchozí JŘ 2026)
  HANDBOOK_REFRESH_DAYS  jak často kontrolovat novou verzi Excelu, výchozí 7 (přepočítá se jen při změně)
  ADMIN_TOKEN     heslo pro ruční aktualizaci na /admin (bez něj je administrace vypnutá)
  REPORT_WEBHOOK_URL  kam poslat upozornění na nahlášenou chybu (Discord webhook, ntfy.sh téma apod.; nepovinné)

Endpointy (všechny GET, odpověď JSON, CORS povolen):
  /health                         stav služby (ready = databáze vlaků je připravena)
  /api/info                       informace o datech (platnost pomůcky, verze GTFS)
  /api/stations?q=Brno            vyhledání stanice (stanice z pomůcky mají in_handbook=true)
  /api/trains?station=&date=      vlaky ve stanici v daný den: příjezdy a odjezdy
  /api/connection?station=&date=&arriving=&departing=
                                  vyhodnocení: arriving = čím přijíždím (přípojný vlak),
                                  departing = na co přestupuji (návazný vlak)
  /api/station-table?station=&date=   tabulka stanice z pomůcky platná k datu
  /api/itinerary?text=&url=&date=     rozbor sdíleného spojení (text / odkaz z vyhledávače) -> přestupy a čekací doby
  /share?text=&url=&title=            stránka, na kterou Android posílá sdílený obsah (Web Share Target)
  /admin                              ruční aktualizace GTFS a pomůcky (POST /admin/action, heslo ADMIN_TOKEN)
"""
import datetime as dt
import hmac
import secrets
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from engine.evaluate import Engine, _norm_name, hhmm, base_number, number_tiers  # noqa: E402
from engine.itinerary import analyze, ics_to_text, parse_trains, prague_now, URL_RE, MAPY_RE  # noqa: E402
from app.stats import Stats  # noqa: E402
from email.parser import BytesParser  # noqa: E402
from email import policy  # noqa: E402

REPO_DATA_DIR = os.path.join(ROOT, "data")
STATIC_DIR = os.path.join(ROOT, "app", "static")
TRAINS_DB = os.environ.get("TRAINS_DB", os.path.join(ROOT, "var", "trains.db"))
VAR_DIR = os.path.dirname(TRAINS_DB)
RUNTIME_DATA_DIR = os.path.join(VAR_DIR, "data")            # pomůcka stažená za běhu (přednost před repozitářem)
SETTINGS_FILE = os.path.join(VAR_DIR, "settings.json")
HANDBOOK_META = os.path.join(VAR_DIR, "handbook_meta.json")
GTFS_PATH = os.environ.get("GTFS_PATH")
REFRESH_HOURS = float(os.environ.get("REFRESH_HOURS", "84"))   # 2× týdně
HANDBOOK_REFRESH_DAYS = float(os.environ.get("HANDBOOK_REFRESH_DAYS", "7"))
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
STATS = Stats(os.path.join(VAR_DIR, "stats.json"))
DEFAULTS = {
    "gtfs_url": os.environ.get("GTFS_URL", "https://motis.obehy.cz/get-feeds/cz-czptt-gtfs.zip"),
    "handbook_url": os.environ.get("HANDBOOK_URL",
                                   "https://provoz.spravazeleznic.cz/Portal/Show.aspx?path=/Data/Grafikon/data/sena/pripoje/JR2026/Pripoje_2026.xlsx"),
}


class State:
    engine = None
    lock = threading.Lock()
    status = "starting"
    built_at = None
    error = None
    gtfs_event = threading.Event()
    handbook_event = threading.Event()
    gtfs_upload = None          # cesta k ručně nahranému GTFS zipu
    handbook_upload = None      # cesta k ručně nahranému Excelu
    jobs = {"gtfs": "–", "handbook": "–"}


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def settings():
    s = dict(DEFAULTS)
    try:
        s.update(json.load(open(SETTINGS_FILE, encoding="utf-8")))
    except Exception:
        pass
    return s


def save_settings(**kw):
    s = settings()
    s.update({k: v for k, v in kw.items() if v})
    os.makedirs(VAR_DIR, exist_ok=True)
    json.dump(s, open(SETTINGS_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def data_dir():
    return RUNTIME_DATA_DIR if os.path.exists(os.path.join(RUNTIME_DATA_DIR, "index.json")) else REPO_DATA_DIR


def download(url, dest, timeout=300):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (vhd-pripoje)"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    return dest


# ---------------------------------------------------------------- GTFS
def build_database(src_override=None):
    """Stáhne GTFS a postaví databázi vlaků do dočasného souboru, pak ji atomicky vymění."""
    os.makedirs(VAR_DIR, exist_ok=True)
    tmpdir = tempfile.mkdtemp(prefix="gtfs-")
    try:
        if src_override or GTFS_PATH:
            src = src_override or GTFS_PATH
            log("GTFS z lokální cesty", src)
        else:
            url = settings()["gtfs_url"]
            src = os.path.join(tmpdir, "gtfs.zip")
            log("Stahuji GTFS", url)
            download(url, src)
            log("Staženo", os.path.getsize(src) // 1024, "kB")
        new_db = TRAINS_DB + ".new"
        t0 = time.time()
        # přepočet v samostatném procesu – jeho paměť se po skončení celá vrátí (hlavní služba zůstane malá)
        subprocess.run([sys.executable, os.path.join(ROOT, "tools", "build_gtfs_db.py"), src, new_db], check=True)
        log(f"Databáze vlaků hotová za {time.time() - t0:.0f} s")
        os.replace(new_db, TRAINS_DB)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def load_engine():
    eng = Engine(data_dir(), TRAINS_DB)
    with State.lock:
        State.engine = eng
        State.status = "ready"
        State.built_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(TRAINS_DB)))
    log("Engine připraven, data pomůcky:", data_dir())


def gtfs_worker():
    first = True
    while True:
        try:
            forced = State.gtfs_event.is_set()
            State.gtfs_event.clear()
            fresh = os.path.exists(TRAINS_DB) and (time.time() - os.path.getmtime(TRAINS_DB)) < REFRESH_HOURS * 3600
            if first and fresh and not forced:
                load_engine()
            else:
                if State.engine is None:
                    State.status = "building"
                State.jobs["gtfs"] = "probíhá " + time.strftime("%H:%M:%S")
                upload, State.gtfs_upload = State.gtfs_upload, None
                build_database(upload)
                load_engine()
                State.jobs["gtfs"] = "hotovo " + time.strftime("%Y-%m-%d %H:%M:%S")
            State.error = None
        except Exception as e:
            State.error = f"GTFS: {type(e).__name__}: {e}"
            State.jobs["gtfs"] = "chyba: " + State.error
            log("Chyba při přípravě GTFS:", State.error)
            traceback.print_exc()
            if State.engine is None and os.path.exists(TRAINS_DB):
                try:
                    load_engine()   # použij starší databázi, pokud existuje
                except Exception:
                    pass
            first = False
            State.gtfs_event.wait(600)
            continue
        first = False
        State.gtfs_event.wait(REFRESH_HOURS * 3600)


# ---------------------------------------------------------------- pomůcka (Excel)
def handbook_meta():
    try:
        return json.load(open(HANDBOOK_META, encoding="utf-8"))
    except Exception:
        return {}


def update_handbook(force=False):
    """Stáhne Excel pomůcky; pokud se změnil, přepočítá data stanic a vymění je za běhu."""
    import hashlib
    os.makedirs(VAR_DIR, exist_ok=True)
    tmpdir = tempfile.mkdtemp(prefix="handbook-")
    try:
        upload, State.handbook_upload = State.handbook_upload, None
        if upload:
            xlsx, source = upload, "ruční nahrání"
        else:
            source = settings()["handbook_url"]
            xlsx = os.path.join(tmpdir, "pripoje.xlsx")
            log("Stahuji pomůcku", source)
            download(source, xlsx, timeout=120)
        raw = open(xlsx, "rb").read()
        if raw[:2] != b"PK":
            raise ValueError("Stažený soubor není Excel (xlsx) – zkontrolujte odkaz.")
        sha = hashlib.sha256(raw).hexdigest()
        meta = handbook_meta()
        meta["checked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if sha == meta.get("sha256") and not force and os.path.exists(os.path.join(RUNTIME_DATA_DIR, "index.json")):
            json.dump(meta, open(HANDBOOK_META, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            log("Pomůcka beze změny")
            return "beze změny"
        out = os.path.join(VAR_DIR, "data.new")
        shutil.rmtree(out, ignore_errors=True)
        os.makedirs(out)
        subprocess.run([sys.executable, os.path.join(ROOT, "tools", "build_json.py"), xlsx, out], check=True)
        idx = json.load(open(os.path.join(out, "index.json"), encoding="utf-8"))
        if idx.get("station_count", 0) < 50:
            raise ValueError(f"Nová data mají jen {idx.get('station_count')} stanic – nepoužito.")
        for f in ("rules.json", "station_types.json", "coverage_report.json"):
            if os.path.exists(os.path.join(REPO_DATA_DIR, f)):
                shutil.copy(os.path.join(REPO_DATA_DIR, f), os.path.join(out, f))
        old = os.path.join(VAR_DIR, "data.old")
        shutil.rmtree(old, ignore_errors=True)
        if os.path.exists(RUNTIME_DATA_DIR):
            os.replace(RUNTIME_DATA_DIR, old)
        os.replace(out, RUNTIME_DATA_DIR)
        review = json.load(open(os.path.join(RUNTIME_DATA_DIR, "review.json"), encoding="utf-8"))
        meta.update(sha256=sha, source=source, updated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                    timetable=idx["source"].get("timetable"), stations=idx.get("station_count"), review_items=review.get("count"))
        json.dump(meta, open(HANDBOOK_META, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        if State.engine is not None:
            load_engine()
        log("Pomůcka aktualizována:", meta)
        return "aktualizováno"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def handbook_worker():
    time.sleep(5)
    while True:
        forced = State.handbook_event.is_set()
        State.handbook_event.clear()
        meta = handbook_meta()
        last = meta.get("checked_at")
        due = forced or not last or (time.time() - time.mktime(time.strptime(last, "%Y-%m-%d %H:%M:%S"))) > HANDBOOK_REFRESH_DAYS * 86400 \
            or not os.path.exists(os.path.join(RUNTIME_DATA_DIR, "index.json"))
        if due:
            try:
                State.jobs["handbook"] = "probíhá " + time.strftime("%H:%M:%S")
                res = update_handbook(force=forced)
                State.jobs["handbook"] = f"{res} " + time.strftime("%Y-%m-%d %H:%M:%S")
            except Exception as e:
                State.jobs["handbook"] = f"chyba: {type(e).__name__}: {e}"
                log("Chyba při aktualizaci pomůcky:", State.jobs["handbook"])
                traceback.print_exc()
        State.handbook_event.wait(6 * 3600)     # kontrola termínu každých 6 h


# ---------------------------------------------------------------- API
def parse_date(s):
    if not s:
        return dt.date.today()
    return dt.date.fromisoformat(s)


def api_stations(q):
    eng = State.engine
    qn = _norm_name(q or "")
    handbook = {st["station"] for st in eng.index["stations"]}
    out = []
    for st in sorted(handbook):
        if not qn or _norm_name(st).startswith(qn) or qn in _norm_name(st):
            out.append({"name": st, "in_handbook": True})
    if qn:
        seen = {_norm_name(x["name"]) for x in out}
        hb_ids = set(eng.station_gtfs.values())
        for sid, name in eng.names.items():
            n = _norm_name(name)
            if n in seen or sid in hb_ids or ":CZ:" not in sid:
                continue
            if n.startswith(qn):
                out.append({"name": name, "in_handbook": False})
                seen.add(n)
    out.sort(key=lambda x: (not _norm_name(x["name"]).startswith(qn), not x["in_handbook"], x["name"]))
    return {"stations": out[:30]}


def api_trains(station, date):
    eng = State.engine
    name, sid = eng.find_station(station)
    if sid is None:
        return 404, {"error": f"Stanice '{station}' nenalezena."}
    group = eng.districts.get(sid, [sid])
    arrivals, departures = [], []
    seen_a, seen_d = set(), set()
    for shift in (0, -1):
        d = date + dt.timedelta(days=shift)
        rows = eng.db.execute(
            f"""select t.tid, t.number, t.category, t.operator, t.headsign, p.arr, p.dep, p.seq,
                       (select min(seq) from stops where tid=t.tid), (select max(seq) from stops where tid=t.tid),
                       (select station_id from stops where tid=t.tid order by seq limit 1)
                from trains t join service_dates s on s.service_id=t.service_id join stops p on p.tid=t.tid
                where s.date=? and p.station_id in ({','.join('?' * len(group))})""",
            (int(d.strftime("%Y%m%d")), *group)).fetchall()
        for tid, num, cat, op, hs, arr, dep, seq, smin, smax, first in rows:
            arr = arr - 1440 * (-shift) if arr is not None else None
            dep = dep - 1440 * (-shift) if dep is not None else None
            t_here = dep if dep is not None else arr
            if t_here is None or t_here < 0 or t_here >= 1440 + 180:
                continue
            origin = eng.names.get(first, "")
            base = {"number": num, "category": cat, "operator": op, "label": f"{cat} {num}"}
            if seq != smin and arr is not None and num not in seen_a:
                seen_a.add(num)
                arrivals.append({**base, "time": hhmm(arr), "minutes": arr, "from": origin})
            if (seq != smax or (hs and hs != eng.names.get(sid))) and dep is not None and num not in seen_d:
                seen_d.add(num)
                departures.append({**base, "time": hhmm(dep), "minutes": dep, "to": hs})
    arrivals.sort(key=lambda x: x["minutes"])
    departures.sort(key=lambda x: x["minutes"])
    return 200, {"station": name or eng.names[sid], "date": date.isoformat(), "day_type": eng.day_type(date),
                 "in_handbook": name is not None, "arrivals": arrivals, "departures": departures}


def api_station_table(station, date):
    eng = State.engine
    name, sid = eng.find_station(station)
    if not name:
        return 404, {"error": "Stanice nemá v pomůcce vlastní tabulku – platí obecná pravidla."}
    st, warn = eng.station_file(name, date)
    return 200, {"warning": warn, "table": st}


def fetch_url(url):
    """Stáhne stránku sdíleného odkazu (max. 3 MB) – použije se, když sdílený text neobsahuje čísla vlaků."""
    if not url.startswith(("http://", "https://")):
        raise ValueError("Neplatný odkaz")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (vhd-pripoje)", "Accept-Language": "cs"})
    with urllib.request.urlopen(req, timeout=12) as r:
        raw = r.read(3_000_000)
        enc = r.headers.get_content_charset() or "utf-8"
    return raw.decode(enc, "replace")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


def fetch_raw(url):
    """Jeden krok rozbalení krátkého odkazu: (stav, Location, začátek těla) – bez následování přesměrování."""
    if not url.startswith(("http://", "https://")):
        raise ValueError("Neplatný odkaz")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Linux; Android 14) vhd-pripoje",
                                               "Accept-Language": "cs"})
    try:
        with _NO_REDIRECT_OPENER.open(req, timeout=12) as r:
            raw = r.read(1_000_000)
            return r.status, None, raw.decode(r.headers.get_content_charset() or "utf-8", "replace")
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400:
            return e.code, e.headers.get("Location"), ""
        raise


RENDER_SEM = threading.Semaphore(int(os.environ.get("RENDER_CONCURRENCY", "1")))
_HAS_PLAYWRIGHT = None


def render_route(text):
    """Odkaz Mapy.com bez čísel vlaků -> spojení přečtená ze stránky v neviditelném prohlížeči (samostatný proces).
    Vypnutí: RENDER_ROUTES=0. Bez nainstalovaného Playwrightu se tiše přeskočí (spojení se dohledá v jízdním řádu)."""
    global _HAS_PLAYWRIGHT
    if os.environ.get("RENDER_ROUTES", "1") == "0" or not text:
        return None
    if len(parse_trains(text)) >= 2:
        return None
    urls = [u.rstrip(").,") for u in URL_RE.findall(text) if MAPY_RE.search(u)]
    if not urls:
        return None
    if _HAS_PLAYWRIGHT is None:
        import importlib.util
        _HAS_PLAYWRIGHT = importlib.util.find_spec("playwright") is not None
    if not _HAS_PLAYWRIGHT:
        return None
    if not RENDER_SEM.acquire(timeout=60):
        return {"connections": [], "error": "Prohlížeč je vytížený, zkuste to za chvíli."}
    try:
        t0 = time.time()
        r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "render_route.py"), urls[0], "4", "30"],
                           capture_output=True, text=True, timeout=50)
        log(f"render {urls[0]}: {time.time() - t0:.1f} s, exit {r.returncode}")
        if r.returncode != 0:
            return {"connections": [], "error": (r.stderr or "")[-200:]}
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception as e:
        return {"connections": [], "error": f"{type(e).__name__}"}
    finally:
        RENDER_SEM.release()


REPORTS_FILE = os.path.join(VAR_DIR, "reports.jsonl")
REPORTS_DONE = os.path.join(VAR_DIR, "reports_done.json")
_REPORT_LOCK = threading.Lock()
_REPORT_RATE = {}


def debug_trains(numbers, date):
    """Podklady k nahlášené chybě: co o vlacích ví náš jízdní řád (jízdy, dny, první/poslední stanice)."""
    eng = State.engine
    if eng is None:
        return {}
    out = {}
    with State.lock:
        for n in sorted(set(numbers))[:6]:
            cand = sorted(set().union(*number_tiers(n)))
            rows = eng.db.execute(
                f"select tid, number, category, operator, part, service_id, headsign, bus from trains where number in ({','.join('?' * len(cand))})",
                cand).fetchall()
            trips = []
            for tid, num, cat, op, part, sid, hs, bus in rows[:40]:
                days = [r[0] for r in eng.db.execute(
                    "select date from service_dates where service_id=? and date between ? and ?",
                    (sid, int((date - dt.timedelta(days=1)).strftime("%Y%m%d")), int((date + dt.timedelta(days=1)).strftime("%Y%m%d"))))]
                st = eng.db.execute("select station_id, arr, dep from stops where tid=? order by seq", (tid,)).fetchall()
                trips.append({"number": num, "cat": cat, "op": op, "part": part, "bus": bool(bus), "headsign": hs,
                              "runs_around_date": days, "stops": len(st),
                              "first": [eng.names.get(st[0][0], st[0][0]), st[0][1], st[0][2]] if st else None,
                              "last": [eng.names.get(st[-1][0], st[-1][0]), st[-1][1], st[-1][2]] if st else None})
            similar = [r[0] for r in eng.db.execute(
                "select distinct number from trains where cast(number as text) like ? limit 10", (f"%{n}%",))]
            out[str(n)] = {"trips_total": len(rows), "trips": trips, "similar_numbers": similar}
    return out


def notify_report(rep):
    """Upozornění na nové hlášení: REPORT_WEBHOOK_URL (Discord webhook, ntfy.sh/… nebo jiný POST)."""
    url = os.environ.get("REPORT_WEBHOOK_URL", "").strip()
    if not url:
        return
    inp = rep.get("input") or {}
    resp = rep.get("response") or {}
    lines = [f"Nahlášená chyba #{rep['id']} ({rep.get('page')})"]
    if rep.get("note"):
        lines.append(f"Poznámka: {rep['note']}")
    lines.append("Zadání: " + (inp.get("text") or " ".join(f"{k}={v}" for k, v in inp.items()))[:600])
    msgs = [t.get("message") for t in resp.get("transfers", []) if t.get("message")] or [resp.get("message")]
    lines.append("Odpověď: " + " | ".join(m for m in msgs if m)[:800])
    text = "\n".join(lines)
    try:
        if "discord.com/api/webhooks" in url or "discordapp.com/api/webhooks" in url:
            data, ctype = json.dumps({"content": text[:1900]}).encode(), "application/json"
        else:
            data, ctype = text.encode("utf-8"), "text/plain; charset=utf-8"
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": ctype, "Title": "Pripoje: nahlasena chyba", "User-Agent": "vhd-pripoje"})
        urllib.request.urlopen(req, timeout=8).close()
    except Exception as e:
        log(f"webhook hlášení selhal: {type(e).__name__}: {e}")


def save_report(body, ip):
    import hashlib
    key = hashlib.sha256(ip.encode()).hexdigest()[:12]
    now = time.time()
    hits = [t for t in _REPORT_RATE.get(key, []) if now - t < 3600]
    if len(hits) >= 20:
        raise ValueError("Příliš mnoho hlášení, zkuste to později.")
    _REPORT_RATE[key] = hits + [now]
    resp = body.get("response") or {}
    inp = body.get("input") or {}
    nums = []
    for l in resp.get("legs", []) or []:
        if isinstance(l, dict) and isinstance(l.get("number"), int):
            nums.append(l["number"])
    for k in ("arriving", "departing"):
        try:
            nums.append(int(str(inp.get(k, "")).strip()))
        except ValueError:
            pass
    for k in ("connecting_train", "waiting_train"):
        if isinstance((resp.get(k) or {}).get("number"), int):
            nums.append(resp[k]["number"])
    try:
        d = dt.date.fromisoformat(str(resp.get("date") or inp.get("date") or (resp.get("input") or {}).get("date")))
    except ValueError:
        d = dt.date.today()
    rep = {"id": f"{int(now)}-{secrets.token_hex(2)}", "time": prague_now().isoformat(timespec="seconds"),
           "page": str(body.get("page", ""))[:20], "note": str(body.get("note", ""))[:1000],
           "input": inp, "response": resp, "debug_trains": debug_trains(nums, d),
           "version": os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")[:7], "timetable_built_at": State.built_at}
    with _REPORT_LOCK:
        os.makedirs(VAR_DIR, exist_ok=True)
        with open(REPORTS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rep, ensure_ascii=False) + "\n")
    threading.Thread(target=notify_report, args=(rep,), daemon=True).start()
    return rep["id"]


def load_reports():
    done = set()
    try:
        with open(REPORTS_DONE, encoding="utf-8") as f:
            done = set(json.load(f))
    except (OSError, ValueError):
        pass
    out = []
    try:
        with open(REPORTS_FILE, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                r["done"] = r["id"] in done
                out.append(r)
    except OSError:
        pass
    return list(reversed(out))


def mark_report_done(rid, done=True):
    with _REPORT_LOCK:
        ids = {r["id"]: r["done"] for r in load_reports()}
        cur = {k for k, v in ids.items() if v}
        (cur.add if done else cur.discard)(rid)
        with open(REPORTS_DONE, "w", encoding="utf-8") as f:
            json.dump(sorted(cur), f)


def api_info():
    eng = State.engine
    return {"handbook": eng.index.get("source", eng.rules["document"]), "stations_in_handbook": len(eng.index["stations"]),
            "timetable_db_built_at": State.built_at, "gtfs_source": GTFS_PATH or settings()["gtfs_url"],
            "handbook_data": "staženo za běhu" if data_dir() == RUNTIME_DATA_DIR else "z repozitáře",
            "handbook_meta": handbook_meta(), "handbook_url": settings()["handbook_url"], "jobs": State.jobs}


class Handler(BaseHTTPRequestHandler):
    server_version = "vhd-pripoje/1.0"

    def log_message(self, fmt, *args):
        log(self.address_string(), fmt % args)

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store" if ctype.startswith("application/json") else "public, max-age=300")
        self.end_headers()
        self.wfile.write(data)

    def _client(self):
        ip = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip() or self.client_address[0]
        return ip, self.headers.get("User-Agent", "")

    def _internal(self):
        """Dotaz z našich stránek (Referer ze stejné domény) – jinak jde o cizí využití API."""
        ref = urllib.parse.urlparse(self.headers.get("Referer", "")).netloc
        return bool(ref) and ref == (self.headers.get("Host") or "")

    def _count(self, fn, *a):
        try:
            fn(*a)
        except Exception:
            traceback.print_exc()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def _admin_action(self):
        """Ruční aktualizace dat (heslo ADMIN_TOKEN): action=gtfs|handbook, volitelně url (uloží se jako nový zdroj) a soubor."""
        import hmac
        length = int(self.headers.get("Content-Length") or 0)
        if length > 300_000_000:
            return self._send(413, {"error": "Soubor je příliš velký."})
        body = self.rfile.read(length)
        ctype = self.headers.get("Content-Type", "")
        fields, files = {}, {}
        if ctype.startswith("multipart/form-data"):
            msg = BytesParser(policy=policy.default).parsebytes(
                b"Content-Type: " + ctype.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
            for part in msg.iter_parts():
                name = part.get_param("name", header="content-disposition") or ""
                data = part.get_payload(decode=True) or b""
                if part.get_filename():
                    if data:
                        files[name] = (part.get_filename(), data)
                else:
                    fields[name] = data.decode("utf-8", "replace").strip()
        else:
            fields = {k: v[0] for k, v in urllib.parse.parse_qs(body.decode("utf-8", "replace")).items()}
        token = fields.get("token") or self.headers.get("X-Admin-Token", "")
        if not ADMIN_TOKEN:
            return self._send(403, {"error": "Administrace je vypnutá – nastavte na Railway proměnnou ADMIN_TOKEN."})
        if not hmac.compare_digest(token.encode(), ADMIN_TOKEN.encode()):
            return self._send(403, {"error": "Špatné heslo."})
        action = fields.get("action")
        url = fields.get("url", "")
        if url and not url.startswith(("http://", "https://")):
            return self._send(400, {"error": "Odkaz musí začínat http:// nebo https://"})
        os.makedirs(VAR_DIR, exist_ok=True)
        if action == "gtfs":
            if url:
                save_settings(gtfs_url=url)
            if "file" in files:
                p = os.path.join(VAR_DIR, "upload-gtfs.zip")
                open(p, "wb").write(files["file"][1]); State.gtfs_upload = p
            State.jobs["gtfs"] = "naplánováno " + time.strftime("%H:%M:%S")
            State.gtfs_event.set()
            return self._send(200, {"ok": True, "message": "Aktualizace jízdního řádu (GTFS) spuštěna – trvá asi 1–2 minuty."})
        if action == "handbook":
            if url:
                save_settings(handbook_url=url)
            if "file" in files:
                p = os.path.join(VAR_DIR, "upload-pripoje.xlsx")
                open(p, "wb").write(files["file"][1]); State.handbook_upload = p
            State.jobs["handbook"] = "naplánováno " + time.strftime("%H:%M:%S")
            State.handbook_event.set()
            return self._send(200, {"ok": True, "message": "Aktualizace pomůcky (Excel) spuštěna – trvá asi 1 minutu."})
        return self._send(400, {"error": "Neznámá akce."})

    def do_POST(self):
        """Cíl sdílení z Androidu (Web Share Target, multipart/form-data): text, odkaz i soubory (.ics, .txt)."""
        u = urllib.parse.urlparse(self.path)
        if u.path == "/admin/action":
            try:
                return self._admin_action()
            except Exception as e:
                traceback.print_exc()
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        if u.path == "/api/report":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > 600_000:
                    return self._send(413, {"error": "Hlášení je příliš velké."})
                body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                rid = save_report(body, self._client()[0])
                return self._send(200, {"ok": True, "id": rid})
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            except Exception as e:
                traceback.print_exc()
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        if u.path == "/admin/reports":
            tok = self.headers.get("X-Admin-Token", "")
            if not ADMIN_TOKEN or not hmac.compare_digest(tok.encode(), ADMIN_TOKEN.encode()):
                return self._send(403, {"error": "Nesprávné heslo."})
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8") or "{}")
            mark_report_done(str(body.get("id", "")), bool(body.get("done", True)))
            return self._send(200, {"ok": True})
        if u.path != "/share":
            return self._send(404, {"error": "Nenalezeno"})
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 5_000_000)
            body = self.rfile.read(length)
            ctype = self.headers.get("Content-Type", "")
            parts = []
            if ctype.startswith("multipart/form-data"):
                msg = BytesParser(policy=policy.default).parsebytes(
                    b"Content-Type: " + ctype.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
                for part in msg.iter_parts():
                    name = part.get_param("name", header="content-disposition") or ""
                    fname = part.get_filename() or ""
                    data = part.get_payload(decode=True) or b""
                    if not data:
                        continue
                    txt = data[:300_000].decode(part.get_content_charset() or "utf-8", "replace")
                    if fname.lower().endswith(".ics") or "calendar" in part.get_content_type() or txt.lstrip().startswith("BEGIN:VCALENDAR"):
                        txt = ics_to_text(txt)
                    elif fname and not part.get_content_type().startswith("text/") and not fname.lower().endswith((".txt", ".ics")):
                        continue
                    parts.append(txt.strip())
            else:
                q = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
                parts = [q[k][0] for k in ("title", "text", "url") if q.get(k)]
            text = "\n".join(p for p in parts if p)[:20000]
            self.send_response(303)
            self.send_header("Location", "/share?" + urllib.parse.urlencode({"text": text}))
            self.send_header("Content-Length", "0")
            self.end_headers()
        except Exception as e:
            traceback.print_exc()
            return self._send(400, {"error": f"Sdílený obsah nelze přečíst: {e}"})

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                self._count(STATS.page_view, "hledani", *self._client())
                with open(os.path.join(STATIC_DIR, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if u.path == "/admin":
                with open(os.path.join(STATIC_DIR, "admin.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if u.path == "/share":
                self._count(STATS.page_view, "sdileni", *self._client())
                if q.get("text") or q.get("url"):
                    self._count(STATS.event, "shares")
                with open(os.path.join(STATIC_DIR, "share.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            static = {"/manifest.webmanifest": "application/manifest+json", "/sw.js": "text/javascript; charset=utf-8", "/report.js": "text/javascript; charset=utf-8",
                      "/icon-192.png": "image/png", "/icon-512.png": "image/png", "/icon-maskable-512.png": "image/png"}
            if u.path in static:
                with open(os.path.join(STATIC_DIR, u.path.lstrip("/")), "rb") as f:
                    return self._send(200, f.read(), static[u.path])
            if u.path == "/admin/reports":
                tok = self.headers.get("X-Admin-Token", "")
                if not ADMIN_TOKEN or not hmac.compare_digest(tok.encode(), ADMIN_TOKEN.encode()):
                    return self._send(403, {"error": "Nesprávné heslo."})
                return self._send(200, {"reports": load_reports()})
            if u.path == "/admin/stats":
                tok = self.headers.get("X-Admin-Token", "")
                if not ADMIN_TOKEN or not hmac.compare_digest(tok.encode(), ADMIN_TOKEN.encode()):
                    return self._send(403, {"error": "Nesprávné heslo."})
                return self._send(200, STATS.summary())
            if u.path == "/health":
                return self._send(200, {"status": "ok", "ready": State.engine is not None, "data": State.status,
                                        "built_at": State.built_at, "error": State.error, "jobs": State.jobs,
                                        "handbook": handbook_meta(), "admin_enabled": bool(ADMIN_TOKEN),
                                        "sources": {k: v for k, v in settings().items() if k.endswith("_url")}})
            if u.path == "/api/review":
                with open(os.path.join(data_dir(), "review.json"), "rb") as f:
                    return self._send(200, f.read())
            if not u.path.startswith("/api/"):
                return self._send(404, {"error": "Nenalezeno"})
            if State.engine is None:
                return self._send(503, {"error": "Data jízdního řádu se připravují (stahuje se GTFS), zkuste to za minutu.",
                                        "data": State.status})
            if u.path in ("/api/itinerary", "/api/connection", "/api/trains", "/api/station-table") and not self._internal():
                self._count(STATS.api_call, *self._client())
            rendered = None
            if u.path == "/api/itinerary":
                # stránku Mapy.com čteme v prohlížeči mimo zámek – trvá několik sekund a nesmí blokovat ostatní dotazy
                rendered = render_route(" ".join(x for x in (q.get("title", ""), q.get("text", ""), q.get("url", "")) if x))
            with State.lock:
                if u.path == "/api/info":
                    return self._send(200, api_info())
                if u.path == "/api/stations":
                    return self._send(200, api_stations(q.get("q", "")))
                if u.path == "/api/trains":
                    if not q.get("station"):
                        return self._send(400, {"error": "Chybí parametr station."})
                    return self._send(*api_trains(q["station"], parse_date(q.get("date"))))
                if u.path == "/api/station-table":
                    return self._send(*api_station_table(q.get("station", ""), parse_date(q.get("date"))))
                if u.path == "/api/itinerary":
                    text = " ".join(x for x in (q.get("title", ""), q.get("text", ""), q.get("url", "")) if x)
                    if not text.strip():
                        return self._send(400, {"error": "Chybí parametr text nebo url."})
                    d = parse_date(q["date"]) if q.get("date") else None
                    res = analyze(State.engine, text, d, fetch=fetch_url, resolve=fetch_raw, rendered=rendered)
                    if self._internal():
                        self._count(STATS.event, "searches", res.get("source") or "text")
                    return self._send(200, res)
                if u.path == "/api/connection":
                    miss = [k for k in ("station", "arriving", "departing") if not q.get(k)]
                    if miss:
                        return self._send(400, {"error": "Chybí parametry: " + ", ".join(miss)})
                    try:
                        arr_no, dep_no = int(q["arriving"]), int(q["departing"])
                    except ValueError:
                        return self._send(400, {"error": "Čísla vlaků musí být čísla."})
                    res = State.engine.evaluate(q["station"], parse_date(q.get("date")), arr_no, dep_no)
                    res.pop("trace", None)
                    if self._internal():
                        self._count(STATS.event, "manual")
                    return self._send(200, res)
            return self._send(404, {"error": "Neznámý endpoint"})
        except ValueError as e:
            return self._send(400, {"error": f"Chybný vstup: {e}"})
        except Exception as e:
            traceback.print_exc()
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})


def main():
    port = int(os.environ.get("PORT", "8000"))
    threading.Thread(target=gtfs_worker, daemon=True).start()
    threading.Thread(target=handbook_worker, daemon=True).start()
    import signal

    def _stop(signum, frame):
        STATS.flush()
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, _stop)
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log(f"Server běží na portu {port}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
