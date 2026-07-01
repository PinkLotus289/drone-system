"""Terrain elevation services.

Two sources:
  - SRTM30m via OpenTopoData (batch, global, ~30m resolution, radar DSM)
  - USGS 3DEP via National Map (single-point, USA only, LiDAR DSM incl. buildings, ~1m)

TerrainService      — SRTM batch, used by route planner (apply_terrain_agl).
TerrainService3DEP  — USGS 3DEP single-point, used for precise spot queries.

Hardening over the naive version:
  * persistent on-disk cache (survives restarts, avoids re-hitting public API limits)
  * request throttling to respect OpenTopoData's free tier (~1 req/s)
  * retry with exponential backoff on transient/HTTP-429 failures
  * per-point *nearest-known* fallback instead of a blanket 0 m (0 m MSL in the
    mountains means "fly underground"); callers learn whether data was degraded
    via TerrainResult so they can warn the operator instead of silently lying.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("terrain")

_SRTM_URL = "https://api.opentopodata.org/v1/srtm30m"
_3DEP_URL = (
    "https://elevation.nationalmap.gov/arcgis/rest/services"
    "/3DEPElevation/ImageServer/identify"
)
_CACHE_PREC = 4        # decimal places ≈ 11 m grid
_MAX_BATCH = 100       # OpenTopoData public limit per request
_MIN_INTERVAL_S = 1.1  # OpenTopoData free tier: 1 call/sec — stay just under
_MAX_RETRIES = 3
_SRTM_CACHE_MAX = 250_000  # LRU cap — bounds RAM + on-disk JSON (~1M points ≈ tens of MB)
_3DEP_CACHE_MAX = 60_000
_DATA_DIR = Path(__file__).resolve().parents[3] / "data"


# ---------------------------------------------------------------------------
# Geometry helpers (shared with the route planner for track profiling)
# ---------------------------------------------------------------------------

_EARTH_R = 6371000.0  # m


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two lat/lon points."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_R * math.asin(math.sqrt(a))


def interpolate(lat1: float, lon1: float, lat2: float, lon2: float, t: float) -> Tuple[float, float]:
    """Linear lat/lon interpolation (good enough below a few km per leg)."""
    return lat1 + (lat2 - lat1) * t, lon1 + (lon2 - lon1) * t


@dataclass
class TerrainResult:
    """Outcome of a batch elevation lookup.

    elevations  — MSL metres, one per requested location, input order preserved.
    resolved    — how many points came from real data or cache (not fallback).
    total       — number of requested points.
    degraded    — True if ANY point fell back to nearest-known / 0 m. When True
                  the route altitudes are less trustworthy and the operator
                  should be warned.
    """
    elevations: List[float]
    resolved: int
    total: int
    degraded: bool


# ---------------------------------------------------------------------------
# SRTM — batch, global
# ---------------------------------------------------------------------------

class TerrainService:
    def __init__(self, timeout_s: float = 10.0, cache_file: str = "terrain_srtm_cache.json") -> None:
        self._cache: "OrderedDict[Tuple[float, float], float]" = OrderedDict()
        self._timeout = timeout_s
        self._cache_path = _DATA_DIR / cache_file
        self._net_lock = asyncio.Lock()   # serialises network calls → enforces rate limit
        self._last_fetch = 0.0            # monotonic ts of last network hit
        self._dirty = False
        self._load_disk_cache()

    def _cache_put(self, key: Tuple[float, float], elev: float) -> None:
        """Insert with LRU eviction so the cache (RAM + disk JSON) stays bounded."""
        self._cache[key] = elev
        self._cache.move_to_end(key)
        self._dirty = True
        while len(self._cache) > _SRTM_CACHE_MAX:
            self._cache.popitem(last=False)   # drop least-recently-used

    def _cache_get(self, key: Tuple[float, float]) -> Optional[float]:
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        return None

    # --- disk cache ---------------------------------------------------------
    def _load_disk_cache(self) -> None:
        try:
            if self._cache_path.exists():
                raw = json.loads(self._cache_path.read_text())
                for k, v in raw.items():
                    lat_s, lon_s = k.split(",")
                    self._cache[(float(lat_s), float(lon_s))] = float(v)
                # Trim if a previous version left an oversized file.
                while len(self._cache) > _SRTM_CACHE_MAX:
                    self._cache.popitem(last=False)
                log.info("[TERRAIN/SRTM] loaded %d cached points from %s",
                         len(self._cache), self._cache_path)
        except Exception as exc:
            log.warning("[TERRAIN/SRTM] disk cache load failed: %s", exc)

    def _save_disk_cache(self) -> None:
        if not self._dirty:
            return
        try:
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_suffix(".tmp")
            payload = {f"{lat},{lon}": elev for (lat, lon), elev in self._cache.items()}
            tmp.write_text(json.dumps(payload))
            os.replace(tmp, self._cache_path)   # atomic
            self._dirty = False
        except Exception as exc:
            log.warning("[TERRAIN/SRTM] disk cache save failed: %s", exc)

    def _key(self, lat: float, lon: float) -> Tuple[float, float]:
        return round(lat, _CACHE_PREC), round(lon, _CACHE_PREC)

    # --- network ------------------------------------------------------------
    def _fetch_sync(self, loc_str: str) -> List[float]:
        url = f"{_SRTM_URL}?locations={loc_str}"
        with urllib.request.urlopen(url, timeout=int(self._timeout)) as r:
            data = json.loads(r.read().decode())
        results = data.get("results") or []
        elevations = []
        for item in results:
            raw = item.get("elevation")
            elevations.append(float(raw) if raw is not None else None)
        return elevations

    async def _throttled_fetch(self, loc_str: str) -> Optional[List[float]]:
        """Rate-limited + retried network call. Returns None on hard failure."""
        loop = asyncio.get_running_loop()
        async with self._net_lock:
            for attempt in range(1, _MAX_RETRIES + 1):
                wait = _MIN_INTERVAL_S - (time.monotonic() - self._last_fetch)
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    elevs = await loop.run_in_executor(None, self._fetch_sync, loc_str)
                    self._last_fetch = time.monotonic()
                    return elevs
                except urllib.error.HTTPError as exc:
                    self._last_fetch = time.monotonic()
                    retryable = exc.code in (429, 500, 502, 503, 504)
                    log.warning("[TERRAIN/SRTM] HTTP %s (attempt %d/%d)%s",
                                exc.code, attempt, _MAX_RETRIES,
                                "" if retryable else " — giving up")
                    if not retryable or attempt == _MAX_RETRIES:
                        return None
                except Exception as exc:
                    self._last_fetch = time.monotonic()
                    log.warning("[TERRAIN/SRTM] fetch error (attempt %d/%d): %s",
                                attempt, _MAX_RETRIES, exc)
                    if attempt == _MAX_RETRIES:
                        return None
                await asyncio.sleep(1.5 * attempt)  # exponential-ish backoff
        return None

    # --- public -------------------------------------------------------------
    async def get_elevations(self, locations: List[Tuple[float, float]]) -> List[float]:
        """Back-compat shim: MSL elevation (m) per (lat, lon), input order kept."""
        return (await self.get_elevations_meta(locations)).elevations

    async def get_elevations_meta(self, locations: List[Tuple[float, float]]) -> TerrainResult:
        """Resolve elevations with caching, throttling and safe fallback.

        Unresolved points (API down) fall back to the *nearest* resolved point
        rather than 0 m, and the result is flagged ``degraded`` so callers can
        warn the operator. Only if nothing at all resolves do we return 0 m.
        """
        if not locations:
            return TerrainResult([], 0, 0, False)

        resolved: Dict[int, float] = {}
        to_fetch: List[Tuple[int, float, float]] = []

        for i, (lat, lon) in enumerate(locations):
            key = self._key(lat, lon)
            cached = self._cache_get(key)
            if cached is not None:
                resolved[i] = cached
            else:
                to_fetch.append((i, lat, lon))

        for batch_start in range(0, len(to_fetch), _MAX_BATCH):
            batch = to_fetch[batch_start: batch_start + _MAX_BATCH]
            loc_str = "|".join(f"{lat},{lon}" for _, lat, lon in batch)
            elevs = await self._throttled_fetch(loc_str)
            if elevs is None:
                continue  # leave these indices unresolved → fallback below
            for j, (i, lat, lon) in enumerate(batch):
                elev = elevs[j] if j < len(elevs) else None
                if elev is None:
                    continue
                self._cache_put(self._key(lat, lon), elev)
                resolved[i] = elev
            log.info("[TERRAIN/SRTM] fetched %d points (cache %d)", len(batch), len(self._cache))

        self._save_disk_cache()

        n = len(locations)
        elevations = [0.0] * n
        unresolved = [i for i in range(n) if i not in resolved]

        if not resolved:
            # Total blackout: nothing cached, nothing fetched. Keep flat-0 but
            # shout — this is the dangerous case the caller must surface.
            log.error("[TERRAIN/SRTM] NO elevation data for any of %d points — "
                      "falling back to 0 m MSL (UNSAFE over terrain)", n)
            return TerrainResult(elevations, 0, n, True)

        for i, e in resolved.items():
            elevations[i] = e
        for i in unresolved:
            elevations[i] = self._nearest(i, locations, resolved)
        if unresolved:
            log.warning("[TERRAIN/SRTM] %d/%d points used nearest-known fallback",
                        len(unresolved), n)

        return TerrainResult(elevations, len(resolved), n, bool(unresolved))

    @staticmethod
    def _nearest(idx: int, locations: List[Tuple[float, float]],
                 resolved: Dict[int, float]) -> float:
        lat, lon = locations[idx]
        best_d, best_e = float("inf"), 0.0
        for j, e in resolved.items():
            jlat, jlon = locations[j]
            d = haversine_m(lat, lon, jlat, jlon)
            if d < best_d:
                best_d, best_e = d, e
        return best_e


# ---------------------------------------------------------------------------
# USGS 3DEP — single-point, USA only, LiDAR DSM (includes buildings & trees)
# ---------------------------------------------------------------------------

class TerrainService3DEP:
    """Single-point elevation from USGS 3D Elevation Program (LiDAR DSM).

    Coverage: continental USA + Alaska + Hawaii.
    Resolution: up to 1 m (varies by area).
    Includes: buildings, vegetation canopy — true surface model.
    Returns None outside coverage or on network error.
    """

    def __init__(self, timeout_s: float = 10.0, cache_file: str = "terrain_3dep_cache.json") -> None:
        self._cache: "OrderedDict[Tuple[float, float], Optional[float]]" = OrderedDict()
        self._timeout = timeout_s
        self._cache_path = _DATA_DIR / cache_file
        self._dirty = False
        self._load_disk_cache()

    def _cache_put(self, key: Tuple[float, float], elev: Optional[float]) -> None:
        self._cache[key] = elev
        self._cache.move_to_end(key)
        self._dirty = True
        while len(self._cache) > _3DEP_CACHE_MAX:
            self._cache.popitem(last=False)

    def _load_disk_cache(self) -> None:
        try:
            if self._cache_path.exists():
                raw = json.loads(self._cache_path.read_text())
                for k, v in raw.items():
                    lat_s, lon_s = k.split(",")
                    self._cache[(float(lat_s), float(lon_s))] = (None if v is None else float(v))
                while len(self._cache) > _3DEP_CACHE_MAX:
                    self._cache.popitem(last=False)
                log.info("[TERRAIN/3DEP] loaded %d cached points", len(self._cache))
        except Exception as exc:
            log.warning("[TERRAIN/3DEP] disk cache load failed: %s", exc)

    def _save_disk_cache(self) -> None:
        if not self._dirty:
            return
        try:
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_suffix(".tmp")
            payload = {f"{lat},{lon}": elev for (lat, lon), elev in self._cache.items()}
            tmp.write_text(json.dumps(payload))
            os.replace(tmp, self._cache_path)
            self._dirty = False
        except Exception as exc:
            log.warning("[TERRAIN/3DEP] disk cache save failed: %s", exc)

    def _key(self, lat: float, lon: float) -> Tuple[float, float]:
        return round(lat, _CACHE_PREC), round(lon, _CACHE_PREC)

    def _fetch_sync(self, lat: float, lon: float) -> Optional[float]:
        geom = json.dumps({"x": lon, "y": lat, "spatialReference": {"wkid": 4326}})
        params = urllib.parse.urlencode({
            "geometryType": "esriGeometryPoint",
            "geometry": geom,
            "returnGeometry": "false",
            "f": "json",
        })
        url = f"{_3DEP_URL}?{params}"
        with urllib.request.urlopen(url, timeout=int(self._timeout)) as r:
            data = json.loads(r.read().decode())
        raw = data.get("value")
        if raw is None or raw == "NoData":
            return None
        return float(raw)

    async def get_elevation(self, lat: float, lon: float) -> Optional[float]:
        """Return surface elevation (m MSL) at (lat, lon), or None if unavailable."""
        key = self._key(lat, lon)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        loop = asyncio.get_running_loop()
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                elev = await loop.run_in_executor(None, self._fetch_sync, lat, lon)
                self._cache_put(key, elev)
                self._save_disk_cache()
                src = f"{elev:.1f} m" if elev is not None else "no data"
                log.info("[TERRAIN/3DEP] (%.5f, %.5f) → %s", lat, lon, src)
                return elev
            except Exception as exc:
                log.warning("[TERRAIN/3DEP] error at (%.5f, %.5f) attempt %d/%d: %s",
                            lat, lon, attempt, _MAX_RETRIES, exc)
                if attempt == _MAX_RETRIES:
                    return None
                await asyncio.sleep(1.0 * attempt)
        return None


terrain_service = TerrainService()
terrain_service_3dep = TerrainService3DEP()
