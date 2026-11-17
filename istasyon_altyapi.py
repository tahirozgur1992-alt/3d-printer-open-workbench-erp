"""İstasyon altyapısı: onaylı G-code, baskı kuyruğu durumları ve gözetimsiz baskı koşulları.

Kurallar burada tek yerde durur; MCP araçları ve ERP ekranı bu işlevleri çağırır.
Her işlev bir oturum alır ve sözlük döndürür: {"status": "success" | "error", ...}.
"""
import hashlib
import os
import re
import shutil
from typing import Any, Dict, List, Optional

from sqlmodel import Session, select

from cost_engine import hours_minutes_to_decimal, makine_coz
from database import DB_PATH
from workbench_config import MACHINE_NAME, PRINTER_MODEL
from models import Filament, Machine, MachinePart, Product, ProductBOMFilament, ProductionQueueItem, utc_now
from slicer_parser import parse_gcode_content

GCODE_DEPO = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "gcode")
YAZICI_MODELI = PRINTER_MODEL  # env: WORKBENCH_PRINTER_MODEL
STOK_PAYI = 1.10  # gereken gramın üstüne %10
URUN_TIPLERI = ("STANDART", "KISIYE_OZEL")
EMIR_DURUMLARI = ("BEKLIYOR", "ONAY_BEKLIYOR", "DILIM_BEKLIYOR", "BASILIYOR", "HATA", "TAMAMLANDI", "IPTAL")
MAKINE_ADI = MACHINE_NAME  # env: WORKBENCH_MACHINE_NAME
BAKIM_PARCALARI = ("PEI tabla", "Nozul 0.4 mm")


def _hata(mesaj: str, **ek: Any) -> Dict[str, Any]:
    return {"status": "error", "message": mesaj, **ek}


def dosya_ozeti(yol: str) -> str:
    ozet = hashlib.sha256()
    with open(yol, "rb") as f:
        for parca in iter(lambda: f.read(1 << 20), b""):
            ozet.update(parca)
    return ozet.hexdigest()


def gcode_yazici_modeli(metin: str) -> Optional[str]:
    """OrcaSlicer'ın G-code'a yazdığı yazıcı adını döndürür; bulunamazsa None."""
    for anahtar in ("printer_model", "printer_settings_id"):
        m = re.search(rf"^;\s*{anahtar}\s*=\s*(.+)$", metin, re.MULTILINE)
        if m and m.group(1).strip():
            return m.group(1).strip().strip('"')
    return None


def gcode_oku(yol: str) -> Dict[str, Any]:
    """G-code'un özetini, yazıcı modelini, süre ve gramını okur."""
    with open(yol, "r", encoding="utf-8", errors="ignore") as f:
        metin = f.read()
    b = parse_gcode_content(metin)
    return {
        "sha256": dosya_ozeti(yol),
        "yazici_modeli": gcode_yazici_modeli(metin),
        "saat": int(b.get("hours", 0)),
        "dakika": int(b.get("minutes", 0)),
        "gram": float(b.get("filament_grams", 0.0)),
        "gram_renk_bazli": [float(g) for g in b.get("filament_grams_per_extruder") or []],
        "katman_mm": b.get("layer_height_mm"),
    }


def _gcode_denetle(gcode_yolu: str) -> Any:
    """Yolu çözer ve dosyayı okur; sorun varsa hata sözlüğü döndürür."""
    yol = os.path.abspath(os.path.expanduser(gcode_yolu))
    if not os.path.isfile(yol):
        return _hata(f"G-code bulunamadı: {yol}")
    if not yol.lower().endswith(".gcode"):
        return _hata(f"Yalnızca .gcode dosyası onaylanır: {os.path.basename(yol)}")
    bilgi = gcode_oku(yol)
    model = bilgi["yazici_modeli"]
    if not model or YAZICI_MODELI.lower() not in model.lower():
        return _hata(
            f"G-code başlığındaki yazıcı {YAZICI_MODELI} değil: {model or 'yazıcı adı yok'}", gcode=bilgi
        )
    if bilgi["gram"] <= 0 or (bilgi["saat"] == 0 and bilgi["dakika"] == 0):
        return _hata("G-code'dan süre ya da gram okunamadı; dosya dilimleyici çıktısı olmayabilir.", gcode=bilgi)
    return yol, bilgi


def _depoya_kopyala(kaynak: str, sku: str, dosya_adi: str) -> str:
    klasor = os.path.join(GCODE_DEPO, re.sub(r"[^A-Za-z0-9._-]", "_", sku))
    os.makedirs(klasor, exist_ok=True)
    hedef = os.path.join(klasor, dosya_adi)
    if not os.path.exists(hedef):  # aynı özet aynı dosyadır; var olan ezilmez
        shutil.copyfile(kaynak, hedef)
        os.chmod(hedef, 0o444)
    return hedef


def _urun(session: Session, sku: str) -> Optional[Product]:
    return session.exec(select(Product).where(Product.sku == sku)).first()


def urun_gcode_onayla(session: Session, sku: str, gcode_yolu: str) -> Dict[str, Any]:
    """G-code'u depoya değişmez kopyalar, özetini alır, süre ve gramı ürün kartına yazar."""
    product = _urun(session, sku)
    if not product:
        return _hata(f"Ürün bulunamadı: {sku}")
    sonuc = _gcode_denetle(gcode_yolu)
    if isinstance(sonuc, dict):
        return sonuc
    yol, bilgi = sonuc
    hedef = _depoya_kopyala(yol, sku, f"{bilgi['sha256'][:8]}.gcode")

    product.gcode_path = hedef
    product.gcode_sha256 = bilgi["sha256"]
    product.gcode_onay_tarihi = utc_now()
    product.print_time_hours_int = bilgi["saat"]
    product.print_time_minutes_int = bilgi["dakika"]
    product.print_time_decimal_hours = hours_minutes_to_decimal(bilgi["saat"], bilgi["dakika"])
    if bilgi["katman_mm"]:
        product.layer_height_mm = float(bilgi["katman_mm"])
    product.updated_at = utc_now()
    session.add(product)

    # Gram reçetede durur. Tek filamentli reçeteye toplam gram, çok renkliye renk sırasıyla yazılır.
    kalemler = list(
        session.exec(
            select(ProductBOMFilament).where(ProductBOMFilament.product_id == product.id).order_by(ProductBOMFilament.id)
        ).all()
    )
    renkler = [g for g in bilgi["gram_renk_bazli"] if g > 0]
    uyari = None
    if len(kalemler) == 1:
        kalemler[0].net_weight_gr = round(bilgi["gram"], 2)
    elif kalemler and len(kalemler) == len(renkler):
        for kalem, gram in zip(kalemler, renkler):
            kalem.net_weight_gr = round(gram, 2)
    elif not kalemler:
        uyari = "Reçetede filament kalemi yok; gram yazılmadı. Reçeteye filament ekleyin."
    else:
        uyari = (
            f"Reçetede {len(kalemler)} filament, G-code'da {len(renkler)} renk var; "
            "gram yazılmadı, reçeteyi elle düzeltin."
        )
    for kalem in kalemler:
        session.add(kalem)
    session.commit()
    session.refresh(product)
    return {
        "status": "success",
        "sku": sku,
        "gcode_path": hedef,
        "gcode_sha256": bilgi["sha256"],
        "saat": bilgi["saat"],
        "dakika": bilgi["dakika"],
        "gram": bilgi["gram"],
        "yazici_modeli": bilgi["yazici_modeli"],
        "uyari": uyari,
    }


def _emir(session: Session, emir_id: int) -> Optional[ProductionQueueItem]:
    return session.get(ProductionQueueItem, emir_id)


def _basilan(session: Session, machine_id: int) -> Optional[ProductionQueueItem]:
    return session.exec(
        select(ProductionQueueItem)
        .where(ProductionQueueItem.machine_id == machine_id)
        .where(ProductionQueueItem.status == "BASILIYOR")
    ).first()


def emir_baskiya_al(session: Session, emir_id: int) -> Dict[str, Any]:
    """Bekleyen emri BASILIYOR yapar. Aynı makinede başka baskı varsa reddeder."""
    emir = _emir(session, emir_id)
    if not emir:
        return _hata(f"Emir bulunamadı: {emir_id}")
    if emir.status != "BEKLIYOR":
        return _hata(f"Yalnızca bekleyen emir baskıya alınır; emir {emir.status}.")
    diger = _basilan(session, emir.machine_id)
    if diger:
        return _hata(f"Makinede başka baskı sürüyor: emir {diger.id}.")
    emir.status = "BASILIYOR"
    emir.baski_basladi_at = utc_now()
    session.add(emir)
    session.commit()
    session.refresh(emir)
    return {"status": "success", "emir": emir.model_dump()}


def emir_hata(session: Session, emir_id: int, gram: float, saat: int, dakika: int) -> Dict[str, Any]:
    """Başarısız baskıyı fire olarak kaydeder ve emri kuyruğa (BEKLIYOR) döndürür."""
    from fastapi import HTTPException

    from main import ProductionOrderInput, create_production_order

    emir = _emir(session, emir_id)
    if not emir:
        return _hata(f"Emir bulunamadı: {emir_id}")
    if emir.status not in ("BASILIYOR", "HATA"):
        return _hata(f"Yalnızca basılan ya da hatalı emir için fire girilir; emir {emir.status}.")
    if gram < 0 or saat < 0 or dakika < 0:
        return _hata("Gram ve süre eksi olamaz.")
    machine = session.get(Machine, emir.machine_id)
    filament_id = machine.yuklu_filament_id if machine else None
    if filament_id is None:
        ilk = session.exec(
            select(ProductBOMFilament).where(ProductBOMFilament.product_id == emir.product_id)
        ).first()
        filament_id = ilk.filament_id if ilk else None
    if gram > 0 and filament_id is None:
        return _hata("Fire hangi filamentten düşülecek belli değil: makinede yüklü filament ve reçete yok.")
    try:
        kayit = create_production_order(
            ProductionOrderInput(
                order_type="WASTE",
                channel=emir.channel,
                product_id=emir.product_id,
                machine_id=emir.machine_id,
                waste_hours=saat,
                waste_minutes=dakika,
                waste_filament_id=filament_id,
                waste_grams=gram,
                notes=f"Emir {emir.id} başarısız baskı",
            ),
            session,
        )
    except HTTPException as e:
        return _hata(str(e.detail))
    emir.status = "BEKLIYOR"
    emir.baski_basladi_at = None
    session.add(emir)
    session.commit()
    session.refresh(emir)
    session.refresh(kayit)
    return {"status": "success", "emir": emir.model_dump(), "fire_kaydi": kayit.model_dump()}


def emir_ac(
    session: Session,
    sku: str,
    adet: int = 1,
    channel: str = "IKAS_B2C",
    machine_id: Optional[int] = None,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """Standart ürün için baskı kuyruğuna emir açar (BEKLIYOR). Stok baskı tamamlanınca düşülür."""
    product = _urun(session, sku)
    if not product:
        return _hata(f"Ürün bulunamadı: {sku}")
    if product.urun_tipi == "KISIYE_OZEL":
        return _hata(f"{sku} kişiye özel ürün; emir müşterinin metniyle ozel_emir_ac ile açılır.")
    if adet < 1:
        return _hata(f"Adet en az 1 olmalı: {adet}")
    try:
        machine_id = makine_coz(
            list(session.exec(select(Machine)).all()), machine_id if machine_id is not None else product.machine_id
        )
    except ValueError as e:
        return _hata(str(e))
    emir = ProductionQueueItem(
        product_id=product.id, machine_id=machine_id, quantity=adet, channel=channel, notes=notes
    )
    session.add(emir)
    session.commit()
    session.refresh(emir)
    return {"status": "success", "emir": emir.model_dump()}


def emir_tamamla(session: Session, emir_id: int) -> Dict[str, Any]:
    """Baskısı biten emri tamamlar: stoğu ve makine saatini düşer, üretim kaydı açar."""
    from fastapi import HTTPException

    from main import ProductionOrderInput, create_production_order
    from models import GlobalSettings

    emir = _emir(session, emir_id)
    if not emir:
        return _hata(f"Emir bulunamadı: {emir_id}")
    if emir.status not in ("BEKLIYOR", "BASILIYOR"):
        return _hata(f"Emir {emir.status}; yalnızca bekleyen ya da basılan emir tamamlanır, stok düşülmedi.")
    settings = session.get(GlobalSettings, 1)
    try:
        kayit = create_production_order(
            ProductionOrderInput(
                channel=emir.channel,
                product_id=emir.product_id,
                machine_id=emir.machine_id,
                quantity=emir.quantity,
                vat_enabled=settings.default_vat_enabled,
                notes=emir.notes,
            ),
            session,
        )
    except HTTPException as e:
        return _hata(str(e.detail))
    emir.status = "TAMAMLANDI"
    emir.production_order_id = kayit.id
    emir.completed_at = utc_now()
    session.add(emir)
    session.commit()
    session.refresh(kayit)
    return {"status": "success", "uretim_kaydi": kayit.model_dump()}


def emir_iptal(session: Session, emir_id: int, neden: str) -> Dict[str, Any]:
    """Baskıya girmemiş emri stok düşmeden iptal eder."""
    emir = _emir(session, emir_id)
    if not emir or emir.status not in ("BEKLIYOR", "ONAY_BEKLIYOR", "DILIM_BEKLIYOR"):
        return _hata("Yalnızca baskıya girmemiş emir iptal edilebilir.")
    emir.status = "IPTAL"
    emir.notes = neden
    session.add(emir)
    session.commit()
    session.refresh(emir)
    return {"status": "success", "emir": emir.model_dump()}


def emir_listesi(session: Session, durum: Optional[str] = None) -> List[Dict[str, Any]]:
    """Kuyruktaki emirleri ürün bilgisiyle listeler; durum verilmezse hepsi, yeniden eskiye."""
    sorgu = select(ProductionQueueItem).order_by(ProductionQueueItem.created_at.desc(), ProductionQueueItem.id.desc())
    if durum:
        sorgu = sorgu.where(ProductionQueueItem.status == durum.upper())
    sonuc = []
    for emir in session.exec(sorgu).all():
        product = session.get(Product, emir.product_id)
        sonuc.append(
            {
                **emir.model_dump(),
                "sku": product.sku if product else "?",
                "urun": product.name if product else "silinmiş ürün",
                "urun_tipi": product.urun_tipi if product else None,
            }
        )
    return sonuc


def ozel_emir_ac(
    session: Session,
    sku: str,
    metin: str,
    adet: int = 1,
    channel: str = "IKAS_B2C",
    machine_id: Optional[int] = None,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """Kişiye özel ürün için emir açar; müşteri onayı gelene kadar ONAY_BEKLIYOR kalır."""
    product = _urun(session, sku)
    if not product:
        return _hata(f"Ürün bulunamadı: {sku}")
    if product.urun_tipi != "KISIYE_OZEL":
        return _hata(f"{sku} kişiye özel ürün değil; standart ürün için uretim_emri_ac kullanılır.")
    if not metin or not metin.strip():
        return _hata("Kişiye özel emirde müşterinin metni boş olamaz.")
    if adet < 1:
        return _hata(f"Adet en az 1 olmalı: {adet}")
    try:
        machine_id = makine_coz(
            list(session.exec(select(Machine)).all()), machine_id if machine_id is not None else product.machine_id
        )
    except ValueError as e:
        return _hata(str(e))
    emir = ProductionQueueItem(
        product_id=product.id,
        machine_id=machine_id,
        quantity=adet,
        channel=channel,
        status="ONAY_BEKLIYOR",
        ozel_metin=metin.strip(),
        notes=notes,
    )
    session.add(emir)
    session.commit()
    session.refresh(emir)
    return {"status": "success", "emir": emir.model_dump()}


def musteri_onayi(session: Session, emir_id: int) -> Dict[str, Any]:
    """Müşteri önizlemeyi onayladı: emir dilimleme bekler."""
    emir = _emir(session, emir_id)
    if not emir:
        return _hata(f"Emir bulunamadı: {emir_id}")
    if emir.status != "ONAY_BEKLIYOR":
        return _hata(f"Yalnızca onay bekleyen emir onaylanır; emir {emir.status}.")
    emir.status = "DILIM_BEKLIYOR"
    emir.musteri_onay_at = utc_now()
    session.add(emir)
    session.commit()
    session.refresh(emir)
    return {"status": "success", "emir": emir.model_dump()}


def emir_gcode_bagla(session: Session, emir_id: int, gcode_yolu: str) -> Dict[str, Any]:
    """Kişiye özel emrin G-code'unu depoya kopyalar ve emri baskı kuyruğuna (BEKLIYOR) alır."""
    emir = _emir(session, emir_id)
    if not emir:
        return _hata(f"Emir bulunamadı: {emir_id}")
    if emir.status != "DILIM_BEKLIYOR":
        return _hata(f"Yalnızca dilimleme bekleyen emre G-code bağlanır; emir {emir.status}.")
    sonuc = _gcode_denetle(gcode_yolu)
    if isinstance(sonuc, dict):
        return sonuc
    yol, bilgi = sonuc
    product = session.get(Product, emir.product_id)
    emir.gcode_path = _depoya_kopyala(yol, product.sku, f"emir-{emir.id}-{bilgi['sha256'][:8]}.gcode")
    emir.status = "BEKLIYOR"
    session.add(emir)
    session.commit()
    session.refresh(emir)
    return {"status": "success", "emir": emir.model_dump(), "gcode": bilgi}


def basilabilir_emir(
    session: Session, machine_id: int, tabla_bos: bool, yazici_bosta: Optional[bool] = None
) -> Dict[str, Any]:
    """Gözetimsiz baskı koşullarını (RUNBOOK Bölüm 4) denetler. Salt okur.

    Sıradaki emir, makinede en eski BEKLIYOR emirdir (kuyruktan tek emir alınır).
    Eksik her koşul adıyla `nedenler` listesinde döner; liste boşsa `basilabilir` True olur.
    `yazici_bosta` yazıcının SDCP durumudur ve istasyondan gelir; verilmezse koşul sağlanmamış sayılır.
    """
    machine = session.get(Machine, machine_id)
    if not machine:
        return _hata(f"Makine bulunamadı: {machine_id}")
    nedenler: List[Dict[str, str]] = []

    def eksik(kosul: str, aciklama: str) -> None:
        nedenler.append({"kosul": kosul, "aciklama": aciklama})

    emir = session.exec(
        select(ProductionQueueItem)
        .where(ProductionQueueItem.machine_id == machine_id)
        .where(ProductionQueueItem.status == "BEKLIYOR")
        .order_by(ProductionQueueItem.created_at, ProductionQueueItem.id)
    ).first()
    sonuc: Dict[str, Any] = {"status": "success", "machine_id": machine_id, "emir": None, "gcode_path": None}

    diger = _basilan(session, machine_id)
    if diger:
        eksik("tek_baski", f"Makinede başka baskı sürüyor: emir {diger.id}.")
    if yazici_bosta is None:
        eksik("yazici_bosta", "Yazıcının durumu bildirilmedi; boşta ve hatasız olduğu doğrulanmadı.")
    elif not yazici_bosta:
        eksik("yazici_bosta", "Yazıcı boşta değil ya da hata veriyor.")
    if not tabla_bos:
        eksik("tabla_bos", "Tabla boş onayı yok: son baskıdan sonra 'Tabla boş' düğmesine basılmadı.")

    if not emir:
        eksik("emir", "Kuyrukta bekleyen emir yok.")
        return {**sonuc, "basilabilir": False, "nedenler": nedenler}

    product = session.get(Product, emir.product_id)
    sonuc["emir"] = {**emir.model_dump(), "sku": product.sku, "urun": product.name}

    if product.urun_tipi != "STANDART":
        eksik("urun_tipi", f"{product.sku} kişiye özel ürün; yalnızca kullanıcı başlatır.")
    if not product.gozetimsiz_baski:
        eksik("gozetimsiz_izin", f"{product.sku} için gözetimsiz baskı izni kapalı.")

    gcode_tamam = False
    if not product.gcode_path or not product.gcode_sha256:
        eksik("onayli_gcode", f"{product.sku} için onaylı G-code yok.")
    elif not os.path.isfile(product.gcode_path):
        eksik("onayli_gcode", f"Onaylı G-code dosyası yerinde değil: {product.gcode_path}")
    elif dosya_ozeti(product.gcode_path) != product.gcode_sha256:
        eksik("gcode_ozeti", "G-code dosyasının SHA-256 özeti kayıttakiyle aynı değil; dosya değişmiş.")
    else:
        gcode_tamam = True
        with open(product.gcode_path, "r", encoding="utf-8", errors="ignore") as f:
            model = gcode_yazici_modeli(f.read())
        if not model or YAZICI_MODELI.lower() not in model.lower():
            eksik("yazici_modeli", f"G-code başlığındaki yazıcı {YAZICI_MODELI} değil: {model or 'yazıcı adı yok'}")
            gcode_tamam = False

    kalemler = list(
        session.exec(select(ProductBOMFilament).where(ProductBOMFilament.product_id == product.id)).all()
    )
    filamentler = {k.filament_id for k in kalemler}
    yuklu_idleri = [fid for fid in machine.yuklu_filamentler_listesi() if fid is not None]
    max_yuva = max(1, int(machine.filament_slot_sayisi or 4))
    if not kalemler:
        eksik("filament", f"{product.sku} reçetesinde filament yok.")
    elif len(filamentler) > max_yuva:
        eksik("filament", f"{product.sku} reçetesinde {len(filamentler)} filament var; makinede {max_yuva} yuva var.")
    elif not yuklu_idleri:
        eksik("filament", "Makinede yüklü filament kayıtlı değil.")
    elif not filamentler.issubset(set(yuklu_idleri)):
        ad = lambda f: f"{f.brand} {f.color_name}" if f else "kayıt yok"  # noqa: E731
        yuklu_adlar = ", ".join(ad(session.get(Filament, fid)) for fid in yuklu_idleri)
        istenen_adlar = ", ".join(ad(session.get(Filament, fid)) for fid in sorted(filamentler))
        eksik("filament", f"Yüklü filamentler ({yuklu_adlar}) reçetedekiyle ({istenen_adlar}) uyuşmuyor.")
    else:
        gereken = sum(k.net_weight_gr * (1.0 + k.waste_rate_percent / 100.0) for k in kalemler) * emir.quantity
        sonuc["gereken_gr"] = round(gereken, 2)
        ilk_yuklu = session.get(Filament, next(iter(filamentler)))
        sonuc["kalan_gr"] = ilk_yuklu.remaining_weight_gr if ilk_yuklu else 0.0
        for fid in sorted(filamentler):
            fid_gereken = sum(
                k.net_weight_gr * (1.0 + k.waste_rate_percent / 100.0) for k in kalemler if k.filament_id == fid
            ) * emir.quantity
            yuklu = session.get(Filament, fid)
            sinir = round(fid_gereken * STOK_PAYI, 2)
            if yuklu and yuklu.remaining_weight_gr < sinir:
                eksik(
                    "filament_stok",
                    f"{yuklu.brand} {yuklu.color_name} kalan {yuklu.remaining_weight_gr:.0f} g; "
                    f"gereken {fid_gereken:.0f} g + %10 = {sinir:.0f} g. Makara değiştir.",
                )

    if gcode_tamam:
        sonuc["gcode_path"] = product.gcode_path
    return {**sonuc, "basilabilir": not nedenler, "nedenler": nedenler}


def makine_karti_hazirla(session: Session) -> Dict[str, Any]:
    """Varsayılan makine kartını (WORKBENCH_MACHINE_NAME) ve bakım parçalarını yoksa açar. Fiyat ve ömür boş kalır; kullanıcı girer."""
    machine = session.exec(select(Machine).where(Machine.brand_model == MAKINE_ADI)).first()
    eklenen: List[str] = []
    if not machine:
        machine = Machine(
            name=MAKINE_ADI,
            brand_model=MAKINE_ADI,
            purchase_price_tl=0.0,
            lifespan_hours=0.0,
            power_consumption_kw=0.0,
            notes="İstasyon kurulumunda açıldı; alış fiyatı, ömür ve güç değerlerini girin.",
        )
        session.add(machine)
        session.commit()
        session.refresh(machine)
        eklenen.append(MAKINE_ADI)
    var_olan = {p.name for p in session.exec(select(MachinePart).where(MachinePart.machine_id == machine.id)).all()}
    for ad in BAKIM_PARCALARI:
        if ad not in var_olan:
            session.add(MachinePart(machine_id=machine.id, name=ad, cost_tl=0.0, lifespan_hours=0.0))
            eklenen.append(ad)
    session.commit()
    return {"status": "success", "machine_id": machine.id, "eklenen": eklenen}
