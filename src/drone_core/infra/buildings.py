"""Building footprints + heights from OpenStreetMap (Overpass API).

Used to render city buildings as extruded 3D boxes on the map. We fetch the
building outlines in a bounding box and resolve each one's height with a
cascade:

    1. ``height`` tag (metres)                — most reliable when present
    2. ``building:levels`` × storey height    — common fallback
    3. default height                         — when nothing is tagged

Hardened like the terrain service: on-disk cache (keyed by a snapped bbox
tile so nearby views reuse it), request throttling, retry with backoff across
mirror endpoints, and a hard cap on bbox size so a zoomed-out view can never
fire a giant Overpass query.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("buildings")

# Public Overpass mirrors — tried in order on failure.
_OVERPASS_MIRRORS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)
_STOREY_M = 3.0          # assumed metres per floor when only levels are known
_DEFAULT_HEIGHT_M = 8.0  # last-resort height for untagged buildings
_MIN_INTERVAL_S = 1.1    # be polite to public Overpass
_MAX_RETRIES = 3
_TILE_DEG = 0.01         # cache granularity (~1.1 km) — snap bbox to this grid
_MAX_BBOX_DEG = 0.08     # reject requests larger than this (~9 km) — zoom in first
_CACHE_MAX_TILES = 600   # LRU cap on cached tiles — bounds RAM + disk JSON
_DATA_DIR = Path(__file__).resolve().parents[3] / "data"


@dataclass
class Building:
    """One building outline ready for extrusion.

    outline  — outer ring as [[lat, lon], ...] (not closed).
    height_m — resolved height in metres.
    source   — where height came from: "tag" | "levels" | "default".
    """
    outline: List[Tuple[float, float]]
    height_m: float
    source: str


@dataclass
class BuildingResult:
    buildings: List[Building] = field(default_factory=list)
    cached: bool = False
    ok: bool = True
    error: Optional[str] = None


def _parse_height(tags: Dict[str, str]) -> Tuple[float, str]:
    """Resolve a building height (m) + which rule produced it."""
    raw = tags.get("height")
    if raw:
        # "12", "12 m", "12m", "12.5", "39'" (feet) — handle the common cases.
        s = raw.strip().lower().replace(",", ".")
        try:
            if s.endswith("'") or s.endswith("ft"):
                feet = float(s.rstrip("ft'").strip())
                return feet * 0.3048, "tag"
            s = s.replace("m", "").strip()
            val = float(s.split()[0])
            if val > 0:
                return val, "tag"
        except (ValueError, IndexError):
            pass
    levels = tags.get("building:levels") or tags.get("levels")
    if levels:
        try:
            n = float(str(levels).replace(",", ".").split(";")[0])
            if n > 0:
                return n * _STOREY_M, "levels"
        except ValueError:
            pass
    return _DEFAULT_HEIGHT_M, "default"


class BuildingService:
    def __init__(self, timeout_s: float = 30.0, cache_file: str = "buildings_cache.json") -> None:
        # cache key = snapped tile string -> list of serialised buildings (LRU)
        self._cache: "OrderedDict[str, List[dict]]" = OrderedDict()
        self._timeout = timeout_s
        self._cache_path = _DATA_DIR / cache_file
        self._net_lock = asyncio.Lock()
        self._last_fetch = 0.0
        self._dirty = False
        self._load_disk_cache()

    def _cache_put(self, key: str, tile: List[dict]) -> None:
        """Insert with LRU eviction so the tile cache stays bounded."""
        self._cache[key] = tile
        self._cache.move_to_end(key)
        self._dirty = True
        while len(self._cache) > _CACHE_MAX_TILES:
            self._cache.popitem(last=False)

    # --- disk cache ---------------------------------------------------------
    def _load_disk_cache(self) -> None:
        try:
            if self._cache_path.exists():
                self._cache = OrderedDict(json.loads(self._cache_path.read_text()))
                while len(self._cache) > _CACHE_MAX_TILES:
                    self._cache.popitem(last=False)
                log.info("[BUILDINGS] loaded %d cached tiles", len(self._cache))
        except Exception as exc:
            log.warning("[BUILDINGS] disk cache load failed: %s", exc)
            self._cache = OrderedDict()

    def _save_disk_cache(self) -> None:
        if not self._dirty:
            return
        try:
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._cache))
            os.replace(tmp, self._cache_path)
            self._dirty = False
        except Exception as exc:
            log.warning("[BUILDINGS] disk cache save failed: %s", exc)

    @staticmethod
    def _tile_key(s: float, w: float, n: float, e: float) -> str:
        """Snap bbox out to the cache grid so neighbouring views share tiles."""
        import math
        ss = math.floor(s / _TILE_DEG) * _TILE_DEG
        ww = math.floor(w / _TILE_DEG) * _TILE_DEG
        nn = math.ceil(n / _TILE_DEG) * _TILE_DEG
        ee = math.ceil(e / _TILE_DEG) * _TILE_DEG
        return f"{ss:.3f},{ww:.3f},{nn:.3f},{ee:.3f}"

    # --- network ------------------------------------------------------------
    def _fetch_sync(self, query: str) -> dict:
        last_exc: Optional[Exception] = None
        for url in _OVERPASS_MIRRORS:
            try:
                data = urllib.parse.urlencode({"data": query}).encode()
                req = urllib.request.Request(url, data=data,
                                             headers={"User-Agent": "skybite-drone/1.0"})
                with urllib.request.urlopen(req, timeout=int(self._timeout)) as r:
                    return json.loads(r.read().decode())
            except Exception as exc:  # try next mirror
                last_exc = exc
                log.warning("[BUILDINGS] mirror failed (%s): %s", url.split("/")[2], exc)
        raise last_exc if last_exc else RuntimeError("no overpass mirror")

    @staticmethod
    def _build_query(s: float, w: float, n: float, e: float) -> str:
        bbox = f"{s},{w},{n},{e}"
        return (
            f"[out:json][timeout:25];"
            f'(way["building"]({bbox});'
            f'relation["building"]["type"="multipolygon"]({bbox}););'
            f"out body geom;"
        )

    @staticmethod
    def _parse_elements(data: dict) -> List[Building]:
        out: List[Building] = []
        for el in data.get("elements", []):
            tags = el.get("tags") or {}
            geom = el.get("geometry")
            ring: List[Tuple[float, float]] = []
            if geom:  # way with inline geometry
                ring = [(p["lat"], p["lon"]) for p in geom if "lat" in p and "lon" in p]
            elif el.get("type") == "relation":
                # take the first outer way of a multipolygon as the footprint
                for m in el.get("members", []):
                    if m.get("role") == "outer" and m.get("geometry"):
                        ring = [(p["lat"], p["lon"]) for p in m["geometry"]]
                        break
            if len(ring) < 3:
                continue
            if ring[0] == ring[-1]:
                ring = ring[:-1]  # drop closing dup
            if len(ring) < 3:
                continue
            h, src = _parse_height(tags)
            out.append(Building(outline=ring, height_m=h, source=src))
        return out

    async def _throttled_fetch(self, query: str) -> Optional[dict]:
        loop = asyncio.get_running_loop()
        async with self._net_lock:
            for attempt in range(1, _MAX_RETRIES + 1):
                wait = _MIN_INTERVAL_S - (time.monotonic() - self._last_fetch)
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    data = await loop.run_in_executor(None, self._fetch_sync, query)
                    self._last_fetch = time.monotonic()
                    return data
                except Exception as exc:
                    self._last_fetch = time.monotonic()
                    log.warning("[BUILDINGS] fetch attempt %d/%d failed: %s",
                                attempt, _MAX_RETRIES, exc)
                    if attempt == _MAX_RETRIES:
                        return None
                    await asyncio.sleep(1.5 * attempt)
        return None

    # --- public -------------------------------------------------------------
    async def get_buildings(self, s: float, w: float, n: float, e: float) -> BuildingResult:
        """Buildings within bbox (south, west, north, east). Cached per tile."""
        if n <= s or e <= w:
            return BuildingResult(ok=False, error="bad_bbox")
        if (n - s) > _MAX_BBOX_DEG or (e - w) > _MAX_BBOX_DEG:
            return BuildingResult(ok=False, error="bbox_too_large")

        key = self._tile_key(s, w, n, e)
        if key in self._cache:
            self._cache.move_to_end(key)   # LRU touch
            raw = self._cache[key]
            buildings = [Building(outline=[tuple(p) for p in b["outline"]],
                                  height_m=b["height_m"], source=b["source"]) for b in raw]
            return BuildingResult(buildings=buildings, cached=True)

        # Fetch the snapped tile (slightly larger than the request) so the cache
        # entry is reusable for nearby pans.
        ts, tw, tn, te = (float(x) for x in key.split(","))
        query = self._build_query(ts, tw, tn, te)
        data = await self._throttled_fetch(query)
        if data is None:
            return BuildingResult(ok=False, error="overpass_unavailable")

        buildings = self._parse_elements(data)
        self._cache_put(key, [
            {"outline": [list(p) for p in b.outline], "height_m": b.height_m, "source": b.source}
            for b in buildings
        ])
        self._save_disk_cache()
        log.info("[BUILDINGS] tile %s → %d buildings", key, len(buildings))
        return BuildingResult(buildings=buildings, cached=False)


building_service = BuildingService()
