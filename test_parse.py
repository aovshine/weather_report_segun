#!/usr/bin/env python3
"""
Offline unit tests for the CityPage XML parser. No network required.

    python3 -m unittest test_parse -v
    ./test_parse.py

These cover the paths that are hard to exercise against the live feed —
active weather alerts, missing elements, and coordinate conversion.
"""

import unittest
import xml.etree.ElementTree as ET

from ca_weather_pull import CSV_COLUMNS, dms_to_decimal, flatten, parse_site_data

FULL_XML = """<?xml version="1.0" encoding="ISO-8859-1"?>
<siteData>
  <license>Contains information licensed under the Open Government Licence - Canada.</license>
  <dateTime name="xmlCreation" zone="UTC" UTCOffset="0">
    <year>2026</year><month>09</month><day>09</day><hour>04</hour><minute>01</minute>
    <timeStamp>20260909040100</timeStamp>
  </dateTime>
  <location>
    <country code="ca">Canada</country>
    <province code="MB">Manitoba</province>
    <name code="s0000193" lat="49.90N" lon="97.14W">Winnipeg</name>
    <region>City of Winnipeg</region>
  </location>
  <warnings url="https://weather.gc.ca/warnings/report_e.html?mb7">
    <event type="warning" priority="high" description="Blizzard Warning">
      <dateTime name="eventIssue" zone="UTC" UTCOffset="0">
        <timeStamp>20260909033000</timeStamp>
      </dateTime>
    </event>
    <event type="watch" priority="medium" description="Extreme Cold Watch">
      <dateTime name="eventIssue" zone="UTC" UTCOffset="0">
        <timeStamp>20260909020000</timeStamp>
      </dateTime>
    </event>
  </warnings>
  <currentConditions>
    <station code="YWG">Winnipeg Richardson Int'l Airport</station>
    <dateTime name="observation" zone="UTC" UTCOffset="0">
      <timeStamp>20260909040000</timeStamp>
    </dateTime>
    <dateTime name="observation" zone="CDT" UTCOffset="-5">
      <timeStamp>20260908230000</timeStamp>
    </dateTime>
    <condition>Snow</condition>
    <iconCode format="gif">16</iconCode>
    <temperature unitType="metric" units="C">-24.6</temperature>
    <dewpoint unitType="metric" units="C">-28.1</dewpoint>
    <windChill unitType="metric">-38</windChill>
    <pressure unitType="metric" units="kPa" change="0.4" tendency="rising">99.8</pressure>
    <visibility unitType="metric" units="km">1.2</visibility>
    <relativeHumidity units="%">73</relativeHumidity>
    <wind>
      <speed unitType="metric" units="km/h">45</speed>
      <gust unitType="metric" units="km/h">70</gust>
      <direction>NW</direction>
      <bearing units="degrees">315.0</bearing>
    </wind>
  </currentConditions>
  <forecastGroup>
    <regionalNormals>
      <temperature class="high" unitType="metric" units="C">-9</temperature>
      <temperature class="low" unitType="metric" units="C">-19</temperature>
    </regionalNormals>
    <forecast>
      <period textForecastName="Tonight">Tonight</period>
      <textSummary>Blizzard conditions. Low minus 27.</textSummary>
      <abbreviatedForecast>
        <iconCode format="gif">16</iconCode>
        <pop units="%">90</pop>
        <textSummary>Snow</textSummary>
      </abbreviatedForecast>
      <temperatures>
        <temperature class="low" unitType="metric" units="C">-27</temperature>
      </temperatures>
      <winds><textSummary>NW 50 gusting to 80.</textSummary></winds>
    </forecast>
    <forecast>
      <period textForecastName="Wednesday">Wednesday</period>
      <textSummary>Snow ending. High minus 18.</textSummary>
      <abbreviatedForecast>
        <pop units="%">60</pop>
        <textSummary>Snow ending</textSummary>
      </abbreviatedForecast>
      <temperatures>
        <temperature class="high" unitType="metric" units="C">-18</temperature>
      </temperatures>
      <uv category="low"><index>1</index></uv>
      <relativeHumidity units="%">70</relativeHumidity>
    </forecast>
  </forecastGroup>
  <hourlyForecastGroup>
    <hourlyForecast dateTimeUTC="202609090500">
      <condition>Snow</condition>
      <temperature unitType="metric" units="C">-25</temperature>
      <lop category="High" units="%">90</lop>
      <windChill unitType="metric">-39</windChill>
      <humidex unitType="metric" />
      <wind>
        <speed unitType="metric" units="km/h">50</speed>
        <direction windDirFull="Northwest">NW</direction>
        <gust unitType="metric" units="km/h">80</gust>
      </wind>
    </hourlyForecast>
  </hourlyForecastGroup>
  <yesterdayConditions>
    <temperature class="high" unitType="metric" units="C">-11</temperature>
    <temperature class="low" unitType="metric" units="C">-22</temperature>
    <precip units="mm">4.2</precip>
  </yesterdayConditions>
  <riseSet>
    <dateTime name="sunrise" zone="CST" UTCOffset="-6">
      <timeStamp>20260909073900</timeStamp>
    </dateTime>
    <dateTime name="sunset" zone="CST" UTCOffset="-6">
      <timeStamp>20260909164500</timeStamp>
    </dateTime>
  </riseSet>
</siteData>
"""

# What the live feed actually looks like most days: no warnings, no yesterday block.
SPARSE_XML = """<?xml version="1.0" encoding="ISO-8859-1"?>
<siteData>
  <location>
    <province code="ON">Ontario</province>
    <name code="s0000458" lat="43.65N" lon="79.38W">Toronto</name>
  </location>
  <warnings />
  <currentConditions>
    <station code="XTO">Toronto</station>
    <temperature unitType="metric" units="C"></temperature>
    <wind><speed unitType="metric" units="km/h"></speed></wind>
  </currentConditions>
</siteData>
"""


class TestCoordinates(unittest.TestCase):
    def test_north_and_east_are_positive(self):
        self.assertAlmostEqual(dms_to_decimal("49.90N"), 49.90)
        self.assertAlmostEqual(dms_to_decimal("12.5E"), 12.5)

    def test_south_and_west_are_negative(self):
        self.assertAlmostEqual(dms_to_decimal("97.14W"), -97.14)
        self.assertAlmostEqual(dms_to_decimal("33.1S"), -33.1)

    def test_junk_and_empty(self):
        self.assertIsNone(dms_to_decimal(None))
        self.assertIsNone(dms_to_decimal(""))
        self.assertIsNone(dms_to_decimal("not a coordinate"))
        self.assertAlmostEqual(dms_to_decimal("43.65"), 43.65)


class TestFullFeed(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rec = parse_site_data(ET.fromstring(FULL_XML))

    def test_location(self):
        self.assertEqual(self.rec["feed_location"], "Winnipeg")
        self.assertEqual(self.rec["feed_province"], "MB")
        self.assertEqual(self.rec["region"], "City of Winnipeg")
        self.assertAlmostEqual(self.rec["latitude"], 49.90)
        self.assertAlmostEqual(self.rec["longitude"], -97.14)

    def test_current_conditions(self):
        cur = self.rec["current"]
        self.assertEqual(cur["condition"], "Snow")
        self.assertEqual(cur["temperature_c"], -24.6)
        self.assertEqual(cur["wind_chill_c"], -38.0)
        self.assertEqual(cur["wind_gust_kmh"], 70.0)
        self.assertEqual(cur["pressure_tendency"], "rising")
        self.assertEqual(cur["station"], "Winnipeg Richardson Int'l Airport")
        self.assertEqual(cur["observation_time_utc"], "2026-09-09T04:00:00+00:00")
        self.assertIn("2026-09-08T23:00:00", cur["observation_time_local"])

    def test_alerts(self):
        alerts = self.rec["alerts"]
        self.assertEqual(len(alerts), 2)
        self.assertEqual(alerts[0]["description"], "Blizzard Warning")
        self.assertEqual(alerts[0]["type"], "warning")
        self.assertEqual(alerts[0]["priority"], "high")
        self.assertEqual(alerts[0]["issued"], "2026-09-09T03:30:00+00:00")
        self.assertEqual(alerts[1]["description"], "Extreme Cold Watch")
        self.assertTrue(self.rec["alerts_url"].startswith("https://weather.gc.ca"))

    def test_forecast_and_derived_high_low(self):
        self.assertEqual(len(self.rec["forecasts"]), 2)
        self.assertEqual(self.rec["forecasts"][0]["period"], "Tonight")
        self.assertEqual(self.rec["forecasts"][0]["pop_pct"], 90.0)
        self.assertEqual(self.rec["forecasts"][1]["uv_index"], 1.0)
        self.assertEqual(self.rec["forecasts"][1]["uv_category"], "low")
        # "Tonight" comes first, so the low precedes the high - both must be found.
        self.assertEqual(self.rec["next_low_c"], -27.0)
        self.assertEqual(self.rec["next_high_c"], -18.0)
        self.assertEqual(self.rec["normal_high_c"], -9.0)
        self.assertEqual(self.rec["normal_low_c"], -19.0)

    def test_hourly(self):
        hour = self.rec["hourly"][0]
        self.assertEqual(hour["time_utc"], "2026-09-09T05:00:00+00:00")
        self.assertEqual(hour["temperature_c"], -25.0)
        self.assertEqual(hour["precip_probability_pct"], 90.0)
        self.assertEqual(hour["precip_probability_category"], "High")
        self.assertEqual(hour["wind_gust_kmh"], 80.0)
        self.assertIsNone(hour["humidex"])

    def test_yesterday_and_riseset(self):
        self.assertEqual(self.rec["yesterday"]["high_c"], -11.0)
        self.assertEqual(self.rec["yesterday"]["low_c"], -22.0)
        self.assertEqual(self.rec["yesterday"]["precip_mm"], 4.2)
        self.assertIn("07:39:00", self.rec["sunrise_local"])
        self.assertIn("16:45:00", self.rec["sunset_local"])

    def test_hourly_can_be_skipped(self):
        rec = parse_site_data(ET.fromstring(FULL_XML), include_hourly=False)
        self.assertEqual(rec["hourly"], [])

    def test_flatten_matches_csv_columns(self):
        row = flatten({**self.rec, "city": "Winnipeg", "province": "MB",
                       "site_code": "s0000193", "status": "ok"})
        self.assertEqual(list(row.keys()), CSV_COLUMNS)
        self.assertEqual(row["alert_count"], 2)
        self.assertEqual(row["alerts"], "Blizzard Warning; Extreme Cold Watch")
        self.assertEqual(row["temperature_c"], -24.6)
        self.assertEqual(row["fc1_period"], "Tonight")


class TestSparseFeed(unittest.TestCase):
    """Missing and empty elements must yield None, never an exception."""

    @classmethod
    def setUpClass(cls):
        cls.rec = parse_site_data(ET.fromstring(SPARSE_XML))

    def test_no_crash_and_empty_values(self):
        self.assertEqual(self.rec["feed_location"], "Toronto")
        self.assertIsNone(self.rec["region"])
        self.assertIsNone(self.rec["current"]["temperature_c"])
        self.assertIsNone(self.rec["current"]["wind_speed_kmh"])
        self.assertEqual(self.rec["alerts"], [])
        self.assertEqual(self.rec["forecasts"], [])
        self.assertEqual(self.rec["hourly"], [])
        self.assertIsNone(self.rec["next_high_c"])
        self.assertIsNone(self.rec["yesterday"]["high_c"])
        self.assertIsNone(self.rec["sunrise_local"])

    def test_flatten_still_produces_a_full_row(self):
        row = flatten({**self.rec, "city": "Toronto", "status": "ok"})
        self.assertEqual(list(row.keys()), CSV_COLUMNS)
        self.assertEqual(row["alert_count"], 0)
        self.assertEqual(row["alerts"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
