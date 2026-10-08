"""A stop's live timetable, against a real (tiny) GTFS database.

Unlike the captured cases next to this file, these build a feed, load it
with pygtfs into sqlite and run the real queries, since what is under test
here is mostly SQL: which stops a search finds, which lines a stop offers
in the route filter, and which departures come out.

The feed: line 12 runs City -> Main Street (north side) -> Central Station
platform 1 -> Airport, and back Airport -> platform 2 -> Main Street (south
side) -> City. Line 15 runs City -> Main Street north and ends there.
Everything runs every day; the clock is pinned at 10:00 Paris time.
"""
from __future__ import annotations

import datetime
import zipfile

import pytest

import ha_stub

ha_stub.install()

import homeassistant.util.dt as dt_util  # noqa: E402

pygtfs = pytest.importorskip("pygtfs")
gtfs_helper = ha_stub.load("gtfs_helper")

TIMEZONE = "Europe/Paris"
NOW = datetime.datetime(2026, 10, 9, 10, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=2)))

FEED = {
    "agency.txt": """agency_id,agency_name,agency_url,agency_timezone
A,Agency,https://example.org,Europe/Paris
""",
    "stops.txt": """stop_id,stop_code,stop_name,stop_lat,stop_lon,location_type,parent_station,platform_code
MAIN_N,101,Main Street,48.0001,2.0,0,,
MAIN_S,102,Main Street,48.0,2.0,0,,
STN,,Central Station,48.1,2.1,1,,
STN_1,,Central Station,48.1,2.1,0,STN,1
STN_2,,Central Station,48.1,2.1,0,STN,2
AIRPORT,,Airport,48.2,2.2,0,,
CITY,,City,47.9,1.9,0,,
""",
    "routes.txt": """route_id,agency_id,route_short_name,route_long_name,route_type
R12,A,12,Airport - City,3
R15,A,15,City - Main Street,3
""",
    "calendar.txt": """service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date
S,1,1,1,1,1,1,1,20260101,20271231
""",
    "trips.txt": """route_id,service_id,trip_id,trip_headsign,direction_id
R12,S,OUT_1,Airport,0
R12,S,OUT_2,Airport,0
R12,S,IN_1,City,1
R15,S,END_1,Main Street,0
""",
    "stop_times.txt": """trip_id,arrival_time,departure_time,stop_id,stop_sequence
OUT_1,10:05:00,10:05:00,CITY,1
OUT_1,10:10:00,10:10:00,MAIN_N,2
OUT_1,10:20:00,10:20:00,STN_1,3
OUT_1,10:40:00,10:40:00,AIRPORT,4
OUT_2,11:05:00,11:05:00,CITY,1
OUT_2,11:10:00,11:10:00,MAIN_N,2
OUT_2,11:20:00,11:20:00,STN_1,3
OUT_2,11:40:00,11:40:00,AIRPORT,4
IN_1,10:00:00,10:00:00,AIRPORT,1
IN_1,10:25:00,10:25:00,STN_2,2
IN_1,10:35:00,10:35:00,MAIN_S,3
IN_1,10:45:00,10:45:00,CITY,4
END_1,10:00:00,10:00:00,CITY,1
END_1,10:15:00,10:15:00,MAIN_N,2
""",
}


@pytest.fixture(scope="module")
def schedule(tmp_path_factory):
    folder = tmp_path_factory.mktemp("feed")
    feed_zip = folder / "feed.zip"
    with zipfile.ZipFile(feed_zip, "w") as archive:
        for name, content in FEED.items():
            archive.writestr(name, content)
    schedule = pygtfs.Schedule(str(folder / "feed.sqlite"))
    pygtfs.append_feed(schedule, str(feed_zip))
    return schedule


@pytest.fixture(autouse=True)
def pinned_clock():
    dt_util.set_default_time_zone(dt_util.get_time_zone(TIMEZONE))
    ha_stub.freeze(NOW.timestamp())
    yield
    ha_stub.freeze(None)


class _FakeConfig:
    time_zone = TIMEZONE

    def path(self, value: str = "") -> str:
        return value


class _FakeHass:
    config = _FakeConfig()


class _StopContext:
    """What the coordinator hands get_stop_departures: static only."""

    def __init__(self, schedule, stop, route_filter=None, timerange=120):
        self.hass = _FakeHass()
        self._realtime = False
        self._data = {
            "schedule": schedule,
            "gtfs_dir": "gtfs2",
            "file": "feed",
            "offset": 0,
            "timerange": timerange,
            "departure_stop": stop,
            "route_filter": route_filter or [],
            "max_departures": 20,
        }


def departures(schedule, stop, route_filter=None, timerange=120):
    rows = gtfs_helper.get_stop_departures(_StopContext(schedule, stop, route_filter, timerange))
    return [(d["departure"], d["route"], d["headsign"], d["stop_id"]) for d in rows]


def test_search_finds_both_sides_of_the_street_with_their_lines(schedule):
    found = gtfs_helper.get_stop_search_list(schedule, "main")
    assert [stop["value"] for stop in found] == ["MAIN_N: Main Street", "MAIN_S: Main Street"]
    assert found[0]["label"] == "MAIN_N: Main Street - 12 → Airport, 15 → Main Street"
    assert found[1]["label"] == "MAIN_S: Main Street - 12 → City"


def test_search_by_code_and_station_with_its_platforms(schedule):
    assert [s["value"] for s in gtfs_helper.get_stop_search_list(schedule, "102")] == ["MAIN_S: Main Street"]
    station = [s for s in gtfs_helper.get_stop_search_list(schedule, "Central") if s["value"].startswith("STN:")]
    assert station[0]["label"] == "STN: Central Station [station] - 12 → Airport, 12 → City"


def test_route_filter_offers_each_direction_and_skips_lines_ending_there(schedule):
    # line 15 only ends at Main Street north, it never leaves from there
    assert gtfs_helper.get_stop_route_list(schedule, "MAIN_N: Main Street") == [
        {"value": "R12##0", "label": "12 → Airport"},
    ]
    assert gtfs_helper.get_stop_route_list(schedule, "STN: Central Station") == [
        {"value": "R12##0", "label": "12 → Airport"},
        {"value": "R12##1", "label": "12 → City"},
    ]


def test_departures_from_one_side_of_the_street(schedule):
    assert departures(schedule, "MAIN_N: Main Street") == [
        ("10:10", "12", "Airport", "MAIN_N"),
        ("11:10", "12", "Airport", "MAIN_N"),
    ]
    assert departures(schedule, "MAIN_S: Main Street") == [
        ("10:35", "12", "City", "MAIN_S"),
    ]


def test_a_station_merges_its_platforms_soonest_first(schedule):
    assert departures(schedule, "STN: Central Station") == [
        ("10:20", "12", "Airport", "STN_1"),
        ("10:25", "12", "City", "STN_2"),
        ("11:20", "12", "Airport", "STN_1"),
    ]


def test_route_filter_keeps_one_direction(schedule):
    assert departures(schedule, "STN: Central Station", route_filter=["R12##1"]) == [
        ("10:25", "12", "City", "STN_2"),
    ]


def test_time_range_and_max_departures(schedule):
    assert departures(schedule, "STN: Central Station", timerange=30) == [
        ("10:20", "12", "Airport", "STN_1"),
        ("10:25", "12", "City", "STN_2"),
    ]
    context = _StopContext(schedule, "STN: Central Station")
    context._data["max_departures"] = 1
    assert len(gtfs_helper.get_stop_departures(context)) == 1


def test_route_direction_key_without_direction():
    assert gtfs_helper.route_direction_key("R1", None) == "R1##"
    assert gtfs_helper.route_direction_key("R1", 0) == "R1##0"


def test_local_stops_around_a_location_are_unchanged(schedule):
    """The radius query shares its SQL with the stop query: around Main
    Street it still finds both sides, and keeps a trip ending there."""
    now = dt_util.now().replace(tzinfo=None)
    rows = gtfs_helper._fetch_local_stop_rows(
        schedule, 48.0, 2.0, 200 / 111111, "+60 minute", "-15 minute", now
    )
    assert sorted((row["stop_id"], row["trip_id"]) for row in rows) == [
        ("MAIN_N", "END_1"), ("MAIN_N", "OUT_1"), ("MAIN_S", "IN_1"),
    ]
