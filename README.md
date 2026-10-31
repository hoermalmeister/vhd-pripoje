# Přípoje vlaků ČR – čekací doby (web + API)

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
3. **Ruční revize:** `data/review.json` je po ručním převodu prázdný (dříve 95 míst – volně formulovaná pravidla části B a 17 prázdných buněk čekacích dob, které jádro řeší obecnými pravidly s upozorněním). Doslovný text je vždy zachován.

## Zdroj a omezení

Zdrojem je dokument Správy železnic označený „Jen pro služební potřebu“. Před zveřejněním dat přes veřejné API je vhodné ověřit, zda je jejich zveřejnění dovoleno.

## Napojení na jízdní řád (GTFS) a vyhodnocení přípoje

Tabulky pomůcky neobsahují druh vlaku, dopravce, linku, směr ani časy. Ty se berou z GTFS celostátního jízdního řádu (obehy.cz / CIS JŘ):

```bash
# 1) z GTFS (adresář nebo zip) postavit databázi vlaků a železniční sítě (~110 MB, do gitu nepatří)
python tools/build_gtfs_db.py cesta/ke/gtfs data/trains.db

# 2) dotaz: stanice, datum, přípojný vlak (čím přijíždím), návazný vlak (na co přestupuji)
python -m engine.cli "Ostrava-Svinov" 2026-11-02 113 3360
```

Co z GTFS jádro (`engine/`) používá:

| údaj v pomůcce | odkud se bere |
|---|---|
| druh vlaku Ex / R / Sp / Os | kategorie vlaku (EC, IC, rj, RJ, LE → Ex; TLX → R; TL → Os) |
| dopravce (ČD, ARR, GWTR, RJ, …) | `agency` (převodní tabulka v `tools/build_gtfs_db.py`) |
| linka (R8, R23, S1, Ex3, …) | `route_short_name` linkových tras |
| „směr X“, „ze směru X“, „od X“ | trasa vlaku přes železniční síť sestavenou ze všech jízd – včetně stanic, kterými vlak jen projíždí; názvy ve 2. pádě („od Prahy“, „Kutné Hory“, „Žďáru n. S.“) se porovnávají přes kmeny slov; zahraniční cíle (Wien, Hof, Linz) přes hraniční stanice |
| dny jízdy, svátky | `calendar_dates` + seznam svátků v `rules.json` |
| časy, interval přestupu | `stop_times` (vč. vlaků jedoucích přes půlnoc) |
| šestimístná čísla | převod na kmenové číslo |

Postup vyhodnocení (`engine/evaluate.py`):

1. najde oba vlaky v daný den ve stanici a vybere verzi tabulky stanice platnou k datu,
2. typ dne ⚒ / ⑥ / † (svátky jako †),
3. projde pravidla části C: sekce „vlaky směr X“ se ověřuje podle trasy návazného vlaku, vyhrává nejkonkrétnější pravidlo (výčet čísel > rozsah > obecné „všechny vlaky … (vyjma níže uvedených)“),
4. bez pravidla: článek 1 (2–60 min), 2a (0–1 min, minimální interval z části B), 2f (různí dopravci – výjimky z části B), 2b/2c (opačný / stejný směr téže trati), pak základní čekací doba (Ex 5, ostatní 10, nebo doba dopravce z části B),
5. k výsledku přidá poznámky pod čarou, pokyny pod tabulkou, přestupní doby (část A) a **doslovné nestrukturované texty části B jako upozornění** – složité případy z revize se tak nemusí převádět do struktury, uživatel je uvidí u výsledku.

Zatím se neověřuje: čísla tratí („z trati 340“) – pravidlo se pak vybere s upozorněním na nejistotu.

`data/coverage_report.json` – kontrola nad jízdním řádem: z 2 971 pravidel části C se 2959 uplatní na reálné dvojice vlaků nebo jsou vyhodnotitelné přímo podle čísel. U zbylých 12 obecných řádků je uveden ověřený důvod (v JŘ 2026 jim neodpovídá žádný vlak, všechny vlaky mají vlastní konkrétnější řádek, duplicitní řádek…). Obvody stanic samostatné v GTFS jsou v `STATION_DISTRICTS`, linky vedené v pomůcce jako Ex (R15, R18) v `LINE_ALSO_DRUH` – obojí v `engine/evaluate.py`.


### Ručně převedená pravidla části A/B (`tools/partb_manual.json`)

70 textů, které nešly rozpoznat automaticky, je převedeno ručně na pravidla typu `connection_rule` (je přípoj / není přípoj), `operator_basic_wait`, `min_interval` nebo `info`. Používají stejné selektory jako část C, takže se vyhodnocují nad jízdním řádem: dopravce, linka, druh, směr („ze směru X“, „směr X“), čísla vlaků, dny (⑤, ⑥ a †…), podmínky na interval. Čísla tratí jsou převedena na směry a označena `approx` (Brno hl.n.: 240/244 = směr Střelice; Kralupy: 092/093/110/111 = Neratovice/Kladno/Slaný/Velvary; Ostrava hl.n.: 321/323 = Havířov/Frýdek-Místek). Nevyhodnocují se zatím přestupní doby podle konkrétních nástupišť.


### Výslovné řádky tabulky platí vždy

Je-li v tabulce stanice řádek s číslem návazného vlaku a číslem přípojného vlaku (nebo „V“), vrátí se jeho čekací doba **bez ohledu na jízdní řád** – i když vlak podle GTFS v daný den nejede nebo stanicí neprojíždí (výsledek nese upozornění). „V“ znamená každý vlak, který je ve stanici přípojem (garantovaný přestup dle článků 1 a 2: 2–60 min, týž dopravce nebo povoleno částí B); jsou-li oba vlaky v jízdním řádu, tyto podmínky se ověří. Jízdní řád je potřeba jen pro obecné řádky („všechny vlaky Os směr X“, dopravce, linka) a pro dvojice, které tabulka neuvádí.


## Webová služba a API

Server (`app/server.py`) používá jen standardní knihovnu Pythonu – žádné závislosti. Při startu stáhne GTFS z `GTFS_URL` (výchozí `https://motis.obehy.cz/get-feeds/cz-czptt-gtfs.zip`), postaví databázi vlaků (~20 s) a pak ji každých `REFRESH_HOURS` (24) hodin obnoví. Do té doby vrací `/api/*` kód 503, `/health` je vždy 200 (`ready: false/true`).

Lokálně:
```bash
python -m app.server                                    # stáhne GTFS z obehy.cz
GTFS_PATH=cesta/ke/gtfs.zip python -m app.server        # nebo z lokálního souboru
# http://localhost:8000
```

| endpoint | popis |
|---|---|
| `GET /` | webová stránka (stanice → datum → čím přijíždím → na co přestupuji) |
| `GET /health` | stav služby |
| `GET /api/info` | verze pomůcky a dat |
| `GET /api/stations?q=Brno` | vyhledání stanice (`in_handbook` = má vlastní tabulku) |
| `GET /api/trains?station=Ostrava-Svinov&date=2026-11-02` | příjezdy a odjezdy ve stanici v daný den |
| `GET /api/connection?station=Ostrava-Svinov&date=2026-11-02&arriving=113&departing=3360` | vyhodnocení přípoje (`arriving` = čím přijíždím, `departing` = na co přestupuji) |
| `GET /api/station-table?station=Kolín&date=2026-11-02` | tabulka stanice z pomůcky platná k datu |
| `GET /api/itinerary?text=…&date=…` | rozbor sdíleného spojení: najde čísla vlaků, dopočítá stanice přestupu a vyhodnotí každý přestup |
| `GET /share?text=…&url=…` | stránka pro sdílené spojení (cíl sdílení z Androidu) |

Výsledek `/api/connection` – pole `result`: `waits` / `waits_basic` (čeká `wait_minutes`), `always` (čeká vždy), `no_wait` (N), `no_connection` (vazba v daný den neexistuje), `not_a_connection` (není přípoj), `unspecified`, `train_not_found`, `unknown_station`; dále `message`, `warnings`, `matched_rule`, `part_b_rule`, `station_info`. CORS je povolen (`Access-Control-Allow-Origin: *`).

### Nasazení na Railway

1. railway.com → New Project → Deploy from GitHub repo → `vhd-pripoje`.
2. Railway použije `Dockerfile` a `railway.json` (healthcheck `/health`). Proměnné prostředí nejsou nutné; volitelně `GTFS_URL`, `REFRESH_HOURS`.
3. Settings → Networking → Generate Domain.
4. Volitelně připojte Volume na `/app/var` – databáze vlaků pak přežije restart a nemusí se pokaždé stahovat.


## Aplikace na Android (sdílení spojení z vyhledávače)

Webová služba je zároveň instalovatelná aplikace (PWA) s cílem sdílení (Web Share Target):

1. V telefonu otevřete adresu služby na Railway v **Chrome** → menu ⋮ → **Přidat na plochu / Instalovat aplikaci**.
2. Ve vyhledávači spojení (IDOS, Můj vlak, …) zvolte **Sdílet** → **Přípoje**.
3. Otevře se `/share` se sdíleným textem/odkazem a ukáže všechny přestupy a jejich čekací doby.

Podporované zdroje: **IDOS** (odkaz `idos.cz/…/spojeni/prehled/?p=…` – server stránku načte), **Seznam Jízdní řády / pubtran** (odkaz `seznam.cz/jizdnirady/detail/…` – vezme se jen vybrané spojení, ne další nabízená), **Můj vlak** (sdílený text, úseky oddělené prázdným řádkem) a obecný text. Úseky autobusem/tramvají apod. se poznají a přestup přes ně se zatím nevyhodnocuje. Počet přestupů není omezen.

Rozbor (`engine/itinerary.py`) jinak nezávisí na formátu vyhledávače: z textu vezme čísla vlaků v pořadí („R 881“, „Os 2803“, „EC 113“…) a datum, stanici přestupu dopočítá z jízdního řádu (dává přednost stanici zmíněné v textu). Obsahuje-li sdílený obsah jen odkaz, server zkusí stránku odkazu načíst a čísla vlaků najít v ní.
