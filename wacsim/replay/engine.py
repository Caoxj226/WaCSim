"""Persistent in-process EPANET engine for CTown replay tasks."""
from __future__ import annotations
import tempfile
from pathlib import Path
import yaml
from wacsim.epanet import toolkit as en
from .contract import ReplayResult, ReplayTask
from .control import PIController, apply_pump_speed
from .injections import Ramp
from .legacy import SampledControls, real32
from .oracle import annotate_final_score, rules_from_scenario
from .scenario import RunConfig, validate_task
from .trace import make_row


def prepared_inp(source: Path, target: Path) -> None:
    """Move INP switching rules to the sampled PLC adapter; retain the network."""
    section = ""
    removed = 0
    lines = []
    for line in source.read_text(encoding="utf-8").splitlines(keepends=True):
        fields = line.split(";", 1)[0].split()
        if fields and fields[0].startswith("["):
            section = fields[0]
        if section == "[CONTROLS]" and fields and fields[0] == "LINK":
            if fields[1] == "PU10":
                removed += 1
            continue
        lines.append(line)
    if removed != 2:
        raise ValueError(f"expected two legacy PU10 controls, got {removed}")
    target.write_text("".join(lines), encoding="utf-8")


def run_epanet(task: ReplayTask, config: RunConfig, inp: Path, scenario: Path,
               *, allow_unreviewed: bool = False,
               include_network_detail: bool = False) -> ReplayResult:
    """Run a task; only explicit internal tests may bypass the formal lock gate."""
    declaration = validate_task(task, config, inp, scenario, require_lock=not allow_unreviewed)
    oracle_rules = rules_from_scenario(declaration, config.theta_m)
    ramps = {spec.role: Ramp(spec) for spec in task.injections}
    controller = PIController(config)
    rows = []
    with tempfile.TemporaryDirectory(prefix="ctown-replay-") as directory:
        work = Path(directory)
        prepared = work / "ctown-no-pu10-rule.inp"
        prepared_inp(inp, prepared)
        project = en.createproject()
        opened = hydraulics_open = False
        try:
            en.open(ph=project, inpFile=str(prepared), rptFile=str(work / "run.rpt"),
                    outFile=str(work / "run.bin"))
            opened = True
            en.settimeparam(ph=project, param=en.DURATION, value=task.total_steps * 300)
            if (en.gettimeparam(ph=project, param=en.HYDSTEP) != 300 or
                    en.gettimeparam(ph=project, param=en.PATTERNSTEP) != 3600):
                raise RuntimeError("CTown hydraulic/pattern timestep mismatch")
            t7 = en.getnodeindex(ph=project, id="T7")
            pu10 = en.getlinkindex(ph=project, id="PU10")
            pu11 = en.getlinkindex(ph=project, id="PU11")
            j317 = en.getnodeindex(ph=project, id="J317")
            links = {en.getlinkid(ph=project, index=i): i
                     for i in range(1, en.getcount(ph=project, object=en.LINKCOUNT) + 1)
                     if en.getlinktype(ph=project, index=i) >= en.PUMP}
            initial_commands = {name: float(en.getlinkvalue(ph=project, index=i,
                                   property=en.INITSTATUS) != 0) for name, i in links.items()}
            inventory = yaml.safe_load((inp.parent / "ctown_plcs.yaml").read_text(encoding="utf-8"))
            sampled = SampledControls(inp.read_text(encoding="utf-8"), inventory, initial_commands)
            tanks = {}
            junctions = {}
            for index in range(1, en.getcount(ph=project, object=en.NODECOUNT) + 1):
                kind = en.getnodetype(ph=project, index=index)
                if kind == en.TANK:
                    tanks[en.getnodeid(ph=project, index=index)] = index
                elif kind == en.JUNCTION:
                    junctions[en.getnodeid(ph=project, index=index)] = index
            en.openH(ph=project)
            hydraulics_open = True
            en.initH(ph=project, initFlag=0)
            current_time = en.runH(ph=project)
            if current_time != 0:
                raise RuntimeError("initial hydraulic time is not zero")
            initial_t7 = en.getnodevalue(ph=project, index=t7, property=en.PRESSURE)
            for k in range(task.total_steps):
                levels = {name: en.getnodevalue(ph=project, index=index, property=en.PRESSURE)
                          for name, index in tanks.items()}
                commands = sampled.apply(k, levels)
                for name, index in links.items():
                    if name != "PU10":
                        en.setlinkvalue(ph=project, index=index, property=en.STATUS, value=commands[name])
                measured = real32(levels["T7"])
                effective_sp = (ramps["masker"].value(k, config.sp_m)
                                if "masker" in ramps else config.sp_m)
                raw, pi_command = controller.step(measured, effective_sp)
                final_command = (ramps["driver"].value(k, pi_command)
                                 if "driver" in ramps else pi_command)
                apply_pump_speed(en, project, pu10, final_command)
                if en.runH(ph=project) != current_time:
                    raise RuntimeError("same-time hydraulic solve changed the clock")
                target_time = (k + 1) * 300
                while current_time < target_time:
                    delta = en.nextH(ph=project)
                    if delta <= 0:
                        raise RuntimeError(f"EPANET stopped before step {k}")
                    current_time = en.runH(ph=project)
                    if current_time > target_time:
                        raise RuntimeError(f"EPANET skipped output time {target_time}")
                true_t7 = en.getnodevalue(ph=project, index=t7, property=en.PRESSURE)
                tank_levels = {name: en.getnodevalue(ph=project, index=index, property=en.PRESSURE)
                               for name, index in tanks.items()}
                junction_pressures = {
                    name: en.getnodevalue(ph=project, index=index, property=en.PRESSURE)
                    for name, index in junctions.items()
                }
                rows.append(make_row(
                    step=k, seconds=target_time, measured_t7=measured, true_t7=true_t7,
                    nominal_sp=config.sp_m, effective_sp=effective_sp, pi_raw=raw,
                    pi_command=pi_command, final_command=final_command,
                    integral=controller.integral,
                    pump_setting=en.getlinkvalue(ph=project, index=pu10, property=en.SETTING),
                    pump_status=en.getlinkvalue(ph=project, index=pu10, property=en.STATUS),
                    pump_flow_m3h=en.getlinkvalue(ph=project, index=pu10, property=en.FLOW)*3.6,
                    pu11_status=en.getlinkvalue(ph=project, index=pu11, property=en.STATUS),
                    j317_pressure=en.getnodevalue(ph=project, index=j317, property=en.PRESSURE),
                    tank_levels=tank_levels, junction_pressures=junction_pressures,
                    oracle_rules=oracle_rules, include_network_detail=include_network_detail,
                ))
        finally:
            if hydraulics_open:
                en.closeH(ph=project)
            if opened:
                en.close(ph=project)
            en.deleteproject(ph=project)
        report = work / "run.rpt"
        warning_lines = [line.strip() for line in report.read_text(errors="replace").splitlines()
                         if "WARNING:" in line] if report.exists() else []
    if any("unbalanced" in line.lower() or "cannot solve" in line.lower()
           for line in warning_lines):
        raise RuntimeError("EPANET hydraulic convergence warning")
    result = ReplayResult(
        status="ok", rows=rows,
        score=annotate_final_score(rows, score_start_step=task.score_start_step),
        injection_echo=[ramps[spec.role].echo() for spec in task.injections],
        meta={"backend": "EPANET-toolkit-direct", "initial_T7_LEVEL_m": initial_t7,
              "control_timing": "sampled_PLC_rules_every_300s",
              "measurement_encoding": "remote_REAL32",
              "alarm_status": "theta_not_calibrated" if config.theta_m is None else "evaluated",
              "score_start_step": task.score_start_step,
              "score_start_semantics": "retain_warmup_telemetry_exclude_warmup_from_CRAVE_score",
              "config": {"sp_m": config.sp_m, "kp": config.kp,
                         "ki_per_step": config.ki_per_step, "theta_m": config.theta_m},
              "epanet_warning_count": len(warning_lines),
              "epanet_warning_kinds": sorted({line.split(" at ", 1)[0] for line in warning_lines}),
              "epanet_warning_examples": warning_lines[:5]},
        run_key=task.run_key,
    )
    result.validate_for(task)
    return result
