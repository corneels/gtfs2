"""Entries on the same realtime feed share one request.

Each coordinator refreshes on its own clock, so several entries on one
provider used to download and parse the same feed within seconds of each
other. `get_gtfs_feed_entities` now keeps a response per url and headers for
DEFAULT_RT_CACHE_TTL; these cases pin down what is shared and what is not.

Only `requests.get` and the clock are replaced, the rest runs as is.
"""
from __future__ import annotations

import json
import threading
import time
from unittest.mock import patch

import pytest

import ha_stub

ha_stub.install()

gtfs_rt_helper = ha_stub.load("gtfs_rt_helper")

URL = "https://example.org/gtfs-rt/tripupdates"
FEED = {"entity": [{"id": "1", "trip_update": {"trip": {"trip_id": "t1", "route_id": "r1"}, "stop_time_update": []}}]}


class FakeResponse:
    status_code = 200

    def __init__(self, body=FEED):
        self.text = json.dumps(body)
        self.content = self.text.encode()


@pytest.fixture(autouse=True)
def empty_cache():
    gtfs_rt_helper._rt_cache.clear()
    gtfs_rt_helper._rt_key_locks.clear()
    yield
    gtfs_rt_helper._rt_cache.clear()
    gtfs_rt_helper._rt_key_locks.clear()


def fetch(url=URL, headers=None, label="trip_data"):
    return gtfs_rt_helper.get_gtfs_feed_entities(url=url, headers=headers, label=label)


def test_same_feed_is_fetched_and_parsed_once():
    with patch.object(gtfs_rt_helper.requests, "get", return_value=FakeResponse()) as get:
        first = fetch()
        second = fetch()
    assert get.call_count == 1
    assert first is second
    assert first == FEED["entity"]


def test_labels_share_the_request_but_parse_on_their_own():
    with patch.object(gtfs_rt_helper.requests, "get", return_value=FakeResponse()) as get, \
         patch.object(gtfs_rt_helper, "_parse_feed_entities", wraps=gtfs_rt_helper._parse_feed_entities) as parse:
        fetch(label="trip_data")
        fetch(label="vehicle_positions")
        fetch(label="trip_data")
    assert get.call_count == 1
    assert parse.call_count == 2


def test_different_feeds_and_api_keys_are_not_mixed():
    with patch.object(gtfs_rt_helper.requests, "get", return_value=FakeResponse()) as get:
        fetch(url=URL)
        fetch(url=URL + "?api_key=a")
        fetch(url=URL + "?api_key=b")
        fetch(url=URL, headers={"x_api_key": "a"})
        fetch(url=URL, headers={"x_api_key": "b"})
    assert get.call_count == 5


def test_feed_is_fetched_again_once_the_ttl_has_passed():
    now = time.monotonic()
    with patch.object(gtfs_rt_helper.requests, "get", return_value=FakeResponse()) as get, \
         patch.object(gtfs_rt_helper.time, "monotonic") as clock:
        clock.return_value = now
        fetch()
        clock.return_value = now + gtfs_rt_helper.DEFAULT_RT_CACHE_TTL - 1
        fetch()
        clock.return_value = now + gtfs_rt_helper.DEFAULT_RT_CACHE_TTL
        fetch()
    assert get.call_count == 2


def test_stale_feeds_are_dropped():
    now = time.monotonic()
    with patch.object(gtfs_rt_helper.requests, "get", return_value=FakeResponse()), \
         patch.object(gtfs_rt_helper.time, "monotonic") as clock:
        clock.return_value = now
        fetch(url=URL + "/old")
        clock.return_value = now + gtfs_rt_helper.DEFAULT_RT_CACHE_TTL
        fetch(url=URL + "/new")
    assert [key[0] for key in gtfs_rt_helper._rt_cache] == [URL + "/new"]


def test_a_failing_provider_is_asked_once_per_ttl():
    with patch.object(gtfs_rt_helper.requests, "get", side_effect=TimeoutError("timed out")) as get:
        for _ in range(3):
            with pytest.raises(TimeoutError):
                fetch()
    assert get.call_count == 1


def test_entries_refreshing_together_wait_for_one_request():
    started = threading.Event()

    def slow_get(*args, **kwargs):
        started.set()
        time.sleep(0.2)
        return FakeResponse()

    results = []
    with patch.object(gtfs_rt_helper.requests, "get", side_effect=slow_get) as get:
        threads = [threading.Thread(target=lambda: results.append(fetch())) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    assert get.call_count == 1
    assert len(results) == 5
    assert all(result is results[0] for result in results)


def test_local_files_are_not_shared(tmp_path):
    feed_file = tmp_path / "localstop.rt"
    feed_file.write_text(json.dumps(FEED))
    url = "file://" + str(feed_file)
    assert fetch(url=url) == FEED["entity"]
    assert gtfs_rt_helper._rt_cache == {}
