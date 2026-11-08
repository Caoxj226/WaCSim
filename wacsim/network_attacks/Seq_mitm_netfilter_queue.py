"""Master-clock sequential EtherNet/IP masker used by the CTown CRAVE scene."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path

from scapy.layers.inet import IP, TCP
from scapy.packet import Raw

from wacsim.network_attacks.mitm_netfilter_queue_subprocess import PacketQueue
from wacsim.replay.contract import InjectionSpec
from wacsim.replay.network import SequentialMasker


class SeqMiTMNetfilterQueue(PacketQueue):
    """Match replies to queued reads and alter only reviewed CRAVE masker tags."""

    def __init__(self, intermediate_yaml_path: Path, yaml_index: int, queue_number: int):
        super().__init__(intermediate_yaml_path, yaml_index, queue_number)
        config = self.intermediate_attack.get("crave_masker")
        if config:
            spec = InjectionSpec.from_dict(config["spec"])
            if spec.role != "masker" or config["tag"] != "T7SP":
                raise ValueError("unexpected sequential MITM point")
            self.masker = SequentialMasker({config["tag"]: spec},
                                           {config["tag"]: config["scale_interval"]})
            self.audit_path = Path(config["audit_file"])
            self.ready_path = Path(config["ready_file"])
            self.execution_id = config["execution_id"]
        else:
            trigger = self.intermediate_attack["trigger"]
            if trigger["type"] != "time":
                raise ValueError("sequential MITM needs a time trigger for master-clock scheduling")
            duration = trigger["end"] - trigger["start"] + 1
            specs, intervals, offsets, values = {}, {}, {}, {}
            for tag in self.intermediate_attack["tags"]:
                name = tag["tag"]
                specs[name] = InjectionSpec(name, "masker", tag["scaleParam"], duration,
                                            trigger["start"], (-1.e30, 1.e30))
                intervals[name] = tag["scaleTime"]
                if "value" in tag:
                    values[name] = tag["value"]
                else:
                    offsets[name] = tag["offset"]
            self.masker = SequentialMasker(specs, intervals, offsets, values)
            output = Path(self.intermediate_yaml["output_path"])
            self.audit_path = output / f"sequential_mitm_{self.intermediate_attack['name']}.jsonl"
            self.ready_path = output / f"sequential_mitm_{self.intermediate_attack['name']}.ready.json"
            self.execution_id = None

    def on_bound(self):
        self.ready_path.parent.mkdir(parents=True, exist_ok=True)
        self.ready_path.write_text(json.dumps({"execution_id": self.execution_id,
                                               "name": self.intermediate_attack["name"],
                                               "queue": self.queue_number}))

    @staticmethod
    def _connection(packet, response):
        if response:
            return (packet[IP].dst, packet[TCP].dport, packet[IP].src,
                    packet[TCP].sport)
        return (packet[IP].src, packet[TCP].sport, packet[IP].dst,
                packet[TCP].dport)

    def _record(self, events):
        if not events:
            return
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as audit:
            for event in events:
                audit.write(json.dumps({"execution_id": self.execution_id,
                                        **asdict(event)}, allow_nan=False) + "\n")

    def capture(self, packet):
        """Use the simulation master clock, never packet count or value changes."""
        try:
            parsed = IP(packet.get_payload())
            if TCP in parsed and Raw in parsed:
                response = parsed[TCP].sport == 44818
                payload = bytes(parsed[Raw].load)
                modified, events = self.masker.process(
                    payload, self._connection(parsed, response), response,
                    int(self.get_master_clock()))
                if modified != payload:
                    parsed[Raw].load = modified
                    del parsed[IP].chksum
                    del parsed[TCP].chksum
                    packet.set_payload(bytes(parsed))
                self._record(events)
        except Exception as exc:
            # A missing applied-values record subsequently fails the experiment
            # audit; allowing this packet through avoids a hidden packet loss.
            self.logger.error("Sequential MITM packet left unmodified: %s", exc)
        packet.accept()


def is_valid_file(parser_instance, arg):
    if not Path(arg).is_file():
        parser_instance.error(arg + " does not exist")
    return arg


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Start deterministic SeqMiTM NFQUEUE")
    parser.add_argument("intermediate_yaml", type=lambda x: is_valid_file(parser, x))
    parser.add_argument("index", type=int)
    parser.add_argument("number", type=int)
    args = parser.parse_args()
    SeqMiTMNetfilterQueue(Path(args.intermediate_yaml), args.index, args.number).main_loop()
