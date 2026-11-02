#!/usr/bin/env python3
"""Henter anbud fra Doffin og lagrer treff i data/notices.json.

Bruk:
  python fetch_doffin.py            normal kjøring
  python fetch_doffin.py --debug    viser rått svar fra ett søk og lagrer ingenting

Krever miljøvariabelen DOFFIN_API_KEY. Lokalt kan den ligge i en .env-fil
(DOFFIN_API_KEY=...). På GitHub legges den inn som en Actions-secret.
"""
import argparse
import json
import os
import re
import sys
import time
import datetime as dt
from pathlib import Path

import requests

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
DATA_PATH = ROOT / "data" / "notices.json"

# Feltnavn i svaret fra Doffin. Første navn som finnes brukes.
# Hvis et felt blir tomt i dashbordet: kjør --debug og legg til riktig navn her.
ID_KEYS = ["id", "doffinId", "noticeId"]
TITLE_KEYS = ["heading", "title", "name"]
DESC_KEYS = ["description", "shortDescription", "summary"]
BUYER_KEYS = ["buyer", "buyers", "contractingAuthority", "buyerName"]
VALUE_KEYS = ["estimatedValue", "value", "totalValue"]
TYPE_KEYS = ["type", "noticeType"]
STATUS_KEYS = ["status"]
PUBLISHED_KEYS = ["publicationDate", "issueDate", "publishedDate", "published"]
DEADLINE_KEYS = ["deadline", "submissionDeadline", "tenderDeadline"]
HITS_KEYS = ["hits", "results", "notices", "items", "data"]


def load_dotenv():
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def pick(d, keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, "", []):
            return d[k]
    return default


def clean_text(value, limit=None):
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if limit and len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def buyer_name(hit):
    b = pick(hit, BUYER_KEYS)
    if isinstance(b, list):
        names = [x.get("name") if isinstance(x, dict) else str(x) for x in b]
        return ", ".join(n for n in names if n)
    if isinstance(b, dict):
        return clean_text(b.get("name", ""))
    return clean_text(b)


def parse_value(raw):
    try:
        if isinstance(raw, dict):
            amount = pick(raw, ["amount", "value"])
            currency = pick(raw, ["currencyCode", "currency"], "NOK")
            return (float(amount) if amount is not None else None), currency
        if isinstance(raw, (int, float)):
            return float(raw), "NOK"
    except (TypeError, ValueError):
        pass
    return None, "NOK"


def normalize(hit):
    nid = pick(hit, ID_KEYS)
    if not nid:
        return None
    value, currency = parse_value(pick(hit, VALUE_KEYS))
    return {
        "id": str(nid),
        "title": clean_text(pick(hit, TITLE_KEYS, ""), 300),
        "buyer": buyer_name(hit),
        "description": clean_text(pick(hit, DESC_KEYS, ""), 700),
        "type": str(pick(hit, TYPE_KEYS, "") or ""),
        "status": str(pick(hit, STATUS_KEYS, "") or ""),
        "published": str(pick(hit, PUBLISHED_KEYS, "") or ""),
        "deadline": str(pick(hit, DEADLINE_KEYS, "") or ""),
        "value": value,
        "currency": currency,
    }


def extract_hits(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in HITS_KEYS:
            if isinstance(data.get(k), list):
                return data[k]
    return []


def api_get(session, base_url, path, params):
    url = base_url.rstrip("/") + path
    for attempt in range(5):
        r = session.get(url, params=params, timeout=30)
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", "10")) + 1
            print(f"  429 fra Doffin, venter {wait} s", file=sys.stderr)
            time.sleep(min(wait, 120))
            continue
        if r.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Ga opp etter flere forsøk: {path} {params}")


def run_search(session, cfg, extra):
    api = cfg["api"]
    p = api["params"]
    results = []
    for page in range(1, api["max_pages"] + 1):
        params = {p["page"]: page, p["per_page"]: api["per_page"]}
        if api.get("status") and p.get("status"):
            params[p["status"]] = api["status"]
        params.update(extra)
        data = api_get(session, api["base_url"], api["search_path"], params)
        hits = extract_hits(data)
        results.extend(hits)
        if len(hits) < api["per_page"]:
            break
        time.sleep(api["delay_seconds"])
    return results


def compute_tier(core, secondary, keywords):
    if core and keywords:
        return 1
    if core or keywords:
        return 2
    if secondary:
        return 3
    return 0


def load_existing():
    if DATA_PATH.exists():
        try:
            return json.loads(DATA_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"updated": None, "notices": []}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    load_dotenv()
    key = os.environ.get("DOFFIN_API_KEY")
    if not key:
        sys.exit("Mangler DOFFIN_API_KEY (miljøvariabel eller .env).")

    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    session = requests.Session()
    session.headers["Ocp-Apim-Subscription-Key"] = key
    session.headers["Accept"] = "application/json"

    if args.debug:
        p = cfg["api"]["params"]
        data = api_get(
            session, cfg["api"]["base_url"], cfg["api"]["search_path"],
            {p["query"]: cfg["keywords"][0], p["page"]: 1, p["per_page"]: 3},
        )
        print(json.dumps(data, ensure_ascii=False, indent=2)[:6000])
        return

    p = cfg["api"]["params"]
    groups = [("_core", p["cpv"], cfg["cpv_core"])]
    if cfg.get("search_secondary"):
        groups.append(("_sec", p["cpv"], cfg["cpv_secondary"]))
    groups.append(("_kw", p["query"], cfg["keywords"]))

    found = {}
    queries = errors = 0
    for bucket, param_name, values in groups:
        for value in values:
            queries += 1
            try:
                hits = run_search(session, cfg, {param_name: value})
            except Exception as exc:  # noqa: BLE001
                errors += 1
                print(f"  ! Feil for {value}: {exc}", file=sys.stderr)
                continue
            print(f"  {value}: {len(hits)} treff")
            for hit in hits:
                rec = normalize(hit)
                if not rec:
                    continue
                cur = found.setdefault(
                    rec["id"], {**rec, "_core": set(), "_sec": set(), "_kw": set()}
                )
                cur[bucket].add(value)
            time.sleep(cfg["api"]["delay_seconds"])

    if queries and errors / queries > 0.5:
        sys.exit(f"For mange feil ({errors} av {queries} søk). Lagrer ingenting.")

    excludes = [x.lower() for x in cfg.get("exclude_in_title", [])]
    allowed_types = [x.upper() for x in cfg.get("include_types", [])]
    keywords = [k.lower() for k in cfg["keywords"]]
    today = dt.date.today().isoformat()
    old = {n["id"]: n for n in load_existing().get("notices", [])}
    seen_now = set()

    for nid, rec in found.items():
        title_l = rec["title"].lower()
        text_l = f"{title_l} {rec['description'].lower()}"
        if any(x in title_l for x in excludes):
            continue
        if allowed_types and rec["type"].upper() not in allowed_types:
            continue
        kw_hits = set(rec.pop("_kw")) | {k for k in keywords if k in text_l}
        core, sec = rec.pop("_core"), rec.pop("_sec")
        tier = compute_tier(core, sec, kw_hits)
        if tier == 0:
            continue
        prev = old.get(nid)
        rec.update(
            tier=tier,
            matched_cpv=sorted(core | sec),
            matched_keywords=sorted(kw_hits),
            first_seen=prev["first_seen"] if prev else today,
            last_seen=today,
            still_listed=True,
        )
        old[nid] = rec
        seen_now.add(nid)

    if errors == 0:
        for nid, n in old.items():
            if nid not in seen_now:
                n["still_listed"] = False

    cutoff_seen = (dt.date.today() - dt.timedelta(days=45)).isoformat()
    cutoff_deadline = (dt.date.today() - dt.timedelta(days=60)).isoformat()
    kept = [
        n for n in old.values()
        if n.get("still_listed", True)
        or n.get("last_seen", today) >= cutoff_seen
        if not n.get("deadline") or n["deadline"][:10] >= cutoff_deadline
    ]
    kept.sort(key=lambda n: n["id"])

    out = {
        "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "notices": kept,
    }
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    new_today = sum(1 for n in kept if n.get("first_seen") == today)
    tier1 = sum(1 for n in kept if n.get("tier") == 1 and n.get("still_listed", True))
    print(f"Ferdig: {len(kept)} anbud lagret, {new_today} nye i dag, {tier1} aktive i Tier 1. "
          f"Søk: {queries}, feil: {errors}.")


if __name__ == "__main__":
    main()
