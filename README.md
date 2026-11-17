# 3D Printer Open Workbench ERP

A small, self-hosted ERP for a 3D-printing workshop: it knows what your filament and parts cost,
what each print really costs from its G-code, what you should charge for it, and what is in the
print queue. It runs on one machine (FastAPI + SQLite), has a web UI, a REST API and an
**MCP server** so Claude (Claude Code, Claude Desktop or any MCP client) can run it for you.

> Türkçe: [README.tr.md](README.tr.md)

## What it does

| Area | Features |
|---|---|
| **Materials and stock** | Filament spools (price per kg, remaining grams, low-stock alert), auxiliary materials (LEDs, boxes, screws…), purchase records with supplier / invoice no, material groups (kits) |
| **Machines** | Purchase price, lifespan, power draw → hourly machine cost; wear parts (nozzle, bed…) with usage hours and replacement warnings; loaded filament per slot (up to 4) |
| **BOM (bill of materials)** | Per product: filaments (net grams + waste %), materials, material groups, print time, size, layer height, infill |
| **Cost from G-code** | Parses OrcaSlicer `.gcode` / `.gcode.3mf`: print time, grams (per extruder), size, layer height, thumbnail. Time rule: 3 h 55 min = 3.92 h |
| **Pricing** | Machine depreciation + electricity + wear + filament + materials + waste, then commission, VAT (optional), target margin, shipping brackets (by volumetric weight, "desi"), free-shipping threshold, B2B quantity discount tiers |
| **Quotes** | B2B / custom quotes from G-code or manual hours and grams, saved for later |
| **Production** | Production orders that consume stock, scrap/failed-print logging, a print queue with states (waiting → customer approval → slicing → printing → done / error / cancelled), approved G-code per product (SHA-256 locked), "can this print run unattended?" check |
| **Export** | CSV product export (bulk-upload friendly, e.g. for ikas or similar stores), an "AI bundle" prompt with the product's real numbers for copywriting / market research |
| **MCP tools** | ~30 tools: inventory summary, add filament / material, purchases, create product with BOM, quote, G-code cost, OrcaSlicer CLI slicing, production orders, queue control |

The UI and most identifiers are in Turkish (the project started in a Turkish workshop); the API is
self-documenting at `/docs`. Money fields are named `*_tl` but you can treat them as any single
currency.

## Quick start

```bash
git clone <this repo> 3d-printer-open-workbench-erp
cd 3d-printer-open-workbench-erp
./install.sh            # venv + pinned deps + .env + empty database
./run.sh                # http://127.0.0.1:8000  (API docs: /docs)
```

Want to look around first? Start with sample data (clearly labelled examples, made-up round
prices): `WORKBENCH_ERP_DEMO_SEED=1 WORKBENCH_ERP_DB=./data/demo.db ./run.sh`

Full step-by-step for a clean Ubuntu machine, systemd service and first-run setup:
**[docs/install.md](docs/install.md)** (Türkçe: [docs/kurulum.md](docs/kurulum.md)).

## Configuration

All settings are environment variables; `install.sh` copies `.env.example` to `.env`, which is
read on start (variables already set in the environment win).

| Variable | Default | Meaning |
|---|---|---|
| `WORKBENCH_ERP_DB` | `./data/erp.db` | SQLite database path |
| `WORKBENCH_COMPANY_NAME` | `Open Workbench` | Name shown in the UI (also editable in Settings) |
| `WORKBENCH_ERP_DEMO_SEED` | `0` | `1` = load sample data on an empty database |
| `WORKBENCH_ERP_HOST` / `_PORT` | `127.0.0.1` / `8000` | Web server bind |
| `WORKBENCH_PRINTER_MODEL` | `Centauri Carbon` | Approved G-code must name this printer (substring) |
| `WORKBENCH_MACHINE_NAME` | `Elegoo Centauri Carbon` | Machine card created by the default-machine helper |
| `WORKBENCH_ORCA_PROFILES` | `./examples/orca` | OrcaSlicer profile folder (`machine/`, `process/`, `filament/`) |
| `WORKBENCH_ORCA_MACHINE` / `_PROCESS` / `_FILAMENT` | see `.env.example` | Default profile names for slicing |
| `ORCA_BIN` | `orcaslicer` on PATH | OrcaSlicer executable |

The Elegoo Centauri Carbon + OrcaSlicer values are only an example; set your own printer.
`examples/orca/process/` contains example process profiles. Machine and filament profiles are
not shipped — export them from your own OrcaSlicer (see the install guide).

## API overview

Interactive docs at `http://127.0.0.1:8000/docs`. Main groups:

| Endpoint | Purpose |
|---|---|
| `GET /api/dashboard` | Totals, stock value, low-stock and wear warnings |
| `GET/PUT /api/settings` | Electricity price, commission, VAT, margins, shipping threshold |
| `/api/shipping-rates`, `/api/b2b-tiers`, `/api/categories` | Shipping brackets, quantity discounts, categories |
| `/api/machines`, `/api/machine-parts` | Machines, wear parts, loaded filament (`PUT /api/machines/{id}/yuklu-filament`) |
| `/api/filaments`, `/api/materials`, `/api/material-groups`, `/api/alimlar` | Stock, purchases (`POST …/{id}/alim`) |
| `POST /api/slicer/parse-file`, `GET /api/slicer/convert-time` | G-code / 3MF parsing |
| `/api/products` | Products with BOM and computed B2C/B2B prices |
| `/api/quotes` (`POST /api/quotes/calculate`) | Quotes |
| `/api/orders` | Production orders (consume stock) |
| `/api/queue…` | Print queue: create, customer approval, attach G-code, start, error, complete, cancel, `basilabilir` check |
| `GET /api/products/{id}/ai-bundle`, `GET /api/export/ikas-csv` | AI prompt bundle, CSV export |

## MCP setup (Claude Code)

```bash
claude mcp add workbench-erp -- "$PWD/.venv/bin/python" "$PWD/mcp_server.py"
# with a custom database:
claude mcp add workbench-erp -e WORKBENCH_ERP_DB="$PWD/data/erp.db" -- "$PWD/.venv/bin/python" "$PWD/mcp_server.py"
```

Run it from the repository folder (or use absolute paths). Then ask Claude things like
*"What is low on stock?"*, *"Add a 1 kg black PLA spool at 700 per kg"*, *"Cost this G-code with
5 % waste and 20 for packaging"*. Before the first price calculation the `hazirlik_durumu` tool
lists the settings you still have to confirm (electricity price, commission, VAT, margins,
shipping) — the ERP never invents them.

Claude Desktop / other MCP clients: use the same command (`.venv/bin/python mcp_server.py`, stdio).

## Works with (optional)

- **3d-printer-open-workbench-bot** — a Telegram bot that talks to this ERP (stock, purchases,
  quotes, print status) from your phone.
- **3d-printer-open-workbench-studio** — parametric 3D product generators and print checks; its
  output G-code can be costed and approved here.

Each project works on its own; the ERP has no dependency on them.

## Safety

The ERP never sends commands to a printer. Queue "printing" states are bookkeeping only;
`basilabilir` only checks conditions and changes nothing.

## Tests

```bash
./install.sh --test      # or: .venv/bin/python -m pytest -q
```

Tests use a temporary database and never touch your data.

## Disclaimer

This software calculates costs and suggested prices from the numbers you enter. It is **not tax,
accounting or legal advice**. Tax options such as VAT or the Turkish small-trader exemption
("esnaf muafiyeti") and bank withholding are simple configurable percentages; check the rules that
apply to you with a qualified accountant.

## License

MIT — see [LICENSE](LICENSE).
