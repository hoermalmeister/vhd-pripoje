"""Dohledání spojení podle jízdního řádu (GTFS) – pro sdílené odkazy z Mapy.com a Google Maps.

Tyto aplikace při sdílení pošlou jen odkaz na trasu (start, cíl, případně čas); čísla vlaků v něm nejsou
a stránka se skládá až v prohlížeči. Z odkazu se proto vezmou souřadnice a čas a spojení se vyhledá
v našem jízdním řádu (Connection Scan Algorithm). Uživatel pak vybere, kterým spojením jede.
"""
import datetime as dt
import json
import math
import re
import urllib.parse

MAPY_ALPHABET = "0ABCD2EFGH4IJKLMN6OPQRST8UVWXYZ-1abcd3efgh5ijklmn7opqrst9uvwxyz."
MIN_CHANGE = 2          # minimální přestupní doba v téže stanici (min)
WALK_KMH = 4.5
WALK_DETOUR = 1.3


# ---------------------------------------------------------------- odkazy

def _mapy_num(s, n):
    r = 0
    for ch in s[:n]:
        r = (r << 6) + MAPY_ALPHABET.index(ch)
    return r


def mapy_decode_coords(s):
    """Parametr rc z adresy Mapy.com/Mapy.cz -> [(lon, lat), …]."""
    out, cur, k, i = [], [0, 0], 0, 0
    s = "".join(ch for ch in s if ch in MAPY_ALPHABET)
    while i < len(s):
        code = MAPY_ALPHABET.index(s[i])
        if code >= 32:                                   # absolutní hodnota (28 bitů)
            cur[k] = ((code & 15) << 24) + _mapy_num(s[i + 1:], 4)
            i += 5
        elif code >= 16:                                 # relativní, delší
            d = ((code & 7) << 12) + _mapy_num(s[i + 1:], 2)
            cur[k] += -d if code & 8 else d
            i += 3
        else:                                            # relativní, krátká
            d = ((code & 7) << 6) + _mapy_num(s[i + 1:], 1)
            cur[k] += -d if code & 8 else d
            i += 2
        k ^= 1
        if k == 0:
            out.append((cur[0] * 360 / 2 ** 28 - 180, cur[1] * 180 / 2 ** 28 - 90))
    return out


def _parse_when(v):
    """Čas z odkazu: ISO („2026-11-03T18:30“), unixové sekundy/milisekundy -> naivní místní datetime."""
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)) or re.fullmatch(r"\d{9,13}", str(v)):
        x = int(v)
        x = x / 1000 if x > 10 ** 11 else x
        return dt.datetime.utcfromtimestamp(x)
    m = re.match(r"(20\d\d)-(\d\d)-(\d\d)[T ](\d\d):(\d\d)", str(v))
    if m:
        return dt.datetime(*map(int, m.groups()))
    return None


def parse_route_url(url):
    """Rozbor dlouhé adresy trasy. Vrací {"source", "points": [{"lat","lon","name"}], "when", "arrive_by"} nebo None."""
    u = urllib.parse.urlsplit(url)
    host = u.netloc.lower()
    q = urllib.parse.parse_qs(u.query, keep_blank_values=True)
    if "mapy." in host:
        rc = (q.get("rc") or [""])[0]
        if not rc:
            return None
        pts = [{"lon": lo, "lat": la} for lo, la in mapy_decode_coords(rc)]
        when, arrive_by = None, False
        try:
            mrp = json.loads((q.get("mrp") or ["{}"])[0])
            when = _parse_when(mrp.get("dt"))
            arrive_by = mrp.get("d") is False
        except (ValueError, AttributeError):
            pass
        return {"source": "mapy", "points": pts, "when": when, "arrive_by": arrive_by} if len(pts) >= 2 else None
    if "google." in host and "/maps/dir/" in u.path:
        path = urllib.parse.unquote_plus(u.path.split("/maps/dir/", 1)[1])
        parts = path.split("/")
        names = [p for p in parts if p and not p.startswith("@") and not p.startswith("data=") and p != "am=t"]
        data = path[path.find("data="):] if "data=" in path else ""
        coords = re.findall(r"!1d(-?\d+\.\d+)!2d(-?\d+\.\d+)", data)
        pts = []
        for i, nm in enumerate(names):
            p = {"name": nm}
            m = re.fullmatch(r"(-?\d+\.\d+),\s*(-?\d+\.\d+)", nm)
            if m:
                p.update(lat=float(m.group(1)), lon=float(m.group(2)))
            pts.append(p)
        # souřadnice v data= patří postupně bodům, které je mají (bod bez souřadnic = např. „Moje poloha“)
        if coords and len(coords) <= len(pts):
            j = len(pts) - len(coords) if len(coords) < len(pts) else 0
            for (lon, lat), p in zip(coords, pts[j:]):
                p.setdefault("lat", float(lat))
                p.setdefault("lon", float(lon))
        elif coords and not pts:
            pts = [{"lon": float(a), "lat": float(b)} for a, b in coords]
        m = re.search(r"!6e(\d)", data)
        mode = int(m.group(1)) if m else 0
        m = re.search(r"!8j(\d{9,11})", data)
        when = dt.datetime.utcfromtimestamp(int(m.group(1))) if m else None   # Google ukládá místní čas jako „UTC“
        return {"source": "google", "points": pts, "when": when, "arrive_by": mode == 1} if len(pts) >= 2 else None
    return None


def resolve_short_link(url, opener):
    """Rozbalí krátký odkaz (mapy.com/s/…, maps.app.goo.gl/…). opener(url) -> (status, location, body)."""
    seen = 0
    while seen < 8:
        seen += 1
        if parse_route_url(url):
            return url
        u = urllib.parse.urlsplit(url)
        if "consent." in u.netloc:                                  # souhlas s cookies – cíl je v parametru continue
            cont = urllib.parse.parse_qs(u.query).get("continue")
            if cont:
                url = cont[0]
                continue
        status, location, body = opener(url)
        if location:
            url = urllib.parse.urljoin(url, location)
            continue
        for pat in (r"https://www\.google\.[a-z.]+/maps/dir/[^\"'\s<>\\]+", r"https://mapy\.(?:com|cz)/[^\"'\s<>\\]*rc=[^\"'\s<>\\]+"):
            m = re.search(pat, (body or "").replace("\\u0026", "&").replace("&amp;", "&"))
            if m and parse_route_url(m.group(0)):
                return m.group(0)
        return None
    return None


# ---------------------------------------------------------------- vyhledání spojení

def _dist_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def nearby_stations(db, point, net=None):
    """Stanice v docházkové vzdálenosti od bodu: {station_id: minuty chůze}. Bez souřadnic se hledá podle názvu."""
    if point.get("lat") is not None:
        lat, lon = point["lat"], point["lon"]
        for radius in (2.5, 6.0, 12.0):
            dlat, dlon = radius / 111.0, radius / (111.0 * math.cos(math.radians(lat)))
            rows = db.execute("select id, lat, lon from stations where lat between ? and ? and lon between ? and ?",
                              (lat - dlat, lat + dlat, lon - dlon, lon + dlon)).fetchall()
            res = {}
            for sid, la, lo in rows:
                d = _dist_km(lat, lon, la, lo)
                if d <= radius:
                    res[sid] = int(round(d * WALK_DETOUR / WALK_KMH * 60))
            if res:
                return res
    if point.get("name") and net is not None:
        ids = net.resolve_all(point["name"]) if hasattr(net, "resolve_all") else []
        return {sid: 0 for sid in ids or []}
    return {}


def _load_connections(db, date):
    """Elementární spojení (odjezd, příjezd, odkud, kam, tid) pro den 'date' včetně vlaků z předchozího dne po půlnoci."""
    conns = []
    for shift in (0, -1, 1):
        d = int((date + dt.timedelta(days=shift)).strftime("%Y%m%d"))
        rows = db.execute(
            """select p.tid, p.station_id, p.arr, p.dep, p.pickup, p.dropoff from stops p
               where p.tid in (select t.tid from trains t join service_dates s on s.service_id=t.service_id where s.date=?)
               order by p.tid, p.seq""", (d,)).fetchall()
        prev = None
        for tid, st, arr, dep, pu, do in rows:
            if prev and prev[0] == tid and prev[3] is not None and arr is not None:
                dep0 = prev[3] + shift * 1440
                arr1 = arr + shift * 1440
                if (shift == 0 or arr1 >= 0) and dep0 < 36 * 60:
                    conns.append((dep0, arr1, prev[1], st, tid, prev[4] != 1, do != 1))
            prev = (tid, st, arr, dep, pu)
    conns.sort()
    return conns


def _scan(conns, origins, dests, t0, groups, max_legs=5):
    """Hledání od času t0 s omezeným počtem úseků (CSA po kolech). Vrací {počet_úseků: [(tid, z, odjezd, do, příjezd)]}
    – pro každý počet úseků nejdřívější příjezd (jen když je dřívější než s menším počtem přestupů)."""
    INF = 10 ** 9
    ready = [dict() for _ in range(max_legs + 1)]      # ready[k][stanice] = od kdy lze nastoupit po k úsecích
    arrival = [dict() for _ in range(max_legs + 1)]
    via = [dict() for _ in range(max_legs + 1)]        # via[k][stanice] = (tid, nástup, odjezd, příjezd)
    for sid, w in origins.items():
        for g in groups.get(sid, [sid]):
            ready[0][g] = min(ready[0].get(g, INF), t0 + w)
    boarded = {}                # tid -> {k: (nástupní stanice, odjezd)} – k = počet úseků před nástupem
    best = [INF] * (max_legs + 1)
    best_st = [None] * (max_legs + 1)
    import bisect
    i = bisect.bisect_left(conns, (t0,))
    for c in conns[i:]:
        dep, arr, a, b, tid, can_board, can_alight = c
        if dep >= best[max_legs]:
            break
        bt = boarded.get(tid)
        if can_board:
            for k in range(max_legs):
                if ready[k].get(a, INF) <= dep:
                    if bt is None:
                        bt = boarded[tid] = {}
                    if k not in bt and not any(kk < k for kk in bt):
                        bt[k] = (a, dep)
                    break
        if not bt or not can_alight:
            continue
        for k, (frm, d0) in bt.items():
            kk = k + 1
            if arr >= arrival[kk].get(b, INF) or arr >= best[kk]:
                continue
            arrival[kk][b] = arr
            via[kk][b] = (tid, frm, d0, arr)
            for g in groups.get(b, [b]):
                r = arr + MIN_CHANGE
                for k2 in range(kk, max_legs + 1):
                    if r < ready[k2].get(g, INF):
                        ready[k2][g] = r
            if b in dests:
                for k2 in range(kk, max_legs + 1):
                    if arr + dests[b] < best[k2]:
                        best[k2], best_st[k2] = arr + dests[b], (b, kk)
    res, last = {}, INF
    for k in range(1, max_legs + 1):
        if best_st[k] is None or best[k] >= last:
            continue
        last = best[k]
        st, kk = best_st[k]
        legs, ok = [], True
        while kk > 0:
            v = via[kk].get(st)
            if v is None:
                alt = [g for g in groups.get(st, [st]) if g in via[kk]]
                if not alt:
                    ok = False
                    break
                st = alt[0]
                v = via[kk][st]
            tid, frm, d0, a_ = v
            legs.append((tid, frm, d0, st, a_))
            st, kk = frm, kk - 1
            if kk > 0 and st not in via[kk]:
                # nástup mohl být umožněn příjezdem s menším počtem úseků
                while kk > 0 and st not in via[kk] and not any(g in via[kk] for g in groups.get(st, [st])):
                    kk -= 1
        if ok and legs:
            legs.reverse()
            res[len(legs)] = legs
    return res


def plan(engine, origin_pt, dest_pt, when, arrive_by=False, count=5):
    """Několik spojení vlakem od času 'when' (naivní místní datetime). Vrací (spojení, chyba)."""
    db = engine.db
    origins = nearby_stations(db, origin_pt, engine.net)
    dests = nearby_stations(db, dest_pt, engine.net)
    if not origins:
        return [], "U startu trasy jsem nenašel žádnou železniční stanici."
    if not dests:
        return [], "U cíle trasy jsem nenašel žádnou železniční stanici."
    if set(origins) & set(dests):
        return [], "Start i cíl trasy jsou u stejné stanice."
    date = when.date()
    conns = _load_connections(db, date)
    groups = {}
    for main, grp in getattr(engine, "districts", {}).items():
        for g in grp:
            groups[g] = grp
    t = when.hour * 60 + when.minute
    t0 = t - 240 if arrive_by else t
    found, seen = [], set()
    for _ in range(count * 3 if arrive_by else count + 3):
        res = _scan(conns, origins, dests, t0, groups)
        if not res:
            break
        for legs in res.values():
            key = tuple(l[0] for l in legs)
            if key not in seen:
                seen.add(key)
                found.append(legs)
        t0 = min(l[0][2] - origins.get(l[0][1], 0) for l in res.values()) + 1
    # vyřaď spojení, která jsou horší ve všem (později příjezd, dřív odjezd, víc přestupů) než jiné
    def dominated(x, y):
        return (y[0][2] >= x[0][2] and y[-1][4] <= x[-1][4] and len(y) <= len(x)
                and (y[0][2], -y[-1][4], -len(y)) != (x[0][2], -x[-1][4], -len(x)))
    found = [x for x in found if not any(dominated(x, y) for y in found if y is not x)]
    found.sort(key=lambda j: (j[0][2], j[-1][4]))
    if arrive_by:
        found = [j for j in found if j[-1][4] <= t][-count:]
    else:
        found = found[:count]
    out = []
    for legs in found:
        jl = []
        for tid, frm, dep, to, arr in legs:
            number, category, bus = db.execute("select number, category, bus from trains where tid=?", (tid,)).fetchone()
            if jl and jl[-1]["number"] == number:          # týž vlak pokračuje (jiná část v GTFS) – není to přestup
                jl[-1].update(to=engine.names.get(to, to), arr=_hhmm(arr))
                continue
            jl.append({"number": number, "category": category, "bus": bool(bus),
                       "from": engine.names.get(frm, frm), "to": engine.names.get(to, to),
                       "dep": _hhmm(dep), "arr": _hhmm(arr)})
        out.append({"dep": jl[0]["dep"], "arr": jl[-1]["arr"], "legs": jl,
                    "date": (date + dt.timedelta(days=legs[0][2] // 1440)).isoformat()})
    return out, None if out else "Mezi startem a cílem jsem v jízdním řádu nenašel spojení vlakem."


def _hhmm(m):
    m = int(round(m)) % 1440
    return f"{m // 60}:{m % 60:02d}"
