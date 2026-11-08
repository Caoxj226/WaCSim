"""Deterministic EtherNet/IP read-response masker for the opt-in CTown scene.

Only complete, successful CIP REAL reads are eligible. The request's ENIP
session and sender context retain the tag queue, so interleaved tags cannot
exchange replies even when the local CTown route NATs the return packet. The
ramp itself is the shared replay Ramp.
"""
from collections import defaultdict, deque
from dataclasses import dataclass
import math
import struct

from .contract import InjectionSpec
from .injections import Ramp


def _u16(data, offset):
    return struct.unpack_from("<H", data, offset)[0]


def _frames(payload):
    pos = 0
    while pos + 24 <= len(payload):
        end = pos + 24 + _u16(payload, pos + 2)
        if end > len(payload):
            return
        yield pos, end
        pos = end


def _cip_item(data, start, end):
    """Return the exact CPF data item, never guess by packet length."""
    if end - start < 32 or _u16(data, start) != 0x6f:
        return None
    pos = start + 24 + 6
    count = _u16(data, pos)
    pos += 2
    for _ in range(count):
        if pos + 4 > end:
            return None
        kind, length = struct.unpack_from("<HH", data, pos)
        pos += 4
        if pos + length > end:
            return None
        if kind in (0xb1, 0xb2):
            return pos, pos + length
        pos += length
    return None


def _read_tag(data, first, end):
    if first >= end:
        return None
    cip = first
    if data[cip] == 0x52:  # Unconnected Send wraps the ordinary read request.
        path_end = cip + 2 + 2 * data[cip + 1]
        if path_end + 4 > end:
            return None
        cip = path_end + 4
    if cip + 4 > end or data[cip] != 0x4c:
        return None
    path_end = cip + 2 + 2 * data[cip + 1]
    if path_end > end or data[cip + 2] != 0x91:
        return None
    length = data[cip + 3]  # ANSI segment counts bytes, not 16-bit words.
    if cip + 4 + length > path_end:
        return None
    try:
        return data[cip + 4:cip + 4 + length].decode("ascii").split(":", 1)[0]
    except UnicodeDecodeError:
        return None


def _real_offset(data, first, end):
    if end - first != 10 or data[first] != 0xcc or data[first + 2:first + 4] != b"\0\0":
        return None
    if _u16(data, first + 4) != 0x00ca:
        return None
    return first + 6


@dataclass
class NetworkEvent:
    step: int
    tag: str
    session: int
    context: str
    original: float
    base: float
    applied: float


class SequentialMasker:
    def __init__(self, specs: dict[str, InjectionSpec], scale_intervals=None,
                 base_adjustments=None, fixed_bases=None):
        self.ramps = {tag: Ramp(spec) for tag, spec in specs.items()}
        self.intervals = scale_intervals or {tag: 1 for tag in specs}
        self.base_adjustments = base_adjustments or {}
        self.fixed_bases = fixed_bases or {}
        if any(type(n) is not int or n < 1 for n in self.intervals.values()):
            raise ValueError("scale interval must be a positive integer")
        self.pending = defaultdict(deque)
        self.events = []

    def process(self, payload: bytes, connection: tuple, response: bool, step: int):
        output = bytearray(payload)
        events = []
        for start, end in _frames(payload):
            item = _cip_item(payload, start, end)
            if item is None:
                continue
            session = struct.unpack_from("<I", payload, start + 4)[0]
            context = payload[start + 12:start + 20]
            # EtherNet/IP session plus sender context is the request/reply
            # identity. Do not use the IP tuple here: the CTown attacker NATs
            # the return leg, so its pre-DNAT destination differs from the
            # PLC5 source address observed on the request leg.
            key = (session, context)
            first, last = item
            if not response:
                tag = _read_tag(payload, first, last)
                if tag is not None:
                    self.pending[key].append(tag)
                continue
            if not self.pending[key]:
                continue
            tag = self.pending[key].popleft()
            if not self.pending[key]:
                del self.pending[key]
            if tag not in self.ramps:
                continue
            value_offset = _real_offset(payload, first, last)
            if value_offset is None:
                continue
            original = struct.unpack_from("<f", payload, value_offset)[0]
            if not math.isfinite(original):
                continue
            ramp = self.ramps[tag]
            if step < ramp.spec.start_step:
                continue
            if ramp.base_value is None:
                ramp.base_value = self.fixed_bases.get(tag, original + self.base_adjustments.get(tag, 0.))
            elapsed = step - ramp.spec.start_step
            scaled_step = ramp.spec.start_step + (elapsed // self.intervals[tag])
            # scaleTime advances by one scaleParam per interval. CRAVE uses 1,
            # so this is the identical Ramp calculation as fast replay.
            applied = ramp.value(scaled_step, original)
            encoded = struct.pack("<f", applied)
            output[value_offset:value_offset + 4] = encoded
            event = NetworkEvent(step, tag, session, context.hex(), original,
                                 ramp.base_value, struct.unpack("<f", encoded)[0])
            events.append(event)
            self.events.append(event)
        return bytes(output), events
