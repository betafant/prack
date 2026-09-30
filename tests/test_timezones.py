import os
import subprocess
import sys


def test_regions_load_without_system_timezone_database():
    """Windows has no IANA time zone files: zoneinfo must fall back to the tzdata package."""
    env = {**os.environ, "PYTHONTZPATH": ""}  # hide /usr/share/zoneinfo like on Windows
    code = "from prack.regions import load_regions; print(load_regions(['ch'])[0].tz.key)"
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "Europe/Zurich"
