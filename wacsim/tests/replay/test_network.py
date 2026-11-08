"""Tests for master-clock sequential EtherNet/IP masking, without Mininet."""
import json
import struct

import pytest
from scapy.layers.inet import IP, TCP

from wacsim.network_attacks.Seq_mitm_netfilter_queue import SeqMiTMNetfilterQueue
from wacsim.replay.contract import InjectionSpec
from wacsim.replay.network import NetworkEvent, SequentialMasker


CONNECTION = ("10.0.6.1", 50123, "10.0.10.1", 44818)


def _enip(cip, *, session=9, context=b"context0"):
    cpf = b"\0" * 6 + struct.pack("<H", 2) + struct.pack("<HH", 0, 0)
    cpf += struct.pack("<HH", 0xb2, len(cip)) + cip
    return struct.pack("<HHII", 0x6f, len(cpf), session, 0) + context + b"\0" * 4 + cpf


def _request(tag, **key):
    name = (tag + ":1").encode("ascii")
    path = b"\x91" + bytes((len(name),)) + name + (b"\0" if len(name) % 2 else b"")
    inner = b"\x4c" + bytes((len(path) // 2,)) + path + b"\x01\0"
    return _enip(b"\x52\x02\x20\x06\x24\x01\x05\x9d" + struct.pack("<H", len(inner)) + inner, **key)


def _response(value, **key):
    return _enip(b"\xcc\0\0\0\xca\0" + struct.pack("<f", value), **key)


def _read(masker, tag, value, step, **key):
    masker.process(_request(tag, **key), CONNECTION, False, step)
    payload, events = masker.process(_response(value, **key), CONNECTION, True, step)
    return struct.unpack("<f", payload[-4:])[0], events


def _spec(tag, slope=-1., duration=3, start=0, limits=(0., 5.)):
    return InjectionSpec(tag, "masker", slope, duration, start, limits)


def test_reply_connection_is_canonicalized_to_its_original_request_direction():
    reply = IP(src="10.0.10.1", dst="10.0.6.1") / TCP(sport=44818, dport=50123)
    assert SeqMiTMNetfilterQueue._connection(reply, True) == CONNECTION


def test_master_clock_duration_hold_clamp_and_zero_are_independent_of_packets():
    masker = SequentialMasker({"T7SP": _spec("T7SP", start=10)})
    # A packet at the same master step does not advance the ramp.
    assert _read(masker, "T7SP", 2.5, 10)[0] == pytest.approx(2.5)
    assert _read(masker, "T7SP", 2.5, 10)[0] == pytest.approx(2.5)
    assert _read(masker, "T7SP", 2.5, 11)[0] == pytest.approx(1.5)
    assert _read(masker, "T7SP", 2.5, 12)[0] == pytest.approx(.5)
    assert _read(masker, "T7SP", 2.5, 99)[0] == pytest.approx(.5)

    zero = SequentialMasker({"T7SP": _spec("T7SP", slope=-1., duration=5)})
    received, events = _read(zero, "T7SP", 0., 0)
    assert received == 0. and events[0].original == events[0].applied == 0.

    clamped = SequentialMasker({"T7SP": _spec("T7SP", slope=-4., duration=3)})
    assert _read(clamped, "T7SP", 5., 2)[0] == 0.
    assert _read(clamped, "T7SP", 5., 50)[0] == 0.


def test_session_context_queues_keep_multiple_tags_from_crossing():
    masker = SequentialMasker({"T7": _spec("T7", slope=-.5),
                                "T7SP": _spec("T7SP", slope=-2.)})
    # Same connection/session, two outstanding reads, replies deliberately reversed.
    first = dict(session=71, context=b"request1")
    second = dict(session=71, context=b"request2")
    masker.process(_request("T7", **first), CONNECTION, False, 1)
    masker.process(_request("T7SP", **second), CONNECTION, False, 1)
    second_value, second_events = masker.process(_response(3., **second), CONNECTION, True, 1)
    first_value, first_events = masker.process(_response(1., **first), CONNECTION, True, 1)
    assert struct.unpack("<f", second_value[-4:])[0] == pytest.approx(1.)
    assert struct.unpack("<f", first_value[-4:])[0] == pytest.approx(.5)
    assert [event.tag for event in second_events + first_events] == ["T7SP", "T7"]


def test_session_context_queue_survives_nat_changing_the_ip_tuple_on_reply():
    masker = SequentialMasker({"T7SP": _spec("T7SP", slope=-.5)})
    request = dict(session=91, context=b"natreply")
    masker.process(_request("T7SP", **request), CONNECTION, False, 1)
    nat_visible_reply = ("192.168.1.11", 50123, "10.0.10.1", 44818)
    payload, events = masker.process(_response(2., **request), nat_visible_reply, True, 1)
    assert struct.unpack("<f", payload[-4:])[0] == pytest.approx(1.5)
    assert events[0].tag == "T7SP"


def test_scale_interval_is_master_clock_based_and_terminal_value_holds():
    masker = SequentialMasker({"T7SP": _spec("T7SP")}, {"T7SP": 2})
    actual = [_read(masker, "T7SP", 5., step)[0] for step in range(6)]
    assert actual == pytest.approx([5., 5., 4., 4., 3., 3.])


def test_event_has_the_actual_float32_value_and_audit_format(tmp_path):
    masker = SequentialMasker({"T7SP": _spec("T7SP", slope=-.1)})
    _, events = _read(masker, "T7SP", 2.5, 1)
    event = events[0]
    assert event.applied == pytest.approx(struct.unpack("<f", struct.pack("<f", 2.4))[0])
    audit = tmp_path / "applied.jsonl"
    audit.write_text(json.dumps({"execution_id": "test", **event.__dict__}) + "\n")
    assert json.loads(audit.read_text())["tag"] == "T7SP"


def test_offset_base_is_copied_without_mutating_a_yaml_like_tag_definition():
    yaml_tag = {"tag": "T7SP", "offset": .2, "scaleParam": -.1, "scaleTime": 1}
    snapshot = dict(yaml_tag)
    masker = SequentialMasker({"T7SP": _spec("T7SP", slope=yaml_tag["scaleParam"])},
                               {"T7SP": yaml_tag["scaleTime"]},
                               {"T7SP": yaml_tag["offset"]})
    actual, _ = _read(masker, "T7SP", 2., 0)
    assert actual == pytest.approx(2.2)
    assert yaml_tag == snapshot
