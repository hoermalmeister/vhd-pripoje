"""Spojení podle Google (oficiální Routes API) pro sdílené odkazy z Google Maps.

Odkaz z Google Maps obsahuje jen start, cíl a případně čas. Routes API vrátí tytéž varianty, jaké ukazuje
Google Maps (včetně autobusů), i s čísly vlaků. Potřebuje klíč v proměnné prostředí GOOGLE_MAPS_API_KEY
(Google Cloud → APIs & Services → Routes API). Bez klíče se spojení dohledá ve vlastním jízdním řádu.
"""
import datetime as dt
import json
import urllib.error
import os
import re
import urllib.request

API_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
FIELD_MASK = "routes.legs.steps.travelMode,routes.legs.steps.transitDetails"
RAIL_TYPES = {"RAIL", "HEAVY_RAIL", "COMMUTER_TRAIN", "HIGH_SPEED_TRAIN", "LONG_DISTANCE_TRAIN", "TRAIN"}
MODE_BY_VEHICLE = {"BUS": "bus", "INTERCITY_BUS": "bus", "TROLLEYBUS": "bus", "SHARE_TAXI": "bus", "TRAM": "tram",
                   "LIGHT_RAIL": "tram", "SUBWAY": "metro", "METRO_RAIL": "metro", "MONORAIL": "metro", "FERRY": "ship",
                   "CABLE_CAR": "cable", "GONDOLA_LIFT": "cable", "FUNICULAR": "cable"}


def api_key():
    return os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()


def _prague(dt_utc):
    try:
        from zoneinfo import ZoneInfo
        return dt_utc.astimezone(ZoneInfo("Europe/Prague")).replace(tzinfo=None)
    except Exception:
        return (dt_utc + dt.timedelta(hours=1)).replace(tzinfo=None)


def _to_utc(local_naive):
    try:
        from zoneinfo import ZoneInfo
        return local_naive.replace(tzinfo=ZoneInfo("Europe/Prague")).astimezone(dt.timezone.utc)
    except Exception:
        return (local_naive - dt.timedelta(hours=1)).replace(tzinfo=dt.timezone.utc)


def _waypoint(p):
    if p.get("lat") is not None:
        return {"location": {"latLng": {"latitude": p["lat"], "longitude": p["lon"]}}}
    return {"address": p.get("name", "")}


def _parse_ts(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _train(td, parse_trains, cats):
    """Kategorie a číslo vlaku z odpovědi: tripShortText („R 324“ / „324“), jinak z názvu linky."""
    trip = (td.get("tripShortText") or "").strip()
    line = td.get("transitLine") or {}
    short = (line.get("nameShort") or "").strip()
    found = parse_trains(trip) or parse_trains(f"{short} {trip}")
    if found:
        return found[0]
    m = re.fullmatch(r"(\d{1,6})", trip)
    if m:
        letters = re.match(r"[A-Za-z]+", short)
        cat = letters.group(0) if letters and re.fullmatch(cats, letters.group(0)) else "Os"
        return cat, int(m.group(1))
    return None


def options(points, when, arrive_by, now, parse_trains, cats, option_text, timeout=12):
    """Varianty spojení od Googlu ve formátu nabídky aplikace. Vrací (options, chyba)."""
    key = api_key()
    if not key:
        return None, None
    body = {"origin": _waypoint(points[0]), "destination": _waypoint(points[-1]), "travelMode": "TRANSIT",
            "computeAlternativeRoutes": True, "languageCode": "cs", "regionCode": "CZ"}
    t = when or (now - dt.timedelta(minutes=15))
    body["arrivalTime" if (arrive_by and when) else "departureTime"] = _to_utc(t).strftime("%Y-%m-%dT%H:%M:%SZ")
    req = urllib.request.Request(API_URL, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-Goog-Api-Key": key,
                                          "X-Goog-FieldMask": FIELD_MASK})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode("utf-8")).get("error", {}).get("message", "")
        except Exception:
            pass
        return [], f"Google Routes API odmítlo dotaz ({e.code}{': ' + detail[:120] if detail else ''})."
    except Exception as e:
        return [], f"Google Routes API neodpovědělo ({type(e).__name__})."
    out = []
    for route in data.get("routes", []):
        legs, first_dep = [], None
        for leg in route.get("legs", []):
            for step in leg.get("steps", []):
                td = step.get("transitDetails")
                if not td:
                    continue
                sd = td.get("stopDetails") or {}
                dep_utc, arr_utc = _parse_ts(sd.get("departureTime")), _parse_ts(sd.get("arrivalTime"))
                dep_l = _prague(dep_utc) if dep_utc else None
                arr_l = _prague(arr_utc) if arr_utc else None
                first_dep = first_dep or dep_l
                vtype = ((td.get("transitLine") or {}).get("vehicle") or {}).get("type", "")
                item = {"from": (sd.get("departureStop") or {}).get("name", ""), "to": (sd.get("arrivalStop") or {}).get("name", ""),
                        "dep": f"{dep_l.hour}:{dep_l.minute:02d}" if dep_l else "", "arr": f"{arr_l.hour}:{arr_l.minute:02d}" if arr_l else ""}
                tr = _train(td, parse_trains, cats) if vtype in RAIL_TYPES or not vtype else None
                if tr:
                    item.update(category=tr[0], number=tr[1], bus=False)
                else:
                    line = td.get("transitLine") or {}
                    item.update(category=None, number=None, bus=True, mode=MODE_BY_VEHICLE.get(vtype, "bus"),
                                label=(line.get("nameShort") or line.get("name") or "")[:30])
                legs.append(item)
        if not legs:
            continue
        o = {"dep": legs[0]["dep"], "arr": legs[-1]["arr"], "legs": legs,
             "date": (first_dep.date() if first_dep else now.date()).isoformat()}
        o["text"] = option_text(o)
        out.append(o)
    out.sort(key=lambda o: (o["date"], tuple(int(x) for x in o["dep"].split(":")) if o["dep"] else (0, 0)))
    return out, None if out else "Google nenašel spojení hromadnou dopravou."
