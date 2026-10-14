"""Live laptop telemetry for the HUD (battery, CPU, RAM, disk)."""
from __future__ import annotations

import shutil

import psutil

_cached_cpu = 0.0


def snapshot() -> dict:
    """Non-blocking telemetry snapshot; safe to call ~1x/second."""
    global _cached_cpu
    out = {"bat": None, "cpu": None, "ram_gb": None, "disk_gb": None, "plugged": None}
    try:
        _cached_cpu = psutil.cpu_percent(interval=None)
        out["cpu"] = int(round(_cached_cpu))
    except Exception:
        pass
    try:
        vm = psutil.virtual_memory()
        out["ram_gb"] = vm.used / (1024 ** 3)
    except Exception:
        pass
    try:
        bat = psutil.sensors_battery()
        if bat is not None:
            out["bat"] = int(round(bat.percent)) if bat.percent is not None else None
            out["plugged"] = bool(bat.power_plugged) if bat.power_plugged is not None else None
    except Exception:
        pass
    try:
        from app.core.config import PROJECT_ROOT
        free = shutil.disk_usage(str(PROJECT_ROOT)).free
        out["disk_gb"] = free / (1024 ** 3)
    except Exception:
        pass
    return out
