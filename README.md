# Přípoje vlaků ČR – data čekacích dob

Strukturovaná data z pomůcky Správy železnic **„Přípoje mezi vlaky osobní dopravy 2026“** (JŘ 2026, platí od 14. 12. 2025, zapracovány změny 1–28) jako podklad pro webovou službu a veřejné API, které odpoví na otázku:

> Přestupuji ve stanici **X**, dne **D**, přijedu vlakem **A** a chci pokračovat vlakem **B** – počká na mě B, a jak dlouho?

## Obsah

```
data/
  index.json            seznam stanic, varianty a jejich platnost (od–do)
  rules.json            obecná pravidla z úvodu pomůcky (články 1–15), legenda, základní
                        čekací a přestupní doby, svátky, doporučený algoritmus vyhledání
  review.json           souhrn míst, která parser nerozpoznal úplně (k ruční kontrole)
  stations/<id>.json    jeden soubor na každý list Excelu (= jedna stanice / varianta)
tools/
  build_json.py         převod Excel -> JSON (spustitelné znovu při každé změně pomůcky)
  xlsx_reader.py        čtení buněk vč. přeškrtnutí, červeně vyznačených změn a značek KJŘ
  parse_rules.py        rozpoznání selektorů vlaků, čekacích dob, pravidel části A/B
  overrides.json        ruční opravy atypických textů
```

Přegenerování po vydání další změny pomůcky:

```bash
pip install openpyxl
python tools/build_json.py Pripoje_2026.xlsx data/
```

## Soubor stanice (`data/stations/<id>.json`)

| klíč | obsah |
|---|---|
| `station`, `variant` | název stanice; varianta listu (`do 30.6.`, `od 1.7.`, `MIMO`, `SEZ`) |
| `validity` | `valid_from` / `valid_to` – od kdy tabulka platí (každá stanice může mít jiné datum podle poslední změny) |
| `part_a` | odchylky přestupních dob – text + `structured` (`transfer_time`, `default_transfer_times`, …) |
| `part_b` | přípoje odchylně od článku 2 – text + `structured` (`operator_connection`, `operator_basic_wait`, `min_interval`, `connection_pairs`, `default_rules`, `text`) |
| `part_c.sections[]` | sekce „vlaky směr X“ (`direction` = směr návazného, čekajícího vlaku), příznaky `basic_wait_times_only` („Platí základní čekací doby“) |
| `part_c.sections[].rules[]` | jednotlivé vazby (viz níže) |
| `footnotes` | poznámky pod tabulkou podle hvězdiček (`*`, `**`, `***`) |
| `notes` | ostatní pokyny pod tabulkou – např. mimořádné zastavení spoje, když se přípoj nestihne |
| `review` | co parser nerozpoznal úplně |

### Pravidlo (řádek tabulky části C)

```json
{
  "id": "ostrava-svinov-013",
  "source_row": 36,
  "status": "active",
  "waiting_train":    { "text": "3360", "trains": [3360], "parse": "full" },
  "connecting_train": { "text": "113",  "trains": [113],  "parse": "full" },
  "wait": {
    "workday":        { "raw": "5", "kind": "minutes", "minutes": 5 },
    "saturday":       { "raw": "5", "kind": "minutes", "minutes": 5 },
    "sunday_holiday": { "raw": "5", "kind": "minutes", "minutes": 5 }
  },
  "changes": { "workday": { "before": "10", "after": "5" } }
}
```

* `waiting_train` = **sloupec 1 – návazný vlak, který čeká** („má vlak(y) číslo“).
* `connecting_train` = **sloupec 2 – přípojný vlak, na který se čeká** („připojení od vlaku(ů)“; `V` = všechny).
* `wait` podle typu dne: `workday` ⚒, `saturday` ⑥, `sunday_holiday` † (neděle a svátky).
* `kind`: `minutes` | `never` (N) | `always` (vždy) | `always_through_coach` (přechod) | `no_connection` (–) | `empty` | `variants` | `text`.
* `status`: `active`, nebo `cancelled` – řádek je v podkladu celý přeškrtnutý (zrušená vazba, uchováno pro přehled).
* `changes`: buňky se změnou – `before` (přeškrtnuté) a `after` (platné). Platná hodnota je vždy už v hlavních polích.

Selektor vlaků může obsahovat `trains`, `ranges` (vč. sudé/liché), `patterns` (`16xx`), `categories` (Ex/R/Sp/Os), `operators`, `lines` (R8, R23…), `tracks` (trať 260), `from_directions` / `to_directions`, `except` / `include` (vnořené selektory), `except_listed_below`, `date_condition`, `footnote_marks`. Úplný popis je v `rules.json → selector_schema`.

## Co je potřeba vědět pro další krok (webová služba)

1. **Data jízdního řádu.** Tabulky neříkají druh vlaku, dopravce, směr ani časy. Pro pravidla typu „všechny vlaky Os“, „vlaky ČD“, „směr Jihlava“ i pro článek 1 (interval 2–60 min) bude služba potřebovat napojení na data JŘ (celostátní data CIS JŘ v XML).
2. **Postup vyhledání** je popsán v `rules.json → lookup_algorithm` (výběr platné varianty podle data, typ dne podle svátků, normalizace šestimístných čísel, priorita konkrétního pravidla před obecným, fallback na základní čekací dobu Ex 5 / ostatní 10 min).
3. **Ruční revize:** `data/review.json` obsahuje 107 míst – hlavně volně formulovaná pravidla části B (např. intervaly podle tratí v Brně hl.n.) a 17 prázdných buněk čekacích dob. Doslovný text je vždy zachován.

## Zdroj a omezení

Zdrojem je dokument Správy železnic označený „Jen pro služební potřebu“. Před zveřejněním dat přes veřejné API je vhodné ověřit, zda je jejich zveřejnění dovoleno.
