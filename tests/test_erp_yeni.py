"""Çoklu filament yuvası, ambalaj alt-BOM grupları ve Esnaf Muafiyeti stopaj testleri."""
import pytest
from fastapi.testclient import TestClient

import cost_engine
import istasyon_altyapi as ist
import models
from conftest import gcode_yaz


@pytest.fixture()
def istemci(session, dunya, tmp_path, monkeypatch):
    monkeypatch.setattr(ist, "GCODE_DEPO", str(tmp_path / "depo"))
    import main

    with TestClient(main.app) as c:
        yield c


def test_makine_coklu_filament_yuvasi_ve_basilabilir_emir(istemci, session, dunya, tmp_path):
    mid = dunya["makine"].id
    f1 = dunya["filament"]
    f2 = models.Filament(code="FIL-2", brand="Elegoo", color_name="Beyaz", remaining_weight_gr=1000)
    f3 = models.Filament(code="FIL-3", brand="Elegoo", color_name="Kırmızı", remaining_weight_gr=1000)
    f4 = models.Filament(code="FIL-4", brand="Elegoo", color_name="Mavi", remaining_weight_gr=1000)
    session.add_all([f2, f3, f4])
    session.commit()
    for f in (f2, f3, f4):
        session.refresh(f)

    # 4 renkli BOM'a sahip ürün oluştur ve gözetimsiz onayı ile G-code'u kaydet
    r_prod = istemci.post(
        "/api/products",
        json={
            "sku": "PRT-MULTI",
            "name": "4 Renkli Figür",
            "machine_id": mid,
            "print_time_hours_int": 2,
            "gozetimsiz_baski": True,
            "filaments": [
                {"filament_id": f1.id, "net_weight_gr": 25, "waste_rate_percent": 0},
                {"filament_id": f2.id, "net_weight_gr": 25, "waste_rate_percent": 0},
                {"filament_id": f3.id, "net_weight_gr": 25, "waste_rate_percent": 0},
                {"filament_id": f4.id, "net_weight_gr": 25, "waste_rate_percent": 0},
            ],
        },
    )
    assert r_prod.status_code == 200
    uid_multi = r_prod.json()["product_id"]
    assert istemci.post(
        f"/api/products/{uid_multi}/gcode-onayla",
        json={"gcode_yolu": gcode_yaz(tmp_path, ad="multi.gcode")},
    ).status_code == 200

    # Kuyruğa emir aç
    assert istemci.post("/api/queue", json={"product_id": uid_multi}).status_code == 200

    # Önce slot sayısını 2'ye düşür -> 4 filamentli ürün için reddetmeli
    r_slot2 = istemci.put(
        f"/api/machines/{mid}/yuklu-filament",
        json={"filament_slot_sayisi": 2, "filament_ids": [f1.id, f2.id]},
    )
    assert r_slot2.status_code == 200
    assert r_slot2.json()["filament_slot_sayisi"] == 2
    chk2 = istemci.get(
        "/api/queue/basilabilir",
        params={"machine_id": mid, "tabla_bos": True, "yazici_bosta": True},
    ).json()
    assert chk2["basilabilir"] is False
    assert any("2 yuva var" in n["aciklama"] for n in chk2["nedenler"])

    # Slot sayısını 4'e çıkar, 3 filament yükle -> eksik 4. filament uyarısı vermeli
    istemci.put(
        f"/api/machines/{mid}/yuklu-filament",
        json={"filament_slot_sayisi": 4, "filament_ids": [f1.id, f2.id, f3.id, None]},
    )
    chk3 = istemci.get(
        "/api/queue/basilabilir",
        params={"machine_id": mid, "tabla_bos": True, "yazici_bosta": True},
    ).json()
    assert chk3["basilabilir"] is False
    assert any(n["kosul"] == "filament" for n in chk3["nedenler"])

    # Slot 4'ü tekli slot güncellemesi ile yükle -> basılabilir=True olmalı
    r_s4 = istemci.put(
        f"/api/machines/{mid}/yuklu-filament",
        json={"slot": 4, "filament_id": f4.id},
    )
    assert r_s4.status_code == 200
    assert r_s4.json()["yuklu_filament_4_id"] == f4.id

    chk4 = istemci.get(
        "/api/queue/basilabilir",
        params={"machine_id": mid, "tabla_bos": True, "yazici_bosta": True},
    ).json()
    assert chk4["basilabilir"] is True


def test_malzeme_grubu_alt_bom_maliyet_stok_ve_silme_korumasi(istemci, session, dunya):
    # Alt malzemeleri ekle: Kutu (18 TL), Delikli Kraft Kağıt (12.50 TL/metre), Bant+Etiket (3.50 TL)
    r_box = istemci.post(
        "/api/materials",
        json={
            "code": "AMB-KUTU-KUCUK",
            "name": "E-Ticaret Kutusu Küçük",
            "category": "AMBALAJ",
            "unit": "adet",
            "unit_cost_tl": 18.0,
            "stock_quantity": 2.0,
            "min_stock_alert": 1.0,
        },
    )
    r_pad = istemci.post(
        "/api/materials",
        json={
            "code": "AMB-KRAFT-PETEK",
            "name": "Delikli Oluklu Kraft Dolgu",
            "category": "AMBALAJ",
            "unit": "metre",
            "unit_cost_tl": 12.5,
            "stock_quantity": 2.0,
            "min_stock_alert": 0.5,
        },
    )
    r_lbl = istemci.post(
        "/api/materials",
        json={
            "code": "AMB-ETIKET-BANT",
            "name": "Kraft Bant + Etiket",
            "category": "AMBALAJ",
            "unit": "set",
            "unit_cost_tl": 3.5,
            "stock_quantity": 2.0,
            "min_stock_alert": 1.0,
        },
    )
    m_box, m_pad, m_lbl = r_box.json(), r_pad.json(), r_lbl.json()

    # Ambalaj_Kucuk grubunu oluştur: 18*1 + 12.5*0.5 + 3.5*1 = 27.75 TL
    r_grp = istemci.post(
        "/api/material-groups",
        json={
            "code": "Ambalaj_Kucuk",
            "name": "Küçük E-Ticaret Ambalaj Seti",
            "category": "AMBALAJ_GRUBU",
            "description": "Standart küçük kutu + delikli kraft dolgu + etiket",
            "items": [
                {"material_id": m_box["id"], "quantity": 1.0, "waste_rate_percent": 0.0},
                {"material_id": m_pad["id"], "quantity": 0.5, "waste_rate_percent": 0.0},
                {"material_id": m_lbl["id"], "quantity": 1.0, "waste_rate_percent": 0.0},
            ],
        },
    )
    assert r_grp.status_code == 200
    grp = r_grp.json()
    assert grp["code"] == "Ambalaj_Kucuk"
    assert grp["unit_cost_tl"] == pytest.approx(27.75, abs=0.01)

    # Ürünü gruba bağlamadan önce ve bağladıktan sonraki üretim maliyet farkını kontrol et
    uid = dunya["urun"].id
    before_prod = next(p for p in istemci.get("/api/products").json() if p["product_id"] == uid)
    base_cost = before_prod["production_cost_tl"]

    r_upd = istemci.put(
        f"/api/products/{uid}",
        json={
            "sku": "PRT-1",
            "name": "Stand",
            "machine_id": dunya["makine"].id,
            "print_time_hours_int": 2,
            "filaments": [{"filament_id": dunya["filament"].id, "net_weight_gr": 100, "waste_rate_percent": 0}],
            "groups": [{"group_id": grp["id"], "quantity": 1.0}],
        },
    )
    assert r_upd.status_code == 200
    after_prod = next(p for p in istemci.get("/api/products").json() if p["product_id"] == uid)
    assert after_prod["total_group_cost_tl"] == pytest.approx(27.75, abs=0.01)
    assert after_prod["production_cost_tl"] == pytest.approx(base_cost + 27.75, abs=0.02)
    assert len(after_prod["material_groups"]) == 1

    # Reçetede kullanıldığı için grup silinemez (409)
    assert istemci.delete(f"/api/material-groups/{grp['id']}").status_code == 409

    # 1 adet üretim yap -> alt malzemeler 2 -> (1, 1.5, 1) düşmeli
    r_ord1 = istemci.post(
        "/api/orders",
        json={
            "order_type": "PRODUCTION",
            "channel": "IKAS_B2C",
            "product_id": uid,
            "machine_id": dunya["makine"].id,
            "quantity": 1,
        },
    )
    assert r_ord1.status_code == 200
    mats = {m["code"]: m for m in istemci.get("/api/materials").json()}
    assert mats["AMB-KUTU-KUCUK"]["stock_quantity"] == pytest.approx(1.0)
    assert mats["AMB-KRAFT-PETEK"]["stock_quantity"] == pytest.approx(1.5)
    assert mats["AMB-ETIKET-BANT"]["stock_quantity"] == pytest.approx(1.0)

    # 2 adet üretim stok yetmediği için 409 vermeli (kutu sadece 1 kaldı)
    r_ord2 = istemci.post(
        "/api/orders",
        json={
            "order_type": "PRODUCTION",
            "channel": "IKAS_B2C",
            "product_id": uid,
            "machine_id": dunya["makine"].id,
            "quantity": 2,
        },
    )
    assert r_ord2.status_code == 409

    # Üründen grubu kaldırınca grup silinebilir
    istemci.put(
        f"/api/products/{uid}",
        json={
            "sku": "PRT-1",
            "name": "Stand",
            "machine_id": dunya["makine"].id,
            "print_time_hours_int": 2,
            "filaments": [{"filament_id": dunya["filament"].id, "net_weight_gr": 100, "waste_rate_percent": 0}],
            "groups": [],
        },
    )
    assert istemci.delete(f"/api/material-groups/{grp['id']}").status_code == 200


def test_esnaf_muafiyeti_stopaj_hesabi_ve_dashboard_limiti(istemci, session, dunya):
    # Doğrudan cost_engine testi: comm=%4, stopaj=%4, KDV kapalı
    p4 = cost_engine.calculate_pricing_from_cost(
        production_cost_tl=100.0,
        shipping_cost_tl=50.0,
        commission_rate_percent=4.0,
        target_margin_percent=30.0,
        vat_enabled=False,
        bank_withholding_rate_percent=4.0,
    )
    # combined = 0.04 + (0.96 * 0.04) = 0.0784 -> break_even = 150 / (1 - 0.0784) = 162.76
    assert p4["break_even_gross_tl"] == pytest.approx(150.0 / (1.0 - 0.0784), abs=0.05)
    assert p4["bank_incoming_tl"] == pytest.approx(
        p4["final_sale_price_gross_tl"] - p4["commission_amount_tl"], abs=0.05
    )
    assert p4["bank_withholding_amount_tl"] == pytest.approx(
        p4["bank_incoming_tl"] * 0.04, abs=0.05
    )
    assert p4["bank_net_payout_tl"] == pytest.approx(
        p4["bank_incoming_tl"] - p4["bank_withholding_amount_tl"], abs=0.05
    )
    assert p4["actual_margin_percent"] == pytest.approx(30.0, abs=0.2)

    # Başa Baş noktasında net kâr 0.00 TL olmalı
    be_check = cost_engine.calculate_pricing_from_cost(
        production_cost_tl=100.0,
        shipping_cost_tl=50.0,
        commission_rate_percent=4.0,
        target_margin_percent=30.0,
        vat_enabled=False,
        manual_sale_price_tl=p4["break_even_gross_tl"],
        bank_withholding_rate_percent=4.0,
    )
    assert be_check["net_profit_tl"] == pytest.approx(0.0, abs=0.05)

    # İşçili %2 stopaj senaryosu daha düşük net fiyat üretmeli
    p2 = cost_engine.calculate_pricing_from_cost(
        production_cost_tl=100.0,
        shipping_cost_tl=50.0,
        commission_rate_percent=4.0,
        target_margin_percent=30.0,
        vat_enabled=False,
        bank_withholding_rate_percent=2.0,
    )
    assert p2["final_sale_price_gross_tl"] < p4["final_sale_price_gross_tl"]

    # Settings üzerinden Esnaf Muafiyeti (%4, yıllık limit 1000 TL test için) etkinleştir
    s_cur = istemci.get("/api/settings").json()["settings"]
    r_set = istemci.put(
        "/api/settings",
        json={
            "brand_name": s_cur["brand_name"],
            "electricity_price_per_kwh": 4.5,
            "commission_rate_percent": 4.0,
            "esnaf_muafiyeti_enabled": True,
            "bank_withholding_rate_percent": 4.0,
            "esnaf_yillik_limit_tl": 1000.0,
            "default_vat_rate_percent": s_cur["default_vat_rate_percent"],
            "default_vat_enabled": False,
            "default_target_margin_percent": 30.0,
            "default_b2b_margin_percent": s_cur["default_b2b_margin_percent"],
            "default_shipping_cost": 150.0,
            "kargo_esigi_tl": 1500.0,
        },
    )
    assert r_set.status_code == 200

    # Sipariş oluştur -> stopaj snapshot'ları yazılmalı
    r_ord = istemci.post(
        "/api/orders",
        json={
            "order_type": "PRODUCTION",
            "channel": "IKAS_B2C",
            "product_id": dunya["urun"].id,
            "machine_id": dunya["makine"].id,
            "quantity": 1,
            "custom_unit_sale_price_tl": 900.0,
        },
    )
    assert r_ord.status_code == 200
    ord_data = r_ord.json()
    assert ord_data["snapshot_bank_withholding_rate_percent"] == pytest.approx(4.0)
    assert ord_data["bank_incoming_tl"] == pytest.approx(864.0, abs=0.05)  # 900 * (1 - 0.04)
    assert ord_data["bank_withholding_amount_tl"] == pytest.approx(34.56, abs=0.05)  # 864 * 0.04
    assert ord_data["bank_net_payout_tl"] == pytest.approx(829.44, abs=0.05)

    # Dashboard esnaf_muafiyeti_summary ve >= %80 yıllık limit uyarısı
    dash = istemci.get("/api/dashboard").json()
    esnaf = dash["esnaf_muafiyeti_summary"]
    assert esnaf["enabled"] is True
    assert esnaf["stopaj_rate_percent"] == pytest.approx(4.0)
    assert esnaf["ytd_gross_revenue_tl"] == pytest.approx(900.0, abs=0.05)
    assert esnaf["limit_usage_percent"] == pytest.approx(90.0, abs=0.1)
    assert esnaf["total_bank_withholding_tl"] == pytest.approx(34.56, abs=0.05)
    assert any("Esnaf Muafiyeti Yıllık Hasılat Sınırı" in w for w in dash["maintenance_warnings"])

    # KDV açıkken de Başa Baş noktasında net kâr 0.00 TL olmalı
    p_kdv = cost_engine.calculate_pricing_from_cost(
        production_cost_tl=100.0,
        shipping_cost_tl=50.0,
        commission_rate_percent=4.0,
        target_margin_percent=30.0,
        vat_enabled=True,
        vat_rate_percent=20.0,
        bank_withholding_rate_percent=4.0,
    )
    be_kdv = cost_engine.calculate_pricing_from_cost(
        production_cost_tl=100.0,
        shipping_cost_tl=50.0,
        commission_rate_percent=4.0,
        target_margin_percent=30.0,
        vat_enabled=True,
        vat_rate_percent=20.0,
        manual_sale_price_tl=p_kdv["break_even_gross_tl"],
        bank_withholding_rate_percent=4.0,
    )
    assert be_kdv["net_profit_tl"] == pytest.approx(0.0, abs=0.05)
