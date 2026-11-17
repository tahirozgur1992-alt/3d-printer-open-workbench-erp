"""Testler geçici bir veritabanında çalışır; canlı veritabanına dokunmaz."""
import os
import sys
import tempfile
from pathlib import Path

_GECICI = tempfile.mkdtemp(prefix="workbench-erp-test-")
os.environ["WORKBENCH_ERP_SKIP_DOTENV"] = "1"
os.environ["WORKBENCH_ERP_DB"] = os.path.join(_GECICI, "test.db")
for _ad in ("WORKBENCH_ERP_DEMO_SEED", "WORKBENCH_PRINTER_MODEL", "WORKBENCH_MACHINE_NAME", "WORKBENCH_COMPANY_NAME"):
    os.environ.pop(_ad, None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from sqlmodel import Session, SQLModel  # noqa: E402

import database  # noqa: E402
import models  # noqa: E402


@pytest.fixture()
def session():
    SQLModel.metadata.drop_all(database.engine)
    database.init_db_and_seed()
    with Session(database.engine) as s:
        yield s


def gcode_yaz(klasor, ad="urun.gcode", model="Elegoo Centauri Carbon", sure="1h 30m 0s", gram="42.50", ek=""):
    metin = (
        f"; total estimated time: {sure}\n"
        f"; filament used [g] = {gram}\n"
        "; layer_height = 0.2\n"
        "G1 X10 Y10 E1\n"
        f"; printer_model = {model}\n{ek}"
    )
    yol = Path(klasor) / ad
    yol.write_text(metin, encoding="utf-8")
    return str(yol)


@pytest.fixture()
def dunya(session):
    """Bir makine, bir filament ve reçeteli bir standart ürün."""
    s = session.get(models.GlobalSettings, 1)
    s.electricity_price_per_kwh = 3.0
    makine = models.Machine(
        name="CC", brand_model="Elegoo Centauri Carbon", purchase_price_tl=30000, lifespan_hours=5000,
        power_consumption_kw=0.15,
    )
    filament = models.Filament(code="FIL-1", brand="Elegoo", color_name="Siyah", remaining_weight_gr=1000)
    session.add_all([s, makine, filament])
    session.commit()
    urun = models.Product(sku="PRT-1", name="Stand", machine_id=makine.id, print_time_hours_int=2)
    session.add(urun)
    session.commit()
    session.add(models.ProductBOMFilament(product_id=urun.id, filament_id=filament.id, net_weight_gr=100, waste_rate_percent=0))
    session.commit()
    for n in (makine, filament, urun):
        session.refresh(n)
    return {"makine": makine, "filament": filament, "urun": urun}
