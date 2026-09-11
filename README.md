# Canadian City Weather — daily pull

Pulls current conditions, a multi-day forecast, a 24-hour hourly forecast and any
active weather alerts for the 20 largest Canadian metros, and writes them as CSV
and JSON. Designed to run once a day on an Ubuntu server under Jenkins.

## Data source

Environment and Climate Change Canada — **CityPage Weather**, published on the
Meteorological Service of Canada Datamart:

```
https://dd.weather.gc.ca/today/citypage_weather/{PROV}/{HH}/
  {YYYYMMDD}T{HHMMSS.sss}Z_MSC_CitypageWeather_{siteCode}_{en|fr}.xml
```

Why this source rather than a commercial weather API:

- **No API key, no account, no rate-limit tier.** Nothing to rotate or expire.
- **Licensed under the Open Government Licence – Canada**, which permits
  commercial use with attribution. Most free weather APIs (Open-Meteo's free
  tier included) are non-commercial only, which would be a licensing problem here.
- It is the **authoritative** source for Canadian public forecasts and warnings —
  the same data behind weather.gc.ca.
- Refreshed roughly hourly, every province, every major city.

Attribution is embedded in every JSON output and printed at the end of each run:

> Contains information licensed under the Open Government Licence – Canada.
> Source: Environment and Climate Change Canada.

## What you get

Per run, in `<outdir>`:

```
<outdir>/2026-09-09/canada_weather_2026-09-09.csv    51 columns, one row per city
<outdir>/2026-09-09/canada_weather_2026-09-09.json   full detail, nested
<outdir>/latest.csv                                  copy of the newest run
<outdir>/latest.json
```

**CSV** (flat, ready for Excel/Power BI/a database load) — city, province, site
code, lat/lon, observation time and station, condition, temperature, dewpoint,
humidity, humidex, wind chill, pressure and tendency, visibility, wind
speed/gust/direction/bearing, the next two forecast periods with summary and
temperature, next high and low, seasonal normals, sunrise/sunset, alert count and
alert text, plus run status and the exact source URL each row came from.

**JSON** — everything in the CSV plus the full forecast period list (~12 periods,
six days of day/night), the **24-hour hourly forecast** (temperature, condition,
precipitation probability, wind per hour), and structured alert objects.

Files are written to a temp name and renamed into place, so a consumer reading
`latest.csv` never sees a half-written file.

## Cities

The 20 largest census metropolitan areas, in `cities.json`:

Toronto · Montreal · Vancouver · Calgary · Edmonton · Ottawa · Winnipeg ·
Quebec City · Hamilton · Kitchener · London · Victoria · Halifax · Oshawa ·
Windsor · Saskatoon · Regina · St John's · Kelowna · Barrie

To add or change one, edit `cities.json`. Find the right ECCC site code with:

```bash
./ca_weather_pull.py --discover ON      # every site code + city name for Ontario
```

Each entry has an optional `expect` string. If the feed's own location name does
not contain it, the run logs a WARNING — this is what catches a site code that
was mistyped or that ECCC reassigned. (This check earned its keep: six of the
twenty codes in the first draft of this config pointed at the wrong towns.)

## Install on the Ubuntu server

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip git tzdata

sudo mkdir -p /opt/ca-city-weather /var/lib/ca-weather
sudo chown -R jenkins:jenkins /opt/ca-city-weather /var/lib/ca-weather

sudo -u jenkins git clone <your-repo-url> /opt/ca-city-weather
cd /opt/ca-city-weather
sudo -u jenkins python3 -m venv .venv
sudo -u jenkins .venv/bin/pip install -r requirements.txt

# offline parser tests — no network needed
sudo -u jenkins .venv/bin/python -m unittest test_parse -v

# smoke test — resolves every site code against the live feed, writes nothing
sudo -u jenkins .venv/bin/python ca_weather_pull.py --validate-only
```

`tzdata` matters: without it `--timezone America/Winnipeg` falls back to the
server's local time and your dated folders may land on the wrong day.

Outbound HTTPS to `dd.weather.gc.ca` (and, for `--discover`,
`collaboration.cmc.ec.gc.ca`) must be allowed through the firewall/proxy. If the
server uses a proxy, set `HTTPS_PROXY` in the Jenkins job environment.

## Running it with Ansible (recommended on numerictraining)

`weather_pull.yml` runs the whole thing through Ansible, which fits a Jenkins
server whose other jobs are already Ansible freestyle jobs. It needs no Pipeline
or Git plugin.

```bash
ansible-playbook -i weather_hosts.ini weather_pull.yml
```

It runs unprivileged (`become: false`) against `localhost` with a local
connection — no SSH, no managed host. Everything it needs is checked first, and
each check fails with the exact command that fixes it:

| Preflight check | Fails with |
|---|---|
| Source files present in `src_dir` | which files are missing, and how to point at the checkout |
| `python3` present and ≥ 3.9 | `sudo apt-get install -y python3` |
| `venv` / `ensurepip` importable | `sudo apt-get install -y python3-venv` |
| Timezone database resolves | `sudo apt-get install -y tzdata` |
| `dd.weather.gc.ca` reachable | firewall, proxy, and DNS steps, with the exact error |
| Output directory writable | the `mkdir` + `chown` to run once as an admin |

The network probe is the one that earns its keep. It tries `requests` first and
falls back to stdlib `urllib`, so a proxy that only one of them is configured for
still passes. Without it, a blocked egress path means the collector spends about
eight minutes retrying twenty cities before failing; with it, the run stops in
seconds with a message that names the cause.

After preflight it copies the four runtime files out of the repo into
`~/ca-weather`, builds the virtualenv, runs the offline tests, runs the
collector, then prints a summary, any active weather alerts, and any cities that
failed.

### Variables

Override with `-e`, or in the Jenkins Ansible plugin's **Extra Variables**:

| Variable | Default | Purpose |
|---|---|---|
| `src_dir` | the playbook's own directory | where the repo files are read from |
| `app_dir` | `~/ca-weather` | where the runtime copy and venv live |
| `out_dir` | `/var/lib/ca-weather` | where dated output folders are written |
| `tz` | `America/Winnipeg` | timezone for the run date and folder name |
| `cities` | *(blank)* | comma-separated subset; blank = all 20 |
| `retain_days` | `90` | prune dated folders older than this; `0` keeps all |
| `min_success_pct` | `80` | below this share of cities the collector fails |
| `fail_on_partial` | `false` | set `true` to fail the build on a partial pull |
| `run_tests` | `true` | set `false` to skip the offline parser tests |
| `skip_net_check` | `false` | set `true` to bypass the reachability probe |

### One-time setup on the server

The playbook is deliberately unprivileged, so the output directory is prepared
once by an admin:

```bash
sudo mkdir -p /var/lib/ca-weather
sudo chown jenkins:jenkins /var/lib/ca-weather      # or segun:segun, whoever runs the job
```

If you'd rather avoid that entirely, point it somewhere already writable:

```bash
ansible-playbook -i weather_hosts.ini weather_pull.yml -e out_dir=$HOME/ca-weather-data
```

### Jenkins freestyle job

1. **New Item → Freestyle project**, name it `canada-weather-daily`.
2. **Source Code Management:** `None` if the repo is already checked out on disk
   (then set `src_dir` below), or `Git` if you want Jenkins to pull it.
3. **Build Triggers → Build periodically:**

   ```
   TZ=America/Winnipeg
   30 6 * * *
   ```

4. **Build → Add build step → Invoke Ansible Playbook**
   - **Playbook path:** `weather_pull.yml`, or the absolute path if SCM is None,
     e.g. `/home/segun/weather_report_segun/weather_pull.yml`
   - **Inventory:** *File or host list* → `weather_hosts.ini` (absolute path if
     SCM is None), or *Inline content* → `localhost ansible_connection=local`
   - **Credentials:** leave empty — this runs locally, no SSH
   - **Extra Variables:** add `src_dir` pointing at the checkout if the playbook
     path is absolute
5. **Post-build Actions → Archive the artifacts:** skip this if `out_dir` is
   outside the workspace; read the files from `/var/lib/ca-weather` instead.

Unlike the Pipeline version, a freestyle job has no UNSTABLE state of its own. A
partial pull (exit 2) succeeds by default and prints `WEATHER_PULL_PARTIAL` in
the console. To surface that, either install the **Text Finder** plugin and mark
the build unstable on that string, or set `fail_on_partial=true` to make it a
hard failure.

## Jenkins job (Pipeline alternative)

1. **New Item → Pipeline** (or Multibranch if the repo has branches).
2. **Pipeline script from SCM** → Git → your repo → script path `Jenkinsfile`.
3. Set the agent label in the `Jenkinsfile` (`agent { label 'ubuntu' }`) to match
   your Ubuntu node, or change it to `agent any`.
4. Save, then **Build Now** once. The first build registers the cron trigger —
   scheduled builds do not start until a manual build has run.

The pipeline creates its own virtualenv in the workspace, installs
`requirements.txt`, runs the pull, archives the CSV/JSON and the log, and prunes
output older than `RETAIN_DAYS`.

### Schedule

```groovy
triggers {
    cron('''TZ=America/Winnipeg
            30 6 * * *''')
}
```

06:30 Winnipeg time daily. Change the cron line to move it. Environment Canada
refreshes hourly, so any morning slot works.

### Build parameters

| Parameter | Default | What it does |
|---|---|---|
| `OUTDIR` | `/var/lib/ca-weather` | Persistent output directory on the agent |
| `CITIES` | *(blank)* | Comma-separated subset, e.g. `Toronto,Winnipeg`. Blank = all |
| `RETAIN_DAYS` | `90` | Delete dated folders older than this. `0` keeps everything |
| `MIN_SUCCESS_PCT` | `80` | Below this share of successful cities the build fails |
| `VALIDATE_ONLY` | `false` | Check every site code resolves, write no files |

### Build status

The script's exit code drives the build result:

| Exit | Meaning | Jenkins |
|---|---|---|
| `0` | Every city collected | SUCCESS |
| `2` | Some cities missing, still at or above `MIN_SUCCESS_PCT` | UNSTABLE |
| `1` | Below threshold, or a fatal error | FAILURE |

UNSTABLE is the useful middle state: one station going quiet for an hour should
not page anyone, but it should be visible. If no city at all succeeds the script
writes nothing, so `latest.csv` keeps the last good data rather than being
replaced with an empty file.

Email notification is stubbed out in the `post` block of the `Jenkinsfile` —
uncomment the `mail` steps and set the address, or swap in your Teams webhook.

## Running it by hand

```bash
./ca_weather_pull.py                                  # all cities, ./data
./ca_weather_pull.py --outdir /var/lib/ca-weather
./ca_weather_pull.py --cities Toronto,Winnipeg -v     # subset, debug logging
./ca_weather_pull.py --validate-only                  # health check, writes nothing
./ca_weather_pull.py --discover BC                    # site codes for a province
./ca_weather_pull.py --lang fr                        # French feed
./ca_weather_pull.py --no-hourly                      # smaller JSON
./ca_weather_pull.py --help                           # every option
```

## Without Jenkins

If you ever need it outside Jenkins, a systemd timer does the same job:

```ini
# /etc/systemd/system/ca-weather.service
[Unit]
Description=Canadian city weather pull
After=network-online.target

[Service]
Type=oneshot
User=jenkins
WorkingDirectory=/opt/ca-city-weather
ExecStart=/opt/ca-city-weather/.venv/bin/python /opt/ca-city-weather/ca_weather_pull.py \
          --outdir /var/lib/ca-weather --log-file /var/log/ca-weather.log
```

```ini
# /etc/systemd/system/ca-weather.timer
[Unit]
Description=Daily Canadian city weather pull

[Timer]
OnCalendar=*-*-* 06:30:00
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl enable --now ca-weather.timer
```

## Troubleshooting

**`no CityPage file found for sXXXXXXX in PROV within the last 6h`**
Usually a wrong site code or the wrong province for that code. Run
`--discover <PROV>` and check `cities.json`. If every city fails at once, the
agent has lost outbound access to `dd.weather.gc.ca`.

**`site_code sXXXXXXX resolves to 'Somewhere Else'`**
The code is valid but points at a different town. Correct it from `--discover`.

**Empty `condition` or `wind_speed_kmh` for a city**
Normal and transient — that city's reporting station occasionally omits fields.
The forecast fields will still be populated.

**`yesterday_high_c` / `yesterday_low_c` / `yesterday_precip_mm` always empty**
Expected. ECCC no longer ships `yesterdayConditions` in this feed. The columns
are kept because the parser will pick them up if it comes back.

**Wrong date on the output folder**
Install `tzdata`, or pass `--timezone` explicitly. The folder name uses the
`--timezone` local date, not UTC.

**Everything is slow or intermittently fails**
Lower `--workers` (default 6). This is a free public service; be polite.

**Build stays SUCCESS but data looks stale**
Check `observation_time_utc` in the CSV. ECCC publishes hourly; if the timestamp
is hours behind for one city, that station is the problem, not the script.

## Tests

`test_parse.py` covers the XML parser offline against two fixtures — a full feed
with two active weather alerts, and a sparse feed with missing and empty elements.
No network, so an Environment Canada outage can never make them fail.

```bash
python3 -m unittest test_parse -v      # 13 tests
```

The pipeline runs these before every pull. They exist mainly to cover the alert
path, which is the highest-value field here and is almost never exercised against
the live feed — most days no city in Canada has an active warning.

## Notes

- Python 3.9+. Only third-party dependency is `requests`.
- Requests retry 4 times with exponential backoff on 408/425/429/5xx.
- Downloads run 6-wide; province/hour directory listings are cached per run, so
  the eight Ontario cities cost one listing request, not eight.
- The script searches back up to 6 hourly folders (`--lookback-hours`) to find
  each city's newest file, so a late or skipped ECCC publish does not break the run.
