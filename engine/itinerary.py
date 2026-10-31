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


def analyze(engine, text, date=None, fetch=None):
    """Hlavní funkce: vrátí rozpoznané vlaky, přestupy a vyhodnocení každého přestupu."""
    out = {"input_text": (text or "")[:2000], "warnings": []}
    trains = parse_trains(text)
    source_text = text or ""
    if len(trains) < 2 and fetch:
        for url in URL_RE.findall(text or ""):
            try:
                page = fetch(url.rstrip(").,"))
                more = parse_trains(html_to_text(page))
                if len(more) >= len(trains):
                    trains, source_text = more, html_to_text(page)
                    out["fetched_url"] = url
                    break
            except Exception as e:
                out["warnings"].append(f"Odkaz se nepodařilo načíst ({type(e).__name__}).")
    if date is None:
        date, explicit = parse_date(source_text)
        if not explicit:
            out["warnings"].append("V textu nebylo datum – použit dnešek.")
    out["date"] = date.isoformat()
    out["trains"] = [{"category": c, "number": n} for c, n in trains]
    if len(trains) < 2:
        out["result"] = "no_transfer"
        out["message"] = ("V textu jsem nenašel žádný přestup mezi vlaky." if trains else
                          "V textu jsem nenašel čísla vlaků (např. „R 881“, „Os 2803“).")
        out["transfers"] = []
        return out
    transfers = []
    for (ca, a), (cb, b) in zip(trains, trains[1:]):
        st = find_transfer(engine, a, b, date, source_text)
        item = {"from_train": f"{ca} {a}", "to_train": f"{cb} {b}"}
        if st is None:
            item["result"] = "station_not_found"
            item["message"] = (f"Nenašel jsem stanici, kde by šlo z {ca} {a} přestoupit na {cb} {b} dne {date.isoformat()} "
                               "(vlaky ten den nejedou, nebo mezi nimi je jiný dopravní prostředek).")
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
    out["result"] = "ok"
    return out
