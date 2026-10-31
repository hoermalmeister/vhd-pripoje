"""Vyhodnocení přípoje: stanice + datum + přípojný vlak (čím přijíždím) + návazný vlak (na co přestupuji) -> čekací doba.

Spojuje data z pomůcky (data/stations/*.json, data/rules.json) s jízdním řádem z GTFS (data/trains.db).
"""
import datetime as dt
import json, os, re, sqlite3, unicodedata
from functools import lru_cache

from .network import Network

# obvody stanic, které jsou v GTFS samostatnými stanicemi (název v pomůcce -> další názvy v GTFS)
STATION_DISTRICTS = {
    "Ostrava hlavní nádraží": ["Ostrava uhelné nádraží"],   # pomůcka: „Ostrava hl. n. obvod uhelné nádraží“
    "Čáslav": ["Čáslav m.n."],                               # místní nádraží – vlaky do Třemošnice
    "Třemešná ve Slezsku": ["Třemešná ve Sl.úz.r."],         # úzkorozchodná dráha do Osoblahy
}

# linky, které pomůcka v pravidlech označuje jako Ex, ačkoli GTFS je vede jako R (jen pro shodu pravidel,
# základní čekací doba zůstává podle kategorie v jízdním řádu)
LINE_ALSO_DRUH = {
    "R15": ["Ex"],   # Praha – Ústí n.L. – Most – Chomutov – Cheb: Chomutov, Most, Kadaň, Teplice, Ústí – „všechny vlaky Ex (vyjma 619)“
    "R18": ["Ex"],   # rychlíky do Luhačovic: Hulín, Uherské Hradiště, Uherský Brod – „vlaky Ex“ (Otrokovice je vede jako R)
}

DAY_LABEL = {"workday": "pracovní den (⚒)", "saturday": "sobota (⑥)", "sunday_holiday": "neděle / svátek (†)"}


def _norm_name(s):
    s = s.lower().strip()
    s = re.sub(r"hl\.\s*n\.?", "hlavní nádraží", s)
    s = s.replace("dolní n.", "dolní nádraží").replace("mas. n.", "masarykovo nádraží").replace("u uh", "u uherského hradiště")
    return re.sub(r"\s+", " ", s)


def easter_sunday(y):
    a = y % 19; b = y // 100; c = y % 100; d = b // 4; e = b % 4; f = (b + 8) // 25; g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30; i = c // 4; k = c % 4; l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451; month = (h + l - 7 * m + 114) // 31; day = ((h + l - 7 * m + 114) % 31) + 1
    return dt.date(y, month, day)


def czech_holidays(y):
    """Státní svátky a ostatní svátky ČR v roce y (vč. Velkého pátku a Velikonočního pondělí)."""
    fixed = [(1, 1), (5, 1), (5, 8), (7, 5), (7, 6), (9, 28), (10, 28), (11, 17), (12, 24), (12, 25), (12, 26)]
    e = easter_sunday(y)
    days = {dt.date(y, m, d) for m, d in fixed} | {e - dt.timedelta(days=2), e + dt.timedelta(days=1)}
    return {d.isoformat() for d in days}


def base_number(n):
    """Šestimístná čísla odvozená od kmenového (1xxxxx, 2xxxxx, 3xxxxx) -> kmenové číslo."""
    if n >= 100000 and str(n)[0] in "123":
        return int(str(n)[1:])
    return n


def hhmm(m):
    if m is None:
        return None
    m = int(round(m))
    return f"{(m // 60) % 24:02d}:{m % 60:02d}"


class Train:
    """Vlak v konkrétní den a stanici."""

    def __init__(self, number, category, druh, operator, line, stations, times, idx, terminus, origin):
        self.number = number
        self.category = category
        self.druh = druh
        self.operator = operator
        self.line = line
        self.stations = stations          # zastavení (id) celé jízdy
        self.times = times                # [(arr, dep)] v minutách vůči půlnoci zvoleného dne
        self.idx = idx                    # index zvolené stanice
        self.terminus = terminus
        self.origin = origin
        self.onward = None                # úplná trasa od stanice dál (vč. projížděných)
        self.backward = None              # úplná trasa od stanice zpět k výchozí

    @property
    def arr(self):
        a, d = self.times[self.idx]
        return a if a is not None else d

    @property
    def dep(self):
        a, d = self.times[self.idx]
        return d if d is not None else a

    @property
    def next_stop(self):
        return self.stations[self.idx + 1] if self.idx + 1 < len(self.stations) else None

    @property
    def prev_stop(self):
        return self.stations[self.idx - 1] if self.idx > 0 else None

    def label(self, names):
        dest = names.get(self.terminus, "?")
        if self.times[-1][1] is not None and getattr(self, "headsign", None):
            dest = self.headsign            # vlak pokračuje mimo data (do zahraničí)
        return f"{self.category} {self.number} ({self.operator}) {names.get(self.origin, '?')} – {dest}"


class Engine:
    def __init__(self, data_dir, db_path=None):
        self.data_dir = data_dir
        self.rules = json.load(open(os.path.join(data_dir, "rules.json"), encoding="utf-8"))
        self.index = json.load(open(os.path.join(data_dir, "index.json"), encoding="utf-8"))
        self.holidays = set(self.rules["day_types"]["public_holidays"])
        self.db = sqlite3.connect(db_path or os.path.join(data_dir, "trains.db"), check_same_thread=False)
        self.net = Network(self.db)
        self.names = self.net.names
        by_norm = {}
        for sid, n in self.names.items():
            by_norm.setdefault(_norm_name(n), sid)
        self.station_gtfs = {}
        for st in self.index["stations"]:
            self.station_gtfs[st["station"]] = by_norm.get(_norm_name(st["station"]))
        self.gtfs_by_name = by_norm
        # typy dopraven (stanice / zastávka) z Wikipedie, párované přes kód SR70
        try:
            self.station_types = json.load(open(os.path.join(data_dir, "station_types.json"), encoding="utf-8"))["by_code"]
        except Exception:
            self.station_types = {}
        self.districts = {}
        for st_name, sid in self.station_gtfs.items():
            extra = STATION_DISTRICTS.get(st_name) or STATION_DISTRICTS.get(self.names.get(sid, ""), [])
            self.districts[sid] = [sid] + [by_norm[_norm_name(x)] for x in extra if _norm_name(x) in by_norm]

    # ------------------------------------------------------------ data pomůcky
    @lru_cache(maxsize=1024)
    def _load_station(self, file):
        return json.load(open(os.path.join(self.data_dir, file), encoding="utf-8"))

    def station_file(self, station_name, date):
        d = date.isoformat()
        for st in self.index["stations"]:
            if st["station"] == station_name:
                for f in st["files"]:
                    for p in f.get("periods", []):
                        if p["from"] <= d and (p["to"] is None or d <= p["to"]):
                            return self._load_station(f["file"]), None
                cands = [f for f in st["files"] if not f.get("periods") and (f["valid_to"] is None or d <= f["valid_to"])]
                exact = [f for f in cands if f["valid_from"] is None or f["valid_from"] <= d]
                if exact:
                    return self._load_station(exact[-1]["file"]), None
                if cands:
                    f = sorted(cands, key=lambda x: x["valid_from"])[0]
                    return self._load_station(f["file"]), f"Tabulka stanice platí od {f['valid_from']}; pro zvolené datum platila starší verze, která v podkladu není."
        return None, None

    def find_station(self, text):
        """Název zadaný uživatelem -> (název v pomůcce nebo None, id stanice v GTFS)."""
        n = _norm_name(text)
        for st in self.index["stations"]:
            if _norm_name(st["station"]) == n:
                return st["station"], self.station_gtfs[st["station"]]
        sid = self.gtfs_by_name.get(n)
        return None, sid

    def station_kind(self, sid):
        """(category, type) – category: station | stop | other | None (neznámé). Podle Wikipedie (SR70)."""
        recs = self.station_types.get(sid.split(":")[-1]) if sid else None
        if not recs:
            return None, None
        name = _norm_name(self.names.get(sid, ""))
        rec = next((r for r in recs if _norm_name(r["name"]) == name), recs[0])
        return rec["category"], rec["type"]

    def day_type(self, date):
        if date.isoformat() in self.holidays or date.isoformat() in czech_holidays(date.year) or date.weekday() == 6:
            return "sunday_holiday"
        if date.weekday() == 5:
            return "saturday"
        return "workday"

    # ------------------------------------------------------------ vlaky z GTFS
    def train_at(self, number, station_id, date):
        """Najde vlak číslo 'number' jedoucí dne 'date' přes stanici. Zohlední i vlaky z předchozího dne po půlnoci."""
        nums = {number, base_number(number)}
        for shift in (0, -1):
            day = date + dt.timedelta(days=shift)
            dint = int(day.strftime("%Y%m%d"))
            rows = self.db.execute(
                f"""select t.tid, t.number, t.category, t.druh, t.operator, t.line, t.block_id, t.part, t.headsign
                    from trains t join service_dates s on s.service_id = t.service_id
                    where t.number in ({','.join('?' * len(nums))}) and s.date = ?""", (*nums, dint)).fetchall()
            if not rows:
                continue
            blocks = {}
            for r in rows:
                blocks.setdefault(r[6], []).append(r)
            for blk, parts in blocks.items():
                parts.sort(key=lambda r: r[7])
                stations, times, meta, kmpos = [], [], [], []
                for pi, (tid, num, cat, druh, op, line, _, _, _hs) in enumerate(parts):
                    for st, arr, dep, km in self.db.execute("select station_id, arr, dep, dist_km from stops where tid=? order by seq", (tid,)):
                        a = arr + shift * 1440 if arr is not None else None
                        d = dep + shift * 1440 if dep is not None else None
                        if stations and stations[-1] == st:      # navázání částí vlaku (např. NAD)
                            times[-1] = (times[-1][0], d)
                            kmpos[-1] = (pi, km)
                            continue
                        stations.append(st); times.append((a, d)); meta.append((num, cat, druh, op, line)); kmpos.append((pi, km))
                group = self.districts.get(station_id, [station_id])
                here = next((x for x in group if x in stations), None)
                if here is None:
                    continue
                # výchozí stanice nemá příjezd, konečná nemá odjezd (GTFS u nich uvádí oba časy)
                # (u vlaků useknutých na hranici – cíl/odjezd mimo data – časy ponecháme)
                a0, d0 = times[0]
                if a0 is None or d0 is None or a0 >= d0:
                    times[0] = (None, d0)
                aN, dN = times[-1]
                hs = (parts[-1][8] or "").strip()
                if not (dN is not None and aN is not None and (dN > aN or (hs and hs != self.names.get(stations[-1])))):
                    times[-1] = (aN, None)
                seg_km = [(k2 - k1) if (p1 == p2 and k2 > k1) else None for (p1, k1), (p2, k2) in zip(kmpos, kmpos[1:])]
                i = stations.index(here)
                t_here = times[i][1] if times[i][1] is not None else times[i][0]
                if shift == -1 and t_here < 0:
                    continue
                if shift == 0 and t_here >= 1440 + 0 and False:
                    continue
                num, cat, druh, op, line = meta[i]
                tr = Train(num, cat, druh, op, line, stations, times, i, stations[-1], stations[0])
                tr.headsign = parts[-1][8]
                tr.onward = self.net.expand(stations[i:], seg_km[i:])
                tr.backward = self.net.expand(list(reversed(stations[: i + 1])), list(reversed(seg_km[:i])))
                return tr
        return None

    # ------------------------------------------------------------ selektory
    def _num_match(self, sel, n):
        b = base_number(n)
        if b in sel.get("trains", []) or n in sel.get("trains", []):
            return True
        for r in sel.get("ranges", []):
            if r["from"] <= b <= r["to"] and (r.get("parity") is None or (b % 2 == 1) == (r["parity"] == "odd")):
                return True
        for p in sel.get("patterns", []):
            if len(str(b)) == len(p) and all(c == "x" or c == d for c, d in zip(p, str(b))):
                return True
        return False

    def match_selector(self, sel, train, station, role, date, trace):
        """True / False / None (nelze rozhodnout). role = 'waiting' | 'connecting'."""
        if sel.get("none"):
            return False
        if "any_of" in sel:
            res = [self.match_selector(s, train, station, role, date, trace) for s in sel["any_of"]]
            return True if True in res else (None if None in res else False)
        for inc in sel.get("include", []):
            if self.match_selector(inc, train, station, role, date, trace) is True:
                return True
        has_numbers = any(k in sel for k in ("trains", "ranges", "patterns"))
        if has_numbers and not self._num_match(sel, train.number):
            return False
        if getattr(train, "stub", False):
            # vlak bez údajů z jízdního řádu: rozhoduje jen číslo; ostatní podmínky nelze ověřit
            filters = [k for k in ("categories", "operators", "lines", "from_directions", "to_directions", "destination_station", "relation", "tracks") if k in sel]
            if "date_condition" in sel and sel["date_condition"].get("periods"):
                md = date.strftime("%m-%d")
                if not any(p.get("from", "01-01") <= md <= p.get("to", "12-31") for p in sel["date_condition"]["periods"]):
                    return False
            unknown = bool(filters) and not has_numbers
            if not has_numbers and not sel.get("all"):
                return None
            if "parity" in sel and (base_number(train.number) % 2 == 1) != (sel["parity"] == "odd"):
                return False
            for ex in sel.get("except", []):
                r = self.match_selector(ex, train, station, role, date, trace)
                if r is True:
                    return False
                if r is None:
                    unknown = True
            return None if unknown else True
        if not has_numbers and not sel.get("all"):
            return None if sel.get("parse") in ("none", "partial") else False
        unknown = False
        if "categories" in sel and not has_numbers and train.druh not in sel["categories"] and train.category not in sel["categories"] \
                and not set(LINE_ALSO_DRUH.get(train.line or "", [])) & set(sel["categories"]):
            return False
        if "operators" in sel and not has_numbers:   # u výčtu čísel je dopravce jen poznámka, např. '6807, 6545 (RJ ÚK)'
            ops = set(sel["operators"])
            if train.operator not in ops and not (train.operator == "LET" and "LE" in ops and False):
                return False
        if "lines" in sel and (train.line or "").replace(" ", "") not in [l.replace(" ", "") for l in sel["lines"]]:
            return False
        if "parity" in sel and (base_number(train.number) % 2 == 1) != (sel["parity"] == "odd"):
            return False
        if "from_directions" in sel:
            res = [self.dir_match(station, train, t, backward=True) for t in sel["from_directions"]]
            if True not in res:
                if None in res:
                    unknown = True
                else:
                    return False
        if "to_directions" in sel:
            res = [self.dir_match(station, train, t) for t in sel["to_directions"]]
            if True not in res:
                if None in res:
                    unknown = True
                else:
                    return False
        if "destination_station" in sel:
            pid = self.net.resolve_place(sel["destination_station"], station)
            if pid != train.terminus:
                return False
        if "relation" in sel:
            ends = self.net.split_places(sel["relation"])
            ids = [self.net.resolve_place(e, station) for e in ends]
            if ids and ids[0] and ids[-1] and not ({train.origin, train.terminus} & {ids[0], ids[-1]}):
                return False
        if "tracks" in sel:
            unknown = True
            trace.append(f"Selektor '{sel['text']}' obsahuje číslo trati – zatím nelze ověřit z jízdního řádu.")
        if "date_condition" in sel:
            md = date.strftime("%m-%d")
            ok = False
            for p in sel["date_condition"].get("periods", []):
                f, t = p.get("from", "01-01"), p.get("to", "12-31")
                if f <= md <= t:
                    ok = True
            if sel["date_condition"].get("periods") and not ok:
                return False
        for ex in sel.get("except", []):
            r = self.match_selector(ex, train, station, role, date, trace)
            if r is True:
                return False
            if r is None:
                unknown = True
        if "not" in sel:
            r = self.match_selector(sel["not"], train, station, role, date, trace)
            if r is True:
                return False
            if r is None:
                unknown = True
        if sel.get("parse") in ("partial", "none"):
            unknown = True
        return None if unknown else True

    def dir_match(self, station, train, token, backward=False, via=None):
        """Jede vlak (z/do) směru 'token'? Zohlední i 'přes X' a cíl vlaku z popisu (headsign) u tras končících na hranici."""
        station = train.stations[train.idx]          # obvod stanice, kde vlak skutečně staví
        path = train.backward if backward else train.onward
        tokens = [token] + ([via] if via else [])
        results = []
        for t in tokens:
            m, pids = self.net.direction_match(station, path, t)
            results.append(m)
            if m:
                return True
            if not backward and getattr(train, "headsign", None) and pids is not None:
                from .network import name_words, name_score
                if any(name_score(tuple(name_words(p)), tuple(name_words(train.headsign))) for p in self.net.split_places(t)):
                    return True
        if all(r is None for r in results):
            return None
        return False

    @staticmethod
    def _specificity(rule):
        def s(sel):
            if "trains" in sel and not sel.get("all"):
                return 4
            if "ranges" in sel or "patterns" in sel:
                return 3
            if "any_of" in sel:
                return 2
            n = sum(1 for k in ("categories", "operators", "lines", "from_directions", "to_directions", "relation", "tracks", "destination_station") if k in sel)
            return 1 + min(n, 3) * 0.2 - (0.5 if sel.get("except_listed_below") else 0) - (0.1 if sel.get("except") else 0)
        return s(rule["waiting_train"]) * 10 + s(rule["connecting_train"])

    # ------------------------------------------------------------ hlavní funkce
    def evaluate(self, station_text, date, connecting_no, waiting_no):
        if isinstance(date, str):
            date = dt.date.fromisoformat(date)
        out = {"input": {"station": station_text, "date": date.isoformat(), "connecting_train": connecting_no, "waiting_train": waiting_no},
               "warnings": [], "trace": []}
        name, sid = self.find_station(station_text)
        if sid is None:
            out["result"] = "unknown_station"
            out["message"] = f"Stanici '{station_text}' nelze najít."
            return out
        kcat, ktype = self.station_kind(sid)
        out["station"] = {"name": name or self.names[sid], "gtfs_id": sid, "in_handbook": name is not None,
                          "kind": ktype, "kind_category": kcat}
        dtype = self.day_type(date)
        out["day_type"] = {"key": dtype, "label": DAY_LABEL[dtype]}

        conn = self.train_at(int(connecting_no), sid, date)
        wait = self.train_at(int(waiting_no), sid, date)
        st, warn = self.station_file(name, date) if name else (None, None)
        missing = [k for k, tr in (("connecting_train", conn), ("waiting_train", wait)) if tr is None]
        for key, tr, no in (("connecting_train", conn, connecting_no), ("waiting_train", wait, waiting_no)):
            if tr is None:
                out[key] = {"label": f"vlak {no}", "number": int(no), "found_in_timetable": False}
                continue
            out[key] = {"label": tr.label(self.names), "number": tr.number, "category": tr.category, "druh": tr.druh,
                        "operator": tr.operator, "line": tr.line, "arrival": hhmm(tr.times[tr.idx][0]), "departure": hhmm(tr.times[tr.idx][1]),
                        "from": self.names.get(tr.origin), "to": self.names.get(tr.terminus), "found_in_timetable": True}
        # 1) výslovný řádek tabulky pro obě čísla vlaků platí vždy – bez ohledu na jízdní řád
        if st:
            exp = self._explicit_rule(st, int(waiting_no), int(connecting_no), sid, date, out["trace"])
            if exp and exp[2] and not missing:
                exp = None      # 'V' a oba vlaky známe -> ověří se podmínky přípoje v plném vyhodnocení
            if exp:
                rule, section, plain_v = exp
                if warn:
                    out["warnings"].append(warn)
                if missing:
                    out["warnings"].append("Vlak " + " a ".join(str(out[k]["number"]) for k in missing) +
                                           f" podle jízdního řádu dne {date.isoformat()} ve stanici nestaví nebo nejede – čekací doba je podle tabulky stanice.")
                if plain_v:
                    out["warnings"].append("Přípojný vlak je v tabulce uveden jako „" + rule["connecting_train"]["text"] +
                                           "“ – platí pro vlaky, které jsou přípojem (garantovaný přestup dle článků 1 a 2: 2–60 min, týž dopravce nebo povoleno částí B).")
                w = rule["wait"][dtype]
                out["matched_rule"] = {"id": rule["id"], "section": section["title"], "waiting_train": rule["waiting_train"]["text"],
                                       "connecting_train": rule["connecting_train"]["text"], "wait_raw": w["raw"], "source_row": rule["source_row"]}
                if not missing and conn.arr is not None and wait.dep is not None:
                    out["scheduled_transfer_minutes"] = round(wait.dep - conn.arr, 1)
                if w["kind"] == "empty":
                    out["warnings"].append("V tabulce stanice není pro tento typ dne u tohoto řádku čekací doba vyplněna.")
                    return self._finish(out, "unspecified", None, "V tabulce není pro tento typ dne čekací doba uvedena.", st, rule)
                return self._finish_rule(out, w, rule, st, None)
        if missing:
            out["result"] = "train_not_found"
            out["message"] = ("Vlak " + " a ".join(str(out[k]["number"]) for k in missing) +
                              f" dne {date.isoformat()} ve stanici {out['station']['name']} podle jízdního řádu nestaví a v tabulce stanice pro tuto dvojici není výslovný řádek – bez údajů o vlaku (druh, dopravce, směr, čas) nelze obecná pravidla vyhodnotit.")
            self._context(out, st, None)
            return out
        if conn.times[conn.idx][0] is None:
            out["result"] = "not_a_connection"
            out["message"] = f"Vlak {conn.number} ve stanici výchozí – nepřijíždí, nejde o přípoj."
            return out
        if wait.times[wait.idx][1] is None:
            out["result"] = "not_a_connection"
            out["message"] = f"Vlak {wait.number} ve stanici končí – neodjíždí."
            return out
        interval = wait.dep - conn.arr
        if interval < -720:
            # přestup přes půlnoc: přípojný vlak přijíždí před půlnocí předchozího dne, nebo návazný odjíždí po půlnoci
            c2 = self.train_at(int(connecting_no), sid, date - dt.timedelta(days=1))
            if c2 and c2.times[c2.idx][0] is not None and 0 <= wait.dep - (c2.arr - 1440) <= 720:
                c2.times = [(a - 1440 if a is not None else None, d - 1440 if d is not None else None) for a, d in c2.times]
                conn = c2
                out["warnings"].append("Přípojný vlak přijíždí předchozí den před půlnocí.")
            else:
                w2 = self.train_at(int(waiting_no), sid, date + dt.timedelta(days=1))
                if w2 and w2.times[w2.idx][1] is not None and 0 <= (w2.dep + 1440) - conn.arr <= 720:
                    w2.times = [(a + 1440 if a is not None else None, d + 1440 if d is not None else None) for a, d in w2.times]
                    wait = w2
                    date = date + dt.timedelta(days=1)
                    dtype = self.day_type(date)
                    out["day_type"] = {"key": dtype, "label": DAY_LABEL[dtype]}
                    out["warnings"].append(f"Návazný vlak odjíždí po půlnoci ({date.isoformat()}).")
            interval = wait.dep - conn.arr
            for key, tr in (("connecting_train", conn), ("waiting_train", wait)):
                out[key]["arrival"] = hhmm(tr.times[tr.idx][0]); out[key]["departure"] = hhmm(tr.times[tr.idx][1])
        out["scheduled_transfer_minutes"] = round(interval, 1)

        if warn:
            out["warnings"].append(warn)
        if conn.number == wait.number or base_number(conn.number) == base_number(wait.number):
            return self._finish(out, "always", None, "Vlak stejného čísla – pokračování jednoho vlaku, přestup se zajišťuje vždy (článek 14).", st, None)
        if interval < 0:
            out["warnings"].append("Návazný vlak podle jízdního řádu odjíždí dřív, než přípojný přijede.")

        rule, section = (None, None)
        if st:
            rule, section = self._find_rule(st, conn, wait, sid, date, out["trace"])
        effect, prule, ptext = (None, None, None)
        if st:
            effect, prule, ptext = self._pair_effect(st, conn, wait, sid, date, dtype, interval, out["trace"])
        if effect:
            out["part_b_rule"] = {"effect": effect, "text": ptext, **({"note": prule["note"]} if prule.get("note") else {}), **({"approx": True} if prule.get("approx") else {})}
        table_explicit = bool(rule) and any(k in rule["connecting_train"] for k in ("trains", "ranges", "patterns", "any_of", "include"))
        if effect == "no_connection" and not table_explicit:
            return self._finish(out, "not_a_connection", None, "Nejde o přípoj – " + self._pb_message(effect, prule, ptext), st, None)
        if rule and not table_explicit and effect != "connection":
            # 'V' / obecný selektor = jen vlaky, které jsou přípojem dle článků 1 a 2 (+ část B)
            partb = [r for r, _ in self._partb(st)]
            if conn.operator != wait.operator and not (rule["connecting_train"].get("operators") and conn.operator in rule["connecting_train"]["operators"]):
                allowed, _ = self._operator_allowed(partb, wait, conn)
                if not allowed:
                    return self._finish(out, "not_a_connection", None,
                                        f"Řádek tabulky '{rule['waiting_train']['text']} ← {rule['connecting_train']['text']}' platí jen pro přípojné vlaky; vlaky různých dopravců ({conn.operator} → {wait.operator}) přípojem nejsou (článek 2f, část B to nepovoluje).", st, None)
            min_int = max([2] + [b["minutes"] for b in partb if b.get("type") == "min_interval"])
            if 0 <= interval < min_int:
                return self._finish(out, "not_a_connection", None, f"Plánovaný přestup {round(interval, 1)} min je kratší než {min_int} min – nejde o přípoj (článek 2a / část B).", st, None)
        if rule:
            w = rule["wait"][dtype]
            out["matched_rule"] = {"id": rule["id"], "section": section["title"], "waiting_train": rule["waiting_train"]["text"],
                                   "connecting_train": rule["connecting_train"]["text"], "wait_raw": w["raw"], "source_row": rule["source_row"]}
            if rule.get("_uncertain"):
                out["warnings"].append("Pravidlo bylo vybráno s nejistotou (část selektoru nelze ověřit z jízdního řádu) – ověřte v tabulce.")
            if w["kind"] == "empty":
                out["warnings"].append("V tabulce stanice není pro tento typ dne u tohoto řádku čekací doba vyplněna – použita obecná pravidla.")
                return self._basic(out, st, conn, wait, sid, interval, effect, prule, ptext)
            return self._finish_rule(out, w, rule, st, interval)
        return self._basic(out, st, conn, wait, sid, interval, effect, prule, ptext)

    def _explicit_rule(self, st, waiting_no, connecting_no, sid, date, trace):
        """Řádek tabulky, kde návazný vlak je uveden číslem a přípojný číslem nebo jako 'V' / 'V (vyjma …)' / 'V (včetně …)'.
        Vrací (rule, section, plain_v) nebo None. Nepotřebuje jízdní řád."""
        w_stub = Train(waiting_no, None, None, None, None, [sid], [(None, None)], 0, sid, sid); w_stub.stub = True
        c_stub = Train(connecting_no, None, None, None, None, [sid], [(None, None)], 0, sid, sid); c_stub.stub = True
        w_stub.onward = w_stub.backward = c_stub.onward = c_stub.backward = [sid]
        best = None
        for sec in st["part_c"]["sections"]:
            if sec.get("status") == "cancelled":
                continue
            rules = [r for r in sec["rules"] if r["status"] == "active"]
            for i, r in enumerate(rules):
                ws, cs = r["waiting_train"], r["connecting_train"]
                if not any(k in ws for k in ("trains", "ranges", "patterns")) and "any_of" not in ws:
                    continue
                if self.match_selector(ws, w_stub, sid, "waiting", date, trace) is not True:
                    continue
                c_numbered = any(k in cs for k in ("trains", "ranges", "patterns")) or (
                    "include" in cs and any(self._num_match(x, connecting_no) for x in cs["include"]))
                plain_v = (not c_numbered) and cs.get("all") and not any(
                    k in cs for k in ("categories", "operators", "lines", "from_directions", "to_directions", "destination_station", "relation", "tracks"))
                if not (c_numbered or plain_v):
                    continue
                if self.match_selector(cs, c_stub, sid, "connecting", date, trace) is not True:
                    continue
                score = (1 if c_numbered else 0, self._specificity(r), i)
                if best is None or score > best[0]:
                    best = (score, r, sec, bool(plain_v and not c_numbered))
        return (best[1], best[2], best[3]) if best else None

    def _find_rule(self, st, conn, wait, sid, date, trace):
        best = None
        for sec in st["part_c"]["sections"]:
            if sec.get("status") == "cancelled":
                continue
            sec_ok = True
            if sec.get("direction"):
                via = None
                det = sec.get("direction_detail") or ""
                mv = re.search(r"přes\s+(.+)", det)
                if mv:
                    via = mv.group(1)
                elif det and not re.search(r"opatření|PKP|ČD|dopravce", det):
                    via = det
                sec_ok = self.dir_match(sid, wait, sec["direction"], via=via)
            rules = [r for r in sec["rules"] if r["status"] == "active"]
            for i, r in enumerate(rules):
                ws_ = r["waiting_train"]
                numbered = any(k in ws_ for k in ("trains", "ranges", "patterns")) or (
                    "any_of" in ws_ and all(any(k in x for k in ("trains", "ranges", "patterns")) for x in ws_["any_of"]))
                if sec_ok is False and not numbered:
                    continue
                mw = self.match_selector(r["waiting_train"], wait, sid, "waiting", date, trace)
                if mw is False:
                    continue
                if r["waiting_train"].get("except_listed_below"):
                    later = rules[i + 1:]
                    if any(self._num_match(x["waiting_train"], wait.number) for x in later if any(k in x["waiting_train"] for k in ("trains", "ranges", "patterns"))):
                        continue
                mc = self.match_selector(r["connecting_train"], conn, sid, "connecting", date, trace)
                if mc is False:
                    continue
                uncertain = (mw is None) or (mc is None) or (sec_ok is None and not numbered)
                score = (0 if uncertain else 1, self._specificity(r), 1 if sec_ok else 0, i)
                if best is None or score > best[0]:
                    best = (score, r, sec, uncertain)
        if best:
            r = dict(best[1]); r["_uncertain"] = best[3]
            return r, best[2]
        return None, None

    # ------------------------------------------------------------ část B (pravidla)
    @staticmethod
    def _partb(st):
        out = []
        for i in (st["part_b"] if st else []):
            if i.get("status") == "cancelled":
                continue
            s = i["structured"]
            for r in (s["rules"] if s["type"] == "rule_set" else [s]):
                out.append((r, i["text"]))
        return out

    def _pair_effect(self, st, conn, wait, sid, date, dtype, interval, trace):
        """Pravidla části B typu 'je přípoj' / 'není přípoj'. Vrací (effect, rule, text) nebo (None, None, None).
        Priorita: dvojice konkrétních vlaků 'je přípoj' > 'není přípoj' > obecné 'je přípoj'."""
        found = {"num_conn": [], "no": [], "conn": []}
        for r, text in self._partb(st):
            if r.get("type") != "connection_rule":
                continue
            days = r.get("days")
            if days and dtype not in days and date.isoweekday() not in days:
                continue
            if "interval_below" in r and not interval < r["interval_below"]:
                continue
            if "min_interval" in r and interval < r["min_interval"]:
                continue
            combos = [(wait, conn)]
            if r.get("reciprocal") or r.get("unordered"):
                combos.append((conn, wait))
            hit = False
            for w_tr, c_tr in combos:
                mw = self.match_selector(r["waiting"], w_tr, sid, "waiting", date, trace)
                mc = self.match_selector(r["connecting"], c_tr, sid, "connecting", date, trace) if mw else False
                if mw is True and mc is True:
                    hit = True
                    break
            if not hit:
                continue
            numbered = "trains" in r["waiting"] and "trains" in r["connecting"]
            key = "no" if r["effect"] == "no_connection" else ("num_conn" if numbered else "conn")
            found[key].append((r, text))
        for key, eff in (("num_conn", "connection"), ("no", "no_connection"), ("conn", "connection")):
            if found[key]:
                r, text = found[key][0]
                return eff, r, text
        return None, None, None

    def _pb_message(self, effect, r, text):
        why = f"Část B: „{text}“"
        if r.get("note"):
            why += f" ({r['note']})"
        if r.get("approx"):
            why += " [čísla tratí převedena na směry – přibližné]"
        return why

    # ------------------------------------------------------------ výsledek
    def _context(self, out, st, rule):
        if not st:
            return
        ctx = {"part_a": [i["text"] for i in st["part_a"] if i["structured"]["type"] != "default_transfer_times"],
               "notes": [n["text"] for n in st["notes"] if n.get("status") != "cancelled"],
               "part_b_info": [i["text"] for i in st["part_b"] if i["structured"]["type"] == "text" or i["structured"].get("additional_text")
                               or (i["structured"]["type"] == "rule_set" and any(r.get("type") == "info" for r in i["structured"]["rules"]))]}
        if rule:
            marks = set(rule["waiting_train"].get("footnote_marks", []) + rule["connecting_train"].get("footnote_marks", []))
            for w in rule["wait"].values():
                marks |= set(w.get("footnote_marks", []))
            ctx["footnotes"] = {m: st["footnotes"][m]["text"] for m in sorted(marks) if m in st["footnotes"]}
        out["station_info"] = {k: v for k, v in ctx.items() if v}
        out["station_table"] = {"file": st["id"], "valid_from": st["validity"]["valid_from"], "valid_to": st["validity"]["valid_to"]}

    def _finish(self, out, result, minutes, message, st, rule):
        out["result"] = result
        if minutes is not None:
            out["wait_minutes"] = minutes
        out["message"] = message
        self._context(out, st, rule)
        return out

    def _finish_rule(self, out, w, rule, st, interval):
        k = w["kind"]
        dep = out["waiting_train"].get("departure")
        if k == "minutes":
            mins = w["minutes"]
            latest = f" (odjezd nejpozději {self._plus(dep, mins)})" if dep else " po pravidelném odjezdu"
            msg = f"Vlak {out['waiting_train']['number']} počká nejvýše {mins} min{latest} – odchylná čekací doba z tabulky stanice."
            if "minutes_alternative" in w:
                msg += f" Podle poznámky může platit {w['minutes_alternative']} min."
            return self._finish(out, "waits", mins, msg, st, rule)
        if k == "never":
            return self._finish(out, "no_wait", 0, "N – návazný vlak nečeká. Výjimka (článek 11): stihne se zpožděný vlak, který přijede do pravidelného odjezdu ke stejnému nástupišti.", st, rule)
        if k in ("always", "always_through_coach"):
            why = " (přecházejí vozy)" if k == "always_through_coach" else ""
            return self._finish(out, "always", None, f"Návazný vlak čeká vždy{why}.", st, rule)
        if k == "no_connection":
            return self._finish(out, "no_connection", None, "V tento typ dne vazba neexistuje (vlak nebo přípoj nejede).", st, rule)
        if k == "variants":
            return self._finish(out, "waits_variants", None, "Čekací doba závisí na poznámce pod čarou: " + ", ".join(f"{v['minutes']} min ({v['footnote_mark']})" for v in w["variants"]), st, rule)
        if k == "empty":
            return self._finish(out, "unspecified", None, "V tabulce není pro tento typ dne čekací doba uvedena.", st, rule)
        return self._finish(out, "see_text", None, f"Čekací doba: {w['raw']}", st, rule)

    @staticmethod
    def _plus(t, m):
        h, mi = map(int, t.split(":"))
        x = h * 60 + mi + m
        return f"{(x // 60) % 24:02d}:{x % 60:02d}"

    def _basic(self, out, st, conn, wait, sid, interval, effect=None, prule=None, ptext=None):
        """Žádné pravidlo v části C -> články 1 a 2, část B a základní čekací doby."""
        reasons = []
        if not st and out["station"].get("kind_category") in ("stop", "dopravna"):
            where = "na zastávkách" if out["station"]["kind_category"] == "stop" else "v dopravnách"
            return self._finish(out, "not_a_connection", None,
                                f"{out['station']['name']} je {out['station']['kind']} (ne stanice) – {where} se přípoje nezajišťují "
                                "a žádné vlaky zde nečekají (článek 1).", st, None)
        partb = [r for r, _ in self._partb(st)] if st else []
        min_int = 2
        for b in partb:
            if b.get("type") == "min_interval":
                min_int = max(min_int, b["minutes"])
        # dvojice vlaků z části B
        pair_ok = False
        if effect == "connection":
            pair_ok = True
            reasons.append(self._pb_message(effect, prule, ptext))
            min_int = prule.get("min_interval", 2)
        for b in partb:
            if b.get("type") == "connection_pairs":
                for p in b["pairs"]:
                    a = {x["number"] for x in p["trains_a"]}; bb = {x["number"] for x in p["trains_b"]}
                    nums = {base_number(conn.number), base_number(wait.number)}
                    if nums <= (a | bb) and nums & a and nums & bb:
                        pair_ok = True
                        if b.get("transfer_minutes"):
                            min_int = min(min_int, b["transfer_minutes"])
        if interval < 0 or (interval > 60 and not pair_ok):
            return self._finish(out, "not_a_connection", None, f"Mezi příjezdem a odjezdem je {round(interval)} min – mimo rozmezí 2–60 min (článek 1), nejde o přípoj.", st, None)
        if interval < min_int:
            return self._finish(out, "not_a_connection", None, f"Plánovaný přestup {round(interval, 1)} min je kratší než {min_int} min – nejde o přípoj (článek 2a / část B).", st, None)
        # různí dopravci
        if conn.operator != wait.operator and not pair_ok:
            allowed, rule_txt = self._operator_allowed(partb, wait, conn)
            if not allowed:
                return self._finish(out, "not_a_connection", None, f"Vlaky různých dopravců ({conn.operator} → {wait.operator}) nejsou přípojem (článek 2f){rule_txt}.", st, None)
            reasons.append(f"Část B: vlaky {wait.operator} jsou přípojem vlaků {conn.operator}.")
        if not pair_ok:
            # opačný směr téže trati: přípojný přijíždí od stanice, kam návazný odjíždí
            if conn.backward and wait.onward and len(conn.backward) > 1 and len(wait.onward) > 1 and conn.backward[1] == wait.onward[1]:
                return self._finish(out, "not_a_connection", None, f"Vlaky opačného směru téže trati (přípojný přijíždí od {self.names.get(conn.backward[1])}, kam návazný odjíždí) – nejde o přípoj (článek 2b).", st, None)
            if conn.next_stop and conn.next_stop == wait.next_stop:
                return self._finish(out, "not_a_connection", None, f"Vlaky stejného směru se shodným následujícím zastavením ({self.names.get(conn.next_stop)}) – nejde o přípoj (článek 2c).", st, None)
        # základní čekací doba
        minutes = self.rules["basic_wait_times"]["by_category"].get(wait.druh or "", 10)
        src = f"základní čekací doba pro druh {wait.druh or '?'}"
        for b in partb:
            if b.get("type") == "operator_basic_wait" and b["waiting_operator"] == wait.operator:
                co = b["connecting_operators"]
                if co == "others" and conn.operator != wait.operator or isinstance(co, list) and conn.operator in co:
                    minutes = b["minutes"]; src = f"část B: základní čekací doba vlaků {wait.operator}"
        if st and not any(s for s in st["part_c"]["sections"]):
            pass
        msg = f"V tabulce stanice není pro tuto dvojici zvláštní pravidlo – platí {src}: vlak {wait.number} počká nejvýše {minutes} min (odjezd nejpozději {self._plus(out['waiting_train']['departure'], minutes)})."
        if not st:
            msg = f"Stanice nemá vlastní tabulku – platí {src}: nejvýše {minutes} min."
            if out["station"].get("kind_category") is None:
                out["warnings"].append("Typ dopravny (stanice / zastávka) není znám. Je-li to zastávka, přípoje se zde nezajišťují (článek 1).")
        out["reasons"] = reasons
        return self._finish(out, "waits_basic", minutes, msg, st, None)

    def _operator_allowed(self, partb, wait, conn):
        for b in partb:
            if b.get("type") != "operator_connection":
                continue
            if b.get("waiting_lines") and (wait.line or "") not in b["waiting_lines"]:
                continue
            wo, co = b["waiting_operators"], b["connecting_operators"]
            def ok(w_op, c_op):
                if w_op not in wo:
                    return False
                if co == "all":
                    return True
                if co == "others":
                    return c_op != w_op
                return c_op in co
            if ok(wait.operator, conn.operator):
                if b.get("connecting_lines") and conn.operator == "RJ" and (conn.line or "") not in b["connecting_lines"]:
                    continue
                return True, ""
            if b.get("reciprocal") and conn.operator in wo and (wait.operator in (co if isinstance(co, list) else [])):
                if b.get("connecting_lines") and wait.operator == "RJ" and (wait.line or "") not in b["connecting_lines"]:
                    continue
                return True, ""
        return False, ""
