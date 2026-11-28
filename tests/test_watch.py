import asyncio
import functools
import struct

import pytest
from bleak.exc import BleakError
from puffco_peak import lorax
from puffco_peak.device import PuffcoDevice
from puffco_peak.lorax import LoraxClient, LoraxError

COMMAND_UUID = "60133d5c-5727-4f2c-9697-d842c5292a3c"
REPLY_UUID = "8dc5ec05-8f7d-45ad-99db-3fbde65dbd9c"
EVENT_UUID = "43312cd1-7d34-46ce-a7d3-0a98fd9b4cb8"
ACK_EVENTS = 0x03
READ_SHORT = 0x10
FILE_OPEN = 0x20
FILE_WATCH = 0x23
FILE_CLOSE = 0x26
WATCH_EVENT = 0
PRUNE_EVENT = 1
EFBIG = 27
EPROTO = 71

STATE_PATH = "/p/app/stat/id"
PERIOD = 2897
HANDLE = 0
IDLE = b"\x05"
PREHEAT = b"\x07"


class Peak:
    def __init__(self):
        self.is_connected = True
        self.notify = {}
        self.commands = []
        self.failed_writes = 0
        self.event_seq = 0
        self.refusals = {}
        self.content = IDLE
        self.hold = set()
        self.held = []
        self.unanswered = set()
        self.drops_link_at = None
        self.next_handle = HANDLE
        self.handle_format = "<H"
        self.has_events = True

    async def start_notify(self, uuid, callback):
        if uuid == EVENT_UUID and not self.has_events:
            raise BleakError("characteristic not found")
        self.notify[uuid] = callback

    async def write_gatt_char(self, uuid, frame, response):
        seq, opcode = struct.unpack_from("<HB", frame, 0)
        if opcode == self.drops_link_at:
            self.is_connected = False
        if not self.is_connected:
            self.failed_writes += 1
            raise BleakError("not connected")
        if uuid != COMMAND_UUID:
            raise BleakError("characteristic not found")
        payload = bytes(frame[3:])
        self.commands.append((opcode, payload))
        if opcode in self.unanswered:
            self.unanswered.discard(opcode)
            return
        if opcode in self.hold:
            self.held.append(seq)
            return
        status = self.refusals.pop(opcode, 0)
        if status:
            self.reply(seq, status)
        elif opcode == FILE_OPEN:
            self.reply(seq, 0, struct.pack(self.handle_format, self.next_handle))
            self.next_handle += 1
        elif opcode == FILE_WATCH:
            self.reply(seq, 0)
            self.push(self.content, struct.unpack_from("<H", payload)[0])
        else:
            self.reply(seq, 0)

    def reply(self, seq, status, data=b""):
        self.send(REPLY_UUID, struct.pack("<HB", seq, status) + data)

    def event(self, opcode, body, seq=None):
        if seq is None:
            seq = self.event_seq
            self.event_seq += 1
        self.send(EVENT_UUID, struct.pack("<HB", seq, opcode) + body)
        return seq

    def push(self, value, handle=HANDLE):
        return self.event(WATCH_EVENT, record(handle, value))

    def prune(self, handle):
        return self.event(PRUNE_EVENT, struct.pack("<H", handle))

    def release(self):
        self.hold.clear()
        for seq in self.held:
            self.reply(seq, 0)
        self.held.clear()

    def send(self, uuid, frame):
        asyncio.get_running_loop().call_soon(self.notify[uuid], None, bytearray(frame))

    def sent(self, opcode):
        return [payload for op, payload in self.commands if op == opcode]


def record(handle, value):
    return struct.pack("<HB", handle, len(value)) + value


async def settle():
    for _ in range(50):
        await asyncio.sleep(0)


def in_event_loop(test):
    @functools.wraps(test)
    def run(**fixtures):
        unhandled = []

        async def body():
            asyncio.get_running_loop().set_exception_handler(
                lambda _loop, context: unhandled.append(context)
            )
            await test(**fixtures)

        asyncio.run(body())
        assert unhandled == []

    return run


async def connected(peak, max_payload=241, timeout=1.0):
    client = LoraxClient(peak, timeout=timeout)
    client.max_payload = max_payload
    await client.start()
    return client


async def watching(peak, max_payload=241, timeout=1.0):
    client = await connected(peak, max_payload, timeout)
    values = []
    handle = await client.watch(STATE_PATH, PERIOD, values.append)
    assert handle == HANDLE
    await settle()
    return client, values


def acks(peak):
    return [
        [seq for seq, _opcode in struct.iter_unpack("<HB", payload)]
        for payload in peak.sent(ACK_EVENTS)
    ]


@in_event_loop
async def test_watch_opens_the_path_and_watches_the_whole_file():
    peak = Peak()
    await watching(peak)
    assert peak.sent(FILE_OPEN) == [b"\x00" + STATE_PATH.encode()]
    assert peak.sent(FILE_WATCH) == [struct.pack("<HHHHH", HANDLE, PERIOD, 400, 0, 239)]


@in_event_loop
async def test_watch_asks_for_no_more_than_one_record_can_carry():
    peak = Peak()
    await watching(peak, max_payload=512)
    (payload,) = peak.sent(FILE_WATCH)
    assert struct.unpack("<HHHHH", payload)[4] == 255


@in_event_loop
async def test_watcher_gets_the_current_value_then_each_change():
    peak = Peak()
    _client, values = await watching(peak)
    peak.push(PREHEAT)
    await settle()
    assert values == [IDLE, PREHEAT]


@in_event_loop
async def test_every_event_is_acknowledged_by_its_own_seq_and_opcode():
    peak = Peak()
    await watching(peak)
    peak.prune(HANDLE)
    await settle()
    assert peak.sent(ACK_EVENTS) == [
        struct.pack("<HB", 0, WATCH_EVENT),
        struct.pack("<HB", 1, PRUNE_EVENT),
    ]


@in_event_loop
async def test_events_that_arrive_during_an_ack_go_out_in_the_next_one():
    peak = Peak()
    peak.hold.add(ACK_EVENTS)
    await watching(peak)
    peak.push(PREHEAT)
    peak.push(IDLE)
    await settle()
    peak.release()
    await settle()
    assert acks(peak) == [[0], [1, 2]]


@in_event_loop
async def test_ack_carries_no_more_events_than_fit_one_payload():
    peak = Peak()
    peak.hold.add(ACK_EVENTS)
    await watching(peak, max_payload=6)
    for _ in range(3):
        peak.push(PREHEAT)
    await settle()
    peak.release()
    await settle()
    assert acks(peak) == [[0], [1, 2], [3]]


@in_event_loop
async def test_refused_ack_is_sent_again(monkeypatch):
    monkeypatch.setattr(lorax, "ACK_RETRY_DELAY", 0)
    peak = Peak()
    peak.refusals[ACK_EVENTS] = EPROTO
    await watching(peak)
    assert acks(peak) == [[0], [0]]


@in_event_loop
async def test_ack_that_gets_no_reply_is_sent_again(monkeypatch):
    monkeypatch.setattr(lorax, "ACK_RETRY_DELAY", 0)
    peak = Peak()
    peak.unanswered.add(ACK_EVENTS)
    await watching(peak, timeout=0.05)
    await asyncio.sleep(0.1)
    await settle()
    assert acks(peak) == [[0], [0]]


@in_event_loop
async def test_ack_is_not_sent_again_once_the_link_is_down(monkeypatch):
    monkeypatch.setattr(lorax, "ACK_RETRY_DELAY", 0)
    peak = Peak()
    await watching(peak)
    peak.is_connected = False
    peak.push(PREHEAT)
    await settle()
    assert peak.failed_writes == 1


@in_event_loop
async def test_event_is_not_taken_for_the_reply_with_the_same_seq():
    peak = Peak()
    client, values = await watching(peak)
    peak.hold.add(READ_SHORT)
    read = asyncio.create_task(client.read_short(STATE_PATH))
    await settle()
    (seq,) = peak.held
    peak.event(WATCH_EVENT, record(HANDLE, PREHEAT), seq=seq)
    await settle()
    assert not read.done()
    peak.reply(seq, 0, IDLE)
    assert await read == IDLE
    assert values == [IDLE, PREHEAT]


@in_event_loop
async def test_pruned_handle_is_no_longer_watched():
    peak = Peak()
    client, _values = await watching(peak)
    assert client.watching(HANDLE)
    peak.prune(HANDLE)
    await settle()
    assert not client.watching(HANDLE)
    assert acks(peak) == [[0], [1]]


@in_event_loop
async def test_prune_with_a_stray_trailing_byte_still_drops_the_handle():
    peak = Peak()
    client, _values = await watching(peak)
    peak.event(PRUNE_EVENT, struct.pack("<H", HANDLE) + b"\x00")
    await settle()
    assert not client.watching(HANDLE)


@in_event_loop
async def test_refused_watch_closes_the_handle():
    peak = Peak()
    peak.refusals[FILE_WATCH] = EFBIG
    client = await connected(peak)
    with pytest.raises(LoraxError):
        await client.watch(STATE_PATH, PERIOD, lambda value: None)
    assert not client.watching(HANDLE)
    assert peak.sent(FILE_CLOSE) == [struct.pack("<H", HANDLE)]


@in_event_loop
async def test_watch_that_loses_the_link_writes_nothing_more():
    peak = Peak()
    peak.drops_link_at = FILE_WATCH
    client = await connected(peak)
    with pytest.raises(BleakError):
        await client.watch(STATE_PATH, PERIOD, lambda value: None)
    assert not client.watching(HANDLE)
    assert peak.failed_writes == 1


@in_event_loop
async def test_disconnect_drops_every_watch():
    peak = Peak()
    client, _values = await watching(peak)
    client.fail_pending(BleakError("disconnected"))
    assert not client.watching(HANDLE)


@in_event_loop
async def test_charger_event_captured_from_a_peak_pro_reaches_both_watchers():
    peak = Peak()
    client = await connected(peak)
    source, state = [], []
    await client.watch("/p/bat/chg/src", 4096, source.append)
    await client.watch("/p/bat/chg/stat", 4096, state.append)
    await settle()
    peak.event(WATCH_EVENT, bytes.fromhex("00 00 01 03 01 00 01 04"))
    await settle()
    assert source == [peak.content, b"\x03"]
    assert state == [peak.content, b"\x04"]
    assert acks(peak) == [[0], [1], [2]]


@in_event_loop
async def test_failing_watcher_does_not_cost_the_other_records():
    peak = Peak()
    client = await connected(peak)
    failing = await client.watch(STATE_PATH, PERIOD, lambda value: 1 / 0)
    others = []
    other = await client.watch("/p/bat/chg/src", PERIOD, others.append)
    await settle()
    peak.event(WATCH_EVENT, record(failing, PREHEAT) + record(other, PREHEAT))
    await settle()
    assert others == [IDLE, PREHEAT]
    assert acks(peak) == [[0], [1], [2]]


@in_event_loop
async def test_record_for_an_unwatched_handle_does_not_cost_the_others():
    peak = Peak()
    _client, values = await watching(peak)
    peak.event(WATCH_EVENT, record(99, PREHEAT) + record(HANDLE, PREHEAT))
    await settle()
    assert values == [IDLE, PREHEAT]


@in_event_loop
async def test_record_cut_short_is_dropped():
    peak = Peak()
    _client, values = await watching(peak)
    cut_short = struct.pack("<HB", HANDLE, 4) + IDLE
    peak.event(WATCH_EVENT, record(HANDLE, PREHEAT) + cut_short)
    await settle()
    assert values == [IDLE, PREHEAT]
    assert acks(peak) == [[0], [1]]


@in_event_loop
async def test_event_frame_too_short_to_parse_is_ignored():
    peak = Peak()
    _client, values = await watching(peak)
    peak.send(EVENT_UUID, b"\x01\x00")
    await settle()
    assert values == [IDLE]
    assert acks(peak) == [[0]]


@in_event_loop
async def test_file_open_reply_that_is_not_one_handle_is_refused():
    peak = Peak()
    peak.handle_format = "<I"
    client = await connected(peak)
    with pytest.raises(LoraxError):
        await client.watch(STATE_PATH, PERIOD, lambda value: None)
    assert peak.sent(FILE_WATCH) == []


@in_event_loop
async def test_watch_is_refused_without_an_event_channel():
    peak = Peak()
    peak.has_events = False
    client = await connected(peak)
    with pytest.raises(BleakError):
        await client.watch(STATE_PATH, PERIOD, lambda value: None)
    assert peak.sent(FILE_OPEN) == []


@in_event_loop
async def test_device_decodes_pushes_and_forgets_a_pruned_field():
    peak = Peak()
    device = PuffcoDevice(await connected(peak))
    values = []
    await device.watch("operatingState", PERIOD, values.append)
    await settle()
    peak.push(PREHEAT)
    await settle()
    assert peak.sent(FILE_OPEN) == [b"\x00" + STATE_PATH.encode()]
    assert values == [5, 7]
    assert device.watching("operatingState")
    assert device.pushed() == {"operatingState": 7}
    peak.prune(HANDLE)
    await settle()
    assert device.pushed() == {}
    assert not device.watching("operatingState")
