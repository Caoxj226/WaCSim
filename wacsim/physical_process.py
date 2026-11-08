import argparse
import csv
import json
import os
import signal
import logging
from datetime import datetime
import random
import pandas as pd
import progressbar
import sqlite3
import sys
import time
import math
from pathlib import Path

from wacsim.parser.file_generator import BatchReadmeGenerator, GeneralReadmeGenerator
from wacsim.py3_logger import get_logger
import yaml

from epanet import toolkit as en
import wntr
import wntr.network.controls as controls
from decimal import Decimal


class Error(Exception):
    """Base class for exceptions in this module."""


class DatabaseError(Error):
    """Raised when not being able to connect to the database"""


class PhysicalPlant:
    """
    Class representing the plant itself, runs each iteration. This class also deals with WNTR
    and updates the database.
    """

    WAIT_FOR_FLAG = 0.005
    """Amount of times to wait for a flag """

    DB_TRIES = 10
    """Amount of times a db query will retry on a exception"""

    def __init__(self, intermediate_yaml):
        signal.signal(signal.SIGINT, self.interrupt)
        signal.signal(signal.SIGTERM, self.interrupt)

        self.intermediate_yaml = intermediate_yaml

        with self.intermediate_yaml.open(mode='r') as file:
            self.data = yaml.safe_load(file)

        logging.getLogger('wntr').setLevel(logging.WARNING)
        self.logger = get_logger(self.data['log_level'])

        self.ground_truth_path = Path(self.data["output_path"]) / "ground_truth.csv"
        self.ground_truth_path.touch(exist_ok=True)

        # Use of prepared statements
        self._name = 'plant'
        self._path = self.data["db_path"]
        self._value = 'value'
        self._what = ()

        self._init_what()

        if not self._what:
            raise ValueError('Primary key not found.')
        else:
            self._init_get_query()
            self._init_set_query()

        # connection to the database
        self.db_path = self.data["db_path"]

        # get simulator: WNTR
        self.prepare_simulator()

        self.scada_junction_list = self.get_scada_junction_list(self.data['plcs'])
        self.values_list = list()

        list_header = ['iteration', 'timestamp']
        list_header.extend(self.create_node_header(self.tank_list))
        list_header.extend(self.create_node_header(self.junction_list))
        list_header.extend(self.create_link_header(self.pump_list, include_speed=True))  # Include pump speed
        list_header.extend(self.create_link_header(self.valve_list))

        list_header.extend(self.create_attack_header())
        if 'crave_ctown_runtime' in self.data:
            list_header.append('hydraulic_time_seconds')

        self.results_list = []
        self.results_list.append(list_header)

        # Set initial physical conditions
        self.set_initial_values()

        self.logger.info("Starting simulation for " +
                         os.path.basename(str(self.data['inp_file']))[:-4] + " topology.")

        self.start_time = datetime.now()
        self.deficit_df_initialized = False
        # Build initial list of actuators
        self.sim = wntr.sim.WNTRSimulator(self.wn)
        self.master_time = 0
        self.connected_links_found = False
        # Initialize dataframes early to avoid errors if simulation exits before first iteration
        self.water_loss_df = pd.DataFrame(0, index=range(self.data["iterations"]+1), columns=self.tank_list)
        self.demand_deficit_df = pd.DataFrame(0, index=range(self.data["iterations"]+1), columns=self.junction_list)
        self.db_update_string = "UPDATE plant SET value = ? WHERE name = ?"

        self.db_sleep_time = random.uniform(0.01, 0.1)
        self.logger.info("DB Sleep time: " + str(self.db_sleep_time))

    def set_sync(self, flag):
        """
        Set this plcs sync flag in the sync table. When this is 1, the physical process
        knows this plc finished the requested iteration.
        On a :code:`sqlite3.OperationalError` it will retry with a max of :code:`DB_TRIES` tries.
        Before it reties, it will sleep for :code:`DB_SLEEP_TIME` seconds.
        :param flag: True for sync to 1, False for sync to 0
        :type flag: bool
        :raise DatabaseError: When a :code:`sqlite3.OperationalError` is still raised after
           :code:`DB_TRIES` tries.
        """
        #"UPDATE sync SET flag=2"
        self.db_query("UPDATE sync SET flag=?", True, (int(flag),))

    def db_query(self, query, write=False, parameters=None):
        """
        Execute a query on the database
        On a :code:`sqlite3.OperationalError` it will retry with a max of :code:`DB_TRIES` tries.
        Before it reties, it will sleep for :code:`DB_SLEEP_TIME` seconds.
        This is necessary because of the limited concurrency in SQLite.
        :param query: The SQL query to execute in the db
        :type query: str
        :param write: Boolean flag to indicate if this query will write into the database
        :param parameters: The parameters to put in the query. This must be a tuple.
        :raise DatabaseError: When a :code:`sqlite3.OperationalError` is still raised after
           :code:`DB_TRIES` tries.
        """
        for i in range(self.DB_TRIES):
            try:
                with sqlite3.connect(self.data["db_path"]) as conn:
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
        raise DatabaseError("Failed to execute db query in database")
    
    
    def calc_water_loss(self):
        if not self.connected_links_found:
            self.connected_link_dict = {}
            self.reverse_dict = {}
            self.prev_tank_level_dict = {}
            self.water_loss_df = pd.DataFrame(0, index=range(self.data["iterations"]+1), columns=self.tank_list)
            for link in self.link_list:
                link = self.wn.get_link(link)
                if link.start_node_name in self.tank_list:
                    self.connected_link_dict[link.start_node_name] = link.name
                    self.reverse_dict[link.start_node_name] = False
                elif link.end_node_name in self.tank_list:
                    self.connected_link_dict[link.end_node_name] = link.name
                    self.reverse_dict[link.end_node_name] = True  
                                         
            for tank in self.tank_list:
                self.prev_tank_level_dict[tank] = self.wn.get_node(tank).init_level
            self.logger.debug("Done with setup")
            self.connected_links_found = True
        for tank in self.tank_list:
            tank = self.wn.get_node(tank)
            link = self.wn.get_link(self.connected_link_dict[tank.name])
            idx = en.getlinkindex(ph=self.proj, id=link.name)
            flow = en.getlinkvalue(ph=self.proj, index=idx, property=en.FLOW)/3600
            if self.reverse_dict[tank.name]:
                V_proj = tank.get_volume(self.prev_tank_level_dict[tank.name]) + flow*self.simulation_step
            else:
                V_proj = tank.get_volume(self.prev_tank_level_dict[tank.name]) - flow*self.simulation_step
                
            if V_proj > tank.get_volume(tank.max_level) and tank.overflow:
                self.water_loss_df.loc[self.master_time, tank.name] = V_proj - tank.get_volume(tank.max_level)
                self.logger.warning("Water loss at " + tank.name + ": " + str(self.water_loss[self.master_time, tank.name]) + " m^3")
            else:
                self.water_loss_df.loc[self.master_time, tank.name] = 0
            idx = en.getnodeindex(ph=self.proj, id=tank.name)
            self.prev_tank_level_dict[tank.name] = en.getnodevalue(ph=self.proj, index=idx, property=en.PRESSURE)
        return
            
    def calc_demand_deficit(self):
        if not self.deficit_df_initialized:
            self.demand_deficit_df = pd.DataFrame(0, index=range(self.data["iterations"]+1), columns=self.junction_list)
            self.deficit_df_initialized = True
        for junction in self.junction_list:
            idx = en.getnodeindex(ph=self.proj, id=junction)
            self.demand_deficit_df.loc[self.master_time, junction] = en.getnodevalue(ph=self.proj, index=idx, property=en.DEMANDDEFICIT)
                    
        
    def prepare_simulator(self):
        self.logger.info("Preparing wntr simulation")
        self.wn = wntr.network.WaterNetworkModel(self.data['inp_file'])

        self.node_list = list(self.wn.node_name_list)
        self.link_list = list(self.wn.link_name_list)

        self.tank_list = self.get_node_list_by_type(self.node_list, 'Tank')
        self.junction_list = self.get_node_list_by_type(self.node_list, 'Junction')

        self.pump_list = self.get_link_list_by_type(self.link_list, 'Pump')
        self.valve_list = self.get_link_list_by_type(self.link_list, 'Valve')

        dummy_condition = controls.ValueCondition(self.wn.get_node(self.tank_list[0]), 'level',
                                                  '>=', -1)

        self.control_list = []
        for valve in self.valve_list:
            self.control_list.append(self.create_control_dict(valve, dummy_condition))

        for pump in self.pump_list:
            self.control_list.append(self.create_control_dict(pump, dummy_condition))

        for control in self.control_list:
            an_action = controls.ControlAction(control['actuator'], control['parameter'],
                                               control['value'])
            a_control = controls.Control(control['condition'], an_action, name=control['name'])
            self.wn.add_control(control['name'], a_control)

        if self.data['demand'] == 'pdd':
            self.wn.options.hydraulic.demand_model = 'PDD'

        self.simulation_step = self.wn.options.time.hydraulic_timestep

    def create_control_dict(self, actuator, dummy_condition):
        act_dict = dict.fromkeys(['actuator', 'parameter', 'value', 'condition', 'name'])
        act_dict['actuator'] = self.wn.get_link(actuator)
        act_dict['parameter'] = 'status'
        act_dict['condition'] = dummy_condition
        act_dict['name'] = actuator
        if type(self.wn.get_link(actuator).status) is int:
            act_dict['value'] = act_dict['actuator'].status
        else:
            act_dict['value'] = act_dict['actuator'].status.value
        return act_dict

    # toDo: Develop a test for this method
    def remove_controls_from_inp_file(self, in_file, out_file):
        write_out = True
        with open(in_file) as infile, open(out_file, "w") as outfile:
            for line in infile:
                if write_out:
                    outfile.write(line)
                if line.startswith('[CONTROLS]'):
                    write_out = False
                    continue

                if not write_out and line.startswith('['):
                    write_out = True

    def get_scada_junction_list(self, plcs):

        junction_list = []

        for PLC in plcs:
            if 'sensors' not in PLC:
                PLC['sensors'] = list()

            for sensor in PLC['sensors']:
                if sensor != "" and sensor in self.junction_list:
                    junction_list.append(sensor)

        return junction_list

    def get_node_list_by_type(self, a_list, a_type):
        result = []
        for node in a_list:
            if self.wn.get_node(node).node_type == a_type:
                result.append(str(node))
        return result

    def get_link_list_by_type(self, a_list, a_type):
        result = []
        for link in a_list:
            if self.wn.get_link(link).link_type == a_type:
                result.append(str(link))
        return result

    @staticmethod
    def create_node_header(a_list):
        result = []
        for node in a_list:
            result.append(node + "_LEVEL")
        return result

    @staticmethod
    def create_link_header(a_list, include_speed=False):
        result = []
        for link in a_list:
            result.append(link + "_FLOW")
            result.append(link + "_STATUS")
            if include_speed:
                result.append(link + "_SPEED")
        return result

    def create_attack_header(self):
        """
        Function that creates csv list headers for device and network attacks
        :return: list of attack names starting with device and ending with network
        """
        result = []
        # Append device attacks
        if "plcs" in self.data:
            for plc in self.data["plcs"]:
                if "attacks" in plc:
                    for attack in plc["attacks"]:
                        result.append(attack['name'])
        # Append network attacks
        if "network_attacks" in self.data:
            for network_attack in self.data["network_attacks"]:
                result.append(network_attack['name'])

        result.extend(a['name'] for a in self.data.get('crave_device_attacks', []))

        return result
        
    def register_initial_results(self):
        self.values_list = [self.master_time, datetime.now()]
        for tank in self.tank_list:
            idx = en.getnodeindex(ph=self.proj, id=tank)
            self.values_list.extend([en.getnodevalue(ph=self.proj, index=idx, property=en.PRESSURE)])
        for junction in self.junction_list:
            idx = en.getnodeindex(ph=self.proj, id=junction)
            self.values_list.extend([en.getnodevalue(ph=self.proj, index=idx, property=en.PRESSURE)])
        for pump in self.pump_list:
            idx = en.getlinkindex(ph=self.proj, id=pump)
            self.values_list.extend([
                en.getlinkvalue(ph=self.proj, index=idx, property=en.FLOW),
                en.getlinkvalue(ph=self.proj, index=idx, property=en.STATUS),
                en.getlinkvalue(ph=self.proj, index=idx, property=en.SETTING)  # Pump speed
            ])

        if 'crave_ctown_runtime' in self.data:
            for valve in self.valve_list:
                idx = en.getlinkindex(ph=self.proj, id=valve)
                self.values_list.extend([
                    en.getlinkvalue(ph=self.proj, index=idx, property=en.FLOW),
                    en.getlinkvalue(ph=self.proj, index=idx, property=en.STATUS),
                ])
        self.extend_attacks()

        if 'crave_ctown_runtime' in self.data:
            self.values_list.append(0)

    def register_results(self, results=None):

        # Results are divided into: nodes: reservoir and tanks, links: flows and status
        self.values_list = [self.master_time, datetime.now()]
        self.extend_tanks(results)
        self.extend_junctions(results)
        self.extend_pumps(results)

        # epynet current's version includes valves status in pumps
        self.extend_valves(results)
        self.extend_attacks()
        if 'crave_ctown_runtime' in self.data:
            self.values_list.append(self.master_time * self.simulation_step)

    def extend_tanks(self, results=None):
        for tank in self.tank_list:
            self.values_list.extend([results[tank]['pressure']])

    def extend_junctions(self, results=None):
        negative_pressure = 0
        for junction in self.junction_list:
            pressure = results[junction]['pressure']
            if pressure < 0:
                negative_pressure = 1
            self.values_list.extend([results[junction]['pressure']])
        if negative_pressure:
            self.logger.warning("At iteration {x}, system has negative pressures - \
                                 negative pressures occurred at one or more junctions with positive demand".format(x=self.master_time))
                                 
    def extend_pumps(self, results=None):
        for pump in self.pump_list:
            try:
                curve_coeff = self.wn.get_link("P9").get_head_curve_coefficients()
                max_flow = (curve_coeff[0]/curve_coeff[1])**(1/curve_coeff[2])
                if self.wn.get_link(pump).flow > max_flow:
                    self.logger.warning("At iteration {x}, pumps cannot deliver enough flow or head - one or more pumps were forced to \
                                            either shut down (due to insufficient head) or operate beyond the maximum rated flow".format(x=self.master_time))
            except Exception:
                pass
            # Get pump speed/setting from EPANET
            idx = en.getlinkindex(ph=self.proj, id=pump)
            pump_speed = en.getlinkvalue(ph=self.proj, index=idx, property=en.SETTING)
            self.values_list.extend([results[pump]['flow'], results[pump]['status'], pump_speed])
            
    def extend_valves(self, results=None):
        # Get valves flows and status
        for valve in self.valve_list:
            self.values_list.extend([results[valve]['flow'], results[valve]['status']])
            
    def extend_attacks(self):
        # Get device attacks
        if "plcs" in self.data:
            for plc in self.data["plcs"]:
                if "attacks" in plc:
                    for attack in plc["attacks"]:
                        self.values_list.append(self.get_attack_flag(attack['name']))
        # get network attacks
        if "network_attacks" in self.data:
            for network_attack in self.data["network_attacks"]:
                self.values_list.append(self.get_attack_flag(network_attack['name']))
        for attack in self.data.get('crave_device_attacks', []):
            self.values_list.append(self.get_attack_flag(attack['name']))

    def update_controls(self):
        """Updates all controls in WNTR."""
        conn = sqlite3.connect(self.data["db_path"])
        c = conn.cursor()

        for control in self.control_list:
            rows_1 = c.execute('SELECT value FROM plant WHERE name = ?', (control['name'],)).fetchone()
            conn.commit()
            # Use float() instead of int() to preserve decimal values for pump speed control
            new_status = float(rows_1[0])

            control['value'] = new_status
            idx = en.getlinkindex(ph=self.proj, id=control['name'])
            if 'crave_ctown_runtime' in self.data and control['name'] == 'PU10':
                from wacsim.replay.control import apply_pump_speed
                apply_pump_speed(en, self.proj, idx, new_status)
                continue
            if not math.isclose(control['value'], 1) and not math.isclose(control['value'], 0):
                en.setlinkvalue(ph=self.proj, index=idx, property=en.SETTING, value=control['value'])
            else:
                en.setlinkvalue(ph=self.proj, index=idx, property=en.STATUS, value=control['value'])

    def _init_what(self):
        """Save a ordered tuple of pk field names in self._what."""
        query = "PRAGMA table_info(%s)" % self._name

        with sqlite3.connect(self._path) as conn:
            try:
                cursor = conn.cursor()
                cursor.execute(query)
                table_info = cursor.fetchall()

                # last tuple element
                pks = []
                for field in table_info:
                    if field[-1] > 0:
                        pks.append(field)

                if not pks:
                    self.logger.error('Please provide at least 1 primary key. Has sqlite DB been initialized?.'
                                      ' Aborting')
                    sys.exit(1)
                else:
                    # sort by pk order
                    pks.sort(key=lambda x: x[5])

                    what_list = []
                    for pk in pks:
                        what_list.append(pk[1])

                    self._what = tuple(what_list)

            except sqlite3.Error as e:
                self.logger.error('Error initializing the sqlite DB. Exiting. Error: ' + str(e))
                sys.exit(1)

    def _init_set_query(self):
        """Use prepared statements."""

        set_query = 'UPDATE %s SET %s = ? WHERE %s = ?' % (
            self._name,
            self._value,
            self._what[0])

        # for composite pk
        for pk in self._what[1:]:
            set_query += ' AND %s = ?' % (
                pk)

        self._set_query = set_query

    def _init_get_query(self):
        """Use prepared statement."""

        get_query = 'SELECT %s FROM %s WHERE %s = ?' % (
            self._value,
            self._name,
            self._what[0])

        # for composite pk
        for pk in self._what[1:]:
            get_query += ' AND %s = ?' % (
                pk)

        self._get_query = get_query

    def write_results(self, results):
        """Writes ground truth file."""
        with self.ground_truth_path.open(mode='w') as f:
            writer = csv.writer(f)
            writer.writerows(results)

    def get_plcs_ready(self, flag):
        """
        Checks whether all PLCs have finished their loop.
        :return: boolean whether all PLCs have finished
        """
        res = self.db_query("""SELECT count(*) FROM sync WHERE flag != ?""", False, (str(flag),))
        return int(res) == 0

    def get_attack_flag(self, name):
        """
        Get the attack flag of this attack.
        :return: False if attack not running, true otherwise
        """
        return self.db_query("SELECT flag FROM attack WHERE name IS ?", False, (name,))

  
    def main(self):
        """Runs the simulation for x iterations."""

        iteration_limit = self.data["iterations"]
        self.logger.info("Temporary file location: " + str(Path(self.data["db_path"]).parent))

        if 'batch_index' in self.data:
            self.logger.info("Running batch simulation {x} out of {y}."
                             .format(x=self.data['batch_index'] + 1,
                                     y=self.data['batch_simulations']))

        self.logger.info("Simulation will run for {x} iterations with hydraulic timestep {step}."
                         .format(x=str(iteration_limit),
                                 step=str(self.simulation_step)))

        p_bar = None
        if self.data['log_level'] != 'debug':
            widgets = [' [', progressbar.Timer(), ' - ', progressbar.SimpleProgress(), '] ',
                       progressbar.Bar(), ' [', progressbar.ETA(), '] ', ]
            p_bar = progressbar.ProgressBar(max_value=iteration_limit, widgets=widgets)
            p_bar.start()
        self.simulate_with_wntr(iteration_limit, p_bar)
        self.finish()
        if 'crave_ctown_runtime' in self.data:
            runtime = self.data['crave_ctown_runtime']
            marker = {'execution_id': runtime['execution_id'], 'run_key': runtime['run_key'],
                      'completed_steps': self.master_time}
            (Path(self.data['output_path']) / 'full_run_complete.json').write_text(json.dumps(marker))

    def simulate_with_wntr(self, iteration_limit, p_bar):
        self.logger.info("Starting WNTR simulation")
        self.wn.options.time.duration = self.wn.options.time.hydraulic_timestep*iteration_limit
        wntr.network.io.write_inpfile(self.wn, 'temp.inp', 'CMH')
        self.remove_controls_from_inp_file('temp.inp', 'temp_processed.inp')
        inpfile = "temp_processed.inp"
        rptfile = "temp.rpt"
        outfile = "temp.bin"
        # Open EPANET files
        self.proj = en.createproject()
        en.open(ph=self.proj, inpFile=inpfile, rptFile=rptfile, outFile=outfile)
        en.openH(ph=self.proj)
        en.initH(ph=self.proj, initFlag=0)
        en.runH(ph=self.proj)
        self.register_initial_results()
        self.results_list.append(self.values_list)
        crave_timing = 'crave_ctown_runtime' in self.data
        if not crave_timing:
            tstep = en.nextH(ph=self.proj)
        for _ in range(iteration_limit):
            # Wait for PLCs
            while not self.get_plcs_ready(1):
                time.sleep(self.WAIT_FOR_FLAG)

            self.set_sync(2)

            while not self.get_plcs_ready(3):
                time.sleep(self.WAIT_FOR_FLAG)
            # Update any controls
            self.update_controls()
            current_time = en.runH(ph=self.proj)
            if crave_timing:
                if current_time != self.master_time * self.simulation_step:
                    raise RuntimeError('CTown control/physical clock mismatch')
                target_time = (self.master_time + 1) * self.simulation_step
                while current_time < target_time:
                    tstep = en.nextH(ph=self.proj)
                    if tstep <= 0:
                        raise RuntimeError('EPANET stopped before the CTown output step')
                    current_time = en.runH(ph=self.proj)
                if current_time != target_time:
                    raise RuntimeError('EPANET skipped the CTown output step')
            else:
                # Preserve the stock WaCSim timing for existing configurations.
                while True:
                    if (current_time) // self.simulation_step > self.master_time:
                        break
                    else:
                        tstep = en.nextH(ph=self.proj)
                        current_time = en.runH(ph=self.proj)
                        self.logger.debug("Skipping Intermediate Timestep")
            if p_bar:
                p_bar.update(self.master_time)
            self.master_time += 1
            # Get final results for this iteration
            node_count = en.getcount(ph=self.proj, object=en.NODECOUNT)
            link_count = en.getcount(ph=self.proj, object=en.LINKCOUNT)
            network_state = {}
            # Retrieve node pressures
            for i in range(1, node_count + 1):
                node_id = en.getnodeid(ph=self.proj, index=i)
                node_pressure = en.getnodevalue(ph=self.proj, index=i, property=en.PRESSURE)
                network_state[node_id] = {"pressure": node_pressure}
            # Retrieve flow and status for each link
            for i in range(1, link_count + 1):
                link_id = en.getlinkid(ph=self.proj, index=i)
                link_flow = en.getlinkvalue(ph=self.proj, index=i, property=en.FLOW)
                link_status = en.getlinkvalue(ph=self.proj, index=i, property=en.STATUS)
                network_state[link_id] = {"flow": link_flow, "status": link_status}
            # Update DB with current step results
            self.update_tanks(network_state)
            self.update_pumps(network_state)
            self.update_valves(network_state)
            self.update_junctions(network_state)
            self.calc_water_loss()
            self.calc_demand_deficit()
            conn = sqlite3.connect(self.data["db_path"])
            c = conn.cursor()
            c.execute("REPLACE INTO master_time (id, time) VALUES(1, ?)", (str(self.master_time),))
            conn.commit()

            self.register_results(network_state)
            self.results_list.append(self.values_list)
            # Optionally save partial results
            if 'saving_interval' in self.data and self.master_time != 0 \
               and self.master_time % self.data['saving_interval'] == 0:
                self.write_results(self.results_list)
            if not crave_timing:
                tstep = en.nextH(ph=self.proj)
                self.logger.debug(tstep)
            self.set_sync(0)

        # Close EPANET
        en.closeH(ph=self.proj)
        en.close(ph=self.proj)

    def update_tanks(self, network_state=None):
        """Update tanks in database."""
        conn = sqlite3.connect(self.data["db_path"])
        c = conn.cursor()
        for tank in self.tank_list:
            a_level = network_state[tank]['pressure']
            c.execute(self.db_update_string, (str(a_level), tank,))
            conn.commit()

    def update_pumps(self, network_state=None):
        """"Update pumps in database."""
        conn = sqlite3.connect(self.data["db_path"])
        c = conn.cursor()
        for pump in self.pump_list:
            flow = network_state[pump]['flow']
            c.execute(self.db_update_string, (str(flow), pump + "F",))
            conn.commit()

    def update_valves(self, network_state=None):
        conn = sqlite3.connect(self.data["db_path"])
        c = conn.cursor()
        for valve in self.valve_list:
            flow = network_state[valve]['flow']
            c.execute(self.db_update_string, (str(flow), valve + "F",))
            conn.commit()

    def update_junctions(self, network_state=None):
        """Update junction pressure in database."""
        conn = sqlite3.connect(self.data["db_path"])
        c = conn.cursor()
        for junction in self.scada_junction_list:
            level = network_state[junction]['pressure']
            c.execute(self.db_update_string, (str(level), junction,))
            conn.commit()

    def interrupt(self, sig, frame):
        self.finish()
        self.logger.info("Simulation ended.")
        sys.exit(0)

    def finish(self):
        self.write_results(self.results_list)
        end_time = datetime.now()
        self.water_loss_df.to_csv(Path(self.data['config_path']).parent / self.data['output_path'] / 'water_loss.csv')
        self.demand_deficit_df.to_csv(Path(self.data['config_path']).parent / self.data['output_path'] / 'demand_deficit.csv')
        if 'batch_simulations' in self.data:
            readme_path = Path(self.data['config_path']).parent / self.data['output_path']\
                          / 'configuration' / 'batch_readme.md'
            os.makedirs(str(readme_path.parent), exist_ok=True)

            BatchReadmeGenerator(self.intermediate_yaml, readme_path, self.start_time, end_time,
                                 self.wn, self.master_time, self.simulation_step).write_batch()
            if self.data['batch_index'] == self.data['batch_simulations'] - 1:
                GeneralReadmeGenerator(self.intermediate_yaml, self.data['start_time'],
                                       end_time, True, self.master_time, self.wn, self.simulation_step).write_readme()
        else:
            GeneralReadmeGenerator(self.intermediate_yaml, self.data['start_time'],
                                   end_time, False, self.master_time, self.wn, self.simulation_step).write_readme()

    def set_initial_values(self):
        """Sets custom initial values for tanks and demand patterns in the WNTR simulation"""

        if "initial_tank_values" in self.data:
            # Initial tank values

            self.logger.debug("Using custom initial tank levels: " + str(self.data["initial_tank_values"]))

            for tank in self.tank_list:
                if str(tank) in self.data["initial_tank_values"]:
                    value = float(self.data["initial_tank_values"][str(tank)])
                    self.wn.get_node(tank).init_level = value

        if "demand_patterns_data" in self.data:
            # Demand patterns for batch
            demands = pd.read_csv(self.data["demand_patterns_data"])
            for name, pat in self.wn.patterns():
                if name in demands:
                    self.logger.debug("Setting demands for " + name +
                                      " to demands defined at: " + self.data["demand_patterns_data"])
                    pat.multipliers = demands[name].values.tolist()
                else:
                    self.logger.debug("Consumer " + name + " has no demands defined, using default...")

def is_valid_file(test_parser, arg):
    if not os.path.exists(arg):
        test_parser.error(arg + " does not exist.")
    else:
        return arg


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Run the simulation')
    parser.add_argument(dest="intermediate_yaml",
                        help="intermediate yaml file", metavar="FILE",
                        type=lambda x: is_valid_file(parser, x))

    args = parser.parse_args()

    simulation = PhysicalPlant(Path(args.intermediate_yaml))
    simulation.main()
