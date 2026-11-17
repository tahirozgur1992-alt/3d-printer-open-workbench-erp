import os

import pytest
from sqlmodel import select

import istasyon_altyapi as ist
import models
from conftest import gcode_yaz


@pytest.fixture(autouse=True)
def depo(tmp_path, monkeypatch):
    monkeypatch.setattr(ist, "GCODE_DEPO", str(tmp_path / "depo"))


def kosullar(sonuc):
    return {n["kosul"] for n in sonuc["nedenler"]}


def hazirla(session, dunya, tmp_path):
    """Bütün koşulları sağlayan durum: onaylı G-code, izin, yüklü filament, bekleyen emir."""
    u, m = dunya["urun"], dunya["makine"]
    assert ist.urun_gcode_onayla(session, u.sku, gcode_yaz(tmp_path))["status"] == "success"
    u.gozetimsiz_baski = True
    m.yuklu_filament_id = dunya["filament"].id
    session.add_all([u, m])
    session.commit()
    return ist.emir_ac(session, u.sku)["emir"]["id"]


# --- urun_gcode_onayla
def test_gcode_onayi_sure_ve_grami_urun_kartina_yazar(session, dunya, tmp_path):
    kaynak = gcode_yaz(tmp_path, sure="1h 30m 0s", gram="42.50")
    r = ist.urun_gcode_onayla(session, "PRT-1", kaynak)
    assert r["status"] == "success" and r["uyari"] is None
    u = session.exec(select(models.Product).where(models.Product.sku == "PRT-1")).one()
    assert (u.print_time_hours_int, u.print_time_minutes_int, u.print_time_decimal_hours) == (1, 30, 1.5)
    assert u.gcode_sha256 == ist.dosya_ozeti(kaynak) and u.gcode_onay_tarihi is not None
    kalem = session.exec(select(models.ProductBOMFilament)).one()
    assert kalem.net_weight_gr == 42.5
    assert u.gcode_path.endswith(f"PRT-1/{u.gcode_sha256[:8]}.gcode") and os.path.isfile(u.gcode_path)
    assert not os.access(u.gcode_path, os.W_OK)  # depodaki kopya salt okunur


def test_gcode_onayi_baska_yaziciyi_reddeder(session, dunya, tmp_path):
    r = ist.urun_gcode_onayla(session, "PRT-1", gcode_yaz(tmp_path, model="Bambu Lab P1S"))
    assert r["status"] == "error" and "Centauri Carbon" in r["message"]
    assert session.exec(select(models.Product)).one().gcode_path is None


def test_gcode_onayi_eksik_dosya_ve_bilinmeyen_urun(session, dunya, tmp_path):
    assert ist.urun_gcode_onayla(session, "PRT-1", str(tmp_path / "yok.gcode"))["status"] == "error"
    assert ist.urun_gcode_onayla(session, "YOK", gcode_yaz(tmp_path))["status"] == "error"
    assert ist.urun_gcode_onayla(session, "PRT-1", gcode_yaz(tmp_path, sure="0s", gram="0"))["status"] == "error"


def test_cok_renkli_recetede_gram_renk_sirasiyla_yazilir(session, dunya, tmp_path):
    ikinci = models.Filament(code="FIL-2", brand="Elegoo", color_name="Beyaz")
    session.add(ikinci)
    session.commit()
    session.add(models.ProductBOMFilament(product_id=dunya["urun"].id, filament_id=ikinci.id, net_weight_gr=1))
    session.commit()
    r = ist.urun_gcode_onayla(session, "PRT-1", gcode_yaz(tmp_path, gram="30.0, 12.0"))
    assert r["uyari"] is None
    gramlar = [k.net_weight_gr for k in session.exec(select(models.ProductBOMFilament).order_by(models.ProductBOMFilament.id))]
    assert gramlar == [30.0, 12.0]
    r = ist.urun_gcode_onayla(session, "PRT-1", gcode_yaz(tmp_path, ad="tek.gcode", gram="50.0"))
    assert "gram yazılmadı" in r["uyari"]


# --- basilabilir_emir: her eksik koşul adıyla döner
def test_butun_kosullar_saglaninca_basilabilir(session, dunya, tmp_path):
    emir_id = hazirla(session, dunya, tmp_path)
    r = ist.basilabilir_emir(session, dunya["makine"].id, tabla_bos=True, yazici_bosta=True)
    assert r["basilabilir"] is True and r["nedenler"] == []
    assert r["emir"]["id"] == emir_id and os.path.isfile(r["gcode_path"])
    assert r["gereken_gr"] == 42.5


@pytest.mark.parametrize(
    "bozan, beklenen",
    [
        ("tabla", "tabla_bos"),
        ("yazici_mesgul", "yazici_bosta"),
        ("yazici_bilinmiyor", "yazici_bosta"),
        ("izin", "gozetimsiz_izin"),
        ("ozel", "urun_tipi"),
        ("gcode_yok", "onayli_gcode"),
        ("gcode_silindi", "onayli_gcode"),
        ("gcode_degisti", "gcode_ozeti"),
        ("yuklu_yok", "filament"),
        ("yuklu_farkli", "filament"),
        ("stok", "filament_stok"),
        ("baska_baski", "tek_baski"),
    ],
)
def test_eksik_kosul_adiyla_doner(session, dunya, tmp_path, bozan, beklenen):
    hazirla(session, dunya, tmp_path)
    u, m, f = dunya["urun"], dunya["makine"], dunya["filament"]
    tabla, yazici = True, True
    if bozan == "tabla":
        tabla = False
    elif bozan == "yazici_mesgul":
        yazici = False
    elif bozan == "yazici_bilinmiyor":
        yazici = None
    elif bozan == "izin":
        u.gozetimsiz_baski = False
    elif bozan == "ozel":
        u.urun_tipi = "KISIYE_OZEL"
    elif bozan == "gcode_yok":
        u.gcode_path = u.gcode_sha256 = None
    elif bozan == "gcode_silindi":
        os.chmod(u.gcode_path, 0o644)
        os.remove(u.gcode_path)
    elif bozan == "gcode_degisti":
        os.chmod(u.gcode_path, 0o644)
        with open(u.gcode_path, "a") as dosya:
            dosya.write("G1 X0\n")
    elif bozan == "yuklu_yok":
        m.yuklu_filament_id = None
    elif bozan == "yuklu_farkli":
        diger = models.Filament(code="FIL-9", brand="Elegoo", color_name="Kırmızı")
        session.add(diger)
        session.commit()
        m.yuklu_filament_id = diger.id
    elif bozan == "stok":
        f.remaining_weight_gr = 46.0  # gereken 42,5 g; %10 payla 46,75 g
    elif bozan == "baska_baski":
        session.add(models.ProductionQueueItem(product_id=u.id, machine_id=m.id, status="BASILIYOR"))
    session.add_all([u, m, f])
    session.commit()
    r = ist.basilabilir_emir(session, m.id, tabla_bos=tabla, yazici_bosta=yazici)
    assert r["basilabilir"] is False
    assert kosullar(r) == {beklenen}
    assert all(n["aciklama"] for n in r["nedenler"])


def test_stok_tam_sinirda_yeter(session, dunya, tmp_path):
    hazirla(session, dunya, tmp_path)
    dunya["filament"].remaining_weight_gr = 46.75
    session.add(dunya["filament"])
    session.commit()
    assert ist.basilabilir_emir(session, dunya["makine"].id, True, True)["basilabilir"] is True


def test_kuyruk_bossa_ve_makine_yoksa(session, dunya):
    r = ist.basilabilir_emir(session, dunya["makine"].id, True, True)
    assert r["basilabilir"] is False and kosullar(r) == {"emir"}
    assert ist.basilabilir_emir(session, 999, True, True)["status"] == "error"


def test_birden_fazla_eksik_birlikte_doner_ve_salt_okur(session, dunya, tmp_path):
    emir_id = hazirla(session, dunya, tmp_path)
    r = ist.basilabilir_emir(session, dunya["makine"].id, tabla_bos=False, yazici_bosta=False)
    assert kosullar(r) == {"tabla_bos", "yazici_bosta"}
    assert session.get(models.ProductionQueueItem, emir_id).status == "BEKLIYOR"
    assert dunya["filament"].remaining_weight_gr == 1000


def test_kuyruktan_en_eski_emir_alinir(session, dunya, tmp_path):
    ilk = hazirla(session, dunya, tmp_path)
    ist.emir_ac(session, "PRT-1")
    assert ist.basilabilir_emir(session, dunya["makine"].id, True, True)["emir"]["id"] == ilk


# --- kuyruk durumları
def test_baskiya_al_tamamla_stok_duser(session, dunya, tmp_path):
    emir_id = hazirla(session, dunya, tmp_path)
    r = ist.emir_baskiya_al(session, emir_id)
    assert r["emir"]["status"] == "BASILIYOR" and r["emir"]["baski_basladi_at"] is not None
    ikinci = ist.emir_ac(session, "PRT-1")["emir"]["id"]
    assert "başka baskı" in ist.emir_baskiya_al(session, ikinci)["message"]
    assert ist.emir_baskiya_al(session, emir_id)["status"] == "error"
    r = ist.emir_tamamla(session, emir_id)
    assert r["status"] == "success"
    session.refresh(dunya["filament"])
    assert dunya["filament"].remaining_weight_gr == 957.5
    assert session.get(models.ProductionQueueItem, emir_id).status == "TAMAMLANDI"
    assert ist.emir_tamamla(session, emir_id)["status"] == "error"  # stok iki kez düşmez


def test_emir_hata_fire_kaydeder_ve_kuyruga_dondurur(session, dunya, tmp_path):
    emir_id = hazirla(session, dunya, tmp_path)
    assert ist.emir_hata(session, emir_id, 10, 0, 20)["status"] == "error"  # henüz basılmıyor
    ist.emir_baskiya_al(session, emir_id)
    r = ist.emir_hata(session, emir_id, gram=12.5, saat=0, dakika=30)
    assert r["status"] == "success" and r["fire_kaydi"]["order_type"] == "WASTE"
    assert r["emir"]["status"] == "BEKLIYOR" and r["emir"]["baski_basladi_at"] is None
    session.refresh(dunya["filament"])
    session.refresh(dunya["makine"])
    assert dunya["filament"].remaining_weight_gr == 987.5
    assert dunya["makine"].total_worked_hours == 0.5


def test_kisiye_ozel_hat(session, dunya, tmp_path):
    u = dunya["urun"]
    assert ist.ozel_emir_ac(session, u.sku, "Öykü")["status"] == "error"  # standart ürün
    u.urun_tipi = "KISIYE_OZEL"
    session.add(u)
    session.commit()
    assert ist.emir_ac(session, u.sku)["status"] == "error"
    assert ist.ozel_emir_ac(session, u.sku, "  ")["status"] == "error"
    emir = ist.ozel_emir_ac(session, u.sku, " Öykü Çağla ")["emir"]
    assert emir["status"] == "ONAY_BEKLIYOR" and emir["ozel_metin"] == "Öykü Çağla"
    assert ist.emir_gcode_bagla(session, emir["id"], gcode_yaz(tmp_path))["status"] == "error"  # önce onay
    assert ist.emir_baskiya_al(session, emir["id"])["status"] == "error"
    r = ist.musteri_onayi(session, emir["id"])
    assert r["emir"]["status"] == "DILIM_BEKLIYOR" and r["emir"]["musteri_onay_at"] is not None
    assert ist.musteri_onayi(session, emir["id"])["status"] == "error"
    assert ist.emir_gcode_bagla(session, emir["id"], gcode_yaz(tmp_path, model="Baska"))["status"] == "error"
    r = ist.emir_gcode_bagla(session, emir["id"], gcode_yaz(tmp_path))
    assert r["emir"]["status"] == "BEKLIYOR" and os.path.isfile(r["emir"]["gcode_path"])
    # kişiye özel emir hiçbir zaman gözetimsiz basılmaz
    r = ist.basilabilir_emir(session, dunya["makine"].id, True, True)
    assert r["basilabilir"] is False and "urun_tipi" in kosullar(r)


def test_iptal_yalnizca_baskiya_girmemis_emirde(session, dunya, tmp_path):
    emir_id = hazirla(session, dunya, tmp_path)
    ist.emir_baskiya_al(session, emir_id)
    assert ist.emir_iptal(session, emir_id, "vazgeçildi")["status"] == "error"
    ikinci = ist.emir_ac(session, "PRT-1")["emir"]["id"]
    assert ist.emir_iptal(session, ikinci, "vazgeçildi")["emir"]["status"] == "IPTAL"


def test_makine_karti_bir_kez_acilir(session):
    r = ist.makine_karti_hazirla(session)
    assert r["eklenen"] == [ist.MAKINE_ADI, *ist.BAKIM_PARCALARI]
    assert ist.makine_karti_hazirla(session)["eklenen"] == []
    assert len(session.exec(select(models.Machine)).all()) == 1
    assert len(session.exec(select(models.MachinePart)).all()) == 2
