import argparse
import os.path
import sqlite3
import time
from pathlib import Path
import random
import importlib.util

import sys
import yaml

from basePLC import BasePLC
from entities.attack import TimeAttack, TriggerBelowAttack, TriggerAboveAttack, TriggerBetweenAttack
from entities.control import AboveControl, BelowControl, TimeControl
from wacsim import py3_logger
import threading
import signal
import pandas as pd
from datetime import datetime


class Error(Exception):
    """Base class for exceptions in this module."""


class TagDoesNotExist(Error):
    """Raised when tag you are looking for does not exist"""


class InvalidControlValue(Error):
    """Raised when tag you are looking for does not exist"""


class DatabaseError(Error):
    """Raised when not being able to connect to the database"""


class GenericPLC(BasePLC):
    """
    This class represents a PLC. This PLC knows what it is connected to by reading the
    YAML file at intermediate_yaml_path and looking at index yaml_index in the plcs section.
    """

    DB_TRIES = 10
    """Amount of times a DB query will retry on an exception"""

    UPDATE_RETRIES = 1
    """Amount of times a PLC will try to update its cache"""

    PLC_CACHE_UPDATE_TIME = 0.05
    """Time in seconds the SCADA server updates its cache"""

    def __init__(self, intermediate_yaml_path, yaml_index):
        self.yaml_index = yaml_index

        with intermediate_yaml_path.open() as yaml_file:
            self.intermediate_yaml = yaml.load(yaml_file, Loader=yaml.FullLoader)

        self.logger = py3_logger.get_logger(self.intermediate_yaml['log_level'])
        self.intermediate_plc = self.intermediate_yaml["plcs"][self.yaml_index]
        self.output_path = Path(self.intermediate_yaml["output_path"]) / (self.intermediate_plc["name"] + "_values.csv")
        self.output_path.touch(exist_ok=True)
        if 'sensors' not in self.intermediate_plc:
            self.intermediate_plc['sensors'] = []
        if 'actuators' not in self.intermediate_plc:
            self.intermediate_plc['actuators'] = []

        self.intermediate_controls = self.intermediate_plc['controls']
        self.controls = self.create_controls(self.intermediate_controls)
        self.crave_pi = None
        self.crave_driver = None
        if 'crave_ctown_runtime' in self.intermediate_yaml and self.intermediate_plc['name'] == 'PLC5':
            from wacsim.replay.full import FullPIControl
            self.crave_pi = FullPIControl(self.intermediate_yaml['crave_ctown_runtime']['config'])
            for attack in self.intermediate_yaml.get('crave_device_attacks', []):
                if attack['plc'] == self.intermediate_plc['name']:
                    from wacsim.replay.device import FullDriverRamp
                    from wacsim.replay.contract import InjectionSpec
                    self.crave_driver = FullDriverRamp(InjectionSpec.from_dict(attack['spec']))
        self.mode = self.intermediate_yaml["mode"]
        self.decision_maker = {}
        if 'decision_maker_per_plc' in self.intermediate_yaml:
            for plc in self.intermediate_yaml['decision_maker_per_plc']:
                if plc['name'] == self.intermediate_plc['name']:
                    for actuator in plc.get('actuators', []):
                        self.decision_maker[actuator['name']] = actuator['decision_maker']
        self.logger.debug(f'PLC {self.intermediate_plc["name"]} has decision_maker: {self.decision_maker}')

        if 'attacks' in self.intermediate_plc.keys():
            self.attacks = self.create_attacks(self.intermediate_plc['attacks'])
        else:
            self.attacks = []

        # Create state from DB values
        state = {
            'name': "plant",
            'path': self.intermediate_yaml['db_path']
        }

        # Create list of dependant sensors
        dependant_sensors = []
        for control in self.intermediate_controls:
            if "dependant" in control:
                dependant_sensors.append(control["dependant"])
        # Create list of PLC sensors
        plc_sensors = self.intermediate_plc['sensors']
        try:
            dependant_sensors.extend(self.intermediate_plc['dependant_sensors'])
        except Exception:
            dependant_sensors.extend(self.intermediate_plc['dependent_sensors'])

        # Create server with real tags generated from sensors, dependants and actuators
        plc_server = {
            'address': self.intermediate_plc['local_ip'],
            'tags': self.generate_real_tags(plc_sensors,
                                            list(set(dependant_sensors) - set(plc_sensors)),
                                            self.intermediate_plc['actuators'])
        }

        # Create protocol
        plc_protocol = {
            'name': 'enip',
            'mode': 1,
            'server': plc_server
        }

        # Create cache for sensor values.
        sensors = self.generate_tags(self.intermediate_plc['sensors'])
        actuators = self.generate_tags(self.intermediate_plc['actuators'])
        columns_list = ['iteration', 'timestamp']
        columns_list.extend(list(set(dependant_sensors) - (set(sensors) | set(actuators))))
        self.write_cache = pd.DataFrame(columns=columns_list)
        self.cache = {}
        self.scadaCache = {}

        # Determine number of iterations from YAML (defaulting to 100 if not provided)
        self.num_iterations = self.intermediate_yaml.get("iterations", 100)

        # Initialize tag freshness as a DataFrame with False values.
        tags_for_cache = list(set(dependant_sensors) - set(plc_sensors))
        self.tag_fresh = pd.DataFrame(False, index=range(self.num_iterations+1), columns=tags_for_cache)
        # Initialize local cache for each dependant tag.
        for tag in tags_for_cache:
            self.cache[tag] = float(0)

        # Initialize SCADA cache and freshness tracking if mode requires it.
        if self.mode == 'scadacontrol' or self.mode == 'hybridcontrol':
            scada_columns = []
            for control in self.intermediate_controls:
                scada_columns.append(f'ScadaCommand_{control["actuator"]}')
                self.scadaCache[f'ScadaCommand_{control["actuator"]}'] = None
            
            # In hybrid mode, register Hybrid_Values_To_Send tags from decision_maker_per_scadacommand
            if self.mode == 'hybridcontrol' and 'decision_maker_per_scadacommand' in self.intermediate_yaml:
                for dm_plc in self.intermediate_yaml['decision_maker_per_scadacommand']:
                    # Only process if this config is for the current PLC
                    if dm_plc['name'] == self.intermediate_plc['name']:
                        for actuator in dm_plc.get('actuators', []):
                            for sensor in actuator.get('Hybrid_Values_To_Send', []):
                                sensor_s_tag = f'{sensor}S'
                                if sensor_s_tag not in scada_columns:
                                    scada_columns.append(sensor_s_tag)
                                    self.scadaCache[sensor_s_tag] = None
            
            # Remove duplicates and create a DataFrame for SCADA tag freshness.
            scada_columns = list(set(scada_columns))
            self.scada_tag_fresh = pd.DataFrame(False, index=range(self.num_iterations+1), columns=scada_columns)

        self.update_cache_flag = False
        self.plcs_ready = False
        self.plc_recieved_scada = False
        self.plc_run = True
        
        # Cache for loaded custom algorithm modules (performance optimization)
        self._module_cache = {}

        self.do_super_construction(plc_protocol, state)

    def do_super_construction(self, plc_protocol, state):
        """
        Performs the super constructor call to BasePLC.
        Introduced to better facilitate testing.
        """
        super(GenericPLC, self).__init__(name=self.intermediate_plc['name'],
                                         state=state, protocol=plc_protocol)

    @staticmethod
    def generate_real_tags(sensors, dependants, actuators):
        """
        Generates real tags with all sensors, dependants, and actuators attached to the PLC.
        """
        real_tags = []
        for sensor_tag in sensors:
            if sensor_tag != "":
                real_tags.append((sensor_tag, 1, 'REAL'))
        for dependant_tag in dependants:
            if dependant_tag != "":
                real_tags.append((dependant_tag, 1, 'REAL'))
        for actuator_tag in actuators:
            if actuator_tag != "":
                real_tags.append((actuator_tag, 1, 'REAL'))
        return tuple(real_tags)

    @staticmethod
    def generate_tags(taggable):
        """
        Generates tags from a list of taggable entities (sensors or actuators).
        """
        tags = []
        if taggable:
            for tag in taggable:
                if tag and tag != "":
                    tags.append((tag, 1))
        return tags

    @staticmethod
    def create_controls(controls_list):
        """
        Generates a list of control objects for a PLC.
        """
        ret = []
        for control in controls_list:
            if control["type"].lower() == "above":
                control_instance = AboveControl(control["actuator"], control["action"],
                                                control["dependant"],
                                                control["value"])
                ret.append(control_instance)
            if control["type"].lower() == "below":
                control_instance = BelowControl(control["actuator"], control["action"],
                                                control["dependant"],
                                                control["value"])
                ret.append(control_instance)
            if control["type"].lower() == "time":
                control_instance = TimeControl(control["actuator"], control["action"],
                                               control["value"])
                ret.append(control_instance)
        return ret

    @staticmethod
    def create_attacks(attack_list):
        """
        Creates an array of DeviceAttacks from a list of attack dicts.
        """
        attacks = []
        for attack in attack_list:
            if attack['trigger']['type'].lower() == "time":
                attacks.append(
                    TimeAttack(attack['name'], attack['actuator'], attack['command'],
                               attack['trigger']['start'], attack['trigger']['end']))
            elif attack['trigger']['type'].lower() == "above":
                attacks.append(
                    TriggerAboveAttack(attack['name'], attack['actuator'], attack['command'],
                                       attack['trigger']['sensor'],
                                       attack['trigger']['value']))
            elif attack['trigger']['type'].lower() == "below":
                attacks.append(
                    TriggerBelowAttack(attack['name'], attack['actuator'], attack['command'],
                                       attack['trigger']['sensor'],
                                       attack['trigger']['value']))
            elif attack['trigger']['type'].lower() == "between":
                attacks.append(
                    TriggerBetweenAttack(attack['name'], attack['actuator'], attack['command'],
                                         attack['trigger']['sensor'],
                                         attack['trigger']['lower_value'],
                                         attack['trigger']['upper_value']))
        return attacks

    def pre_loop(self, sleep=0.5):
        """
        The pre-loop of a PLC. In this phase, everything is set up,
        including starting the sending thread through BasePLC.
        """
        signal.signal(signal.SIGINT, self.sigint_handler)
        signal.signal(signal.SIGTERM, self.sigint_handler)
        self.logger.debug(self.intermediate_plc['name'] + ' enters pre_loop')
        self.db_sleep_time = 0.005  # Fixed small sleep for faster sync (was random 0.01-0.1)
        sensors = self.generate_tags(self.intermediate_plc['sensors'])
        actuators = self.generate_tags(self.intermediate_plc['actuators'])
        values = []
        for tag in sensors:
            values.append(float(self.get(tag)))
        for tag in actuators:
            values.append(int(self.get(tag)))
        # Build noise configuration for this PLC
        # New sensor_noise YAML takes priority over legacy noise_scale_data CSV
        noise_config = 0
        plc_name = self.intermediate_plc["name"]
        try:
            if 'sensor_noise' in self.intermediate_yaml:
                raw_config = None
                sensor_noise_data = self.intermediate_yaml['sensor_noise']
                
                if isinstance(sensor_noise_data, list):
                    # List of dicts format (raw/unparsed config format)
                    for entry in sensor_noise_data:
                        if isinstance(entry, dict) and entry.get('name') == plc_name:
                            raw_config = entry
                            break
                elif isinstance(sensor_noise_data, dict):
                    # Dict format (either processed keyed by PLC name, or raw PLC entry dict)
                    if plc_name in sensor_noise_data:
                        raw_config = sensor_noise_data[plc_name]
                
                if raw_config:
                    if isinstance(raw_config, dict):
                        # Check if it is already in the processed format: {'default': {...}, 'sensors': {...}}
                        if 'default' in raw_config or 'sensors' in raw_config:
                            noise_config = raw_config
                        else:
                            # Translate from raw list/dict format to BasePLC dict expectation
                            translated = {}
                            if 'default_noise' in raw_config or 'default_scale' in raw_config:
                                translated['default'] = {
                                    'type': raw_config.get('default_noise', 'gaussian'),
                                    'scale': float(raw_config.get('default_scale', 0.0))
                                }
                            else:
                                translated['default'] = None
                            
                            translated['sensors'] = {}
                            for s in raw_config.get('sensors', []):
                                if isinstance(s, dict) and 'name' in s:
                                    translated['sensors'][s['name']] = {
                                        'type': s.get('noise_type', 'gaussian'),
                                        'scale': float(s.get('scale', 0.0))
                                    }
                            noise_config = translated
            if noise_config == 0 and 'noise_scale_data' in self.intermediate_yaml and \
               self.intermediate_yaml['noise_scale_data'].get(plc_name):
                # Legacy format: single float noise_scale (backward compatible)
                noise_config = self.intermediate_yaml["noise_scale_data"][plc_name]
        except (KeyError, TypeError):
            noise_config = 0
        BasePLC.set_parameters(self, sensors, actuators, values, self.intermediate_plc['local_ip'], noise_config)
        self.keep_updating_flag = True
        self.cache_update_process = None
        self.cache_updated = False
        time.sleep(sleep)

    def get_tag(self, tag):
        """
        Gets the value of a tag that is connected to this PLC or over the network.
        """
        if tag in self.intermediate_plc["sensors"] or tag in self.intermediate_plc["actuators"]:
            return float(self.get((tag, 1)))
        if tag in self.cache:
            return self.cache[tag]
        self.logger.warning("Cache miss in {plc} for tag {tag}".format(plc=self.intermediate_plc["name"], tag=tag))
        for i, plc_data in enumerate(self.intermediate_yaml["plcs"]):
            if i == self.yaml_index:
                continue
            if tag in plc_data["sensors"] or tag in plc_data["actuators"]:
                received = float(self.receive((tag, 1), plc_data["public_ip"]))
                return received
        raise TagDoesNotExist(tag)

    def get_tag_for_cache(self, tag, plc_ip, cache_update_time):
        """
        Attempts to update the local cache for a tag.
        """
        for retry in range(self.UPDATE_RETRIES):
            clock = self.get_master_clock()
            try:
                self.cache[tag] = float(self.receive((tag, 1), plc_ip))
                self.write_cache.loc[clock, tag] = self.cache[tag]
                return True
            except Exception as e:
                self.logger.info(
                    "{plc} receive {tag} from {ip} failed with exception '{e}'".format(
                        plc=self.intermediate_plc["name"], tag=tag, ip=plc_ip, e=str(e)))
                continue
        return False

    def update_cache(self, cache_update_time):
        """
        Efficiently updates the PLC cache by requesting only stale tags.
        Uses a DataFrame (self.tag_fresh) to track freshness per iteration.
        """
        for i in range(1):
            current_iteration = self.get_master_clock()
            stale_tags = [tag for tag in self.cache if not self.tag_fresh.loc[current_iteration, tag]]
            self.logger.debug("stale_tags: " + str(stale_tags))
            if not stale_tags:
                continue
            start_iteration = current_iteration
            for cached_tag in stale_tags:
                for i, plc_data in enumerate(self.intermediate_yaml["plcs"]):
                    if i == self.yaml_index:
                        continue  # Skip self
                    if cached_tag in plc_data["sensors"] or cached_tag in plc_data["actuators"]:
                        res = self.get_tag_for_cache(cached_tag, plc_data["public_ip"], cache_update_time)
                        if res and self.get_master_clock() == start_iteration:
                            self.tag_fresh.loc[start_iteration, cached_tag] = True
                        else:
                            self.logger.info(f"Warning: Cache for tag {cached_tag} could not be updated")

    def get_tag_for_ScadaCache(self, tag, plc_ip, cache_update_time):
        """
        Attempts to update the SCADA cache for a tag.
        """
        for retry in range(self.UPDATE_RETRIES):
            try:
                self.scadaCache[tag] = float(self.receive((tag, 1), plc_ip))
                self.logger.debug(f'PLC {self.intermediate_plc["name"]} Trying to get {tag} from {plc_ip} and got {self.scadaCache[tag]}')
                return True
            except Exception as e:
                self.logger.info(
                    "{plc} receive {tag} from {ip} failed with exception '{e}'".format(
                        plc=self.intermediate_plc["name"], tag=tag, ip=plc_ip, e=str(e)))
                continue
        return False

    def update_scadaCache(self, cache_update_time):
        """
        Efficiently updates the SCADA cache by only fetching stale tags once per cycle.
        Uses a DataFrame (self.scada_tag_fresh) to track freshness per iteration.
        """
        for i in range(1):
            current_iteration = self.get_master_clock()
            stale_tags = [tag for tag in self.scadaCache if not self.scada_tag_fresh.loc[current_iteration, tag]]
            self.logger.debug("Stale Tags From SCADA: " + str(stale_tags))
            if not stale_tags:
                continue
            start_iteration = current_iteration
            for cached_tag in stale_tags:
                res = self.get_tag_for_ScadaCache(cached_tag, self.intermediate_yaml["scada"]['public_ip'], cache_update_time)
                if res and self.get_master_clock() == start_iteration:
                    self.scada_tag_fresh.loc[start_iteration, cached_tag] = True
                else:
                    self.logger.info(f"Warning: Cache for tag {cached_tag} could not be updated")

    def set_tag(self, tag, value):
        """
        Sets a tag that is connected to this PLC to a value.
        """
        if isinstance(value, str) and value.lower() == "closed":
            value = 0
        elif isinstance(value, str) and value.lower() == "open":
            value = 1
        if tag in self.intermediate_plc["sensors"] or tag in self.intermediate_plc["actuators"]:
            self.set((tag, 1), value)
        else:
            raise TagDoesNotExist(tag + " cannot be set from " + self.intermediate_plc["name"])

    def db_query(self, query, write=False, parameters=None):
        """
        Executes a query on the database with retries.
        """
        for i in range(self.DB_TRIES):
            try:
                with sqlite3.connect(self.intermediate_yaml["db_path"]) as conn:
                    cur = conn.cursor()
                    if parameters:
                        cur.execute(query, parameters)
                    else:
                        cur.execute(query)
                    conn.commit()
                    if not write:
                        return cur.fetchone()[0]
                    else:
                        return
            except sqlite3.OperationalError as exc:
                self.logger.info(
                    "Failed to connect to db with exception {exc}. Trying {i} more times.".format(
                        exc=exc, i=self.DB_TRIES - i - 1))
                time.sleep(self.db_sleep_time)
        self.logger.error("Failed to connect to db. Tried {i} times.".format(i=self.DB_TRIES))
        raise DatabaseError("Failed to get master clock from database")

    def get_master_clock(self):
        """
        Gets the value of the master clock from the database.
        """
        master_time = self.db_query("SELECT time FROM master_time WHERE id IS 1", False, None)
        return master_time

    def get_sync(self, flag):
        """
        Gets the sync flag of this PLC.
        """
        res = self.db_query("SELECT flag FROM sync WHERE name IS ?", False, (self.intermediate_plc["name"],))
        return res == flag

    def set_sync(self, flag):
        """
        Sets this PLC's sync flag in the database.
        """
        self.db_query("UPDATE sync SET flag=? WHERE name IS ?", True, (int(flag), self.intermediate_plc["name"],))

    def scada_get_sync(self, flag):
        """
        Gets the SCADA sync flag.
        """
        res = self.db_query("SELECT flag FROM sync WHERE name IS ?", False, ('scada',))
        return res == flag

    def set_attack_flag(self, flag, attack_name):
        """
        Sets the attack flag in the database.
        """
        self.db_query("UPDATE attack SET flag=? WHERE name IS ?", True, (int(flag), attack_name,))

    def stop_cache_update(self):
        self.update_cache_flag = False

    def sigint_handler(self, sig, frame):
        self.logger.debug('PLC shutdown commencing.')
        self.stop_cache_update()
        self.write_output()
        self.plc_run = False
        self.logger.debug('PLC shutdown finished.')
        sys.exit(0)

    def main_loop(self, sleep=0.5, test_break=False):
        """
        The main loop of a PLC. In here all the controls will be applied.
        Freshness of both local and SCADA caches is now tracked per iteration using DataFrames.
        """
        self.logger.debug(self.intermediate_plc['name'] + ' enters main_loop')
        while self.plc_run:
            if self.mode == 'plccontrol' or self.mode == 'hybridcontrol':
                if not self.plcs_ready:
                    self.plcs_ready = True
                    self.update_cache_flag = True
            while not self.get_sync(0):
                time.sleep(0.001)  # Small sleep to reduce CPU spinning
            self.send_system_state()
            self.set_sync(1)
            while not self.get_sync(2):
                time.sleep(0.001)  # Small sleep to reduce CPU spinning
            if self.mode!='scadacontrol':

                self.update_cache(self.PLC_CACHE_UPDATE_TIME)
            if self.mode == 'plccontrol' or self.mode == 'hybridcontrol':
                current_iteration = self.get_master_clock()
                # Wait until all tags have been updated. TODO, set max attempts
            if self.mode == 'scadacontrol' or self.mode == 'hybridcontrol':
                while not (self.scada_get_sync(25) or self.scada_get_sync(3) or self.get_sync(3)):
                    time.sleep(0.001)  # Small sleep to reduce CPU spinning
                self.logger.debug(f'PLC {self.intermediate_plc["name"]} is here again....')
                self.update_cache_flag = True
                self.plc_recieved_scada = True
                self.update_scadaCache(self.PLC_CACHE_UPDATE_TIME)
                current_iteration = self.get_master_clock()
            LocalSensorsValues = self.get_system_state()
            clock = self.get_master_clock()
            for sensor in LocalSensorsValues.keys():
                self.write_cache.loc[clock, sensor[0]] = LocalSensorsValues[sensor]

            if self.mode == 'plccontrol':
                SkipNextActuatorList = set()
                for control in self.controls:
                    if control.actuator in self.decision_maker:
                        Action = self.decision_maker[control.actuator]
                    else:
                        Action = 'rule'
                    if Action == 'rule' or not Action:
                        self.logger.debug(f'PLC {self.intermediate_plc["name"]} applied {control} because of "PLC" value or no decision maker')
                        control.apply(self)
                    else:
                        if control.actuator in SkipNextActuatorList:
                            continue
                        else:
                            self.logger.debug(f'PLC {self.intermediate_plc["name"]} executing custom algorithm for actuator {control.actuator} (script: {Action})')
                            # Load custom algorithm module (cached for performance)
                            if Action not in self._module_cache:
                                ScriptName = Action.split('/')[-1]
                                spec = importlib.util.spec_from_file_location(ScriptName, Action)
                                module = importlib.util.module_from_spec(spec)
                                sys.modules[ScriptName] = module
                                spec.loader.exec_module(module)
                                self._module_cache[Action] = module
                            else:
                                module = self._module_cache[Action]
                            AlgoRun = getattr(module, 'AlgoRun')
                            result = AlgoRun(self.cache, LocalSensorsValues)
                            if isinstance(result, tuple):
                                result, SkipNextControlActuator = result
                                if SkipNextControlActuator:
                                    SkipNextActuatorList.add(control.actuator)
                            self.logger.debug(f'+++++++++++++ the result from the custom algo IN PLC {self.intermediate_plc["name"]} is:  {result} ++++++++++++++++++')
                            if result == 'rule':
                                control.apply(self)
                            else:
                                control.applyHybridDecision(self, result, None)
                                self.logger.debug(f'PLC {self.intermediate_plc["name"]} applied decision for {control.actuator} with result {result}')
            elif self.mode == 'scadacontrol':
                for control in self.controls:
                    CurrentAction = self.scadaCache[f'ScadaCommand_{control.actuator}']
                    control.applyScadaDecision(self, CurrentAction)
            elif self.mode == 'hybridcontrol':
                SkipNextActuatorList = set()
                print(f'self cache is {self.cache}')
                print(f'LocalSensorsValues is {LocalSensorsValues}')
                print(f'scadaCache is {self.scadaCache}')
                for control in self.controls:
                    if control.actuator in self.decision_maker:
                        HybridAction = self.decision_maker[control.actuator]
                        self.logger.debug("PLC got: " + str(HybridAction))
                    else:
                        HybridAction = 'rule'
                    if HybridAction == 'rule':
                        print(f'PLC {self.intermediate_plc["name"]} applied PLC command')
                        control.apply(self)
                    elif HybridAction == 'scada':
                        print(f'PLC {self.intermediate_plc["name"]} Scada Command')
                        CurrentAction = self.scadaCache[f'ScadaCommand_{control.actuator}']
                        control.applyScadaDecision(self, CurrentAction)
                    elif HybridAction == 'open':
                        control.applyScadaDecision(self, 1.0)
                    elif HybridAction == 'closed':
                        control.applyScadaDecision(self, 0.0)
                    else:
                        if control.actuator in SkipNextActuatorList:
                            continue
                        else:
                            self.logger.debug(f'PLC {self.intermediate_plc["name"]} executing custom algorithm for actuator {control.actuator} (script: {HybridAction})')
                            # Load custom algorithm module (cached for performance)
                            if HybridAction not in self._module_cache:
                                ScriptName = HybridAction.split('/')[-1]
                                spec = importlib.util.spec_from_file_location(ScriptName, HybridAction)
                                module = importlib.util.module_from_spec(spec)
                                sys.modules[ScriptName] = module
                                spec.loader.exec_module(module)
                                self._module_cache[HybridAction] = module
                            else:
                                module = self._module_cache[HybridAction]
                            AlgoRun = getattr(module, 'AlgoRun')
                            result = AlgoRun(self.cache, LocalSensorsValues, self.scadaCache, control)
                            if isinstance(result, tuple):
                                result, SkipNextControlActuator = result
                                if SkipNextControlActuator:
                                    SkipNextActuatorList.add(control.actuator)
                            self.logger.debug(f'+++++++++++++ the result from the custom algo is:  {result} ++++++++++++++++++')
                            if result == 'rule':
                                control.apply(self)
                            else:
                                control.applyHybridDecision(self, result, self.scadaCache[f'ScadaCommand_{control.actuator}'])
            if self.crave_pi is not None:
                self.crave_pi.apply(self, clock)
            for attack in self.attacks:
                attack.apply(self)
            if self.crave_driver is not None:
                self.crave_driver.apply(self)
            self.logger.debug(f'PLC {self.intermediate_plc["name"]} is finished before time update')
            master_time = datetime.now()
            self.logger.debug(f'PLC {self.intermediate_plc["name"]} is after time update')

            self.write_cache.loc[clock, 'iteration'] = clock
            self.write_cache.loc[clock, 'timestamp'] = master_time
            for scada_tag, value in self.scadaCache.items():
                self.write_cache.loc[clock, scada_tag] = value
            for tag in self.cache.keys():
                self.write_cache.loc[clock, tag] = self.cache[tag]
            if 'saving_interval' in self.intermediate_yaml and clock != 0 and clock % self.intermediate_yaml['saving_interval'] == 0:
                self.write_output()
            self.logger.debug(f'PLC {self.intermediate_plc["name"]} finished iteration {clock}, setting sync to 3')
            self.set_sync(3)
            if test_break:
                break

    def write_output(self):
        """
        Writes the CSV output of the PLC.
        """
        results = self.write_cache
        results.to_csv(self.output_path, index=False)
        if self.crave_driver is not None:
            self.crave_driver.save_echo(self.output_path.parent, self.intermediate_yaml['crave_ctown_runtime'])


def is_valid_file(parser_instance, arg):
    if not os.path.exists(arg):
        parser_instance.error(arg + " does not exist")
    else:
        return arg


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Start everything for a plc')
    parser.add_argument(dest="intermediate_yaml",
                        help="intermediate yaml file", metavar="FILE",
                        type=lambda x: is_valid_file(parser, x))
    parser.add_argument(dest="index", help="Index of PLC in intermediate yaml", type=int,
                        metavar="N")
    args = parser.parse_args()
    plc = GenericPLC(
        intermediate_yaml_path=Path(args.intermediate_yaml),
        yaml_index=args.index)
