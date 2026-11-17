# Installation (clean Ubuntu)

Tested on Ubuntu 24.04 (Python 3.12) and newer Python 3.13 / 3.14. Any Linux with Python 3.10+
should work. Nothing needs root except installing system packages.

> Türkçe: [kurulum.md](kurulum.md)

## 1. System packages

```bash
sudo apt update
sudo apt install -y git python3 python3-venv curl
```

Optional: [uv](https://docs.astral.sh/uv/) — `install.sh` uses it automatically when present
(faster installs).

## 2. Get the code and install

```bash
cd ~
git clone <repository URL> 3d-printer-open-workbench-erp
cd 3d-printer-open-workbench-erp
./install.sh --test
```

`install.sh` is idempotent — run it again after every `git pull`. It:

1. creates `.venv/` (reused if present),
2. installs the pinned dependencies from `requirements.lock` (`--dev`/`--test` adds pytest),
3. copies `.env.example` to `.env` (never overwrites an existing `.env`),
4. creates `data/` and the database (`data/erp.db`); on upgrades missing columns are added, data
   is kept,
5. with `--test`, runs the test suite (expect `39 passed`).

## 3. Run

```bash
./run.sh                 # development: auto-reload on code changes
./run.sh --no-reload     # production-style
```

Open <http://127.0.0.1:8000> (web UI) and <http://127.0.0.1:8000/docs> (API).
Change host/port in `.env` (`WORKBENCH_ERP_HOST`, `WORKBENCH_ERP_PORT`). Keep the default
`127.0.0.1` unless you put an authenticating reverse proxy in front: **the API has no login**.

## 4. Run as a systemd user service

```bash
./install.sh --service
systemctl --user status workbench-erp
journalctl --user -u workbench-erp -f      # logs
sudo loginctl enable-linger "$USER"         # keep running after logout / start at boot
```

The unit is rendered from `deploy/workbench-erp.service.in` into
`~/.config/systemd/user/workbench-erp.service` and reads `.env` from the repository folder.
After editing `.env`: `systemctl --user restart workbench-erp`.

## 5. First-run setup

A fresh database contains only an empty settings row: the ERP does not invent prices. Enter your
real values in the web UI (Settings, Machines, Filaments tabs) or via the API. Examples below use
illustrative numbers — replace them with yours.

**Settings** (electricity price per kWh, store/POS commission, VAT, margins, default shipping):

```bash
curl -X PUT http://127.0.0.1:8000/api/settings -H 'Content-Type: application/json' -d '{
  "brand_name": "My Workshop",
  "electricity_price_per_kwh": 5.0,
  "commission_rate_percent": 3.0,
  "default_vat_rate_percent": 20.0,
  "default_vat_enabled": false,
  "default_target_margin_percent": 50.0,
  "default_b2b_margin_percent": 35.0,
  "default_shipping_cost": 100.0,
  "kargo_esigi_tl": 0
}'
```

**Add a machine** (price, expected lifespan in hours, average power in kW):

```bash
curl -X POST http://127.0.0.1:8000/api/machines -H 'Content-Type: application/json' -d '{
  "name": "Printer #1", "brand_model": "Elegoo Centauri Carbon",
  "purchase_price_tl": 30000, "lifespan_hours": 5000, "power_consumption_kw": 0.15
}'
```

**Add a filament spool:**

```bash
curl -X POST http://127.0.0.1:8000/api/filaments -H 'Content-Type: application/json' -d '{
  "code": "FIL-PLA-BLK", "brand": "Sample Brand", "material_type": "PLA",
  "color_name": "Black", "color_hex": "#111111", "price_per_kg_tl": 700,
  "spool_weight_gr": 1000, "remaining_weight_gr": 1000
}'
```

**Add a shipping bracket** (by volumetric weight "desi" = L×W×H cm / 3000):

```bash
curl -X POST http://127.0.0.1:8000/api/shipping-rates -H 'Content-Type: application/json' -d '{
  "tier_name": "Small parcel (0-2 desi)", "min_desi": 0, "max_desi": 2, "cost_tl": 80
}'
```

Then create a product: Products tab → upload the sliced `.gcode` / `.gcode.3mf`; time, grams and
size are read from the file.

### Using it from Claude (MCP)

```bash
claude mcp add workbench-erp -- "$HOME/3d-printer-open-workbench-erp/.venv/bin/python" \
  "$HOME/3d-printer-open-workbench-erp/mcp_server.py"
claude mcp list            # should show workbench-erp as connected
```

Ask Claude *"Is the ERP ready for pricing?"* — the `hazirlik_durumu` tool lists every value you
still have to confirm; Claude asks you and stores them with `ayar_guncelle`. Values saved only
from the web UI are not marked as confirmed for MCP pricing, so confirm them once through Claude.

### Optional: OrcaSlicer slicing via MCP

The `dilimle` tool runs the OrcaSlicer CLI. Put profiles in `WORKBENCH_ORCA_PROFILES`
(default `./examples/orca`):

```
examples/orca/machine/<WORKBENCH_ORCA_MACHINE>.json      # export from OrcaSlicer (not shipped)
examples/orca/filament/<WORKBENCH_ORCA_FILAMENT>.json    # export from OrcaSlicer (not shipped)
examples/orca/process/Workbench Standart 0.20 Utulu.json # examples shipped
```

In OrcaSlicer select your printer / filament / process and use the profile export (or copy the
JSON from OrcaSlicer's user profile folder, `~/.config/OrcaSlicer/user/`). Flatten profiles that
`inherits` a system preset (all keys present) so the CLI can load them standalone. Set `ORCA_BIN`
if `orcaslicer` is not on `PATH`.

## Backups

Everything is in one file, `data/erp.db` (or `WORKBENCH_ERP_DB`). Back it up while the service is
idle, or use `sqlite3 data/erp.db ".backup data/erp-backup.db"`. Approved G-code copies live in
`data/gcode/` next to the database.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `python3 -m venv` fails / `ensurepip is not available` | `sudo apt install python3-venv`, delete `.venv/`, rerun `./install.sh` |
| `Address already in use` on port 8000 | Set `WORKBENCH_ERP_PORT=8010` in `.env`, or stop the other process (`ss -ltnp | grep 8000`) |
| UI shows no prices / MCP says settings missing | Fill Settings, add a machine, filament and a shipping bracket (section 5); confirm settings via Claude |
| G-code approval rejected: "G-code başlığındaki yazıcı … değil" | The G-code header names another printer. Set `WORKBENCH_PRINTER_MODEL` to a substring of your printer name and restart |
| "G-code'dan süre ya da gram okunamadı" | Export a sliced file from OrcaSlicer (`.gcode` or `.gcode.3mf`), not a plain project `.3mf` |
| `dilimle`: "Profil yok" | Profile JSON missing in `WORKBENCH_ORCA_PROFILES/{machine,process,filament}`; check names with the `orca_profilleri` tool |
| Service does not start | `journalctl --user -u workbench-erp -e`; check `.venv/` exists (`./install.sh`) |
| Service stops after logout | `sudo loginctl enable-linger "$USER"` |
| Wrong database used | Relative `WORKBENCH_ERP_DB` paths are resolved from the repository folder; use an absolute path to be explicit |
| MCP not connected | Use absolute paths in `claude mcp add`; test with `.venv/bin/python mcp_server.py` (it waits on stdin — Ctrl+C to quit) |
