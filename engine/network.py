"""Železniční síť z GTFS: trasy vlaků včetně projížděných stanic a vyhodnocení směrů ('směr X', 'ze směru X', 'od X')."""
import heapq, re, unicodedata
from collections import defaultdict
from functools import lru_cache


def strip_diacritics(s):
    s = s.replace("ł", "l").replace("Ł", "L").replace("ß", "ss")
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


# směry mimo síť v GTFS -> hraniční stanice (vybere se ta nejbližší po síti)
FOREIGN_ALIASES = {
    "wien": ["Bernhardsthal", "Gmünd NÖ", "Retz"], "vidne": ["Bernhardsthal", "Gmünd NÖ", "Retz"],
    "hof": ["Selb-Plößberg"], "linz": ["Summerau"], "nurnberg": ["Schirnding", "Furth im Wald"],
    "dresden": ["Bad Schandau"], "drazdany": ["Bad Schandau"], "zilina": ["Čadca"], "bratislava": ["Kúty"],
}


ABBREV = [
    (r"\bhl\.\s*n\.?", "hlavni nadrazi"), (r"\bh\.\s*n\.", "hlavni nadrazi"), (r"\bn\.\s*l\.", "nad labem"),
    (r"\bn\.\s*", "nad "), (r"\bp\.\s*", "pod "), (r"\bu\.\s*", "u "), (r"\bmest\.", "mesto"),
    (r"\bzast\.", ""), (r"\bzst\b", ""),
]
STOPWORDS = {"a", "i", "nebo", "vlaky", "vlak", "od", "ze", "z", "do", "smer", "smeru", "ve", "v", "na", "obvod"}


def name_words(s):
    s = strip_diacritics(s)
    for pat, rep in ABBREV:
        s = re.sub(pat, rep + " ", s)
    s = re.sub(r"[()\-–,./]", " ", s)
    return [w for w in s.split() if w and w not in STOPWORDS]


SUFFIXES = ["ovi", "ech", "ami", "eho", "emu", "ou", "em", "ich", "ym", "y", "u", "a", "o", "e", "i"]


def stems(w):
    """Možné kmeny slova (bez diakritiky): Prahy/Praha -> prah, Hradce/Hradec -> hradc, Plzně/Plzeň -> plzn."""
    out = {w}
    for suf in SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            out.add(w[: -len(suf)])
            break
    for x in list(out):
        if len(x) >= 5 and x[-2] == "e" and x[-1] in "ckn":  # pohyblivé e: hradec->hradc, mistek->mistk, plzen->plzn, buben->bubn
            out.add(x[:-2] + x[-1])
    return out


def _word_match(token, word):
    if token == word:
        return 3
    if len(token) < 3 or len(word) < 3:
        return 0
    return 2 if stems(token) & stems(word) else 0


def name_score(token_words, station_words):
    """Kolik slov tokenu odpovídá slovům názvu stanice (v pořadí). Vrací (shod, -přebytek)."""
    if not token_words:
        return None
    # první slovo směru musí odpovídat prvnímu slovu názvu stanice (Prahy -> Praha…, ne Benešov u Prahy)
    if not _word_match(token_words[0], station_words[0]):
        return None
    i = 0
    score = 0
    for tw in token_words:
        found = False
        while i < len(station_words):
            m = _word_match(tw, station_words[i])
            i += 1
            if m:
                score += m
                found = True
                break
        if not found:
            return None
    extra = len(station_words) - len(token_words)
    return (score, -extra)


class Network:
    def __init__(self, db):
        self.db = db
        self.adj = defaultdict(dict)
        for a, b, km in db.execute("select a, b, km from edges"):
            self.adj[a][b] = km
            self.adj[b][a] = km
        self.names = {}
        self.words = {}
        for sid, name in db.execute("select id, name from stations"):
            self.names[sid] = name
            self.words[sid] = name_words(name)

    # ------------------------------------------------------------ cesty
    @lru_cache(maxsize=200000)
    def shortest_path(self, a, b, limit_km=2000.0):
        if a == b:
            return (a,)
        dist = {a: 0.0}
        prev = {}
        pq = [(0.0, a)]
        while pq:
            d, u = heapq.heappop(pq)
            if u == b:
                break
            if d > dist.get(u, 1e18) or d > limit_km:
                continue
            for v, w in self.adj[u].items():
                nd = d + w
                if nd < dist.get(v, 1e18):
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(pq, (nd, v))
        if b not in dist:
            return None
        path = [b]
        while path[-1] != a:
            path.append(prev[path[-1]])
        return tuple(reversed(path))

    def _sp(self, a, b, banned):
        dist = {a: 0.0}; prev = {}; pq = [(0.0, a)]
        while pq:
            d, u = heapq.heappop(pq)
            if u == b:
                break
            if d > dist.get(u, 1e18):
                continue
            for v, w in self.adj[u].items():
                if (u, v) in banned or (v, u) in banned:
                    continue
                nd = d + w
                if nd < dist.get(v, 1e18):
                    dist[v] = nd; prev[v] = u; heapq.heappush(pq, (nd, v))
        if b not in dist:
            return None, None
        path = [b]
        while path[-1] != a:
            path.append(prev[path[-1]])
        return tuple(reversed(path)), dist[b]

    @lru_cache(maxsize=200000)
    def segment_path(self, a, b, km):
        """Cesta mezi dvěma po sobě jdoucími zastaveními, jejíž délka nejlépe odpovídá kilometráži vlaku (km);
        bez km = nejkratší cesta. Řeší úseky, kde vede více tratí (např. Beroun – Praha přes Řevnice / přes Nučice)."""
        p = self.shortest_path(a, b)
        if p is None or km is None or len(p) < 2:
            return p
        plen = sum(self.adj[x][y] for x, y in zip(p, p[1:]))
        if abs(plen - km) <= max(1.0, 0.04 * km):
            return p
        best, bestd = p, abs(plen - km)
        for x, y in zip(p, p[1:]):
            q, ql = self._sp(a, b, {(x, y)})
            if q and abs(ql - km) < bestd:
                best, bestd = q, abs(ql - km)
        return best

    def expand(self, stations, kms=None):
        """Seznam zastavení vlaku -> úplná trasa včetně projížděných stanic. kms[i] = km mezi stanicí i a i+1 (nebo None)."""
        out = [stations[0]]
        for i, (a, b) in enumerate(zip(stations, stations[1:])):
            km = kms[i] if kms else None
            p = self.segment_path(a, b, round(km, 1) if km else None)
            if p is None:
                out.append(b)
            else:
                out.extend(p[1:])
        return out

    # ------------------------------------------------------------ směry
    @lru_cache(maxsize=50000)
    def resolve_place(self, token, from_station):
        """Text směru ('Prahy', 'Kutné Hory', 'Ostrava hl.n.') -> id nejbližší odpovídající stanice v síti."""
        tw = tuple(name_words(token.replace("*", "")))
        if not tw:
            return None
        alias = FOREIGN_ALIASES.get(tw[0])
        if alias:
            ids = [sid for sid, n in self.names.items() if n in alias]
            ids.sort(key=lambda x: self._km(from_station, x))
            return ids[0] if ids else None
        best = []
        for sid, sw in self.words.items():
            sc = name_score(tw, sw)
            if sc:
                best.append((sc, sid))
        if not best:
            return None
        top = max(b[0] for b in best)
        cands = [sid for sc, sid in best if sc == top]
        # mezi stejně dobrými názvy (Praha hl.n., Praha-Smíchov…) vezmi nejbližší po síti; preferuj 'hlavní nádraží'
        def key(sid):
            p = self.shortest_path(from_station, sid)
            km = sum(self.adj[x][y] for x, y in zip(p, p[1:])) if p else 1e9
            return (0 if "hlavni nadrazi" in " ".join(self.words[sid]) else 1, km)
        if len(cands) > 1:
            cands.sort(key=lambda s: (key(s)[1], key(s)[0]))
        return cands[0]

    @lru_cache(maxsize=50000)
    def resolve_all(self, token):
        """Všechny stanice odpovídající názvu (např. 'Praha' -> všechny pražské stanice)."""
        tw = tuple(name_words(token.replace("*", "")))
        if not tw:
            return frozenset()
        alias = FOREIGN_ALIASES.get(tw[0])
        if alias:
            return frozenset(sid for sid, n in self.names.items() if n in alias)
        scored = [(name_score(tw, sw), sid) for sid, sw in self.words.items()]
        scored = [(sc[0], sid) for sc, sid in scored if sc]
        if not scored:
            return frozenset()
        top = max(x[0] for x in scored)
        return frozenset(sid for sc, sid in scored if sc == top)

    def _km(self, a, b):
        p = self.shortest_path(a, b)
        return sum(self.adj[x][y] for x, y in zip(p, p[1:])) if p else 1e9

    @staticmethod
    def split_places(token):
        """'Havířov – Český Těšín', 'Bludov / Zábřeh na Moravě', 'Ústí nad Labem, Most' -> jednotlivé názvy."""
        parts = re.split(r"\s+[–-]\s+|\s*/\s*|,\s*|\s+a\s+(?=[A-ZČŘŠŽÚŚ])|\s+nebo\s+|\s+i\s+", token)
        return [p.strip(" ()*") for p in parts if p.strip(" ()*–-")]

    def heads_towards(self, station, onward, place_id):
        """Vede trasa vlaku za stanicí (onward = [station, ...]) směrem k place_id?"""
        if place_id is None or len(onward) < 2:
            return False
        if place_id in onward[1:]:
            return True
        p = self.shortest_path(station, place_id)
        if not p or len(p) < 2:
            return False
        # vlak končí dřív, ale jede po cestě k cíli směru
        return onward[1] == p[1] and onward[-1] in p

    def direction_match(self, station, onward, token):
        """Vrací (shoda, [vyřešené stanice]). Shoda, když trasa vede k některé z uvedených míst."""
        whole = self.resolve_place(token, station)
        toks = [token] if whole else self.split_places(token)
        pids, hit = [], False
        for t in toks:
            p = self.resolve_place(t, station)
            if not p or p == station:
                continue
            pids.append(p)
            allc = self.resolve_all(t) - {station}
            if onward and len(onward) > 1 and allc & set(onward[1:]):
                hit = True
            elif self.heads_towards(station, onward, p):
                hit = True
        if not pids:
            return None, []
        return hit, pids
