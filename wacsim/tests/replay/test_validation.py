import csv
import json

import pytest

from wacsim.replay.validation import ValidationError, compare_fast_and_full, normalise_full_output


def row(step, t7, command, alarms=None, hazards=None):
    return {"t_step": step, "time_seconds": (step + 1) * 300,
            "T7_LEVEL": t7, "T7_LEVEL_SP": 2.5, "PU10_SPEED_CMD": command,
            "alarm_rule_ids": alarms or [], "hazard_rule_ids": hazards or [],
            "alarm_margin_m": .1, "hazard_margin_m": t7 - .5}


def trace(rows, first_alarm=None, first_hazard=None, prehazard=None, score_start_step=0):
    return {"rows": rows, "score": {"score_start_step": score_start_step,
            "first_alarm_step": first_alarm,
            "first_hazard_step": first_hazard,
            "prehazard_alarm_rule_ids": prehazard or []}, "run_key": "rk_test"}


def test_compare_accepts_matching_semantic_traces():
    fast = trace([row(0, 2.4, .1), row(1, 2.3, .2)])
    full = trace([row(0, 2.401, .1000001), row(1, 2.299, .2000001)])
    report = compare_fast_and_full(fast, full, pu10_tolerance=1e-5)
    assert report["status"] == "passed"
    assert report["time_alignment_offset_steps"] == 0
    assert report["T7_max_abs_error_m"] == pytest.approx(.001)


def test_compare_reports_first_trigger_and_command_mismatch():
    fast = trace([row(0, 2.4, .1), row(1, .5, .2, ["A-T7-TRACK"], ["H-T7-LOW"])], 1, 1)
    full = trace([row(0, 2.4, .1), row(1, .5, .7, ["A-T7-TRACK"], ["H-T7-LOW"])], 1, 1)
    report = compare_fast_and_full(fast, full, pu10_tolerance=1e-6)
    assert report["status"] == "failed"
    assert not report["checks"]["PU10_command_agreement_ge_0_99"]


def test_full_normalizer_joins_plc_command_and_rejects_missing_column(tmp_path):
    oracle = [row(0, 2.4, 99), row(1, 2.3, 99)]
    for item in oracle:
        item.pop("PU10_SPEED_CMD")
    (tmp_path / "oracle_trace.jsonl").write_text("".join(json.dumps(item) + "\n" for item in oracle))
    (tmp_path / "oracle_summary.json").write_text(json.dumps(trace([], None, None, [])))
    with (tmp_path / "PLC5_values.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["iteration", "PU10_SPEED_CMD"])
        writer.writeheader()
        writer.writerows([{"iteration": 0, "PU10_SPEED_CMD": .1},
                          {"iteration": 1, "PU10_SPEED_CMD": .2}])
    normalized = normalise_full_output(tmp_path)
    assert [item["PU10_SPEED_CMD"] for item in normalized["rows"]] == [.1, .2]
    (tmp_path / "PLC5_values.csv").write_text("iteration\n0\n")
    with pytest.raises(ValidationError, match="PU10_SPEED_CMD"):
        normalise_full_output(tmp_path)
