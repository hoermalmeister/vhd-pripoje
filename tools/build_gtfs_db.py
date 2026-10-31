"""Z GTFS (CIS JŘ / obehy.cz) postaví kompaktní SQLite databázi pro vyhodnocení přípojů.

Použití:  python tools/build_gtfs_db.py <adresář_nebo_zip_GTFS> data/trains.db

Tabulky:
  stations(id, name, lat, lon)                 – železniční stanice (parent stop)
  trains(tid, trip_id, number, category, druh, operator, agency_id, line, block_id, part, service_id, headsign)
  stops(tid, seq, station_id, arr, dep, dist_km, pickup, dropoff)   – časy v minutách od půlnoci dne jízdy
  service_dates(service_id, date)              – YYYYMMDD
  edges(a, b, km)                              – úseky sítě mezi sousedními stanicemi (bez zkratek rychlíků)
"""
import csv, io, os, re, sqlite3, sys, zipfile, heapq
from collections import defaultdict

# druh vlaku dle pomůcky (Ex / R / Sp / Os) podle kategorie v JŘ
CATEGORY_TO_DRUH = {
    "Ex": "Ex", "EC": "Ex", "IC": "Ex", "EN": "Ex", "SC": "Ex", "rj": "Ex", "RJ": "Ex", "LE": "Ex", "ES": "Ex", "NJ": "Ex", "RJX": "Ex",
    "R": "R", "TLX": "R",
    "Sp": "Sp",
    "Os": "Os", "TL": "Os",
}
# agency_id (číslo dopravce v CIS JŘ) -> zkratka používaná v pomůcce
AGENCY_TO_OPERATOR = {
    "1154": "ČD", "3189": "ARR", "3559": "AŽD", "3736": "DLB", "3230": "DLB", "3288": "GWTR", "3332": "KŽC",
    "3616": "LE", "3725": "LET", "3366": "MBMR", "1181": "ÖBB", "5135": "OÚD", "3642": "RCAS", "3246": "RJ",
    "3990": "RJ", "3949": "RJ", "3752": "RJÚK", "3644": "SZD", "3825": "GE", "2197": "ČZD", "3807": "VD",
    "3666": "KDS", "3641": "Lokálka", "3265": "RS", "3407": "CityRail", "5746": "SUD", "1156": "ZSSK",
    "1251": "PKP IC", "2480": "DB", "2580": "DB", "3164": "agilis", "3252": "KD", "3963": "KŚ",
}


def _open(src, name):
    if os.path.isdir(src):
        cands = [f for f in os.listdir(src) if f == f"{name}.txt" or f.endswith(f"-{name}.txt")]
        return open(os.path.join(src, cands[0]), encoding="utf-8-sig", newline="")
    z = zipfile.ZipFile(src)
    return io.TextIOWrapper(z.open(f"{name}.txt"), encoding="utf-8-sig", newline="")


def _rows(src, name):
    with _open(src, name) as f:
        yield from csv.DictReader(f)


def _mins(t):
    if not t:
        return None
    h, m, s = (int(x) for x in t.split(":"))
    return h * 60 + m + s / 60.0


def build(src, out):
    if os.path.exists(out):
        os.remove(out)
    db = sqlite3.connect(out)
    db.executescript("""
    create table stations(id text primary key, name text, lat real, lon real);
    create table trains(tid integer primary key, trip_id text, number int, category text, druh text, operator text, agency_id text,
                        line text, block_id text, part int, service_id int, headsign text, bus int);
    create table stops(tid int, seq int, station_id text, arr real, dep real, dist_km real, pickup int, dropoff int);
    create table service_dates(service_id int, date int);
    create table edges(a text, b text, km real);
    """)
    parent = {}
    for r in _rows(src, "stops"):
        if r["location_type"] == "1":
            db.execute("insert into stations values(?,?,?,?)", (r["stop_id"], r["stop_name"], float(r["stop_lat"] or 0), float(r["stop_lon"] or 0)))
            parent[r["stop_id"]] = r["stop_id"]
        else:
            parent[r["stop_id"]] = r["parent_station"] or r["stop_id"]
    routes = {}
    for r in _rows(src, "routes"):
        line = None
        if ":line:" in r["route_id"]:
            line = re.sub(r"\s*\(NAD\)", "", r["route_short_name"]).strip() or None
        routes[r["route_id"]] = (r["agency_id"].split(":")[-1], line, r["route_type"])
    svc = {}
    tid_of = {}
    for r in _rows(src, "trips"):
        m = re.match(r"^(\S+)\s+(\d+)", r["trip_short_name"])
        if not m:
            continue
        cat, num = m.group(1), int(m.group(2))
        ag, line, rtype = routes[r["route_id"]]
        part = int(r["trip_id"].rsplit(":", 1)[-1]) if r["trip_id"].rsplit(":", 1)[-1].isdigit() else 1
        sid = svc.setdefault(r["service_id"], len(svc) + 1)
        tid_of[r["trip_id"]] = len(tid_of) + 1
        db.execute("insert into trains values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (tid_of[r["trip_id"]], r["trip_id"], num, cat, CATEGORY_TO_DRUH.get(cat), AGENCY_TO_OPERATOR.get(ag, ag), ag, line,
                    r["block_id"] or r["trip_id"], part, sid, r["trip_headsign"], 1 if rtype in ("714", "700", "3") else 0))
    batch = []
    for r in _rows(src, "stop_times"):
        if r["trip_id"] not in tid_of:
            continue
        batch.append((tid_of[r["trip_id"]], int(r["stop_sequence"]), parent.get(r["stop_id"], r["stop_id"]), _mins(r["arrival_time"]),
                      _mins(r["departure_time"]), float(r["shape_dist_traveled"] or 0) / 1000.0, int(r["pickup_type"] or 0), int(r["drop_off_type"] or 0)))
        if len(batch) > 50000:
            db.executemany("insert into stops values(?,?,?,?,?,?,?,?)", batch); batch = []
    db.executemany("insert into stops values(?,?,?,?,?,?,?,?)", batch)
    batch = []
    for r in _rows(src, "calendar_dates"):
        if r["service_id"] in svc and r["exception_type"] == "1":
            batch.append((svc[r["service_id"]], int(r["date"])))
    db.executemany("insert into service_dates values(?,?)", batch)
    # calendar (pravidelné dny v týdnu) – v tomto feedu jsou všechny dny v calendar.txt 0, ale pro jistotu:
    import datetime
    for r in _rows(src, "calendar"):
        if r["service_id"] not in svc:
            continue
        days = [r[d] == "1" for d in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")]
        if not any(days):
            continue
        d = datetime.date(int(r["start_date"][:4]), int(r["start_date"][4:6]), int(r["start_date"][6:]))
        e = datetime.date(int(r["end_date"][:4]), int(r["end_date"][4:6]), int(r["end_date"][6:]))
        while d <= e:
            if days[d.weekday()]:
                db.execute("insert into service_dates values(?,?)", (svc[r["service_id"]], int(d.strftime("%Y%m%d"))))
            d += datetime.timedelta(days=1)
    db.execute("create index i_tr_num on trains(number)")
    db.execute("create index i_st_trip on stops(tid, seq)")
    db.execute("create index i_st_station on stops(station_id)")
    db.execute("create index i_sd on service_dates(service_id, date)")
    db.commit()
    build_edges(db)
    db.execute("vacuum")
    db.close()


def build_edges(db):
    """Síť: hrany mezi po sobě jdoucími zastaveními; odstraní 'zkratky' (rychlík přes stanice bez zastavení),
    pokud existuje cesta přes další stanice s prakticky stejnou délkou."""
    seg = defaultdict(list)
    prev = None
    import math
    pos = {sid: (la, lo) for sid, la, lo in db.execute("select id, lat, lon from stations")}
    def hav(a, b):
        (la1, lo1), (la2, lo2) = pos.get(a, (0, 0)), pos.get(b, (0, 0))
        p = math.pi / 180
        x = math.sin((la2 - la1) * p / 2) ** 2 + math.cos(la1 * p) * math.cos(la2 * p) * math.sin((lo2 - lo1) * p / 2) ** 2
        return 12742 * math.asin(math.sqrt(x))
    for trip, seq, st, dist in db.execute("select s.tid, s.seq, s.station_id, s.dist_km from stops s join trains t on t.tid=s.tid where t.bus=0 order by s.tid, s.seq"):
        if prev and prev[0] == trip and prev[1] != st:
            d = dist - prev[2]
            if d > 0 and d >= 0.9 * hav(prev[1], st):
                a, b = sorted((prev[1], st))
                seg[(a, b)].append(d)
        prev = (trip, st, dist)
    edges = {}
    for k, v in seg.items():
        v.sort()
        edges[k] = v[len(v) // 2]
    adj = defaultdict(dict)
    for (a, b), d in edges.items():
        adj[a][b] = d; adj[b][a] = d

    def alt_path_len(a, b, limit):
        dist = {a: 0.0}
        pq = [(0.0, a, 0)]
        while pq:
            d, u, hops = heapq.heappop(pq)
            if d > limit:
                return None
            if u == b and hops >= 2:
                return d
            if d > dist.get(u, 1e18):
                continue
            for v, w in adj[u].items():
                if u == a and v == b:
                    continue
                nd = d + w
                if nd < dist.get(v, 1e18) and nd <= limit:
                    dist[v] = nd
                    heapq.heappush(pq, (nd, v, hops + 1))
        return None

    keep = []
    for (a, b), d in sorted(edges.items(), key=lambda kv: -kv[1]):
        alt = alt_path_len(a, b, d * 1.06 + 1.0)
        if alt is None:
            keep.append((a, b, d))
        else:
            adj[a].pop(b, None); adj[b].pop(a, None)
    db.executemany("insert into edges values(?,?,?)", keep)
    db.execute("create index i_e_a on edges(a)")
    db.commit()
    print(f"úseků sítě: {len(keep)} (odstraněno zkratek: {len(edges) - len(keep)})")


if __name__ == "__main__":
    build(sys.argv[1], sys.argv[2])
