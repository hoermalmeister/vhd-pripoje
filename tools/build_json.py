"""Sestaví JSON soubory stanic z Excelu 'Přípoje mezi vlaky osobní dopravy'.

Použití:  python tools/build_json.py Pripoje_2026.xlsx data/
"""
import json, os, re, sys, datetime
from collections import OrderedDict

sys.path.insert(0, os.path.dirname(__file__))
from xlsx_reader import load_workbook, sheet_grid, norm_space
from parse_rules import (parse_selector, parse_wait, parse_part_a, parse_part_b, parse_validity,
                         slugify, clean, DAY_KEYS)

SCHEMA_VERSION = "1.0"
OVERRIDES = json.load(open(os.path.join(os.path.dirname(__file__), "overrides.json"), encoding="utf-8"))
PARTB_MANUAL = json.load(open(os.path.join(os.path.dirname(__file__), "partb_manual.json"), encoding="utf-8"))


def apply_overrides(station_id, rule):
    r = rule["source_row"]
    ro = OVERRIDES.get(f"{station_id}:{r}:_rule")
    if ro:
        rule.update(ro)
    for k in ("waiting_train", "connecting_train"):
        o = OVERRIDES.get(f"{station_id}:{r}:{k}")
        if o:
            text = rule[k]["text"]
            marks = rule[k].get("footnote_marks")
            rule[k] = OrderedDict(text=text, **({"footnote_marks": marks} if marks else {}), **o, parse="manual")
    return rule


SECTION_RE = re.compile(r"^(?:vlaky\s+(?:[\wÁ-ž]+\s+)?)?(?:ve\s+|ze\s+)?směru?\s+\S", re.I)


def classify_single(text):
    t = text.strip()
    if not t:
        return "blank"
    if t.startswith("*"):
        return "footnote"
    if re.match(r"^Zpracoval|^Za Správu železnic", t):
        return "signature"
    if re.match(r"^Pokračování čekacích dob", t):
        return "continuation"
    if re.match(r"^platí základní čekací doby\.?$", t, re.I):
        return "basic_wait_times"
    if re.match(r"^Vlaky nejsou přípojem\.?$", t, re.I):
        return "no_connections"
    if SECTION_RE.match(t) and len(t) <= 110:
        return "section"
    return "note"


def section_direction(title):
    m = re.search(r"směru?\s+(.+?)(?:\s*\((.*)\))?$", title)
    if not m:
        return None, None
    return clean(m.group(1)), (clean(m.group(2)) if m.group(2) else None)


def variant_from_sheet(name):
    n = name.strip()
    m = re.search(r"\s+(do|od)\s+(\d{1,2})\.\s*(\d{1,2})\.?$", n)
    if m:
        return n[: m.start()].strip(), f"{m.group(1)} {int(m.group(2))}.{int(m.group(3))}.", m
    m = re.search(r"\s+(MIMO|SEZ)$", n)
    if m:
        return n[: m.start()].strip(), m.group(1), None
    return n, None, None


def build_sheet(ws, year_hint=2026):
    g = sheet_grid(ws)
    review = []
    station_cell = g.get(2, [None])[0]
    station_name = clean(station_cell.effective) if station_cell else ws.title.strip()
    base_name, variant, vm = variant_from_sheet(ws.title)
    validity = parse_validity(g[3][0].effective if 3 in g else "")
    if vm:
        d, mth = int(vm.group(2)), int(vm.group(3))
        iso = f"{year_hint}-{mth:02d}-{d:02d}"
        if vm.group(1) == "do":
            validity["valid_to"] = iso
        else:
            if not validity["valid_from"] or validity["valid_from"] < iso:
                validity["valid_from"] = iso
    if variant in ("MIMO", "SEZ"):
        validity["season_variant"] = {"MIMO": "mimo sezónu", "SEZ": "v sezóně"}[variant]
        if variant == "MIMO":
            validity["periods"] = [{"from": "2025-12-14", "to": "2026-06-13"}, {"from": validity["valid_from"], "to": "2026-12-12"}]
            validity["note"] = "List uvádí jen začátek platnosti; předpokládá se platnost i před sezónou (14. 12. 2025 – 13. 6. 2026)."

    station = OrderedDict()
    station_id = slugify(ws.title.strip())
    station["schema_version"] = SCHEMA_VERSION
    station["id"] = station_id
    station["station"] = base_name if variant else station_name
    station["sheet_name"] = ws.title
    station["variant"] = variant
    station["validity"] = validity
    station["part_a"] = []
    station["part_b"] = []
    station["part_c"] = {"sections": []}
    station["footnotes"] = OrderedDict()
    station["notes"] = []
    station["signatures"] = []

    part = None
    section = None
    rule_no = 0
    last_footnote = None

    def new_section(title, row):
        nonlocal section
        direction, detail = section_direction(title) if title else (None, None)
        section = OrderedDict(title=title, direction=direction)
        if detail:
            section["direction_detail"] = detail
        section["source_row"] = row
        section["basic_wait_times_only"] = False
        section["rules"] = []
        section["notes"] = []
        station["part_c"]["sections"].append(section)
        return section

    for r in sorted(g):
        cells = g[r]
        a = cells[0]
        t = a.effective
        if r <= 3:
            continue
        if t.startswith("Část A"):
            part = "A"; continue
        if t.startswith("Část B"):
            part = "B"; continue
        if t.startswith("Část C"):
            part = "C"; continue
        if part in ("A", "B"):
            if a.empty:
                continue
            item = OrderedDict(text=t, source_row=r)
            if a.changed:
                item["change"] = a.change_info()
            if a.fully_struck:
                item["status"] = "cancelled"
                item["text"] = a.original
            parsed = (parse_part_a if part == "A" else parse_part_b)(t if not a.fully_struck else a.original)
            manual = PARTB_MANUAL.get(f"{station_id}:{part.lower()}:{r}")
            if manual:
                parsed = {"type": "rule_set", "source": "manual", "rules": manual}
            item["structured"] = parsed
            if (parsed.get("type") == "text" or parsed.get("parse") == "partial") and not parsed.get("additional_text"):
                review.append({"where": f"part_{part.lower()}", "row": r, "issue": "Text nebyl plně strukturován – zpracovat ručně.", "text": item["text"]})
            station["part_a" if part == "A" else "part_b"].append(item)
            continue
        if part != "C":
            continue
        # header rows (také opakované na dalších stránkách)
        if t.startswith("má vlak"):
            continue
        if a.empty and cells[2].effective in ("⚒", "") and cells[3].effective in ("⑥", "") and cells[4].effective in ("†", "") and cells[1].empty:
            if not (cells[2].empty and cells[3].empty and cells[4].empty) or True:
                continue
        single = all(c.empty for c in cells[1:5])
        if single:
            if a.empty:
                continue
            kind = classify_single(t if not a.fully_struck else a.original)
            if kind == "section":
                new_section(t if not a.fully_struck else a.original, r)
                if a.fully_struck:
                    section["status"] = "cancelled"
                continue
            if kind == "continuation" or kind == "blank":
                continue
            if kind == "signature":
                station["signatures"].append(t); continue
            if kind == "footnote":
                txt = t if not a.fully_struck else a.original
                m = re.match(r"^(\*+)\s*(.*)$", txt)
                station["footnotes"][m.group(1)] = OrderedDict(text=clean(m.group(2)))
                if a.changed:
                    station["footnotes"][m.group(1)]["change"] = a.change_info()
                if a.fully_struck:
                    station["footnotes"][m.group(1)]["status"] = "cancelled"
                last_footnote = m.group(1)
                continue
            if kind in ("basic_wait_times", "no_connections"):
                if section is None:
                    new_section(None, r)
                if kind == "basic_wait_times":
                    section["basic_wait_times_only"] = True if not section["rules"] else section["basic_wait_times_only"]
                    if section["rules"]:
                        section["notes"].append({"text": t})
                else:
                    section["no_connections"] = True
                continue
            # obecná poznámka / pokyn
            note = OrderedDict(text=t if not a.fully_struck else a.original)
            if a.changed:
                note["change"] = a.change_info()
            if a.fully_struck:
                note["status"] = "cancelled"
            if section is not None:
                note["after_section"] = section["title"]
            station["notes"].append(note)
            continue

        # ------------------------------------------------------- řádek tabulky
        if all(c.from_merge for c in cells[:5] if not c.empty):
            continue  # pokračování svisle sloučeného řádku – nejde o samostatnou vazbu
        if section is None:
            new_section(None, r)
        b = cells[1]
        row_cells = [c for c in cells[:5] if not c.empty]
        row_cancelled = all(c.fully_struck for c in row_cells)
        rule_no += 1
        rule = OrderedDict()
        rule["id"] = f"{station_id}-{rule_no:03d}"
        rule["source_row"] = r
        rule["status"] = "cancelled" if row_cancelled else "active"
        use = (lambda c: c.original) if row_cancelled else (lambda c: c.effective)
        rule["waiting_train"] = parse_selector(use(a), "waiting")
        rule["connecting_train"] = parse_selector(use(b), "connecting")
        wait = OrderedDict()
        for key, c in zip(DAY_KEYS, cells[2:5]):
            wait[key] = parse_wait(use(c), c.empty or (not row_cancelled and c.effective == ""))
        rule["wait"] = wait
        changes = OrderedDict()
        for key, c in zip(["waiting_train", "connecting_train"] + DAY_KEYS, cells[:5]):
            if c.changed and not row_cancelled:
                changes[key] = c.change_info()
        if changes:
            rule["changes"] = changes
        if row_cancelled:
            rule["note"] = "Řádek je v podkladu celý přeškrtnutý – vazba byla změnou zrušena, neuplatňuje se."
        apply_overrides(station_id, rule)
        # review
        if rule["status"] == "active":
            for k in ("waiting_train", "connecting_train"):
                if rule[k]["parse"] not in ("full", "manual"):
                    review.append({"where": rule["id"], "row": r, "field": k, "issue": "Selektor vlaků nebyl plně rozpoznán.", "text": rule[k]["text"]})
            for k, w in wait.items():
                if w["kind"] == "text":
                    review.append({"where": rule["id"], "row": r, "field": f"wait.{k}", "issue": "Prázdná nebo nerozpoznaná čekací doba." if w["kind"] == "empty" else "Nerozpoznaná čekací doba.", "text": w["raw"]})
        section["rules"].append(rule)

    # sekce bez řádků a bez příznaku
    # kontrola, že odkazované poznámky existují
    for sec in station["part_c"]["sections"]:
        for rule in sec["rules"]:
            marks = set(rule["waiting_train"].get("footnote_marks", []) + rule["connecting_train"].get("footnote_marks", []))
            for w in rule["wait"].values():
                marks |= set(w.get("footnote_marks", []))
                for v in w.get("variants", []):
                    marks.add(v["footnote_mark"])
            missing = [m for m in marks if m not in station["footnotes"]]
            if missing and rule["status"] == "active":
                review.append({"where": rule["id"], "issue": f"Odkaz na poznámku {', '.join(missing)}, která pod tabulkou chybí (může být uvedena jako běžná poznámka).", "text": ""})
    station["summary"] = OrderedDict(
        sections=len(station["part_c"]["sections"]),
        rules_active=sum(1 for s in station["part_c"]["sections"] for x in s["rules"] if x["status"] == "active"),
        rules_cancelled=sum(1 for s in station["part_c"]["sections"] for x in s["rules"] if x["status"] == "cancelled"),
        review_items=len(review),
    )
    station["review"] = review
    return station


def main(xlsx, outdir):
    wb = load_workbook(xlsx)
    os.makedirs(os.path.join(outdir, "stations"), exist_ok=True)
    index = []
    for ws in wb.worksheets:
        if ws.title.strip().lower() == "seznam":
            continue
        st = build_sheet(ws)
        fn = f"stations/{st['id']}.json"
        with open(os.path.join(outdir, fn), "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
        index.append(OrderedDict(id=st["id"], station=st["station"], variant=st["variant"],
                                 valid_from=st["validity"]["valid_from"], valid_to=st["validity"]["valid_to"],
                                 **({"periods": st["validity"]["periods"]} if st["validity"].get("periods") else {}),
                                 file=fn, **st["summary"]))
    # seskupení variant
    stations = OrderedDict()
    for it in index:
        stations.setdefault(it["station"], []).append(it)
    out = OrderedDict(
        schema_version=SCHEMA_VERSION,
        source=OrderedDict(document="Přípoje mezi vlaky osobní dopravy 2026", issuer="Správa železnic, státní organizace",
                           timetable="JŘ 2026", valid_from="2025-12-14", valid_to="2026-12-12",
                           source_file=os.path.basename(xlsx),
                           generated_at=datetime.datetime.now().isoformat(timespec="seconds")),
        station_count=len(stations), file_count=len(index),
        stations=[OrderedDict(station=k, slug=slugify(k), files=v) for k, v in sorted(stations.items(), key=lambda kv: kv[0])],
    )
    with open(os.path.join(outdir, "index.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    # souhrn položek k ruční revizi
    review = []
    for it in index:
        st = json.load(open(os.path.join(outdir, it["file"]), encoding="utf-8"))
        for r in st["review"]:
            review.append(OrderedDict(station=st["station"], file=it["file"], **r))
    with open(os.path.join(outdir, "review.json"), "w", encoding="utf-8") as f:
        json.dump(OrderedDict(count=len(review), items=review), f, ensure_ascii=False, indent=2)
    print(f"{len(index)} souborů, {len(stations)} stanic")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
