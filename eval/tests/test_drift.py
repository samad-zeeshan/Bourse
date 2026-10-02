"""
Every number in the README and in the demo's data comes from a result file. This fails if one drifted.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_readme_and_site_data_match_the_results_files():
    proc = subprocess.run([sys.executable, str(ROOT / "eval" / "report.py"), "--check"], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
