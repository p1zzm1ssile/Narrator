import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def pytest_configure(config):
    os.environ.setdefault("CONFIG_DIR", str(Path(config.rootpath) / ".pytest-config"))
