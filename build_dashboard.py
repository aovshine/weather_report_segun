#!/usr/bin/env python3
"""
build_dashboard.py — render the weather pull as a static HTML dashboard.

Reads the output of ca_weather_pull.py and writes a self-contained dashboard:

    <dest>/index.html
    <dest>/dashboard.css

Built to survive Jenkins' default Content-Security-Policy for published HTML:

    sandbox; default-src 'none'; img-src 'self'; style-src 'self';

which means NO JavaScript and NO inline styles or <style> blocks. So:
  * all styling lives in dashboard.css, loaded as a same-origin stylesheet
  * charts are inline <svg> using CSS classes (presentation is not inline style)
  * there is no hover/tooltip layer — the snapshot table is the data view

Standard library only.

Usage:
    ./build_dashboard.py --outdir /var/lib/ca-weather --dest /var/lib/ca-weather/dashboard
    ./build_dashboard.py --outdir /var/lib/ca-weather --dest ./out --days 60
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

__version__ = "1.0.0"

# Small-multiple geometry
SPARK_W, SPARK_H = 168, 52
SPARK_PAD_X, SPARK_PAD_Y = 6, 8


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

def load_latest(outdir: Path) -> Dict[str, Any]:
    path = outdir / "latest.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"FATAL: {path} not found — run ca_weather_pull.py first")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"FATAL: {path} is not valid JSON: {exc}")


def load_history(outdir: Path, days: int) -> Tuple[List[str], Dict[str, Dict[str, float]]]:
    """
    Walk the dated run folders and pull one temperature per city per day.

    Returns (sorted dates, {city: {date: temperature_c}}).
    """
    folders = sorted(
        (p for p in outdir.iterdir() if p.is_dir() and _is_date(p.name)),
        key=lambda p: p.name,
    )[-days:]

    dates: List[str] = []
    series: Dict[str, Dict[str, float]] = {}

    for folder in folders:
        matches = list(folder.glob("canada_weather_*.csv"))
        if not matches:
            continue
        day = folder.name
        found = False
        try:
            with matches[0].open(newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    city = (row.get("city") or "").strip()
                    raw = (row.get("temperature_c") or "").strip()
                    if not city or not raw:
                        continue
                    try:
                        series.setdefault(city, {})[day] = float(raw)
                        found = True
                    except ValueError:
                        continue
        except OSError:
            continue
        if found:
            dates.append(day)

    return dates, series


def _is_date(name: str) -> bool:
    try:
        datetime.strptime(name, "%Y-%m-%d")
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #

def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def num(value: Any, digits: int = 1, suffix: str = "") -> str:
    if value is None or value == "":
        return "—"
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return esc(value)


def feels_like(current: Dict[str, Any]) -> Tuple[str, str]:
    """Humidex in summer, wind chill in winter — whichever the feed supplies."""
    if current.get("humidex") is not None:
        return num(current["humidex"], 0), "humidex"
    if current.get("wind_chill_c") is not None:
        return num(current["wind_chill_c"], 0) + "°", "wind chill"
    return "—", ""


def wind_text(current: Dict[str, Any]) -> str:
    speed = current.get("wind_speed_kmh")
    if speed is None:
        return "—"
    parts = [f"{speed:.0f} km/h"]
    direction = current.get("wind_direction")
    if direction:
        parts.insert(0, str(direction))
    gust = current.get("wind_gust_kmh")
    if gust:
        parts.append(f"gust {gust:.0f}")
    return " ".join(parts)


def short_time(iso: Optional[str]) -> str:
    if not iso:
        return "—"
    text = str(iso)
    if "T" in text:
        date_part, _, rest = text.partition("T")
        return f"{date_part} {rest[:5]}"
    return text


# --------------------------------------------------------------------------- #
# SVG small multiples
# --------------------------------------------------------------------------- #

def sparkline(points: List[Tuple[int, float]], lo: float, hi: float,
              zero_y: Optional[float]) -> str:
    """One city's temperature over time. Shared y-scale across all panels."""
    if not points:
        return ""

    inner_w = SPARK_W - 2 * SPARK_PAD_X
    inner_h = SPARK_H - 2 * SPARK_PAD_Y
    span = (hi - lo) or 1.0
    n = max(len(points) - 1, 1)

    def px(i: int) -> float:
        return SPARK_PAD_X + (i / n) * inner_w if len(points) > 1 else SPARK_W / 2

    def py(value: float) -> float:
        return SPARK_PAD_Y + (1 - (value - lo) / span) * inner_h

    coords = [(px(i), py(value)) for i, value in points]
    parts: List[str] = []

    if zero_y is not None:
        y = SPARK_PAD_Y + (1 - (0 - lo) / span) * inner_h
        parts.append(
            f'<line class="spark-zero" x1="{SPARK_PAD_X}" y1="{y:.1f}" '
            f'x2="{SPARK_W - SPARK_PAD_X}" y2="{y:.1f}" />'
        )

    if len(coords) > 1:
        path = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
        parts.append(f'<polyline class="spark-line" points="{path}" />')

    last_x, last_y = coords[-1]
    parts.append(f'<circle class="spark-dot" cx="{last_x:.1f}" cy="{last_y:.1f}" r="4" />')

    return "".join(parts)


def trends_section(dates: List[str], series: Dict[str, Dict[str, float]],
                   cities_in_order: List[str]) -> str:
    if len(dates) < 2:
        have = len(dates)
        return (
            '<section class="panel">'
            '<h2>Temperature trend</h2>'
            f'<p class="empty">Only {have} day{"" if have == 1 else "s"} of history so far. '
            'Trend charts appear once the job has run on at least two days — '
            'the collector keeps 90 days by default.</p>'
            '</section>'
        )

    values = [v for city in series.values() for v in city.values()]
    lo, hi = min(values), max(values)
    if hi - lo < 4:                       # keep a degenerate range readable
        mid = (hi + lo) / 2
        lo, hi = mid - 2, mid + 2
    zero_y = 0.0 if lo < 0 < hi else None

    index_of = {day: i for i, day in enumerate(dates)}
    panels: List[str] = []

    for city in cities_in_order:
        by_day = series.get(city) or {}
        points = sorted(
            ((index_of[d], v) for d, v in by_day.items() if d in index_of),
            key=lambda p: p[0],
        )
        if not points:
            continue
        latest = points[-1][1]
        panels.append(
            '<figure class="spark">'
            f'<figcaption class="spark-city">{esc(city)}'
            f'<span class="spark-value">{latest:.1f}°</span></figcaption>'
            f'<svg class="spark-svg" viewBox="0 0 {SPARK_W} {SPARK_H}" '
            f'role="img" aria-label="{esc(city)} temperature, latest {latest:.1f} degrees Celsius">'
            f'{sparkline(points, lo, hi, zero_y)}</svg>'
            '</figure>'
        )

    zero_note = ' The hairline marks 0°C.' if zero_y is not None else ''
    return (
        '<section class="panel">'
        '<h2>Temperature trend</h2>'
        f'<p class="panel-note">Observed temperature at each run, '
        f'{esc(dates[0])} to {esc(dates[-1])} ({len(dates)} days). '
        f'All panels share one scale, {lo:.0f}° to {hi:.0f}°C, so heights '
        f'compare directly between cities.{zero_note}</p>'
        f'<div class="spark-grid">{"".join(panels)}</div>'
        '</section>'
    )


# --------------------------------------------------------------------------- #
# Page sections
# --------------------------------------------------------------------------- #

def stat_tiles(data: Dict[str, Any]) -> str:
    cities = data.get("cities") or []
    ok = [c for c in cities if c.get("status") != "error"]
    temps = [
        (c["city"], c["current"]["temperature_c"])
        for c in ok
        if (c.get("current") or {}).get("temperature_c") is not None
    ]
    alert_count = sum(len(c.get("alerts") or []) for c in cities)

    warmest = max(temps, key=lambda t: t[1]) if temps else None
    coldest = min(temps, key=lambda t: t[1]) if temps else None

    def tile(label: str, value: str, sub: str = "", cls: str = "") -> str:
        return (
            f'<div class="tile {cls}">'
            f'<div class="tile-label">{esc(label)}</div>'
            f'<div class="tile-value">{esc(value)}</div>'
            f'<div class="tile-sub">{esc(sub)}</div>'
            '</div>'
        )

    alert_cls = "tile-alert" if alert_count else ""
    return (
        '<section class="tiles">'
        + tile("Cities collected",
               f'{data.get("ok_count", 0)}/{data.get("city_count", 0)}',
               f'run date {data.get("run_date", "—")}')
        + tile("Active alerts", str(alert_count),
               "none in effect" if not alert_count else "see below", alert_cls)
        + tile("Warmest",
               f"{warmest[1]:.1f}°C" if warmest else "—",
               warmest[0] if warmest else "")
        + tile("Coldest",
               f"{coldest[1]:.1f}°C" if coldest else "—",
               coldest[0] if coldest else "")
        + '</section>'
    )


def alerts_section(data: Dict[str, Any]) -> str:
    rows: List[str] = []
    for city in data.get("cities") or []:
        for alert in city.get("alerts") or []:
            kind = (alert.get("type") or "alert").lower()
            severity = "critical" if kind == "warning" else "warning"
            # Icon + word + colour: never colour alone.
            icon = "▲" if severity == "critical" else "●"
            rows.append(
                f'<li class="alert alert-{severity}">'
                f'<span class="alert-icon" aria-hidden="true">{icon}</span>'
                f'<span class="alert-kind">{esc(kind.title())}</span>'
                f'<span class="alert-city">{esc(city.get("city"))} '
                f'({esc(city.get("province"))})</span>'
                f'<span class="alert-desc">{esc(alert.get("description"))}</span>'
                '</li>'
            )
    if not rows:
        return ''
    return (
        '<section class="panel">'
        f'<h2>Active weather alerts ({len(rows)})</h2>'
        f'<ul class="alert-list">{"".join(rows)}</ul>'
        '</section>'
    )


def snapshot_table(data: Dict[str, Any]) -> str:
    head = (
        '<thead><tr>'
        '<th scope="col">City</th><th scope="col">Prov</th>'
        '<th scope="col">Condition</th>'
        '<th scope="col" class="numeric">Temp</th>'
        '<th scope="col" class="numeric">Feels like</th>'
        '<th scope="col">Wind</th>'
        '<th scope="col" class="numeric">RH</th>'
        '<th scope="col" class="numeric">High</th>'
        '<th scope="col" class="numeric">Low</th>'
        '<th scope="col">Next forecast</th>'
        '<th scope="col">Observed</th>'
        '</tr></thead>'
    )

    body: List[str] = []
    for city in data.get("cities") or []:
        if city.get("status") == "error":
            body.append(
                '<tr class="row-error">'
                f'<th scope="row">{esc(city.get("city"))}</th>'
                f'<td>{esc(city.get("province"))}</td>'
                f'<td colspan="9" class="error-cell">'
                f'<span class="alert-icon" aria-hidden="true">▲</span> '
                f'Failed: {esc(city.get("error"))}</td></tr>'
            )
            continue

        current = city.get("current") or {}
        forecasts = city.get("forecasts") or []
        first = forecasts[0] if forecasts else {}
        feels, feels_kind = feels_like(current)
        has_alert = bool(city.get("alerts"))
        # Built outside the f-string: backslash escapes inside f-string
        # expressions are a syntax error before Python 3.12.
        row_open = '<tr class="row-alerted">' if has_alert else '<tr>'
        flag = ('<span class="row-flag" title="under a weather alert">'
                '<span aria-hidden="true">&#9650;</span> alert</span>') if has_alert else ''

        body.append(
            row_open
            + f'<th scope="row">{esc(city.get("city"))}'
            + flag
            + '</th>'
            f'<td>{esc(city.get("province"))}</td>'
            f'<td>{esc(current.get("condition") or "—")}</td>'
            f'<td class="numeric strong">{num(current.get("temperature_c"), 1, "°")}</td>'
            f'<td class="numeric muted">{feels}'
            + (f'<span class="unit"> {esc(feels_kind)}</span>' if feels_kind else '')
            + '</td>'
            f'<td>{esc(wind_text(current))}</td>'
            f'<td class="numeric">{num(current.get("relative_humidity_pct"), 0, "%")}</td>'
            f'<td class="numeric">{num(city.get("next_high_c"), 0, "°")}</td>'
            f'<td class="numeric">{num(city.get("next_low_c"), 0, "°")}</td>'
            f'<td class="forecast">{esc(first.get("period") or "")}: '
            f'{esc(first.get("abbreviated") or first.get("summary") or "—")}</td>'
            f'<td class="muted nowrap">{esc(short_time(current.get("observation_time_local")))}</td>'
            '</tr>'
        )

    return (
        '<section class="panel">'
        '<h2>Current conditions</h2>'
        '<div class="table-scroll">'
        f'<table class="snapshot">{head}<tbody>{"".join(body)}</tbody></table>'
        '</div></section>'
    )


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #

def render_html(data: Dict[str, Any], dates: List[str],
                series: Dict[str, Dict[str, float]]) -> str:
    order = [c.get("city") for c in (data.get("cities") or [])]
    generated = short_time(data.get("generated_at_local"))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Canadian City Weather — {esc(data.get('run_date', ''))}</title>
<link rel="stylesheet" href="dashboard.css">
</head>
<body>
<main class="wrap">
<header class="page-head">
  <h1>Canadian City Weather</h1>
  <p class="subtitle">Environment and Climate Change Canada · run {esc(data.get('run_date', '—'))} ·
     generated {esc(generated)}</p>
</header>
{stat_tiles(data)}
{alerts_section(data)}
{snapshot_table(data)}
{trends_section(dates, series, order)}
<footer class="page-foot">
  <p>{esc(data.get('attribution', ''))}</p>
  <p class="muted">Collector {esc(data.get('collector_version', ''))} ·
     dashboard {esc(__version__)} ·
     source {esc(data.get('source_base_url', ''))}</p>
</footer>
</main>
</body>
</html>
"""


CSS = """/* dashboard.css — external on purpose: Jenkins' CSP for published HTML
   blocks <style> blocks and style="" attributes, but allows a same-origin
   stylesheet. Colours follow the validated data-viz palette. */

:root {
  --surface:        #fcfcfb;
  --plane:          #f9f9f7;
  --ink:            #0b0b0b;
  --ink-2:          #52514e;
  --muted:          #898781;
  --grid:           #e1e0d9;
  --axis:           #c3c2b7;
  --border:         rgba(11, 11, 11, 0.10);
  --series:         #2a78d6;
  --status-critical:#d03b3b;
  --status-warning: #fab219;
  /* #fab219 is only 1.79:1 on the light surface, so text uses a darkened
     step of the same hue; on dark the status step itself clears 9:1. */
  --status-warning-ink: #8a6000;
  color-scheme: light;
}

@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --surface: #1a1a19;
    --plane:   #0d0d0d;
    --ink:     #ffffff;
    --ink-2:   #c3c2b7;
    --muted:   #898781;
    --grid:    #2c2c2a;
    --axis:    #383835;
    --border:  rgba(255, 255, 255, 0.10);
    --series:  #3987e5;
    --status-warning-ink: #fab219;
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --surface: #1a1a19;
  --plane:   #0d0d0d;
  --ink:     #ffffff;
  --ink-2:   #c3c2b7;
  --grid:    #2c2c2a;
  --axis:    #383835;
  --border:  rgba(255, 255, 255, 0.10);
  --series:  #3987e5;
  --status-warning-ink: #fab219;
  color-scheme: dark;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  background: var(--plane);
  color: var(--ink);
  font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
}

.wrap { max-width: 1180px; margin: 0 auto; padding: 24px 20px 48px; }

.page-head { margin-bottom: 20px; }
h1 { font-size: 22px; font-weight: 650; margin: 0 0 4px; letter-spacing: -0.01em; }
.subtitle { margin: 0; color: var(--ink-2); font-size: 13px; }

h2 { font-size: 15px; font-weight: 650; margin: 0 0 12px; }

.panel {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 18px;
  margin-bottom: 16px;
}
.panel-note { margin: -4px 0 16px; color: var(--ink-2); font-size: 12.5px; max-width: 76ch; }
.empty { margin: 0; color: var(--ink-2); }

/* ---- stat tiles ---- */
.tiles {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}
.tile {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px 16px;
}
.tile-label { font-size: 12px; color: var(--ink-2); margin-bottom: 6px; }
.tile-value { font-size: 28px; font-weight: 650; line-height: 1.1; letter-spacing: -0.02em; }
.tile-sub { font-size: 12px; color: var(--muted); margin-top: 4px; }
.tile-alert .tile-value { color: var(--status-critical); }

/* ---- alerts: icon + word + colour, never colour alone ---- */
.alert-list { list-style: none; margin: 0; padding: 0; display: grid; gap: 8px; }
.alert {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 8px;
  padding: 10px 12px;
  border-radius: 8px;
  border: 1px solid var(--border);
  border-left-width: 3px;
}
.alert-critical { border-left-color: var(--status-critical); }
.alert-warning  { border-left-color: var(--status-warning); }
.alert-critical .alert-icon, .alert-critical .alert-kind { color: var(--status-critical); }
.alert-warning  .alert-icon, .alert-warning  .alert-kind { color: var(--status-warning-ink); }
.alert-icon { font-size: 11px; }
.alert-kind { font-weight: 650; font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; }
.alert-city { font-weight: 600; }
.alert-desc { color: var(--ink-2); }

/* ---- snapshot table ---- */
.table-scroll { overflow-x: auto; }
.snapshot { width: 100%; border-collapse: collapse; font-size: 13px; }
.snapshot th, .snapshot td {
  text-align: left;
  padding: 8px 10px;
  border-bottom: 1px solid var(--grid);
  vertical-align: baseline;
}
.snapshot thead th {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--muted);
  font-weight: 600;
  border-bottom: 1px solid var(--axis);
  white-space: nowrap;
}
.snapshot tbody th { font-weight: 600; white-space: nowrap; }
.snapshot tbody tr:last-child th, .snapshot tbody tr:last-child td { border-bottom: 0; }
.numeric { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.strong { font-weight: 650; }
.muted { color: var(--ink-2); }
.nowrap { white-space: nowrap; }
.unit { color: var(--muted); font-size: 11px; }
.forecast { color: var(--ink-2); min-width: 220px; }

.row-flag {
  display: inline-block;
  margin-left: 8px;
  font-size: 10px;
  font-weight: 650;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--status-critical);
}
.row-error th, .row-error td { color: var(--status-critical); }
.error-cell { font-size: 12.5px; }

/* ---- small multiples ---- */
.spark-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 14px 16px;
}
.spark { margin: 0; }
.spark-city {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 8px;
  font-size: 12px;
  font-weight: 600;
  margin-bottom: 2px;
}
.spark-value { font-variant-numeric: tabular-nums; color: var(--ink-2); font-weight: 500; }
.spark-svg { display: block; width: 100%; height: auto; }
.spark-line {
  fill: none;
  stroke: var(--series);
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.spark-dot { fill: var(--series); stroke: var(--surface); stroke-width: 2; }
.spark-zero { stroke: var(--axis); stroke-width: 1; stroke-dasharray: 3 3; }

/* ---- footer ---- */
.page-foot { margin-top: 24px; font-size: 12px; color: var(--ink-2); }
.page-foot p { margin: 0 0 4px; }

@media (max-width: 640px) {
  .wrap { padding: 16px 12px 32px; }
  .tile-value { font-size: 24px; }
}
"""


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render the Canadian city weather pull as a static HTML dashboard.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--outdir", type=Path, default=Path("/var/lib/ca-weather"),
                        help="directory holding latest.json and the dated run folders")
    parser.add_argument("--dest", type=Path, default=None,
                        help="where to write index.html and dashboard.css "
                             "(default: <outdir>/dashboard)")
    parser.add_argument("--days", type=int, default=30,
                        help="how many days of history to chart")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    dest = args.dest or (args.outdir / "dashboard")
    data = load_latest(args.outdir)
    dates, series = load_history(args.outdir, args.days)

    dest.mkdir(parents=True, exist_ok=True)
    (dest / "dashboard.css").write_text(CSS, encoding="utf-8")
    (dest / "index.html").write_text(render_html(data, dates, series), encoding="utf-8")

    alerts = sum(len(c.get("alerts") or []) for c in data.get("cities") or [])
    print(f"Dashboard written to {dest}/index.html")
    print(f"  {data.get('ok_count')}/{data.get('city_count')} cities, "
          f"{alerts} alert(s), {len(dates)} day(s) of history")
    return 0


if __name__ == "__main__":
    sys.exit(main())
