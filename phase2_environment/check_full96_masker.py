"""Independently verify the local-only 9.6 CTown network-masker fixture."""
import json
from pathlib import Path
import struct

import pandas as pd
from scapy.all import IP, Raw, TCP, rdpcap


ROOT = Path(__file__).resolve().parent / "full-96-driver-masker-rerun3"
START, DURATION, SLOPE, TOTAL = 24, 5, -.05, 129


def f32(value):
    return struct.unpack("<f", struct.pack("<f", value))[0]


def main():
    marker = json.loads((ROOT / "full_run_complete.json").read_text())
    audit_file, = ROOT.glob("network_masker_*.jsonl")
    events = [json.loads(line) for line in audit_file.read_text().splitlines()]
    expected_steps = list(range(START, TOTAL))
    if [event["step"] for event in events] != expected_steps:
        raise AssertionError("network masker did not record exactly one T7SP reply per expected step")
    base = events[0]["base"]
    if events[0]["applied"] != f32(base):
        raise AssertionError("first attacked step must retain its captured base")
    for event in events:
        expected = f32(max(0., min(5., base + SLOPE * min(event["step"] - START, DURATION - 1))))
        if event["tag"] != "T7SP" or event["applied"] != expected:
            raise AssertionError(f"incorrect recorded masker value at step {event['step']}")
    if len({event["base"] for event in events}) != 1:
        raise AssertionError("masker base was not captured exactly once")

    plc5 = pd.read_csv(ROOT / "PLC5_values.csv").set_index("iteration")
    for event in events:
        value = float(plc5.loc[event["step"], "T7_LEVEL_SP"])
        if abs(value - event["applied"]) > 1.e-6:
            raise AssertionError(f"PLC5 T7SP differs from recorded packet value at {event['step']}")
    ground = pd.read_csv(ROOT / "ground_truth.csv").set_index("iteration")
    first_changed = next(event["step"] for event in events if event["applied"] != f32(base))
    if ground.loc[:first_changed - 1, "CRAVEMASK"].any() or not ground.loc[first_changed:, "CRAVEMASK"].all():
        raise AssertionError("network attack flag does not start at the first nonzero applied change")

    packets = rdpcap(str(ROOT / "CRAVEMASK-eth0.pcap"))
    tcp_payloads = [packet for packet in packets if TCP in packet and IP in packet and Raw in packet
                    and len(bytes(packet[Raw]))]
    if not tcp_payloads:
        raise AssertionError("attacker capture contains no EtherNet/IP TCP payload")
    report = dict(fixture="synthetic_internal_fixture_not_calibration_or_RQ_result",
                  completed_steps=marker["completed_steps"], tcp_payload_packets=len(tcp_payloads),
                  recorded_modifications=len(events), first_recorded_step=events[0]["step"],
                  first_changed_step=first_changed, last_recorded_step=events[-1]["step"],
                  base=base, terminal_value=events[-1]["applied"],
                  flag_first_recorded_nonzero_step=first_changed,
                  verification="passed")
    (ROOT / "full-96-check.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
