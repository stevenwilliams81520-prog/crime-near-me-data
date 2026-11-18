#!/usr/bin/env python3
"""
Crime Near Me - historical data pack builder.

Downloads data.police.uk monthly archives, splits street-level crimes by police force,
and writes one compact SQLite file per force (raw-deflate compressed) plus manifest.json.

Environment variables:
  FORCES        comma-separated force slugs to include (blank = all), e.g. "north-wales"
  ARCHIVES      comma-separated archive months to use, e.g. "2014-12,2016-12" (blank = automatic)
  RELEASE_BASE  URL prefix where the files will be published (GitHub release download URL)
  VERSION       version label written into the manifest
  ARCHIVE_DIR   optional folder of already-downloaded archive zips (for testing)

Data: Contains public sector information licensed under the Open Government Licence v3.0.
"""
import csv
import datetime
import hashlib
import io
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.request
import zipfile
import zlib

ARCHIVE_URL = "https://data.police.uk/data/archive/{}.zip"
WORK = "work"
OUT = "out"

NAME_TO_SLUG = {
    "Anti-social behaviour": "anti-social-behaviour",
    "Bicycle theft": "bicycle-theft",
    "Burglary": "burglary",
    "Criminal damage and arson": "criminal-damage-arson",
    "Drugs": "drugs",
    "Other crime": "other-crime",
    "Other theft": "other-theft",
    "Possession of weapons": "possession-of-weapons",
    "Public order": "public-order",
    "Robbery": "robbery",
    "Shoplifting": "shoplifting",
    "Theft from the person": "theft-from-the-person",
    "Vehicle crime": "vehicle-crime",
    "Violence and sexual offences": "violent-crime",
    # Older category names (pre-2013)
    "Violent crime": "violent-crime",
    "Public disorder and weapons": "public-order",
}
SLUGS = [
    "violent-crime", "robbery", "possession-of-weapons", "burglary", "vehicle-crime",
    "theft-from-the-person", "other-theft", "shoplifting", "bicycle-theft",
    "criminal-damage-arson", "public-order", "anti-social-behaviour", "drugs", "other-crime",
]
SLUG_ID = {s: i for i, s in enumerate(SLUGS)}

ENTRY_RE = re.compile(r"(?:^|/)(\d{4})-(\d{2})-(.+)-street\.csv$")


def log(*a):
    print(*a, flush=True)


def archive_list():
    env = os.environ.get("ARCHIVES", "").strip()
    if env:
        return [a.strip() for a in env.split(",") if a.strip()]
    # Each archive holds roughly the 36 months up to its date; every 2 years gives overlap.
    this_year = datetime.date.today().year
    return [f"{y}-12" for y in range(2012, this_year - 1, 2)]


def fetch_archive(month):
    local_dir = os.environ.get("ARCHIVE_DIR")
    if local_dir:
        p = os.path.join(local_dir, f"{month}.zip")
        return (p, False) if os.path.exists(p) else (None, False)

    url = ARCHIVE_URL.format(month)
    dest = os.path.join(WORK, f"archive-{month}.zip")
    log(f"Downloading {url}")
    try:
        with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as f:
            done, step = 0, 200 * 1024 * 1024
            nxt = step
            while True:
                chunk = r.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if done >= nxt:
                    log(f"  {done // (1024 * 1024)} MB")
                    nxt += step
    except urllib.error.HTTPError as e:
        log(f"  skipped ({e.code})")
        if os.path.exists(dest):
            os.remove(dest)
        return None, False
    return dest, True


class Pack:
    def __init__(self, slug):
        self.slug = slug
        self.path = os.path.join(WORK, f"{slug}.sqlite")
        if os.path.exists(self.path):
            os.remove(self.path)
        self.db = sqlite3.connect(self.path)
        c = self.db
        c.execute("PRAGMA journal_mode=OFF")
        c.execute("PRAGMA synchronous=OFF")
        c.execute("CREATE TABLE crimes(month INTEGER, cat INTEGER, lat INTEGER, lng INTEGER, street INTEGER, outcome INTEGER)")
        c.execute("CREATE TABLE streets(id INTEGER PRIMARY KEY, name TEXT)")
        c.execute("CREATE TABLE outcomes(id INTEGER PRIMARY KEY, name TEXT)")
        c.execute("CREATE TABLE cats(id INTEGER PRIMARY KEY, slug TEXT)")
        c.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
        c.executemany("INSERT INTO cats VALUES(?,?)", [(i, s) for i, s in enumerate(SLUGS)])
        self.streets, self.outcomes = {}, {}
        self.rows = []
        self.count = 0
        self.months = set()
        self.name = None
        self.bbox = [10**9, -10**9, 10**9, -10**9]  # minlat, maxlat, minlng, maxlng

    def _id(self, table, cache, value):
        if not value:
            return None
        i = cache.get(value)
        if i is None:
            i = len(cache) + 1
            cache[value] = i
            self.db.execute(f"INSERT INTO {table} VALUES(?,?)", (i, value))
        return i

    def add(self, row):
        lat_s, lng_s = row.get("Latitude"), row.get("Longitude")
        if not lat_s or not lng_s:
            return
        try:
            lat = round(float(lat_s) * 1e5)
            lng = round(float(lng_s) * 1e5)
            m = row["Month"]
            month = int(m[:4]) * 100 + int(m[5:7])
        except (ValueError, KeyError):
            return
        if self.name is None:
            self.name = (row.get("Falls within") or row.get("Reported by") or self.slug).strip()
        cat = SLUG_ID[NAME_TO_SLUG.get((row.get("Crime type") or "").strip(), "other-crime")]
        street = self._id("streets", self.streets, (row.get("Location") or "").strip())
        outcome = self._id("outcomes", self.outcomes, (row.get("Last outcome category") or "").strip())
        self.rows.append((month, cat, lat, lng, street, outcome))
        self.months.add(month)
        b = self.bbox
        if lat < b[0]: b[0] = lat
        if lat > b[1]: b[1] = lat
        if lng < b[2]: b[2] = lng
        if lng > b[3]: b[3] = lng
        if len(self.rows) >= 50000:
            self.flush()

    def flush(self):
        if self.rows:
            self.db.executemany("INSERT INTO crimes VALUES(?,?,?,?,?,?)", self.rows)
            self.count += len(self.rows)
            self.rows = []

    def finish(self, release_base, version):
        self.flush()
        c = self.db
        log(f"Indexing {self.slug} ({self.count:,} crimes)")
        c.execute("CREATE INDEX idx_month_lat ON crimes(month, lat)")
        c.execute("CREATE INDEX idx_lat ON crimes(lat)")
        months = sorted(self.months)
        fmt = lambda m: f"{m // 100:04d}-{m % 100:02d}"
        meta = {
            "force": self.slug,
            "name": self.name or self.slug,
            "from": fmt(months[0]),
            "to": fmt(months[-1]),
            "crimes": str(self.count),
            "months": ",".join(fmt(m) for m in months),
            "minlat": str(self.bbox[0]), "maxlat": str(self.bbox[1]),
            "minlng": str(self.bbox[2]), "maxlng": str(self.bbox[3]),
            "version": version,
        }
        c.executemany("INSERT INTO meta VALUES(?,?)", list(meta.items()))
        c.commit()
        c.execute("ANALYZE")
        c.commit()
        c.execute("VACUUM")
        c.close()

        out_name = f"{self.slug}.sqlite.deflate"
        out_path = os.path.join(OUT, out_name)
        sha = hashlib.sha256()
        comp = zlib.compressobj(9, zlib.DEFLATED, -15)  # raw deflate (Apple's COMPRESSION_ZLIB)
        with open(self.path, "rb") as src, open(out_path, "wb") as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                data = comp.compress(chunk)
                dst.write(data)
                sha.update(data)
            data = comp.flush()
            dst.write(data)
            sha.update(data)
        size = os.path.getsize(out_path)
        os.remove(self.path)
        log(f"  {out_name}: {size / 1e6:.1f} MB")
        return {
            "force": self.slug,
            "name": meta["name"],
            "from": meta["from"],
            "to": meta["to"],
            "crimes": self.count,
            "bytes": size,
            "sha256": sha.hexdigest(),
            "url": f"{release_base.rstrip('/')}/{out_name}",
            "version": version,
        }


def main():
    os.makedirs(WORK, exist_ok=True)
    os.makedirs(OUT, exist_ok=True)
    forces = {f.strip() for f in os.environ.get("FORCES", "").split(",") if f.strip()}
    release_base = os.environ.get("RELEASE_BASE", "https://example.com")
    version = os.environ.get("VERSION", datetime.date.today().isoformat())

    packs, seen = {}, set()
    for month in archive_list():
        path, downloaded = fetch_archive(month)
        if not path:
            continue
        log(f"Reading archive {month}")
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                m = ENTRY_RE.search(info.filename)
                if not m:
                    continue
                key = os.path.basename(info.filename)
                if key in seen:
                    continue
                seen.add(key)
                slug = m.group(3)
                if forces and slug not in forces:
                    continue
                pack = packs.get(slug)
                if pack is None:
                    pack = packs[slug] = Pack(slug)
                with z.open(info) as f:
                    text = io.TextIOWrapper(f, encoding="utf-8-sig", errors="replace", newline="")
                    for row in csv.DictReader(text):
                        pack.add(row)
        if downloaded:
            os.remove(path)

    if not packs:
        log("No data found.")
        sys.exit(1)

    entries = [p.finish(release_base, version) for p in packs.values()]
    entries.sort(key=lambda e: e["name"])
    manifest = {
        "version": version,
        "updated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "attribution": "Contains public sector information licensed under the Open Government Licence v3.0.",
        "packs": entries,
    }
    with open(os.path.join(OUT, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1)
    log(f"Done: {len(entries)} packs")


if __name__ == "__main__":
    main()
