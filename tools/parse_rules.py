"""Převod textů z tabulek na strukturované objekty (selektory vlaků, čekací doby, pravidla části B)."""
import re
import unicodedata

CATEGORIES = ["Ex", "R", "Sp", "Os", "EC", "IC", "EN", "SC", "rj", "LE"]
OPERATORS = {
    "ČD": "ČD", "CD": "ČD", "ARR": "ARR", "AŽD": "AŽD", "DLB CZ": "DLB", "DLB": "DLB", "GWTR": "GWTR",
    "KŽC Doprava": "KŽC", "KŽC": "KŽC", "LET": "LET", "LE": "LE", "MBMR": "MBMR", "ÖBB PV AG": "ÖBB", "ÖBB": "ÖBB",
    "OÚD": "OÚD", "RCAS": "RCAS", "RJÚK": "RJÚK", "RJ": "RJ", "SZD": "SZD",
}
OP_RE = r"(?:DLB CZ|KŽC Doprava|ÖBB PV AG|RJÚK|RJ|ČD|ARR|AŽD|DLB|GWTR|KŽC|LET|LE|MBMR|ÖBB|OÚD|RCAS|SZD)"

DAY_KEYS = ["workday", "saturday", "sunday_holiday"]  # ⚒, ⑥, †

DASH = r"[–\-−—]"


def slugify(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s


def clean(s):
    s = (s or "").replace(" ", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def split_marks(s):
    """Vytáhne hvězdičkové odkazy na poznámky ('15*' -> ('15', ['*']))."""
    marks = re.findall(r"\*+", s)
    rest = clean(re.sub(r"\*+", " ", s))
    # zachovat pořadí, unikátně
    seen, out = set(), []
    for m in marks:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return rest, out


# ---------------------------------------------------------------- čísla vlaků

def parse_number_list(text):
    """'6002 – 6016, 6022 a 6030' -> trains [6022, 6030], ranges [{from:6002,to:6016}]
    Vrací (trains, ranges, patterns, leftover)."""
    trains, ranges, patterns = [], [], []
    t = text
    # sudé/liché rozsahy
    def par_range(m):
        parity = "even" if m.group(1).lower().startswith("sud") else "odd"
        ranges.append({"from": int(m.group(2)), "to": int(m.group(3)), "parity": parity})
        return " "
    t = re.sub(r"\b(sudé|liché)\s+(?:vlaky\s+)?(?:(?:Ex|R|Sp|Os)\s+)?(?:" + OP_RE + r"\s+)?(\d{1,6})\s*" + DASH + r"\s*(\d{1,6})", par_range, t, flags=re.I)
    def rng(m):
        a, b = int(m.group(1)), int(m.group(2))
        if b < a and len(m.group(2)) < len(m.group(1)):  # 1101-33 apod.
            b = int(m.group(1)[: len(m.group(1)) - len(m.group(2))] + m.group(2))
        ranges.append({"from": a, "to": b, "parity": None})
        return " "
    t = re.sub(r"(?<![\d.])(\d{1,6})\s*" + DASH + r"\s*(\d{1,6})(?![\d.])", rng, t)
    def pat(m):
        patterns.append(m.group(0).lower())
        return " "
    t = re.sub(r"\b\d{1,4}x{1,4}\b", pat, t, flags=re.I)
    def num(m):
        trains.append(int(m.group(1)))
        return " "
    t = re.sub(r"(?<![\d.:/])(?:(?:Ex|R|Sp|Os)\s*)?(\d{1,6})(?![\d.:])", num, t)
    leftover = clean(re.sub(r"[,;]|\ba\b|\bvlaky\b|\bvlak\b|\bvlaků\b", " ", t))
    return trains, ranges, patterns, leftover


def _cats(text):
    found = []
    for c in ["Ex", "R", "Sp", "Os"]:
        if re.search(r"(?<![\wÁ-ž])" + c + r"(?![\wÁ-ž])", text):
            found.append(c)
    return found


def _ops(text):
    found = []
    for m in re.finditer(r"(?<![\wÁ-ž])" + OP_RE + r"(?![\wÁ-ž])", text):
        o = OPERATORS[m.group(0)]
        if o not in found:
            found.append(o)
    return found


ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10, "XI": 11, "XII": 12}
DATE_TOKEN = r"(\d{1,2})\.\s*(XII|XI|X|IX|VIII|VII|VI|V|IV|III|II|I|\d{1,2})\.?(?:\s*(\d{4}))?"


def _md(m):
    d, mo = int(m.group(1)), m.group(2)
    mo = ROMAN.get(mo) or int(mo)
    return f"{mo:02d}-{d:02d}"


def parse_date_condition(text):
    """'do 24.IV. a od 29.IX.' -> [{'to':'04-24'},{'from':'09-29'}]; 'od 25.4. do 31.8.' -> [{'from','to'}]"""
    periods = []
    for m in re.finditer(r"(?:od\s+" + DATE_TOKEN + r"\s*(?:do|–|-)\s*" + DATE_TOKEN + r")|(?:(od|do)\s+" + DATE_TOKEN + r")", text):
        if m.group(1):
            a = re.match(DATE_TOKEN, m.group(0)[3:].strip())
            parts = re.findall(DATE_TOKEN, m.group(0))
            f = parts[0]; t = parts[1]
            periods.append({"from": f"{(ROMAN.get(f[1]) or int(f[1])):02d}-{int(f[0]):02d}",
                            "to": f"{(ROMAN.get(t[1]) or int(t[1])):02d}-{int(t[0]):02d}"})
        else:
            kind = m.group(7)
            p = re.findall(DATE_TOKEN, m.group(0))[0]
            periods.append({("from" if kind == "od" else "to"): f"{(ROMAN.get(p[1]) or int(p[1])):02d}-{int(p[0]):02d}"})
    return periods


def _split_places(s):
    parts = re.split(r"\s*(?:,|\bnebo\b|\ba\b|\bi\b)\s*", clean(s))
    return [p for p in (clean(x) for x in parts) if p]


def _normalize(t):
    t = re.sub(r"RJ\s*Ú[Kk]", "RJÚK", t)
    t = re.sub(r"(\d{3,})\.(\d{3,})", r"\1, \2", t)       # 19805.19853 -> 19805, 19853
    t = re.sub(r"\bvšchny\b", "všechny", t)
    t = re.sub(r"\bvlak\b", "vlaky", t)
    t = re.sub(r"\(\s*(mimo)\s+", "(vyjma ", t)
    t = re.sub(r"\bpouze\b|\bkategorie\b|\bna všechny směry\b", " ", t)
    t = t.replace("+", ",")
    return t


def parse_selector(text, column, _depth=0):
    """Rozparsuje text ze sloupce 1 (návazný = čekající vlak) nebo 2 (přípojný vlak).

    'parse' = full | partial | none ; nerozpoznaný zbytek je v 'unparsed'.
    """
    raw = clean(text)
    sel = {"text": raw}
    if not raw:
        sel.update(parse="none")
        return sel
    t, marks = split_marks(raw)
    if marks:
        sel["footnote_marks"] = marks
    work = _normalize(t)
    leftovers = []
    flags = []

    # příznaky
    if re.search(r"\(\s*NAD\s*\)", work):
        sel["replacement_bus"] = True; work = re.sub(r"\(\s*NAD\s*\)", " ", work)
    if re.search(r"\bkurzy\b", work):
        sel["through_coaches"] = True; work = re.sub(r"\bkurzy\b", " ", work)
    # časové omezení v závorce: (do 24.IV. a od 29.IX.), (do 30. 6.), ... (vyjma níže uvedených do 24. 4. a od 1. 9.
    def dc(m):
        sel["date_condition"] = {"text": clean(m.group(1)), "periods": parse_date_condition(m.group(1))}
        return " "
    work = re.sub(r"\(?\s*((?:(?:od|do)\s+\d{1,2}\.\s*(?:[IVX]+|\d{1,2})\.?(?:\s*\d{4})?\s*(?:a\s+)?)+)\)?", dc, work)

    # (vyjma ...), (kromě ...), (včetně ...)
    excepts, includes = [], []
    def exc(m):
        kind, body = m.group(1).lower(), clean(m.group(2))
        if re.search(r"níže uveden", body):
            sel["except_listed_below"] = True
            body = clean(re.sub(r"níže uvedených?", " ", body))
            if not body:
                return " "
        sub = parse_selector(body, column, _depth + 1)
        (includes if kind.startswith("včetně") else excepts).append(sub)
        return " "
    work = re.sub(r",?\s*\(?\s*(vyjma|vyma|kromě|včetně)\s+([^()]*)\)?", exc, work, flags=re.I)

    is_all = False
    if re.match(r"^V\b", work.strip()):
        is_all = True
        work = re.sub(r"^\s*V\b", " ", work)
    if re.search(r"\bvšechn[yéa]\b|\bostatní\b|\bvšech\b", work, re.I):
        is_all = True
        work = re.sub(r"\bvšechn[yéa]\b|\bostatní\b|\bvšech\b", " ", work, flags=re.I)

    quals = {}
    def q_line(m):
        quals.setdefault("lines", []).extend(re.findall(r"[A-Z][a-z]?\s?\d{1,3}", m.group(1))); return " "
    def q_track(m):
        quals.setdefault("tracks", []).extend(re.findall(r"\d+", m.group(1))); return " "
    def q_rel(m):
        quals["relation"] = clean(m.group(1)); return " "
    def q_dest(m):
        quals["destination_station"] = clean(m.group(1)); return " "
    def q_from(m):
        quals.setdefault("from_directions", []).extend(_split_places(m.group(1))); return " "
    def q_to(m):
        quals.setdefault("to_directions", []).extend(_split_places(m.group(1))); return " "
    def q_line2(m):
        quals.setdefault("lines", []).append(m.group(1)); return " "
    work = re.sub(r"(?:\(?\blink[ayu]\s+)?(?<![\wÁ-ž])((?:Ex|R|S|L|U|T|P|V|RE)\d{1,2})(?![\d\wÁ-ž])\)?", q_line2, work)
    work = re.sub(r"(?:\(?\blink[ayu]\s+)(?=\s*(?:a\s+)?$)", " ", work)
    work = re.sub(r"\(?\blink[ayu]\s+((?:[A-Z][a-z]?\s?\d{1,3})(?:\s*(?:,|a)\s*[A-Z][a-z]?\s?\d{1,3})*)\)?", q_line, work)
    work = re.sub(r"\b(?:z|od)\s+trat[ěií]\s+([\d, a]+)", q_track, work)
    work = re.sub(r"\brelace\s+([^,(]+)", q_rel, work)
    work = re.sub(r"s cílovou ŽST\s+([^,(]+)", q_dest, work)
    PLACE = r"([A-ZČŘŠŽÚÁÍÉŚŁ][^()\d]*?)"
    work = re.sub(r"\b(?:ze|z)\s+směr[uů]\s+(?:od\s+)?" + PLACE + r"(?=\s*(?:$|\(|\bvlaky\b))", q_from, work)
    work = re.sub(r"\bod\s+" + PLACE + r"(?=\s*(?:$|\(|\bvlaky\b))", q_from, work)
    work = re.sub(r"\bze\s+" + PLACE + r"(?=\s*(?:$|\())", q_from, work)
    work = re.sub(r"\bve?\s+směru\s+" + PLACE + r"(?=\s*(?:$|\(|\d))", q_to, work)
    work = re.sub(r"\bsměr\s+" + PLACE + r"(?=\s*(?:$|\(|\d))", q_to, work)
    for key in ("from_directions", "to_directions"):
        for p in quals.get(key, []):
            if re.search(r"\bvlak|\bsměr|(?<![\wÁ-ž])(?:Ex|R|Sp|Os)(?![\wÁ-ž])", p) or p[:1].islower():
                leftovers.append(f"nejasný směr: {p}")
    if quals:
        sel.update(quals)

    ops = _ops(work)
    work_wo_ops = re.sub(r"(?:\bdopravce\s+)?(?<![\wÁ-ž])" + OP_RE + r"(?![\wÁ-ž])", " ", work)
    parity = None
    pm = re.search(r"\b(liché|sudé)\b(?!\s+(?:vlaky\s+)?(?:(?:Ex|R|Sp|Os)\s+)?(?:" + OP_RE + r"\s+)?\d)", work_wo_ops, re.I)
    if pm:
        parity = "odd" if pm.group(1).lower() == "liché" else "even"
        work_wo_ops = work_wo_ops[: pm.start()] + " " + work_wo_ops[pm.end():]
    cats = _cats(work_wo_ops)
    trains, ranges, patterns, lo = parse_number_list(work_wo_ops)
    lo = clean(re.sub(r"\bnebo\b", " ", lo))
    lo = clean(re.sub(r"\b(Ex|R|Sp|Os)\b|\bdopravce\b|\bsérie\b|\bčíslo\b|\bvlaky\b|\bvlaků\b|\bvlakům\b", " ", lo))
    lo = clean(re.sub(r"^[\s,:;–\-()]+|[\s,:;–\-()]+$", "", lo))
    lo = clean(re.sub(r"^[\s,:;–\-()]+$", "", lo))

    if trains:
        sel["trains"] = trains
    if ranges:
        sel["ranges"] = ranges
    if patterns:
        sel["patterns"] = patterns
    if cats:
        sel["categories"] = cats
    if ops:
        sel["operators"] = ops
    if parity:
        sel["parity"] = parity
    if is_all or (not trains and not ranges and not patterns and (cats or ops or quals or parity)):
        sel["all"] = True
    if excepts:
        sel["except"] = excepts
    if includes:
        sel["include"] = includes
    if lo:
        leftovers.append(lo)
    if sel.get("all") and (trains or ranges or patterns) and _depth == 0 and not re.match(r"^\s*(?:liché|sudé)", raw):
        leftovers.append("kombinace 'všechny' a výčtu čísel – ověřit význam")
    sub_partial = any(s.get("parse") != "full" for s in excepts + includes)
    if leftovers:
        sel["unparsed"] = "; ".join(leftovers)
    has_any = any(k in sel for k in ("trains", "ranges", "patterns", "all"))
    if not has_any and sel.get("except_listed_below"):
        sel["all"] = True; has_any = True
    sel["parse"] = "full" if has_any and not leftovers and not sub_partial else ("partial" if has_any else "none")
    return sel


# ---------------------------------------------------------------- čekací doba

def parse_wait(text, cell_empty):
    raw = clean(text)
    w = {"raw": raw}
    if cell_empty or raw == "":
        w["kind"] = "empty"
        return w
    t, marks = split_marks(raw)
    if marks:
        w["footnote_marks"] = marks
    tl = t.lower()
    if t in ("-", "—", "–") or (t == "" and marks):
        w["kind"] = "no_connection" if t else "see_footnote"
        return w
    if tl == "n":
        w["kind"] = "never"
        return w
    if tl == "vždy":
        w["kind"] = "always"
        return w
    if tl.startswith("přechod"):
        w["kind"] = "always_through_coach"
        extra = clean(t[len("přechod"):])
        if extra:
            w["condition"] = extra
        return w
    if tl.startswith("vždy"):
        w["kind"] = "always"
        w["condition"] = clean(t[4:])
        return w
    m = re.fullmatch(r"(\d+)", t)
    if m:
        w["kind"] = "minutes"; w["minutes"] = int(m.group(1))
        return w
    # 10/5*, 15/10*
    m = re.fullmatch(r"(\d+)\s*/\s*(\d+)", t)
    if m:
        w["kind"] = "minutes"; w["minutes"] = int(m.group(1)); w["minutes_alternative"] = int(m.group(2))
        w["note"] = "Hodnota závisí na podmínce v poznámce pod čarou."
        return w
    # 15 v ⑤  (v uvedené dny)
    m = re.fullmatch(r"(\d+|N)\s+v\s+(.+)", t)
    if m:
        if m.group(1) == "N":
            w["kind"] = "never"
        else:
            w["kind"] = "minutes"; w["minutes"] = int(m.group(1))
        w["only_on_days"] = m.group(2)
        return w
    # '5* /7**' – více variant podle poznámek
    parts = re.findall(r"(\d+)(\*+)", raw)
    if len(parts) >= 2:
        w["kind"] = "variants"
        w["variants"] = [{"minutes": int(a), "footnote_mark": b} for a, b in parts]
        return w
    w["kind"] = "text"
    return w


# ---------------------------------------------------------------- část B

def _op_list(s):
    return [OPERATORS[m] for m in re.findall(OP_RE, s)]


PAIR_RE = re.compile(r"((?:Ex|R|Sp|Os)\s*\d+(?:\s*\+\s*(?:Ex|R|Sp|Os)?\s*\d+)?)(?:\s*\(([^)]*)\))?\s+a\s+((?:Ex|R|Sp|Os)\s*\d+(?:\s*\+\s*(?:Ex|R|Sp|Os)?\s*\d+)?)(?:\s*\(([^)]*)\))?")


def _train_ref(s):
    out = []
    for m in re.finditer(r"(Ex|R|Sp|Os)?\s*(\d+)", s):
        out.append({"category": m.group(1), "number": int(m.group(2))})
    return out


def _parse_part_b_sentence(t):
    tl = t.lower()
    if re.fullmatch(r"(jinak |jiank )?platí základní pravidla dle článku 2\.?", tl):
        return {"type": "default_rules"}
    if re.fullmatch(r"platí základní čekací doby?\.?", tl):
        return {"type": "default_wait_times"}
    # Základní čekací doba (vlaků|dopravce) GWTR na vlaky (ostatních dopravců|dopravce ČD a ARR) je 10 minut.
    m = re.fullmatch(r"Základní čekací doba (?:vlaků |dopravce )?(" + OP_RE + r") na vlaky (ostatních dopravců|(?:dopravců? )?(?:" + OP_RE + r")(?:(?:,\s*| a )" + OP_RE + r")*) je\s+(\d+)\s+min(?:ut)?\.?", t)
    if m:
        conn = "others" if m.group(2).startswith("ostatních") else _op_list(m.group(2))
        return {"type": "operator_basic_wait", "waiting_operator": OPERATORS[m.group(1)],
                "connecting_operators": conn, "minutes": int(m.group(3))}
    m = re.fullmatch(r"Vlaky (" + OP_RE + r") čekají na vlaky (" + OP_RE + r") maximálně (\d+) minut\.?", t)
    if m:
        return {"type": "operator_basic_wait", "waiting_operator": OPERATORS[m.group(1)],
                "connecting_operators": [OPERATORS[m.group(2)]], "minutes": int(m.group(3))}
    m = re.fullmatch(r"(?:Mezi vlaky není přípoj|Ve stanici není zaručen přestup(?: mezi vlaky)?),? (?:je-li|pokud je) mezi příjezdem a odjezdem (?:přípojných )?(?:vlaků )?interval (?:kratší|menší) než (\d+) minut[uy]?(?:,? \(?pokud není v části [AB](?: nebo [AB])? stanoveno jinak\)?)?\.?", t)
    if m:
        return {"type": "min_interval", "minutes": int(m.group(1)),
                "meaning": "Je-li plánovaný interval mezi příjezdem přípojného a odjezdem návazného vlaku kratší, nejde o přípoj."}
    # dvojice vlaků
    m = re.match(r"(Vlaky považované za přípoj|Přípojnými vlaky|Přípojné vlaky(?: opačného směru| stejného směru)?(?: s (?:mimořádnou )?přestupní dobou (\d+(?:,\d+)?) minut[auy]?)?(?: opačného směru)?)(?: jsou)?:?\s*(.*)$", t)
    if m and PAIR_RE.search(m.group(3) or ""):
        head = m.group(1).lower()
        kind = "considered_connection"
        if "opačného" in head:
            kind = "opposite_direction_connection"
        elif "stejného" in head:
            kind = "same_direction_connection"
        pairs = []
        body = m.group(3)
        for pm in PAIR_RE.finditer(body):
            pairs.append({"trains_a": _train_ref(pm.group(1)), "trains_b": _train_ref(pm.group(3)),
                          **({"note_a": pm.group(2)} if pm.group(2) else {}),
                          **({"note_b": pm.group(4)} if pm.group(4) else {})})
        rest = clean(PAIR_RE.sub(" ", body))
        rest = clean(re.sub(r"[,.;]|\ba\b", " ", rest))
        r = {"type": "connection_pairs", "kind": kind, "pairs": pairs,
             "note": "Dvojice jsou uvedeny bez určení, který vlak čeká – platí pro přestup mezi uvedenými vlaky."}
        tm = m.group(2) or (re.search(r"s přestupní dobou (\d+(?:,\d+)?) minut", t).group(1) if re.search(r"s přestupní dobou (\d+(?:,\d+)?) minut", t) else None)
        if tm:
            r["transfer_minutes"] = float(tm.replace(",", ".")) if "," in tm else int(tm)
            rest = clean(re.sub(r"s přestupní dobou \d+(?:,\d+)? minut[auy]?", " ", rest))
        if rest:
            r["unparsed"] = rest
            r["parse"] = "partial"
        return r
    # Vlaky X jsou přípojem (od) vlaků (dopravce) Y [pouze linky R8 ...] (a opačně), nejsou přípojem vlaků ostatních dopravců.
    w = t
    lines_conn = []
    w = re.sub(r'\s*\(zahrnuty i pod "V"\)', "", w)
    w = re.sub(r",?\s*pokud není v části C stanoveno jinak", "", w)
    def lc(m):
        lines_conn.append(m.group(1)); return ""
    w = re.sub(r"\s*\(?\s*(?:pouze\s+)?linky\s+(R\d+)(?:\s+(?:[A-ZČŘŠŽ][^,()]*?|\([^)]*\)))?(?:\s*\([^)]*\))?\)?(?=,| a opačně| a naopak|\.|$)", lc, w)
    m = re.fullmatch(
        r"Vlaky (?:dopravce )?((?:" + OP_RE + r")(?:(?:,\s*| a )" + OP_RE + r")*)"
        r" jsou (?:přípojem|přípojné|přípojné vlaky|přípojnými vlaky)\s+(?:od\s+)?(?:vlaků\s+)?(?:dopravc[eů]\s+)?"
        r"(všech dopravců|ostatních dopravců|(?:" + OP_RE + r")(?:(?:,\s*| a )" + OP_RE + r")*)"
        r"( a (?:opačně|naopak))?(?:,? nejsou (?:přípojem|přípojné) (?:od )?vlaků ostatních dopravců)?\.?", w)
    if m:
        waiting = _op_list(m.group(1))
        conn = m.group(2)
        r = {"type": "operator_connection", "waiting_operators": waiting,
             "connecting_operators": "all" if conn.startswith("všech") else ("others" if conn.startswith("ostatních") else _op_list(conn)),
             "reciprocal": bool(m.group(3)), "exclusive": bool(re.search(r"nejsou (?:přípojem|přípojné)", w))}
        if lines_conn:
            r["connecting_lines"] = lines_conn
            r["note"] = "Omezení na linku se vztahuje k dopravci RJ uvedenému v textu."
        return r
    m = re.fullmatch(r"Vlaky (?:linky (R\d+) )?(?:dopravce )?(" + OP_RE + r")(?: linky (R\d+)[^,]*?)? jsou (?:přípojem|přípojné) (?:od )?vlaků (?:dopravce )?(všech dopravců|" + OP_RE + r")\.?", t)
    if m:
        return {"type": "operator_connection", "waiting_operators": [OPERATORS[m.group(2)]],
                "waiting_lines": [x for x in (m.group(1), m.group(3)) if x],
                "connecting_operators": "all" if m.group(4).startswith("všech") else [OPERATORS[m.group(4)]],
                "reciprocal": False, "exclusive": False}
    return {"type": "text", "parse": "none"}


def parse_part_b(text):
    t = clean(text)
    r = _parse_part_b_sentence(t)
    if r["type"] != "text":
        return r
    # více vět v jedné buňce: rozpoznat první, zbytek ponechat jako doplňující text
    parts = re.split(r"(?<=[a-zá-ž0-9)])\.\s+(?=[A-ZČŘŠŽÚ])", t, maxsplit=1)
    if len(parts) == 2:
        r1 = _parse_part_b_sentence(parts[0] + ".")
        if r1["type"] != "text":
            r1["additional_text"] = parts[1]
            r1["parse"] = "partial"
            return r1
    return r


# ---------------------------------------------------------------- část A

def parse_part_a(text):
    t = clean(text)
    if re.fullmatch(r"(?:Jinak )?Platí základní přestupní doby\.?", t, re.I):
        return {"type": "default_transfer_times"}
    mins = [int(x) for x in re.findall(r"(\d+)\s*minut", t)]
    if len(mins) == 1:
        desc = clean(re.sub(r"\s*[–\-:]?\s*(je\s+)?(přestupní doba\s+)?(je\s+)?\d+\s*minut[ay]?\.?\s*$", "", t))
        if desc.lower() in ("základní", "", "jinak"):
            desc = "základní přestupní doba ve stanici"
        return {"type": "transfer_time", "minutes": mins[0], "applies_to": desc}
    return {"type": "text", "parse": "none"}


# ---------------------------------------------------------------- platnost

MONTHS = {"ledna": 1, "února": 2, "března": 3, "dubna": 4, "května": 5, "června": 6, "července": 7,
          "srpna": 8, "září": 9, "října": 10, "listopadu": 11, "prosince": 12}


def parse_validity(text):
    t = clean(text)
    out = {"text": t, "valid_from": None, "valid_to": None}
    m = re.search(r"od\s+(\d{1,2})\.\s*(\w+)(?:\s+(\d{4}))?", t)
    m2 = re.search(r"do\s+(\d{1,2})\.\s*(\w+)\s+(\d{4})", t)
    if m2:
        out["valid_to"] = f"{int(m2.group(3)):04d}-{MONTHS[m2.group(2)]:02d}-{int(m2.group(1)):02d}"
    if m:
        y = m.group(3) or (m2.group(3) if m2 else None)
        out["valid_from"] = f"{int(y):04d}-{MONTHS[m.group(2)]:02d}-{int(m.group(1)):02d}"
    return out
