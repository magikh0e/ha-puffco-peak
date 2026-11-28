import asyncio
import contextlib
from datetime import timedelta

import pytest
from puffco_peak import coordinator
from puffco_peak.const import POLL_ACTIVE, POLL_FIELDS, POLL_IDLE, PUSHED_FIELD_PERIODS
from puffco_peak.device import PuffcoDevice
from puffco_peak.fields import FIELDS
from puffco_peak.lorax import LoraxError

NAMES = {field["path"]: name for name, field in FIELDS.items()}
EVERY_FIELD = sorted(POLL_FIELDS)
UNPUSHED_FIELDS = sorted(set(POLL_FIELDS) - set(PUSHED_FIELD_PERIODS))
ADDRESS = "00:00:00:00:00:00"
SLEEP = 4
IDLE = 5
PREHEAT = 7
ACTIVE = 8
FADE = 9
QI = 1
ENOENT = 2
EFBIG = 27


class Peak:
    def __init__(self, state):
        self.state = state
        self.reads = []
        self.during_read = {}
        self.unreadable = set()
        self.refuses = set()
        self.watchers = {}
        self.handles = {}
        self.link_lost = False

    async def start(self):
        pass

    async def auth(self):
        pass

    def fail_pending(self, _exc):
        self.link_lost = True
        self.handles.clear()

    def content(self, name):
        return bytes([self.state]) if name == "operatingState" else bytes(8)

    async def read_short(self, path):
        name = NAMES[path]
        self.reads.append(name)
        self.during_read.pop(name, lambda: None)()
        if name in self.unreadable:
            raise LoraxError(ENOENT, "ENOENT")
        return self.content(name)

    async def watch(self, path, period, on_value):
        name = NAMES[path]
        if name in self.refuses:
            raise LoraxError(EFBIG, "EFBIG")
        self.watchers[name] = on_value
        self.handles[name] = len(self.handles)
        on_value(self.content(name))
        return self.handles[name]

    def watching(self, handle):
        return handle in self.handles.values()

    def push(self, name, value):
        self.watchers[name](bytes([value]))

    def prune(self):
        self.handles.clear()


class Link:
    def __init__(self, on_disconnect):
        self.is_connected = True
        self.on_disconnect = on_disconnect

    async def pair(self):
        pass

    async def read_gatt_char(self, _uuid):
        return bytes(4)

    def drop(self):
        self.is_connected = False
        self.on_disconnect(self)


@contextlib.asynccontextmanager
async def no_pairing_agent():
    yield


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(coordinator, "monotonic", clock)
    return clock


def polled(peak, pushed=False):
    c = coordinator.PuffcoCoordinator(None, None, ADDRESS)
    device = PuffcoDevice(peak) if peak else None
    c._device = device

    async def connected():
        if device is None:
            raise coordinator.UpdateFailed("out of range")
        return device

    c._ensure_connected = connected
    if device is not None:
        if pushed:
            asyncio.run(c._watch_pushed_fields(device))
        asyncio.run(c.async_request_refresh())
        peak.reads.clear()
    return c


def poll(c, clock, after):
    clock.now += after.total_seconds()
    asyncio.run(c.async_request_refresh())
    return c.data


def test_idle_peak_is_checked_at_the_active_cadence(clock):
    c = polled(Peak(IDLE))
    assert c.update_interval == POLL_ACTIVE


def test_heat_cycle_start_is_seen_on_the_next_check(clock):
    peak = Peak(IDLE)
    c = polled(peak)
    peak.state = PREHEAT
    data = poll(c, clock, POLL_ACTIVE)
    assert data["operatingState"] == PREHEAT
    assert sorted(peak.reads) == EVERY_FIELD


def test_change_between_two_resting_states_is_seen_on_the_next_check(clock):
    peak = Peak(IDLE)
    c = polled(peak)
    peak.state = SLEEP
    data = poll(c, clock, POLL_ACTIVE)
    assert data["operatingState"] == SLEEP


def test_idle_check_reads_only_the_state(clock):
    peak = Peak(IDLE)
    c = polled(peak)
    before = c.data
    assert poll(c, clock, POLL_ACTIVE) == before
    assert peak.reads == ["operatingState"]


def test_idle_peak_is_fully_read_every_idle_interval(clock):
    peak = Peak(IDLE)
    c = polled(peak)
    poll(c, clock, POLL_IDLE)
    assert sorted(peak.reads) == EVERY_FIELD


@pytest.mark.parametrize("state", [PREHEAT, ACTIVE, FADE])
def test_heat_cycle_is_fully_read_on_every_check(clock, state):
    peak = Peak(state)
    c = polled(peak)
    poll(c, clock, POLL_ACTIVE)
    assert sorted(peak.reads) == EVERY_FIELD


def test_field_that_cannot_be_read_does_not_cost_the_others(clock):
    peak = Peak(PREHEAT)
    c = polled(peak)
    peak.unreadable = {"battSoc"}
    data = poll(c, clock, POLL_ACTIVE)
    assert data["battSoc"] is None
    assert sorted(data) == EVERY_FIELD


def test_idle_peak_that_pushes_its_state_is_checked_at_the_idle_cadence(clock):
    c = polled(Peak(IDLE), pushed=True)
    assert c.update_interval == POLL_IDLE


def test_heat_cycle_is_checked_at_the_active_cadence_when_pushed(clock):
    c = polled(Peak(PREHEAT), pushed=True)
    assert c.update_interval == POLL_ACTIVE


def test_pushed_heat_cycle_start_is_published_at_once(clock):
    peak = Peak(IDLE)
    c = polled(peak, pushed=True)
    before = c.data
    peak.push("operatingState", PREHEAT)
    assert c.published == [{**before, "operatingState": PREHEAT}]
    assert peak.reads == []
    assert c.update_interval == POLL_ACTIVE


def test_pushed_change_is_followed_by_a_full_read(clock):
    peak = Peak(IDLE)
    c = polled(peak, pushed=True)
    peak.state = PREHEAT
    peak.push("operatingState", PREHEAT)
    peak.push("operatingState", IDLE)
    peak.state = IDLE
    poll(c, clock, POLL_ACTIVE)
    assert sorted(peak.reads) == UNPUSHED_FIELDS
    assert c.update_interval == POLL_IDLE


def test_state_pushed_during_a_read_survives_that_read(clock):
    peak = Peak(PREHEAT)
    c = polled(peak, pushed=True)
    peak.during_read["battSoc"] = lambda: peak.push("operatingState", ACTIVE)
    data = poll(c, clock, POLL_ACTIVE)
    assert data["operatingState"] == ACTIVE


def test_pushed_state_is_not_read_again(clock):
    peak = Peak(IDLE)
    c = polled(peak, pushed=True)
    poll(c, clock, POLL_IDLE)
    assert "operatingState" not in peak.reads


def test_state_is_read_again_once_the_push_stops(clock):
    peak = Peak(IDLE)
    c = polled(peak, pushed=True)
    peak.prune()
    peak.state = PREHEAT
    data = poll(c, clock, POLL_ACTIVE)
    assert data["operatingState"] == PREHEAT


def test_pushed_state_that_did_not_change_is_ignored(clock):
    peak = Peak(IDLE)
    c = polled(peak, pushed=True)
    peak.push("operatingState", IDLE)
    assert c.published == []


def test_state_pushed_before_the_first_read_is_ignored(clock):
    c = polled(None)
    c._on_push("operatingState", PREHEAT)
    assert c.published == []


def test_wireless_charging_is_published_when_pushed(clock):
    peak = Peak(IDLE)
    c = polled(peak, pushed=True)
    before = c.data
    peak.push("battChargeSource", QI)
    assert c.published == [{**before, "battChargeSource": QI}]
    assert peak.reads == []


def test_pushed_charging_change_is_followed_by_a_full_read(clock):
    peak = Peak(IDLE)
    c = polled(peak, pushed=True)
    peak.push("battChargeSource", QI)
    data = poll(c, clock, POLL_ACTIVE)
    assert "battSoc" in peak.reads
    assert "battChargeSource" not in peak.reads
    assert data["battChargeSource"] == QI
    assert c.update_interval == POLL_IDLE


def test_charging_pushed_during_a_read_survives_that_read(clock):
    peak = Peak(PREHEAT)
    c = polled(peak, pushed=True)
    peak.during_read["battSoc"] = lambda: peak.push("battChargeSource", QI)
    data = poll(c, clock, POLL_ACTIVE)
    assert data["battChargeSource"] == QI


def test_state_is_still_checked_often_when_only_charging_is_pushed(clock):
    peak = Peak(IDLE)
    peak.refuses = {"operatingState", "battChargeState"}
    c = polled(peak, pushed=True)
    peak.push("battChargeSource", QI)
    peak.state = PREHEAT
    data = poll(c, clock, POLL_ACTIVE)
    assert data["operatingState"] == PREHEAT
    assert c.update_interval == POLL_ACTIVE


def test_refused_watch_leaves_the_other_fields_pushed(clock):
    peak = Peak(IDLE)
    peak.refuses = {"battChargeSource"}
    polled(peak, pushed=True)
    assert set(peak.watchers) == {"operatingState", "battChargeState"}


def test_unreachable_peak_is_retried_at_the_idle_cadence(clock):
    c = polled(None)
    c.update_interval = POLL_ACTIVE
    with pytest.raises(coordinator.UpdateFailed):
        poll(c, clock, POLL_ACTIVE)
    assert c.update_interval == POLL_IDLE


def test_pushed_idle_peak_is_fully_read_on_a_refresh_that_fires_early(clock):
    peak = Peak(IDLE)
    c = polled(peak, pushed=True)
    poll(c, clock, POLL_IDLE - timedelta(seconds=0.5))
    assert sorted(peak.reads) == UNPUSHED_FIELDS


def test_push_during_a_full_read_is_followed_by_another(clock):
    peak = Peak(IDLE)
    c = polled(peak, pushed=True)
    peak.during_read["battSoc"] = lambda: peak.push("battChargeSource", QI)
    poll(c, clock, POLL_IDLE)
    assert c.update_interval == POLL_ACTIVE
    peak.reads.clear()
    poll(c, clock, POLL_ACTIVE)
    assert "battSoc" in peak.reads
    assert c.update_interval == POLL_IDLE


def test_command_is_followed_by_a_full_read(clock):
    peak = Peak(IDLE)
    c = polled(peak)

    async def command(_device):
        pass

    asyncio.run(c.async_command(command))
    assert sorted(peak.reads) == EVERY_FIELD


def test_watches_are_set_up_again_after_the_link_drops(clock, monkeypatch):
    links, peaks = [], []

    async def establish_connection(
        _client, _ble_device, _address, disconnected_callback
    ):
        links.append(Link(disconnected_callback))
        return links[-1]

    def lorax_client(_link):
        peaks.append(Peak(IDLE))
        return peaks[-1]

    monkeypatch.setattr(coordinator, "establish_connection", establish_connection)
    monkeypatch.setattr(coordinator, "LoraxClient", lorax_client)
    monkeypatch.setattr(coordinator, "pairing_agent", no_pairing_agent)
    c = coordinator.PuffcoCoordinator(None, None, ADDRESS)
    c._connectable_ble_device = object
    asyncio.run(c.async_request_refresh())
    links[0].drop()
    asyncio.run(c.async_request_refresh())
    before, after = peaks
    assert before.link_lost
    assert set(before.watchers) == set(after.watchers) == set(PUSHED_FIELD_PERIODS)
    assert c.update_interval == POLL_IDLE
