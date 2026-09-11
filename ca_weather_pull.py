#!/usr/bin/env python3
"""
ca_weather_pull.py — daily weather pull for major Canadian cities.

Source: Environment and Climate Change Canada (ECCC) / Meteorological Service of
Canada "CityPage Weather" open data on the MSC Datamart.

    https://dd.weather.gc.ca/today/citypage_weather/{PROV}/{HH}/
    {YYYYMMDD}T{HHMMSS.sss}Z_MSC_CitypageWeather_{siteCode}_{lang}.xml

No API key. No rate-limit sign-up. Licensed under the Open Government Licence –
Canada, which permits commercial use with attribution.

Outputs, per run:
    <outdir>/<YYYY-MM-DD>/canada_weather_<YYYY-MM-DD>.csv    flat, one row per city
    <outdir>/<YYYY-MM-DD>/canada_weather_<YYYY-MM-DD>.json   full detail incl. 7-day + alerts
    <outdir>/latest.csv, <outdir>/latest.json                copies of the most recent run

Exit codes (the Jenkinsfile relies on these):
    0  every city succeeded
    2  partial success at or above --min-success-pct  -> mark build UNSTABLE
    1  below threshold, or a fatal error              -> mark build FAILED

Usage:
    ./ca_weather_pull.py --outdir /var/lib/ca-weather
    ./ca_weather_pull.py --cities Toronto,Winnipeg --verbose
    ./ca_weather_pull.py --validate-only
    ./ca_weather_pull.py --discover ON
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "ERROR: the 'requests' package is required.\n"
        "       pip install -r requirements.txt\n"
    )
    raise SystemExit(1)

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore

__version__ = "1.1.0"

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

DATAMART_BASE = "https://dd.weather.gc.ca/today/citypage_weather"
SITE_LIST_GEOJSON = (
    "https://collaboration.cmc.ec.gc.ca/cmc/cmos/public_doc/msc-data/"
    "citypage-weather/site_list_en.geojson"
)
ATTRIBUTION = (
    "Contains information licensed under the Open Government Licence - Canada. "
    "Source: Environment and Climate Change Canada."
)

# Matches: 20260909T040114.099Z_MSC_CitypageWeather_s0000458_en.xml
FILENAME_RE = re.compile(
    r"(?P<fn>(?P<ts>\d{8}T\d{6}\.\d{3})Z_MSC_CitypageWeather_"
    r"(?P<site>s\d{7})_(?P<lang>en|fr)\.xml)"
)

log = logging.getLogger("ca_weather")

# Column order for the flat CSV. Keep stable — downstream consumers depend on it.
CSV_COLUMNS = [
    "run_date", "run_time_local", "run_time_utc",
    "city", "province", "site_code", "feed_location", "region",
    "latitude", "longitude",
    "observation_time_utc", "observation_time_local", "station",
    "condition", "temperature_c", "dewpoint_c", "relative_humidity_pct",
    "humidex", "wind_chill_c", "pressure_kpa", "pressure_tendency",
    "visibility_km", "wind_speed_kmh", "wind_gust_kmh",
    "wind_direction", "wind_bearing_deg",
    "fc1_period", "fc1_summary", "fc1_temp_class", "fc1_temp_c",
    "fc1_pop_pct", "fc1_uv_index", "fc1_uv_category",
    "fc2_period", "fc2_summary", "fc2_temp_class", "fc2_temp_c", "fc2_pop_pct",
    "next_high_c", "next_low_c",
    "normal_high_c", "normal_low_c",
    "yesterday_high_c", "yesterday_low_c", "yesterday_precip_mm",
    "sunrise_local", "sunset_local",
    "alert_count", "alerts",
    "status", "source_url",
]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def build_session(retries: int, backoff: float, user_agent: str) -> requests.Session:
    """A session that retries transient errors with exponential backoff."""
    session = requests.Session()
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        backoff_factor=backoff,
        status_forcelist=(408, 425, 429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=16, pool_maxsize=16)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
    return session


def text_of(node: Optional[ET.Element]) -> Optional[str]:
    if node is None or node.text is None:
        return None
    value = node.text.strip()
    return value or None


def num_of(node: Optional[ET.Element]) -> Optional[float]:
    raw = text_of(node)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def dms_to_decimal(raw: Optional[str]) -> Optional[float]:
    """'43.74N' -> 43.74 ; '79.37W' -> -79.37"""
    if not raw:
        return None
    match = re.match(r"^\s*([\d.]+)\s*([NSEW])\s*$", raw, re.IGNORECASE)
    if not match:
        try:
            return float(raw)
        except ValueError:
            return None
    value = float(match.group(1))
    if match.group(2).upper() in ("S", "W"):
        value = -value
    return value


def iso_from_datetime_node(node: Optional[ET.Element]) -> Optional[str]:
    """Turn a CityPage <dateTime> element into an ISO 8601 string."""
    if node is None:
        return None
    stamp = text_of(node.find("timeStamp"))
    if stamp and len(stamp) == 14:
        try:
            parsed = datetime.strptime(stamp, "%Y%m%d%H%M%S")
        except ValueError:
            return text_of(node.find("textSummary"))
        zone = (node.get("zone") or "").upper()
        if zone == "UTC":
            return parsed.replace(tzinfo=timezone.utc).isoformat()
        return parsed.isoformat() + (f" {node.get('zone')}" if node.get("zone") else "")
    return text_of(node.find("textSummary"))


def pick_datetime(parent: Optional[ET.Element], name: str, zone: str) -> Optional[ET.Element]:
    """CityPage repeats <dateTime> once per zone; pick the one we want."""
    if parent is None:
        return None
    fallback = None
    for node in parent.findall("dateTime"):
        if node.get("name") != name:
            continue
        fallback = fallback or node
        if zone == "UTC" and (node.get("zone") or "").upper() == "UTC":
            return node
        if zone == "local" and (node.get("zone") or "").upper() != "UTC":
            return node
    return fallback


def resolve_timezone(name: Optional[str]):
    if not name:
        return None
    if ZoneInfo is None:
        log.warning("zoneinfo unavailable; using system local time instead of %s", name)
        return None
    try:
        return ZoneInfo(name)
    except Exception:
        log.warning("Unknown timezone %r (is tzdata installed?); using system local time", name)
        return None


# --------------------------------------------------------------------------- #
# Datamart access
# --------------------------------------------------------------------------- #

class Datamart:
    """Finds and fetches the newest CityPage XML for a site code."""

    def __init__(self, session: requests.Session, timeout: int,
                 lookback_hours: int, base: str = DATAMART_BASE):
        self.session = session
        self.timeout = timeout
        self.lookback_hours = lookback_hours
        self.base = base.rstrip("/")
        self._listing_cache: Dict[Tuple[str, str], Dict[str, str]] = {}
        self._lock = threading.Lock()

    def _listing(self, province: str, hour_dir: str) -> Dict[str, str]:
        """Map {site_code_lang: filename} for one province/hour directory."""
        key = (province, hour_dir)
        with self._lock:
            if key in self._listing_cache:
                return self._listing_cache[key]

        url = f"{self.base}/{province}/{hour_dir}/"
        files: Dict[str, str] = {}
        try:
            response = self.session.get(url, timeout=self.timeout)
            if response.status_code == 200:
                for match in FILENAME_RE.finditer(response.text):
                    entry_key = f"{match.group('site')}_{match.group('lang')}"
                    current = files.get(entry_key)
                    # Several drops per hour; keep the latest timestamp.
                    if current is None or match.group("fn") > current:
                        files[entry_key] = match.group("fn")
                log.debug("Listing %s -> %d files", url, len(files))
            elif response.status_code == 404:
                log.debug("Listing %s -> 404 (hour not published yet)", url)
            else:
                log.warning("Listing %s -> HTTP %s", url, response.status_code)
        except requests.RequestException as exc:
            log.warning("Listing %s failed: %s", url, exc)

        with self._lock:
            self._listing_cache[key] = files
        return files

    def fetch_xml(self, province: str, site_code: str, lang: str) -> Tuple[ET.Element, str]:
        """Walk back hour by hour until we find this site's newest published file."""
        now = datetime.now(timezone.utc)
        errors: List[str] = []
        for offset in range(self.lookback_hours + 1):
            slot = now - timedelta(hours=offset)
            hour_dir = f"{slot.hour:02d}"
            filename = self._listing(province, hour_dir).get(f"{site_code}_{lang}")
            if not filename:
                continue
            url = f"{self.base}/{province}/{hour_dir}/{filename}"
            try:
                response = self.session.get(url, timeout=self.timeout)
                response.raise_for_status()
                return ET.fromstring(response.content), url
            except (requests.RequestException, ET.ParseError) as exc:
                errors.append(f"{url}: {exc}")
                continue
        detail = f" (last error: {errors[-1]})" if errors else ""
        raise RuntimeError(
            f"no CityPage file found for {site_code} in {province} within the last "
            f"{self.lookback_hours}h{detail}"
        )

    def site_codes_for_province(self, province: str) -> List[str]:
        now = datetime.now(timezone.utc)
        codes: set = set()
        for offset in range(self.lookback_hours + 1):
            slot = now - timedelta(hours=offset)
            for key in self._listing(province, f"{slot.hour:02d}"):
                if key.endswith("_en"):
                    codes.add(key[:-3])
        return sorted(codes)


# --------------------------------------------------------------------------- #
# XML -> record
# --------------------------------------------------------------------------- #

def parse_site_data(root: ET.Element, include_hourly: bool = True) -> Dict[str, Any]:
    """Pull the fields we care about out of a CityPage <siteData> document."""
    out: Dict[str, Any] = {}

    # ---- location -------------------------------------------------------- #
    location = root.find("location")
    name_node = location.find("name") if location is not None else None
    out["feed_location"] = text_of(name_node)
    out["region"] = text_of(location.find("region")) if location is not None else None
    prov_node = location.find("province") if location is not None else None
    out["feed_province"] = (prov_node.get("code") if prov_node is not None else None)
    out["latitude"] = dms_to_decimal(name_node.get("lat") if name_node is not None else None)
    out["longitude"] = dms_to_decimal(name_node.get("lon") if name_node is not None else None)

    # ---- current conditions ---------------------------------------------- #
    cc = root.find("currentConditions")
    current: Dict[str, Any] = {}
    if cc is not None:
        wind = cc.find("wind")
        pressure = cc.find("pressure")
        current = {
            "station": text_of(cc.find("station")),
            "observation_time_utc": iso_from_datetime_node(
                pick_datetime(cc, "observation", "UTC")),
            "observation_time_local": iso_from_datetime_node(
                pick_datetime(cc, "observation", "local")),
            "condition": text_of(cc.find("condition")),
            "icon_code": text_of(cc.find("iconCode")),
            "temperature_c": num_of(cc.find("temperature")),
            "dewpoint_c": num_of(cc.find("dewpoint")),
            "relative_humidity_pct": num_of(cc.find("relativeHumidity")),
            "humidex": num_of(cc.find("humidex")),
            "wind_chill_c": num_of(cc.find("windChill")),
            "pressure_kpa": num_of(pressure),
            "pressure_tendency": (pressure.get("tendency") if pressure is not None else None),
            "visibility_km": num_of(cc.find("visibility")),
            "wind_speed_kmh": num_of(wind.find("speed")) if wind is not None else None,
            "wind_gust_kmh": num_of(wind.find("gust")) if wind is not None else None,
            "wind_direction": text_of(wind.find("direction")) if wind is not None else None,
            "wind_bearing_deg": num_of(wind.find("bearing")) if wind is not None else None,
        }
    out["current"] = current

    # ---- forecast periods ------------------------------------------------- #
    forecasts: List[Dict[str, Any]] = []
    fg = root.find("forecastGroup")
    if fg is not None:
        for fc in fg.findall("forecast"):
            temps = fc.find("temperatures")
            temp_node = temps.find("temperature") if temps is not None else None
            abbrev = fc.find("abbreviatedForecast")
            uv = fc.find("uv")
            winds = fc.find("winds")
            forecasts.append({
                "period": text_of(fc.find("period")),
                "summary": text_of(fc.find("textSummary")),
                "abbreviated": text_of(abbrev.find("textSummary")) if abbrev is not None else None,
                "icon_code": text_of(abbrev.find("iconCode")) if abbrev is not None else None,
                "pop_pct": num_of(abbrev.find("pop")) if abbrev is not None else None,
                "temp_class": (temp_node.get("class") if temp_node is not None else None),
                "temp_c": num_of(temp_node),
                "wind_summary": text_of(winds.find("textSummary")) if winds is not None else None,
                "relative_humidity_pct": num_of(fc.find("relativeHumidity")),
                "uv_index": num_of(uv.find("index")) if uv is not None else None,
                "uv_category": (uv.get("category") if uv is not None else None),
            })
    out["forecasts"] = forecasts

    # First upcoming high and first upcoming low, whatever order the feed uses.
    out["next_high_c"] = next(
        (f["temp_c"] for f in forecasts if f["temp_class"] == "high" and f["temp_c"] is not None),
        None)
    out["next_low_c"] = next(
        (f["temp_c"] for f in forecasts if f["temp_class"] == "low" and f["temp_c"] is not None),
        None)

    normals = fg.find("regionalNormals") if fg is not None else None
    normal_high = normal_low = None
    if normals is not None:
        for node in normals.findall("temperature"):
            if node.get("class") == "high":
                normal_high = num_of(node)
            elif node.get("class") == "low":
                normal_low = num_of(node)
    out["normal_high_c"] = normal_high
    out["normal_low_c"] = normal_low

    # ---- next 24 hours (JSON output only - too wide for the CSV) ---------- #
    hourly: List[Dict[str, Any]] = []
    hg = root.find("hourlyForecastGroup")
    if include_hourly and hg is not None:
        for hour in hg.findall("hourlyForecast"):
            stamp = hour.get("dateTimeUTC") or ""
            try:
                when = datetime.strptime(stamp, "%Y%m%d%H%M").replace(
                    tzinfo=timezone.utc).isoformat()
            except ValueError:
                when = stamp or None
            wind = hour.find("wind")
            lop = hour.find("lop")
            hourly.append({
                "time_utc": when,
                "condition": text_of(hour.find("condition")),
                "temperature_c": num_of(hour.find("temperature")),
                "wind_chill_c": num_of(hour.find("windChill")),
                "humidex": num_of(hour.find("humidex")),
                "precip_probability_pct": num_of(lop),
                "precip_probability_category": (lop.get("category") if lop is not None else None),
                "wind_speed_kmh": num_of(wind.find("speed")) if wind is not None else None,
                "wind_gust_kmh": num_of(wind.find("gust")) if wind is not None else None,
                "wind_direction": text_of(wind.find("direction")) if wind is not None else None,
            })
    out["hourly"] = hourly

    # ---- warnings / watches / statements ---------------------------------- #
    alerts: List[Dict[str, Any]] = []
    warnings = root.find("warnings")
    if warnings is not None:
        for event in warnings.findall("event"):
            alerts.append({
                "type": event.get("type"),
                "priority": event.get("priority"),
                "description": event.get("description"),
                "issued": iso_from_datetime_node(pick_datetime(event, "eventIssue", "UTC")),
            })
    out["alerts"] = alerts
    out["alerts_url"] = warnings.get("url") if warnings is not None else None

    # ---- yesterday -------------------------------------------------------- #
    yesterday = root.find("yesterdayConditions")
    y_high = y_low = None
    if yesterday is not None:
        for node in yesterday.findall("temperature"):
            if node.get("class") == "high":
                y_high = num_of(node)
            elif node.get("class") == "low":
                y_low = num_of(node)
    out["yesterday"] = {
        "high_c": y_high,
        "low_c": y_low,
        "precip_mm": num_of(yesterday.find("precip")) if yesterday is not None else None,
    }

    # ---- sunrise / sunset -------------------------------------------------- #
    rise_set = root.find("riseSet")
    out["sunrise_local"] = iso_from_datetime_node(pick_datetime(rise_set, "sunrise", "local"))
    out["sunset_local"] = iso_from_datetime_node(pick_datetime(rise_set, "sunset", "local"))

    return out


def flatten(record: Dict[str, Any]) -> Dict[str, Any]:
    """Nested record -> one flat CSV row."""
    current = record.get("current") or {}
    forecasts = record.get("forecasts") or []
    fc1 = forecasts[0] if len(forecasts) > 0 else {}
    fc2 = forecasts[1] if len(forecasts) > 1 else {}
    yesterday = record.get("yesterday") or {}
    alerts = record.get("alerts") or []

    row = {
        "run_date": record.get("run_date"),
        "run_time_local": record.get("run_time_local"),
        "run_time_utc": record.get("run_time_utc"),
        "city": record.get("city"),
        "province": record.get("province"),
        "site_code": record.get("site_code"),
        "feed_location": record.get("feed_location"),
        "region": record.get("region"),
        "latitude": record.get("latitude"),
        "longitude": record.get("longitude"),
        "observation_time_utc": current.get("observation_time_utc"),
        "observation_time_local": current.get("observation_time_local"),
        "station": current.get("station"),
        "condition": current.get("condition"),
        "temperature_c": current.get("temperature_c"),
        "dewpoint_c": current.get("dewpoint_c"),
        "relative_humidity_pct": current.get("relative_humidity_pct"),
        "humidex": current.get("humidex"),
        "wind_chill_c": current.get("wind_chill_c"),
        "pressure_kpa": current.get("pressure_kpa"),
        "pressure_tendency": current.get("pressure_tendency"),
        "visibility_km": current.get("visibility_km"),
        "wind_speed_kmh": current.get("wind_speed_kmh"),
        "wind_gust_kmh": current.get("wind_gust_kmh"),
        "wind_direction": current.get("wind_direction"),
        "wind_bearing_deg": current.get("wind_bearing_deg"),
        "fc1_period": fc1.get("period"),
        "fc1_summary": fc1.get("summary"),
        "fc1_temp_class": fc1.get("temp_class"),
        "fc1_temp_c": fc1.get("temp_c"),
        "fc1_pop_pct": fc1.get("pop_pct"),
        "fc1_uv_index": fc1.get("uv_index"),
        "fc1_uv_category": fc1.get("uv_category"),
        "fc2_period": fc2.get("period"),
        "fc2_summary": fc2.get("summary"),
        "fc2_temp_class": fc2.get("temp_class"),
        "fc2_temp_c": fc2.get("temp_c"),
        "fc2_pop_pct": fc2.get("pop_pct"),
        "next_high_c": record.get("next_high_c"),
        "next_low_c": record.get("next_low_c"),
        "normal_high_c": record.get("normal_high_c"),
        "normal_low_c": record.get("normal_low_c"),
        "yesterday_high_c": yesterday.get("high_c"),
        "yesterday_low_c": yesterday.get("low_c"),
        "yesterday_precip_mm": yesterday.get("precip_mm"),
        "sunrise_local": record.get("sunrise_local"),
        "sunset_local": record.get("sunset_local"),
        "alert_count": len(alerts),
        "alerts": "; ".join(
            filter(None, (a.get("description") for a in alerts))
        ),
        "status": record.get("status"),
        "source_url": record.get("source_url"),
    }
    return {key: row.get(key) for key in CSV_COLUMNS}


# --------------------------------------------------------------------------- #
# Collection
# --------------------------------------------------------------------------- #

def collect_city(datamart: Datamart, city: Dict[str, Any], lang: str,
                 run_meta: Dict[str, str], include_hourly: bool = True) -> Dict[str, Any]:
    name = city["name"]
    province = city["province"]
    site_code = city["site_code"]

    record: Dict[str, Any] = {
        **run_meta,
        "city": name,
        "province": province,
        "site_code": site_code,
        "status": "ok",
        "error": None,
    }

    started = time.monotonic()
    try:
        root, url = datamart.fetch_xml(province, site_code, lang)
        record.update(parse_site_data(root, include_hourly=include_hourly))
        record["source_url"] = url

        expect = city.get("expect")
        feed_name = record.get("feed_location") or ""
        if expect and expect.lower() not in feed_name.lower():
            record["status"] = "name_mismatch"
            log.warning(
                "%s: site_code %s resolves to %r, expected something containing %r "
                "- check cities.json",
                name, site_code, feed_name, expect,
            )
        if not (record.get("current") or {}).get("temperature_c"):
            log.info("%s: no current temperature in feed (station may be offline)", name)

        log.info("%-13s %-3s %-9s %6.2fs  %s",
                 name, province, site_code, time.monotonic() - started, feed_name or "-")
    except Exception as exc:  # noqa: BLE001 - one city must not kill the run
        record["status"] = "error"
        record["error"] = str(exc)
        log.error("%-13s %-3s %-9s FAILED: %s", name, province, site_code, exc)

    return record


def run_collection(cities: List[Dict[str, Any]], datamart: Datamart, lang: str,
                   workers: int, run_meta: Dict[str, str],
                   include_hourly: bool = True) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(collect_city, datamart, city, lang, run_meta, include_hourly): city
            for city in cities
        }
        for future in as_completed(futures):
            results.append(future.result())
    order = {city["name"]: i for i, city in enumerate(cities)}
    results.sort(key=lambda r: order.get(r["city"], 999))
    return results


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #

def write_outputs(records: List[Dict[str, Any]], outdir: Path, run_date: str,
                  run_meta: Dict[str, str]) -> Dict[str, Path]:
    day_dir = outdir / run_date
    day_dir.mkdir(parents=True, exist_ok=True)

    csv_path = day_dir / f"canada_weather_{run_date}.csv"
    json_path = day_dir / f"canada_weather_{run_date}.json"

    # Write to a temp file then rename, so a reader never sees a half-written file.
    tmp_csv = csv_path.with_suffix(".csv.tmp")
    with tmp_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            writer.writerow(flatten(record))
    tmp_csv.replace(csv_path)

    payload = {
        "generated_at_utc": run_meta["run_time_utc"],
        "generated_at_local": run_meta["run_time_local"],
        "run_date": run_date,
        "source": "Environment and Climate Change Canada - CityPage Weather (MSC Datamart)",
        "source_base_url": DATAMART_BASE,
        "attribution": ATTRIBUTION,
        "collector_version": __version__,
        "city_count": len(records),
        "ok_count": sum(1 for r in records if r["status"] in ("ok", "name_mismatch")),
        "error_count": sum(1 for r in records if r["status"] == "error"),
        "cities": records,
    }
    tmp_json = json_path.with_suffix(".json.tmp")
    tmp_json.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    tmp_json.replace(json_path)

    shutil.copy2(csv_path, outdir / "latest.csv")
    shutil.copy2(json_path, outdir / "latest.json")

    return {"csv": csv_path, "json": json_path}


def prune(outdir: Path, retain_days: int) -> int:
    """Delete dated run folders older than retain_days."""
    if retain_days <= 0:
        return 0
    cutoff = datetime.now().date() - timedelta(days=retain_days)
    removed = 0
    for child in outdir.iterdir():
        if not child.is_dir():
            continue
        try:
            folder_date = datetime.strptime(child.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if folder_date < cutoff:
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
            log.info("Pruned old run folder %s", child.name)
    return removed


# --------------------------------------------------------------------------- #
# Discovery helper
# --------------------------------------------------------------------------- #

def discover(session: requests.Session, datamart: Datamart, province: str, timeout: int) -> int:
    province = province.upper()
    try:
        response = session.get(SITE_LIST_GEOJSON, timeout=timeout)
        response.raise_for_status()
        data = response.json()
        rows = []
        for feature in data.get("features", []):
            props = feature.get("properties", {})
            code = props.get("Codes") or props.get("code")
            name = props.get("English Names") or props.get("name_en") or props.get("nameEn")
            prov = props.get("Province Codes") or props.get("province")
            if prov and str(prov).upper() == province:
                rows.append((code, name))
        if rows:
            print(f"{'SITE CODE':<12} CITY   ({province}, {len(rows)} sites)")
            for code, name in sorted(rows, key=lambda r: (r[1] or "")):
                print(f"{code:<12} {name}")
            return 0
        log.warning("No %s rows in the site list; falling back to the datamart listing", province)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not read the MSC site list (%s); "
                    "falling back to the datamart listing", exc)

    codes = datamart.site_codes_for_province(province)
    print(f"Site codes currently published for {province} ({len(codes)}):")
    for code in codes:
        print(f"  {code}")
    print("\nOpen one to see its city name, e.g.:")
    print(f"  {DATAMART_BASE}/{province}/<HH>/<timestamp>Z_MSC_CitypageWeather_{codes[0] if codes else 'sXXXXXXX'}_en.xml")
    return 0 if codes else 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def load_cities(path: Path) -> List[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"FATAL: config file not found: {path}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"FATAL: {path} is not valid JSON: {exc}")

    cities = data.get("cities") if isinstance(data, dict) else data
    if not isinstance(cities, list) or not cities:
        raise SystemExit(f"FATAL: {path} has no 'cities' array")

    for i, city in enumerate(cities):
        missing = [k for k in ("name", "province", "site_code") if not city.get(k)]
        if missing:
            raise SystemExit(f"FATAL: cities[{i}] is missing {', '.join(missing)}")
        city["province"] = city["province"].upper()
    return cities


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Pull daily weather for major Canadian cities from Environment Canada.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--config", type=Path, default=here / "cities.json",
                        help="JSON file listing the cities to pull")
    parser.add_argument("--outdir", type=Path,
                        default=Path(os.environ.get("CA_WEATHER_OUTDIR", here / "data")),
                        help="where dated output folders are written")
    parser.add_argument("--cities", default=None,
                        help="comma-separated subset of city names to pull")
    parser.add_argument("--lang", choices=("en", "fr"), default="en",
                        help="feed language")
    parser.add_argument("--timezone", default=os.environ.get("CA_WEATHER_TZ", "America/Winnipeg"),
                        help="timezone used for the run date and folder name")
    parser.add_argument("--timeout", type=int, default=30, help="per-request timeout in seconds")
    parser.add_argument("--retries", type=int, default=4, help="retry attempts per request")
    parser.add_argument("--backoff", type=float, default=1.5, help="retry backoff factor")
    parser.add_argument("--lookback-hours", type=int, default=6,
                        help="how many hourly datamart folders to search back through")
    parser.add_argument("--workers", type=int, default=6,
                        help="parallel downloads (keep modest; this is a public service)")
    parser.add_argument("--min-success-pct", type=float, default=80.0,
                        help="below this share of successful cities the run fails")
    parser.add_argument("--retain-days", type=int, default=90,
                        help="delete dated output folders older than this; 0 disables")
    parser.add_argument("--user-agent",
                        default=os.environ.get(
                            "CA_WEATHER_UA",
                            f"ca-weather-pull/{__version__} (Scootaround IT; automated daily pull)"),
                        help="User-Agent sent to the datamart")
    parser.add_argument("--no-hourly", action="store_true",
                        help="omit the 24-hour hourly forecast from the JSON output")
    parser.add_argument("--validate-only", action="store_true",
                        help="check every site code resolves, write nothing")
    parser.add_argument("--discover", metavar="PROV", default=None,
                        help="list available site codes for a province and exit")
    parser.add_argument("--log-file", type=Path, default=None, help="also write logs here")
    parser.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def setup_logging(verbose: bool, log_file: Optional[Path]) -> None:
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose, args.log_file)

    session = build_session(args.retries, args.backoff, args.user_agent)
    datamart = Datamart(session, args.timeout, args.lookback_hours)

    if args.discover:
        return discover(session, datamart, args.discover, args.timeout)

    cities = load_cities(args.config)
    if args.cities:
        wanted = {name.strip().lower() for name in args.cities.split(",") if name.strip()}
        cities = [c for c in cities if c["name"].lower() in wanted]
        missing = wanted - {c["name"].lower() for c in cities}
        if missing:
            log.error("Not in %s: %s", args.config.name, ", ".join(sorted(missing)))
        if not cities:
            return 1

    tzinfo = resolve_timezone(args.timezone)
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tzinfo) if tzinfo else now_utc.astimezone()
    run_date = now_local.strftime("%Y-%m-%d")
    run_meta = {
        "run_date": run_date,
        "run_time_local": now_local.isoformat(timespec="seconds"),
        "run_time_utc": now_utc.isoformat(timespec="seconds"),
    }

    log.info("ca_weather_pull %s | %d cities | run date %s (%s)",
             __version__, len(cities), run_date, args.timezone)

    started = time.monotonic()
    records = run_collection(cities, datamart, args.lang, args.workers, run_meta,
                             include_hourly=not args.no_hourly)
    elapsed = time.monotonic() - started

    ok = [r for r in records if r["status"] in ("ok", "name_mismatch")]
    mismatched = [r for r in records if r["status"] == "name_mismatch"]
    failed = [r for r in records if r["status"] == "error"]
    success_pct = 100.0 * len(ok) / len(records) if records else 0.0

    log.info("Collected %d/%d cities (%.1f%%) in %.1fs",
             len(ok), len(records), success_pct, elapsed)
    if mismatched:
        log.warning("Site-code/name mismatches: %s",
                    ", ".join(f"{r['city']}->{r.get('feed_location')}" for r in mismatched))
    if failed:
        log.error("Failed: %s", ", ".join(r["city"] for r in failed))

    if args.validate_only:
        log.info("Validate-only: no files written")
        return 0 if not failed and not mismatched else 1

    if not ok:
        log.error("No city succeeded - writing nothing so the last good file is preserved")
        return 1

    try:
        args.outdir.mkdir(parents=True, exist_ok=True)
        paths = write_outputs(records, args.outdir, run_date, run_meta)
    except OSError as exc:
        log.error("Could not write output to %s: %s", args.outdir, exc)
        return 1

    log.info("Wrote %s", paths["csv"])
    log.info("Wrote %s", paths["json"])
    prune(args.outdir, args.retain_days)
    log.info(ATTRIBUTION)

    if failed:
        if success_pct < args.min_success_pct:
            log.error("Success rate %.1f%% is below the %.1f%% threshold",
                      success_pct, args.min_success_pct)
            return 1
        return 2  # partial -> Jenkins UNSTABLE
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.stderr.write("\nInterrupted\n")
        sys.exit(130)
