"""Central, environment-driven configuration.

Every machine- or company-specific value lives here so the code never hardcodes a path, a brand
or a private printer setting. All values can be overridden with environment variables (see
`.env.example`).
"""
import os

APP_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv(path: str) -> None:
    """Loads KEY=VALUE lines from `.env` without overriding variables that are already set."""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip().removeprefix("export ").strip()
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))


# Tests set WORKBENCH_ERP_SKIP_DOTENV=1 so a developer's local .env never leaks into them.
if os.environ.get("WORKBENCH_ERP_SKIP_DOTENV") != "1":
    _load_dotenv(os.environ.get("WORKBENCH_ERP_ENV_FILE", os.path.join(APP_DIR, ".env")))


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _path(name: str, default: str) -> str:
    value = os.path.expanduser(_env(name, default))
    return os.path.normpath(value if os.path.isabs(value) else os.path.join(APP_DIR, value))


# SQLite database file (created on first start). Default: ./data/erp.db inside the repo.
DB_PATH = _path("WORKBENCH_ERP_DB", os.path.join("data", "erp.db"))

# "1" seeds clearly labelled sample data (demo machine, sample filaments, example prices).
DEMO_SEED = _env("WORKBENCH_ERP_DEMO_SEED", "0") == "1"

# Company / brand name shown in the UI and written into the settings row on first start.
COMPANY_NAME = _env("WORKBENCH_COMPANY_NAME", "Open Workbench")

# Printer model that approved G-code must come from (substring match against the
# `printer_model` header OrcaSlicer writes). Example default: Elegoo Centauri Carbon.
PRINTER_MODEL = _env("WORKBENCH_PRINTER_MODEL", "Centauri Carbon")
# Machine card created by the station setup helper (`makine_karti_hazirla`).
MACHINE_NAME = _env("WORKBENCH_MACHINE_NAME", "Elegoo Centauri Carbon")

# OrcaSlicer CLI slicing (optional). Folder layout: <dir>/{machine,process,filament}/*.json
ORCA_PROFILES_DIR = _path("WORKBENCH_ORCA_PROFILES", os.path.join("examples", "orca"))
ORCA_MACHINE_PROFILE = _env("WORKBENCH_ORCA_MACHINE", "Elegoo Centauri Carbon 0.4 nozzle")
ORCA_DEFAULT_PROCESS = _env("WORKBENCH_ORCA_PROCESS", "Workbench Standart 0.20 Utulu")
ORCA_DEFAULT_FILAMENT = _env("WORKBENCH_ORCA_FILAMENT", "Elegoo PLA Matte @ECC")
ORCA_BIN = os.environ.get("ORCA_BIN", "")
