"""Rozbor sdíleného spojení (text nebo odkaz z vyhledávače) -> přestupy a jejich vyhodnocení.

Nezávisí na formátu konkrétního vyhledávače: z textu se vezmou čísla vlaků v pořadí
(„Os 2803“, „R 881“, „EC 113“…) a datum; stanice přestupu mezi dvěma po sobě jdoucími vlaky
se dopočítá z jízdního řádu (stanice, kde oba staví a první přijede před odjezdem druhého).
"""
import datetime as dt
import html
import re

from .evaluate import base_number, _norm_name

CATS = r"(?:Os|Sp|R|Rx|Ex|EC|IC|EN|SC|rj|RJ|RJX|LE|TL|TLX|ES|NJ|IR|EuroCity|InterCity|Railjet|railjet)"
TRAIN_RE = re.compile(r"(?<![\w/])(" + CATS + r")\s+(\d{1,6})(?![\d:.,]\d)|(?<![\w/])(" + CATS + r")(\d{3,6})(?!\d)")
DATE_RES = [
    re.compile(r"(?<!\d)(\d{1,2})\.\s?(\d{1,2})\.\s?(20\d{2})(?!\d)"),
    re.compile(r"(?<!\d)(20\d{2})-(\d{2})-(\d{2})(?!\d)"),
    re.compile(r"(?<![\d.])(\d{1,2})\.\s?(\d{1,2})\.(?!\s?\d)"),
]
URL_RE = re.compile(r"https?://\S+")


def prague_today():
    """Dnešní datum v Praze (bez závislosti na systémové databázi časových pásem)."""
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("Europe/Prague")).date()
    except Exception:
        now = dt.datetime.utcnow()
        y = now.year
        last_sun = lambda m: max(d for d in (dt.date(y, m, x) for x in range(25, 32)) if d.weekday() == 6)
        summer = dt.datetime.combine(last_sun(3), dt.time(1)) <= now < dt.datetime.combine(last_sun(10), dt.time(1))
        return (now + dt.timedelta(hours=2 if summer else 1)).date()


def parse_trains(text):
    out = []
    for m in TRAIN_RE.finditer(text or ""):
        cat = m.group(1) or m.group(3)
        num = int(m.group(2) or m.group(4))
        if num == 0:
            continue
        if out and out[-1][1] == num:
            continue
        out.append((cat, num))
    # stejný vlak se ve sdíleném textu může objevit vícekrát (souhrn + detail) – ponech první výskyt každého úseku
    seen, uniq = set(), []
    for cat, num in out:
        if num in seen:
            continue
        seen.add(num)
        uniq.append((cat, num))
    return uniq


def parse_date(text, today=None):
    today = today or prague_today()
    t = text or ""
    low = t.lower()
    m = DATE_RES[0].search(t)
    if m:
        return dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1))), True
    m = DATE_RES[1].search(t)
    if m:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))), True
    m = DATE_RES[2].search(t)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        if 1 <= d <= 31 and 1 <= mo <= 12:
            cand = dt.date(today.year, mo, d)
            if cand < today - dt.timedelta(days=60):
                cand = dt.date(today.year + 1, mo, d)
            return cand, True
    if "zítra" in low or "zitra" in low:
        return today + dt.timedelta(days=1), True
    return today, False


def html_to_text(s):
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s))


def train_run(engine, number, date):
    """Všechna zastavení vlaku v daný den: {station_id: (arr, dep)} v minutách vůči půlnoci 'date'."""
    res = {}
    for shift in (0, -1):
        d = int((date + dt.timedelta(days=shift)).strftime("%Y%m%d"))
        rows = engine.db.execute(
            """select p.station_id, p.arr, p.dep, p.seq, t.tid,
                      (select min(seq) from stops where tid=t.tid), (select max(seq) from stops where tid=t.tid)
               from trains t join service_dates s on s.service_id=t.service_id join stops p on p.tid=t.tid
               where t.number in (?, ?) and s.date=?""", (number, base_number(number), d)).fetchall()
        for st, arr, dep, seq, tid, smin, smax in rows:
            a = arr + shift * 1440 if arr is not None and seq != smin else None
            dd = dep + shift * 1440 if dep is not None and seq != smax else None
            if shift == -1 and (dd if dd is not None else (a if a is not None else -1)) < 0:
                continue
            prev = res.get(st, (None, None))
            res[st] = (a if a is not None else prev[0], dd if dd is not None else prev[1])
    return res


def find_transfer(engine, a_no, b_no, date, text=""):
    """Stanice přestupu z vlaku a_no na b_no: kde oba staví, a přijede a b odjede nejdříve po příjezdu."""
    ra, rb = train_run(engine, a_no, date), train_run(engine, b_no, date)
    if not ra or not rb:
        return None
    # obvody stanic (např. Ostrava hl.n. + uhelné nádraží) ber jako jednu stanici
    def canon(sid):
        for main, group in engine.districts.items():
            if sid in group:
                return main
        return sid
    a_by, b_by = {}, {}
    for st, (arr, dep) in ra.items():
        if arr is not None:
            a_by.setdefault(canon(st), []).append(arr)
    for st, (arr, dep) in rb.items():
        if dep is not None:
            b_by.setdefault(canon(st), []).append(dep)
    norm_text = " " + re.sub(r"[\s\-–]+", " ", _norm_name(text or "")) + " "
    def mentioned(st):
        n = re.sub(r"[\s\-–]+", " ", _norm_name(engine.names.get(st, "")))
        return bool(n) and f" {n} " in norm_text or (n and n in norm_text and len(n) > 6)
    best = None
    for st in set(a_by) & set(b_by):
        for arr in a_by[st]:
            for dep in b_by[st]:
                gap = dep - arr
                if -1440 < gap < 0:
                    gap += 1440            # přes půlnoc
                if 0 <= gap <= 240:
                    key = (0 if mentioned(st) else 1, gap)
                    if best is None or key < best[0]:
                        best = (key, st)
    return best[1] if best else None


OTHER_MODE_RE = re.compile(r"(?<![\w])(Autobus|autobus|Bus|BUS|Tramvaj|tramvaj|Tram|Trolejbus|trolejbus|Metro|metro|Lanovka|Loď|Přívoz|Taxi)(?![\w])")


def _cut(text, start_markers, end_markers):
    i = 0
    for m in start_markers:
        k = text.find(m)
        if k >= 0:
            i = k
            break
    j = len(text)
    for m in end_markers:
        k = text.find(m, i + 1)
        if k >= 0:
            j = min(j, k)
    return text[i:j]


def _legs_from_chunks(chunks):
    legs = []
    for ch in chunks:
        ch = ch.strip()
        if not ch:
            continue
        tr = parse_trains(ch)
        head = ch[:80]
        if tr and not OTHER_MODE_RE.match(head.strip()):
            cat, num = tr[0]
            legs.append({"type": "train", "category": cat, "number": num, "text": ch[:160]})
        elif OTHER_MODE_RE.search(head):
            legs.append({"type": "other", "text": ch[:160]})
    return legs


def extract_legs(text, url=""):
    """Rozdělí spojení na úseky. Vrací (legs, text_pro_datum, zdroj)."""
    t = text or ""
    if "Jízdní řády Seznam" in t or "seznam.cz/jizdnirady" in url:
        seg = _cut(t, ["Předchozí spojení"], ["Následující spojení"])
        chunks = re.split(r"(?=(?<![\w])(?:Vlak|Autobus|Tramvaj|Trolejbus|Metro|Lanovka|Loď)\s)", seg)
        legs = [l for l in _legs_from_chunks(chunks)]
        # první chunk (souhrn) obsahuje všechny vlaky bez označení – ber jen úseky začínající druhem dopravy
        legs = [l for l in legs if re.match(r"(Vlak|Autobus|Tramvaj|Trolejbus|Metro|Lanovka|Loď)\s", l["text"])]
        return legs, seg, "seznam"
    if "IDOS" in t[:200] or "idos.cz" in url:
        seg = _cut(t, ["Celkový čas"], ["Detaily spojení"])
        chunks = re.split(r"\s[ûü]\s", seg)
        m = re.search(r"Detail spojení [^»]*» [^0-9]*(\d{1,2}\.\d{1,2}\.20\d\d)", t)
        date_text = (m.group(1) + " " if m else "") + _cut(t, ["Datum:"], ["Čas:"]) + " " + seg
        return _legs_from_chunks(chunks[1:] if len(chunks) > 1 else chunks), date_text, "idos"
    if "Můj vlak" in t or re.search(r"\n\s*\n", t):
        chunks = re.split(r"\n\s*\n", t)
        legs = _legs_from_chunks(chunks)
        if len(legs) >= 2 or any(l["type"] == "other" for l in legs):
            return legs, t, "mujvlak" if "Můj vlak" in t else "text"
    # obecný text: vlaky v pořadí, jiné druhy dopravy jako oddělovač
    legs = []
    pos = []
    for m in TRAIN_RE.finditer(t):
        pos.append((m.start(), "train", (m.group(1) or m.group(3), int(m.group(2) or m.group(4)))))
    for m in OTHER_MODE_RE.finditer(t):
        pos.append((m.start(), "other", m.group(0)))
    seen = set()
    for _, kind, val in sorted(pos):
        if kind == "train":
            if val[1] in seen:
                continue
            seen.add(val[1])
            legs.append({"type": "train", "category": val[0], "number": val[1], "text": f"{val[0]} {val[1]}"})
        elif not legs or legs[-1]["type"] != "other":
            legs.append({"type": "other", "text": val})
    while legs and legs[0]["type"] == "other":
        legs.pop(0)
    return legs, t, "text"


def analyze(engine, text, date=None, fetch=None):
    """Hlavní funkce: vrátí úseky spojení, přestupy mezi vlaky a vyhodnocení každého přestupu."""
    out = {"input_text": (text or "")[:2000], "warnings": []}
    legs, date_text, source = extract_legs(text)
    trains = [l for l in legs if l["type"] == "train"]
    if len(trains) < 2 and fetch:
        for url in URL_RE.findall(text or ""):
            url = url.rstrip(").,")
            try:
                page_text = html_to_text(fetch(url))
                l2, d2, src2 = extract_legs(page_text, url)
                if len([x for x in l2 if x["type"] == "train"]) >= len(trains):
                    legs, date_text, source = l2, d2, src2
                    trains = [l for l in legs if l["type"] == "train"]
                    out["fetched_url"] = url
                    break
            except Exception as e:
                out["warnings"].append(f"Odkaz se nepodařilo načíst ({type(e).__name__}).")
    out["source"] = source
    if date is None:
        date, explicit = parse_date(date_text)
        if not explicit:
            out["warnings"].append("V textu nebylo datum – použit dnešek.")
    out["date"] = date.isoformat()
    out["legs"] = [{"type": l["type"], **({"category": l["category"], "number": l["number"]} if l["type"] == "train" else {"text": l["text"][:60]})} for l in legs]
    out["trains"] = [{"category": l["category"], "number": l["number"]} for l in trains]
    transfers = []
    for prev, nxt, between in _pairs(legs):
        a, b = prev["number"], nxt["number"]
        item = {"from_train": f"{prev['category']} {a}", "to_train": f"{nxt['category']} {b}"}
        if between:
            item["result"] = "skipped_other_mode"
            item["message"] = "Mezi vlaky je úsek jiným dopravním prostředkem (autobus apod.) – tento přestup zatím nevyhodnocujeme."
            transfers.append(item)
            continue
        st = find_transfer(engine, a, b, date, date_text)
        if st is None:
            item["result"] = "station_not_found"
            item["message"] = (f"Nenašel jsem stanici, kde by šlo z {item['from_train']} přestoupit na {item['to_train']} dne {date.isoformat()} "
                               "(vlaky ten den nejedou, nebo se mezi nimi přechází jinam).")
            transfers.append(item)
            continue
        name = engine.names.get(st)
        hb, _sid = engine.find_station(name)
        res = engine.evaluate(hb or name, date, a, b)
        res.pop("trace", None)
        item.update(station=res.get("station", {}).get("name", name), evaluation=res,
                    result=res.get("result"), message=res.get("message"))
        transfers.append(item)
    out["transfers"] = transfers
    if not transfers:
        out["result"] = "no_transfer"
        out["message"] = ("Spojení je bez přestupu mezi vlaky." if trains else
                          "V textu ani v odkazu jsem nenašel čísla vlaků (např. „R 881“, „Os 2803“).")
    else:
        out["result"] = "ok"
    return out


def _pairs(legs):
    """Dvojice po sobě jdoucích vlaků; between=True, pokud mezi nimi byl jiný druh dopravy."""
    prev, between = None, False
    for l in legs:
        if l["type"] == "train":
            if prev is not None:
                yield prev, l, between
            prev, between = l, False
        elif prev is not None:
            between = True
