"""Read-only runtime probe for the CRAVE x WaCSim phase-2 environment."""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import platform
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def command(*args: str) -> dict[str, object]:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=15)
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "output": (result.stdout or result.stderr).strip(),
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def module_check(name: str) -> dict[str, object]:
    try:
        module = importlib.import_module(name)
        return {"ok": True, "file": getattr(module, "__file__", None)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def epanet_smoke(inp: Path) -> dict[str, object]:
    from wacsim.epanet import toolkit as en

    project = en.createproject()
    opened = False
    hydraulics_open = False
    try:
        with tempfile.TemporaryDirectory() as output_dir:
            output = Path(output_dir)
            en.open(
                ph=project,
                inpFile=str(inp),
                rptFile=str(output / "probe.rpt"),
                outFile=str(output / "probe.bin"),
            )
            opened = True
            en.openH(ph=project)
            hydraulics_open = True
            en.initH(ph=project, initFlag=0)
            initial_time = en.runH(ph=project)
            tank_index = en.getnodeindex(ph=project, id="T7")
            initial_t7 = en.getnodevalue(ph=project, index=tank_index, property=en.PRESSURE)
            next_step_seconds = en.nextH(ph=project)
            return {
                "ok": True,
                "toolkit_version_code": en.getversion(),
                "model": str(inp),
                "initial_time_seconds": initial_time,
                "initial_t7_pressure_m": initial_t7,
                "next_hydraulic_step_seconds": next_step_seconds,
            }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if hydraulics_open:
            en.closeH(ph=project)
        if opened:
            en.close(ph=project)
        en.deleteproject(ph=project)


def wntr_smoke(inp: Path) -> dict[str, object]:
    try:
        import wntr

        network = wntr.network.WaterNetworkModel(str(inp))
        return {
            "ok": True,
            "node_count": len(network.node_name_list),
            "initial_t7_level_m": network.get_node("T7").init_level,
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    inp = ROOT / "examples" / "ctown_topology" / "ctown_map.inp"
    modules = {
        name: module_check(name)
        for name in (
            "wacsim.epanet.toolkit", "wntr", "mininet.net", "minicps",
            "netfilterqueue", "pytest", "pytest_mock", "mock", "flaky",
            "testresources",
        )
    }
    packages = {
        name: version(name)
        for name in (
            "wacsim", "wntr", "mininet", "minicps", "NetfilterQueue",
            "PyYAML", "numpy", "scapy", "pytest", "cpppo", "epynet",
            "pandas", "antlr4-python3-runtime", "pyyaml-include",
            "pytest-mock", "mock", "flaky", "testresources",
        )
    }
    result = {
        "python": sys.version.split()[0],
        "python_executable": sys.executable,
        "os_release": Path("/etc/os-release").read_text(encoding="utf-8") if Path("/etc/os-release").exists() else None,
        "kernel": platform.release(),
        "machine": platform.machine(),
        "packages": packages,
        "modules": modules,
        "tools": {
            "mininet": command("mn", "--version"),
            "openvswitch": command("ovs-vswitchd", "--version"),
            "iptables": command("iptables", "--version"),
            "pytest": command("python3", "-m", "pytest", "--version"),
        },
        "epanet_smoke": epanet_smoke(inp),
        "wntr_smoke": wntr_smoke(inp),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if (
        all(item["ok"] for item in modules.values())
        and result["epanet_smoke"]["ok"]
        and result["wntr_smoke"]["ok"]
        and all(item["ok"] for item in result["tools"].values())
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
