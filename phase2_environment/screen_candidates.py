"""Exploratory no-attack screening; never creates a calibration lock."""
from __future__ import annotations
import json
import math
from pathlib import Path
from statistics import mean
import yaml
from wacsim.replay.fast_epanet import Candidate, run_no_attack

root=Path('/wacsim')
inp=root/'examples/ctown_topology/ctown_map.inp'
scenario=root/'examples/crave_ctown/scenario.yaml'
out=root/'phase2_environment/fast-replay-grid-exploratory.json'
s=yaml.safe_load(scenario.read_text(encoding='utf-8'))
assert s['calibration']['multicycle_protocol']['status']=='proposed_for_review'
c=s['calibration']
p=c['multicycle_protocol']
bounds={}
section=''
for line in inp.read_text().splitlines():
    line=line.split(';')[0].strip()
    if line.startswith('['): section=line
    elif section=='[TANKS]' and line:
        cells=line.split()
        bounds[cells[0]]=(float(cells[3]),float(cells[4]))
assert set(bounds)=={'T1','T2','T3','T4','T5','T6','T7'}
entries=[]
for sp in c['nominal_setpoint_candidates_m']:
    for kp in c['candidate_grid']['Kp']:
        for ki in c['candidate_grid']['Ki_per_step']:
            entry={'SP_m':sp,'Kp':kp,'Ki_per_step':ki,'status':'error'}
            try:
                task,result=run_no_attack(inp,scenario,Candidate(sp,kp,ki),6072)
                rows=result.rows
                scored=rows[24:]
                weeks=[scored[i*2016:(i+1)*2016] for i in range(3)]
                assert len(scored)==6048 and all(len(w)==2016 for w in weeks)
                fails=[]
                def failed(name,condition):
                    if condition: fails.append(name)
                failed('T7_hazard',any(r['T7_LEVEL']<=.5 for r in rows))
                failed('tank_bounds',any(not (lo-1e-6<=r['tank_levels_m'][name]<=hi+1e-6)
                                         for r in rows for name,(lo,hi) in bounds.items()))
                failed('junction_pressure',any(r['min_junction_pressure_m'] < -1e-6 for r in scored))
                mae=[mean(r['tracking_residual_m'] for r in w) for w in weeks]
                sat=[sum(r['PU10_SPEED_CMD'] in (0,1) for r in w)/len(w) for w in weeks]
                overall_mae=mean(r['tracking_residual_m'] for r in scored)
                overall_sat=sum(r['PU10_SPEED_CMD'] in (0,1) for r in scored)/len(scored)
                failed('tracking_MAE',overall_mae>.25 or any(x>.25 for x in mae))
                failed('command_saturation',overall_sat>.25 or any(x>.25 for x in sat))
                diffs=[abs(a['T7_LEVEL']-b['T7_LEVEL']) for a,b in zip(weeks[1],weeks[2])]
                failed('T7_week_repeat',mean(diffs)>.05 or max(diffs)>.20)
                other_drift=[]
                for name,(lo,hi) in bounds.items():
                    if name=='T7': continue
                    a=[r['tank_levels_m'][name] for r in weeks[1]]
                    b=[r['tank_levels_m'][name] for r in weeks[2]]
                    span=hi-lo
                    if (abs(mean(a)-mean(b))>.02*span or
                        abs(min(a)-min(b))>.05*span or abs(max(a)-max(b))>.05*span):
                        other_drift.append(name)
                failed('other_tank_week_drift',bool(other_drift))
                entry.update(status='partial_pass' if not fails else 'failed',fail_reasons=fails,
                             other_tank_drift=other_drift,tracking_MAE_m=overall_mae,
                             weekly_MAE_m=mae,saturation_fraction=overall_sat,
                             weekly_saturation_fraction=sat,T7_week_repeat_MAE_m=mean(diffs),
                             T7_week_repeat_max_m=max(diffs),T7_min_m=min(r['T7_LEVEL'] for r in rows),
                             min_junction_pressure_m=min(r['min_junction_pressure_m'] for r in scored),
                             epanet_warning_count=result.meta['epanet_warning_count'],
                             epanet_warning_kinds=result.meta['epanet_warning_kinds'],
                             run_key=task.run_key)
            except Exception as exc:
                entry['error']=repr(exc)
            entries.append(entry)
            out.write_text(json.dumps({'status':'exploratory_not_locked',
                                       'scoring_steps':[24,6071],
                                       'note':'each_junction_week_mean_pressure_requires_detail_replay',
                                       'entries':entries},indent=2,allow_nan=False),encoding='utf-8')
            print(f"{len(entries):02d}/48 SP={sp} Kp={kp} Ki={ki} {entry['status']} {entry.get('fail_reasons',entry.get('error'))}",flush=True)
print('partial_pass_count',sum(e['status']=='partial_pass' for e in entries))
