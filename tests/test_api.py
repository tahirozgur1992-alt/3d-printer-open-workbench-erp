"""Ekranın kullandığı uçlar ve MCP araçları aynı kuralları uygular."""
import pytest
from fastapi.testclient import TestClient

import istasyon_altyapi as ist
from conftest import gcode_yaz


@pytest.fixture()
def istemci(session, dunya, tmp_path, monkeypatch):
    monkeypatch.setattr(ist, "GCODE_DEPO", str(tmp_path / "depo"))
    import main

    with TestClient(main.app) as c:
        yield c


def urun_govdesi(dunya, **ek):
    return {
        "sku": "PRT-1", "name": "Stand", "machine_id": dunya["makine"].id, "print_time_hours_int": 2,
        "filaments": [{"filament_id": dunya["filament"].id, "net_weight_gr": 100, "waste_rate_percent": 0}],
        **ek,
    }


def test_urun_alanlari_ekrandan_duzenlenir(istemci, dunya):
    uid = dunya["urun"].id
    r = istemci.put(f"/api/products/{uid}", json=urun_govdesi(dunya, gozetimsiz_baski=True, stl_path="/x/a.stl"))
    assert r.status_code == 200
    u = next(p for p in istemci.get("/api/products").json() if p["product_id"] == uid)
    assert (u["urun_tipi"], u["gozetimsiz_baski"], u["stl_path"], u["gcode_sha256"]) == ("STANDART", True, "/x/a.stl", None)
    r = istemci.put(f"/api/products/{uid}", json=urun_govdesi(dunya, urun_tipi="KISIYE_OZEL", gozetimsiz_baski=True))
    assert r.status_code == 400
    assert istemci.put(f"/api/products/{uid}", json=urun_govdesi(dunya, urun_tipi="BASKA")).status_code == 400


def test_gcode_onayi_ve_kuyruk_ekrandan(istemci, dunya, tmp_path):
    uid, mid, fid = dunya["urun"].id, dunya["makine"].id, dunya["filament"].id
    assert istemci.post(f"/api/products/{uid}/gcode-onayla", json={"gcode_yolu": "/yok.gcode"}).status_code == 409
    r = istemci.post(f"/api/products/{uid}/gcode-onayla", json={"gcode_yolu": gcode_yaz(tmp_path)})
    assert r.status_code == 200 and r.json()["gram"] == 42.5
    # onay, sonraki ürün güncellemesinde silinmez
    istemci.put(f"/api/products/{uid}", json=urun_govdesi(dunya, gozetimsiz_baski=True))
    u = next(p for p in istemci.get("/api/products").json() if p["product_id"] == uid)
    assert u["gcode_sha256"] and u["gcode_onay_tarihi"]

    assert istemci.put(f"/api/machines/{mid}/yuklu-filament", json={"filament_id": 999}).status_code == 404
    assert istemci.put(f"/api/machines/{mid}/yuklu-filament", json={"filament_id": fid}).json()["yuklu_filament_id"] == fid
    assert istemci.get("/api/machines").json()[0]["yuklu_filament_id"] == fid

    emir = istemci.post("/api/queue", json={"product_id": uid}).json()["emir"]
    assert emir["status"] == "BEKLIYOR"
    r = istemci.get("/api/queue/basilabilir", params={"machine_id": mid, "tabla_bos": True, "yazici_bosta": True}).json()
    assert r["basilabilir"] is True
    r = istemci.get("/api/queue/basilabilir", params={"machine_id": mid}).json()
    assert {n["kosul"] for n in r["nedenler"]} == {"tabla_bos", "yazici_bosta"}

    assert istemci.post(f"/api/queue/{emir['id']}/baskiya-al").status_code == 200
    assert istemci.post(f"/api/queue/{emir['id']}/baskiya-al").status_code == 409
    assert istemci.post(f"/api/queue/{emir['id']}/hata", json={"gram": 5, "saat": 0, "dakika": 10}).status_code == 200
    assert istemci.post(f"/api/queue/{emir['id']}/tamamla").status_code == 200
    kuyruk = istemci.get("/api/queue").json()
    assert [(e["id"], e["status"], e["sku"]) for e in kuyruk] == [(emir["id"], "TAMAMLANDI", "PRT-1")]
    assert istemci.get("/api/queue", params={"status": "bekliyor"}).json() == []


def test_mcp_araclari_ayni_kurallari_uygular(istemci, dunya, tmp_path, monkeypatch):
    import mcp_server

    adlar = {"urun_gcode_onayla", "emir_baskiya_al", "emir_hata", "basilabilir_emir", "ozel_emir_ac", "musteri_onayi", "emir_gcode_bagla"}
    assert all(callable(getattr(mcp_server, a)) for a in adlar)
    r = mcp_server.urun_gcode_onayla("PRT-1", gcode_yaz(tmp_path))
    assert r["status"] == "success" and (r["saat"], r["dakika"], r["gram"]) == (1, 30, 42.5)
    emir = mcp_server.uretim_emri_ac("PRT-1")["emir"]
    r = mcp_server.basilabilir_emir(dunya["makine"].id, tabla_bos=False)
    assert {n["kosul"] for n in r["nedenler"]} == {"tabla_bos", "yazici_bosta", "gozetimsiz_izin", "filament"}
    assert mcp_server.emir_baskiya_al(emir["id"])["emir"]["status"] == "BASILIYOR"
    assert mcp_server.uretim_emirleri("BASILIYOR")[0]["id"] == emir["id"]
    assert mcp_server.uretim_emri_tamamla(emir["id"])["status"] == "success"
