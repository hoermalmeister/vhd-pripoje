"""Typy dopraven (stanice / zastávka / …) ze seznamu na Wikipedii -> data/station_types.json.

Zdroj: https://cs.wikipedia.org/wiki/Seznam_železničních_stanic,_zastávek_a_nákladišť_v_Česku
(CC BY-SA 4.0). Uložte stránku jako HTML a spusťte:
  python tools/build_station_types.py stranka.html data/station_types.json

Párování s GTFS: id stanice v GTFS 'czptt:stop:CZ:34434' = prvních 5 číslic kódu SR70 (6. číslice je kontrolní).
"""
import html, json, re, sys
from collections import OrderedDict

STATION_TYPES = {"stanice", "kolejová skupina stanice", "kolejová skupina stanice se zastávkou", "obvod stanice",
                 "tarifní místo v rámci stanice", "seřaďovací nádraží"}
# dopravny, ve kterých k návaznostem nedochází
DOPRAVNA_TYPES = {"dopravna D3", "dopravna radiobloku", "výhybna"}


def category(typ):
    t = typ.strip()
    if t in STATION_TYPES:
        return "station"
    if t in DOPRAVNA_TYPES:
        return "dopravna"
    if t.startswith("zastávka"):
        return "stop"
    return "other"


def parse(path):
    h = open(path, encoding="utf-8", errors="replace").read()
    table = re.findall(r'<table[^>]*class="[^"]*wikitable[^"]*"[^>]*>(.*?)</table>', h, re.S)[0]
    out = OrderedDict()
    for r in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S)[1:]:
        cells = [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", c))).strip()
                 for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", r, re.S)]
        if len(cells) < 6:
            continue
        code = re.sub(r"\D", "", cells[2])
        if len(code) < 5:
            continue
        rec = OrderedDict(name=cells[0], sr70=code, type=cells[5], category=category(cells[5]), line=cells[1])
        out.setdefault(code[:5], []).append(rec)
    return out


if __name__ == "__main__":
    data = parse(sys.argv[1])
    json.dump({"source": "cs.wikipedia.org – Seznam železničních stanic, zastávek a nákladišť v Česku (CC BY-SA 4.0)",
               "by_code": data}, open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    print(len(data), "kódů")
