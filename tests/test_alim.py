"""Fatura / alım listesi ile stok girişi: ağırlıklı ortalama maliyet ve alım kaydı."""
from fastapi.testclient import TestClient

import main


def _istemci(session):
    return TestClient(main.app)


def test_agirlikli_fiyat():
    assert main.agirlikli_fiyat(500, 600.0, 1000, 750.0) == 700.0
    assert main.agirlikli_fiyat(0, 600.0, 1000, 750.0) == 750.0
    assert main.agirlikli_fiyat(500, 600.0, 1000, None) == 600.0


def test_filament_alim_ve_kayit(session):
    c = _istemci(session)
    f = c.post("/api/filaments", json={"code": "FIL-T", "brand": "Elegoo", "material_type": "Matte PLA",
                                         "color_name": "Krem", "price_per_kg_tl": 600, "remaining_weight_gr": 500}).json()
    r = c.post(f"/api/filaments/{f['id']}/alim", json={"miktar": 1000, "birim_fiyat_tl": 750,
                                                       "tedarikci": "Sample Supplier", "fatura_no": "F-1"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["filament"]["remaining_weight_gr"] == 1500 and d["filament"]["price_per_kg_tl"] == 700.0
    assert d["alim"]["total_tl"] == 750.0 and d["alim"]["stock_before"] == 500
    assert c.get("/api/alimlar", params={"fatura_no": "F-1"}).json()[0]["item_type"] == "FILAMENT"
    assert c.post(f"/api/filaments/{f['id']}/alim", json={"miktar": 0}).status_code == 400
    assert c.post("/api/filaments/9999/alim", json={"miktar": 10}).status_code == 404


def test_malzeme_alim(session):
    c = _istemci(session)
    m = c.post("/api/materials", json={"code": "MAT-KUTU", "name": "20x20x20 Kutu", "category": "AMBALAJ",
                                         "unit_cost_tl": 10, "stock_quantity": 10}).json()
    d = c.post(f"/api/materials/{m['id']}/alim", json={"miktar": 30, "birim_fiyat_tl": 14}).json()
    assert d["malzeme"]["stock_quantity"] == 40 and d["malzeme"]["unit_cost_tl"] == 13.0
    assert d["alim"]["total_tl"] == 420.0
