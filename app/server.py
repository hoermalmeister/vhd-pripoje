"""Webová služba a API pro čekací doby přípojů vlaků v ČR.

Bez externích závislostí (jen standardní knihovna Pythonu).

Proměnné prostředí:
  PORT            port HTTP serveru (Railway nastavuje sám), výchozí 8000
  GTFS_URL        odkud stahovat GTFS, výchozí https://motis.obehy.cz/get-feeds/cz-czptt-gtfs.zip
  GTFS_PATH       místo stahování lze použít lokální zip / adresář GTFS (např. pro vývoj)
  TRAINS_DB       kam uložit databázi vlaků, výchozí ./var/trains.db
  REFRESH_HOURS   jak často GTFS znovu stáhnout a přepočítat, výchozí 24

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
"""
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from engine.evaluate import Engine, _norm_name, hhmm  # noqa: E402
from engine.itinerary import analyze  # noqa: E402
import build_gtfs_db  # noqa: E402

DATA_DIR = os.path.join(ROOT, "data")
STATIC_DIR = os.path.join(ROOT, "app", "static")
GTFS_URL = os.environ.get("GTFS_URL", "https://motis.obehy.cz/get-feeds/cz-czptt-gtfs.zip")
GTFS_PATH = os.environ.get("GTFS_PATH")
TRAINS_DB = os.environ.get("TRAINS_DB", os.path.join(ROOT, "var", "trains.db"))
REFRESH_HOURS = float(os.environ.get("REFRESH_HOURS", "24"))


class State:
    engine = None
    lock = threading.Lock()
    status = "starting"
    built_at = None
    feed_version = None
    error = None


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- data
def build_database():
    """Stáhne GTFS a postaví databázi vlaků do dočasného souboru, pak ji atomicky vymění."""
    os.makedirs(os.path.dirname(TRAINS_DB), exist_ok=True)
    tmpdir = tempfile.mkdtemp(prefix="gtfs-")
    try:
        if GTFS_PATH:
            src = GTFS_PATH
            log("GTFS z lokální cesty", src)
        else:
            src = os.path.join(tmpdir, "gtfs.zip")
            log("Stahuji GTFS", GTFS_URL)
            req = urllib.request.Request(GTFS_URL, headers={"User-Agent": "vhd-pripoje/1.0"})
            with urllib.request.urlopen(req, timeout=300) as r, open(src, "wb") as f:
                shutil.copyfileobj(r, f)
            log("Staženo", os.path.getsize(src) // 1024, "kB")
        new_db = TRAINS_DB + ".new"
        t0 = time.time()
        build_gtfs_db.build(src, new_db)
        log(f"Databáze vlaků hotová za {time.time() - t0:.0f} s")
        os.replace(new_db, TRAINS_DB)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def load_engine():
    eng = Engine(DATA_DIR, TRAINS_DB)
    try:
        fv = None
        if not GTFS_PATH:
            fv = "obehy.cz " + time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(TRAINS_DB)))
        State.feed_version = fv
    except Exception:
        pass
    with State.lock:
        State.engine = eng
        State.status = "ready"
        State.built_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(TRAINS_DB)))
    log("Engine připraven")


def data_worker():
    while True:
        try:
            fresh = os.path.exists(TRAINS_DB) and (time.time() - os.path.getmtime(TRAINS_DB)) < REFRESH_HOURS * 3600
            if State.engine is None and fresh:
                load_engine()
            else:
                if State.engine is None:
                    State.status = "building"
                build_database()
                load_engine()
            State.error = None
        except Exception as e:
            State.error = f"{type(e).__name__}: {e}"
            log("Chyba při přípravě dat:", State.error)
            traceback.print_exc()
            if State.engine is None and os.path.exists(TRAINS_DB):
                try:
                    load_engine()   # použij starší databázi, pokud existuje
                except Exception:
                    pass
            time.sleep(600)
            continue
        time.sleep(REFRESH_HOURS * 3600)


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


def api_info():
    eng = State.engine
    return {"handbook": eng.rules["document"], "stations_in_handbook": len(eng.index["stations"]),
            "timetable_db_built_at": State.built_at, "gtfs_source": GTFS_PATH or GTFS_URL}


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

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                with open(os.path.join(STATIC_DIR, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if u.path == "/share":
                with open(os.path.join(STATIC_DIR, "share.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            static = {"/manifest.webmanifest": "application/manifest+json", "/sw.js": "text/javascript; charset=utf-8",
                      "/icon-192.png": "image/png", "/icon-512.png": "image/png", "/icon-maskable-512.png": "image/png"}
            if u.path in static:
                with open(os.path.join(STATIC_DIR, u.path.lstrip("/")), "rb") as f:
                    return self._send(200, f.read(), static[u.path])
            if u.path == "/health":
                return self._send(200, {"status": "ok", "ready": State.engine is not None, "data": State.status,
                                        "built_at": State.built_at, "error": State.error})
            if not u.path.startswith("/api/"):
                return self._send(404, {"error": "Nenalezeno"})
            if State.engine is None:
                return self._send(503, {"error": "Data jízdního řádu se připravují (stahuje se GTFS), zkuste to za minutu.",
                                        "data": State.status})
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
                    return self._send(200, analyze(State.engine, text, d, fetch=fetch_url))
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
                    return self._send(200, res)
            return self._send(404, {"error": "Neznámý endpoint"})
        except ValueError as e:
            return self._send(400, {"error": f"Chybný vstup: {e}"})
        except Exception as e:
            traceback.print_exc()
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})


def main():
    port = int(os.environ.get("PORT", "8000"))
    threading.Thread(target=data_worker, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log(f"Server běží na portu {port}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
