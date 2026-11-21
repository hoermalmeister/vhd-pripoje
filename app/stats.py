"""Návštěvnost bez cookies a bez ukládání IP adres.

Každý den (a každý měsíc) se vylosuje tajný klíč; návštěvník se rozpozná jen podle otisku
sha256(klíč + IP + prohlížeč). Klíč i otisky se drží jen po dobu daného dne/měsíce, pak se zahodí –
zpětně tedy nejde zjistit, kdo to byl, ani návštěvníka sledovat přes více dní/měsíců.
Do historie se ukládají jen součty: zobrazení stránek, unikátní návštěvníci, vyhledávání podle zdroje, API.
"""
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import threading

BOT_RE = re.compile(r"bot|crawl|spider|slurp|preview|monitor|uptime|curl|wget|python|httpx|go-http|java/|okhttp|"
                    r"headless|lighthouse|facebookexternalhit|whatsapp|telegram|discord|scrapy|axios|node-fetch", re.I)


def _prague_now():
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("Europe/Prague"))
    except Exception:
        return dt.datetime.utcnow() + dt.timedelta(hours=1)


class Stats:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.dirty = False
        self.data = {"since": None, "days": {}, "months": {}, "_cur": {}}
        try:
            with open(path, encoding="utf-8") as f:
                self.data.update(json.load(f))
        except (OSError, ValueError):
            pass
        threading.Thread(target=self._flusher, daemon=True).start()

    # ------------------------------------------------------------ interní
    def _period(self, kind, key):
        """Klíč a množina otisků pro aktuální den/měsíc (staré se zahodí)."""
        cur = self.data.setdefault("_cur", {})
        c = cur.get(kind)
        if not c or c.get("key") != key:
            c = cur[kind] = {"key": key, "salt": secrets.token_hex(16), "seen": [], "api_seen": []}
        return c

    def _bucket(self, kind, key):
        b = self.data[kind + "s"].setdefault(key, {})
        for k in ("views", "visitors", "searches", "shares", "manual", "api", "api_clients", "bots"):
            b.setdefault(k, 0)
        b.setdefault("sources", {})
        b.setdefault("pages", {})
        return b

    def _mark(self, ip, ua, field):
        """Zapíše otisk do dne i měsíce; vrátí, ve kterých obdobích je nový."""
        now = _prague_now()
        new = []
        for kind, key in (("day", now.strftime("%Y-%m-%d")), ("month", now.strftime("%Y-%m"))):
            c = self._period(kind, key)
            h = hashlib.sha256(f"{c['salt']}|{ip}|{ua}".encode()).hexdigest()[:16]
            if h not in c[field]:
                c[field].append(h)
                new.append((kind, key))
        return new

    def _inc(self, field, n=1, sub=None, subkey=None):
        now = _prague_now()
        for kind, key in (("day", now.strftime("%Y-%m-%d")), ("month", now.strftime("%Y-%m"))):
            b = self._bucket(kind, key)
            if sub:
                b[sub][subkey] = b[sub].get(subkey, 0) + n
            else:
                b[field] += n
        if not self.data.get("since"):
            self.data["since"] = now.strftime("%Y-%m-%d")
        self.dirty = True

    # ------------------------------------------------------------ veřejné
    def page_view(self, page, ip, ua):
        with self.lock:
            if BOT_RE.search(ua or "") or not ua:
                return self._inc("bots")
            self._inc("views")
            self._inc(None, sub="pages", subkey=page)
            for kind, key in self._mark(ip, ua, "seen"):
                self._bucket(kind, key)["visitors"] += 1

    def event(self, name, source=None):
        """name: searches (vyhodnocení sdíleného/vloženého spojení), shares (příchod sdílením), manual (ruční zadání)."""
        with self.lock:
            self._inc(name)
            if source:
                self._inc(None, sub="sources", subkey=source)

    def api_call(self, ip, ua):
        """Volání API mimo naše stránky (jiné aplikace, skripty)."""
        with self.lock:
            self._inc("api")
            for kind, key in self._mark(ip, ua, "api_seen"):
                self._bucket(kind, key)["api_clients"] += 1

    def summary(self):
        with self.lock:
            days = {k: dict(v) for k, v in self.data["days"].items()}
            months = {k: dict(v) for k, v in self.data["months"].items()}
        today = _prague_now().date()

        def window(n):
            keys = [(today - dt.timedelta(days=i)).isoformat() for i in range(n)]
            out = {"views": 0, "visitors": 0, "searches": 0}
            for k in keys:
                for f in out:
                    out[f] += days.get(k, {}).get(f, 0)
            return out

        total = {f: sum(d.get(f, 0) for d in days.values())
                 for f in ("views", "visitors", "searches", "shares", "manual", "api", "bots")}
        sources = {}
        for d in days.values():
            for s, n in d.get("sources", {}).items():
                sources[s] = sources.get(s, 0) + n
        return {"since": self.data.get("since"), "today": window(1), "last7": window(7), "last30": window(30),
                "total": total, "total_sources": sources,
                "days": dict(sorted(days.items(), reverse=True)), "months": dict(sorted(months.items(), reverse=True))}

    def flush(self):
        with self.lock:
            if not self.dirty:
                return
            data = json.dumps(self.data, ensure_ascii=False)
            self.dirty = False
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, self.path)
        except OSError:
            self.dirty = True

    def _flusher(self):
        import time
        while True:
            time.sleep(60)
            self.flush()
