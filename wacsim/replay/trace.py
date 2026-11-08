"""One post-transition row and contract-compatible scoring output."""
import math
from .oracle import OracleRules, evaluate


def make_row(*, step: int, seconds: int, measured_t7: float, true_t7: float,
             nominal_sp: float, effective_sp: float, pi_raw: float, pi_command: float,
             final_command: float, integral: float, pump_setting: float,
             pump_status: float, pump_flow_m3h: float, pu11_status: float,
             j317_pressure: float, tank_levels: dict, junction_pressures: dict,
             oracle_rules: OracleRules, include_network_detail: bool = False) -> dict:
    values = (measured_t7, true_t7, nominal_sp, effective_sp, pi_raw, pi_command,
              final_command, integral, pump_setting, pump_status, pump_flow_m3h,
              pu11_status, j317_pressure, *tank_levels.values(), *junction_pressures.values())
    if not all(math.isfinite(value) for value in values):
        raise ValueError(f"nonfinite hydraulic or control output at step {step}")
    row = {
        "t_step": step, "time_seconds": seconds,
        "T7_LEVEL": true_t7, "T7_LEVEL_SP_NOMINAL": nominal_sp,
        "T7_LEVEL_SP": effective_sp, "T7_MEASUREMENT_PRE_STEP": measured_t7,
        "PU10_PI_RAW": pi_raw, "PU10_PI_CMD": pi_command,
        "PU10_SPEED_CMD": final_command, "PU10_SETTING": pump_setting,
        "PU10_STATUS": pump_status, "PU10_FLOW": pump_flow_m3h,
        "PU11_STATUS": pu11_status, "J317_PRESSURE": j317_pressure,
        "tank_levels_m": tank_levels, "min_junction_pressure_m": min(junction_pressures.values()),
        "integral": integral,
    }
    row.update(evaluate(true_t7, effective_sp, rules=oracle_rules))
    if include_network_detail:
        row["junction_pressures_m"] = junction_pressures
    return row
