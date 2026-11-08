from pathlib import Path

from wacsim.replay.calibration import nearest_rank, phase1_commit_matches_scenario, select_candidate


ROOT = Path(__file__).resolve().parents[3]


def test_nearest_rank_uses_the_declared_one_based_ceiling_index():
    assert nearest_rank([.1, .4, .2, .3], .75) == .3
    assert nearest_rank([.1, .4, .2, .3], .999) == .4


def test_selection_uses_first_qualifying_sp_then_mae_kp_ki_tiebreak():
    declaration = {"calibration": {"nominal_setpoint_candidates_m": [2.5, 3.0]}}
    entries = [
        {"status": "ok", "SP_m": 2.5, "Kp": .4, "Ki_per_step": .04,
         "metrics": {"qualified": True, "tracking_MAE_m": .2}},
        {"status": "ok", "SP_m": 2.5, "Kp": .2, "Ki_per_step": .02,
         "metrics": {"qualified": True, "tracking_MAE_m": .2}},
        {"status": "ok", "SP_m": 3.0, "Kp": .2, "Ki_per_step": .005,
         "metrics": {"qualified": True, "tracking_MAE_m": .1}},
    ]
    selected = select_candidate(entries, declaration)
    assert (selected["SP_m"], selected["Kp"], selected["Ki_per_step"]) == (2.5, .2, .02)


def test_phase1_commit_match_accepts_a_relative_scenario_path():
    import subprocess
    sha = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], check=True,
                         capture_output=True, text=True).stdout.strip()
    assert phase1_commit_matches_scenario(Path("examples/crave_ctown/scenario.yaml"), sha)
