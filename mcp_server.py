from typing import List, Optional, Dict, Any
from sqlmodel import Session, select
from mcp.server.mcpserver import MCPServer

from database import engine, init_db_and_seed
from models import (
    GlobalSettings,
    SettingConfirmation,
    ShippingRateHistory,
    B2BDiscountTier,
    CategoryItem,
    Machine,
    MachinePart,
    Filament,
    AuxiliaryMaterial,
    MaterialGroup,
    MaterialGroupItem,
    Product,
    ProductBOMFilament,
    ProductBOMMaterial,
    ProductBOMGroup,
    ProductionQueueItem,
    utc_now,
)
from cost_engine import (
    makine_coz,
    hours_minutes_to_decimal,
    calculate_machine_hourly_cost,
    kargo_esikli_fiyat,
)
from main import get_enriched_product, _enrich_material_group
import istasyon_altyapi as istasyon

# Veritabanını hazırla
init_db_and_seed()

mcp = MCPServer("Workbench-3D-ERP")

ZORUNLU_AYARLAR = {
    "electricity_price_per_kwh": "Elektrik birim fiyatı (TL/kWh)",
    "commission_rate_percent": "Mağaza / sanal POS komisyonu (%)",
    "default_vat_rate_percent": "KDV oranı (%)",
    "default_vat_enabled": "Fiyatlara KDV eklensin mi",
    "default_target_margin_percent": "B2C hedef kâr marjı (%)",
    "default_b2b_margin_percent": "B2B hedef kâr marjı (%)",
    "default_shipping_cost": "Standart kargo bedeli (TL)",
    "kargo_esigi_tl": "Kargo eşiği (TL): bu tutardan itibaren kargoyu satıcı öder",
}


def _eksikler(session: Session, kargo_gerekli: bool = True) -> List[str]:
    """Fiyat hesabından önce kullanıcıdan alınması gereken gerçek değerleri listeler.

    kargo_gerekli=False: kargo bedeli çağıran tarafından açıkça verildiyse (ör. mağaza fiyatı kargo hariç,
    kargo_tl=0) desi→kargo baremi gerekmez.
    """
    onayli = {k.key for k in session.exec(select(SettingConfirmation)).all()}
    eksik = [ad for key, ad in ZORUNLU_AYARLAR.items() if key not in onayli]

    machine = session.exec(select(Machine)).first()
    if not machine:
        eksik.append("Makine kaydı")
    else:
        if machine.purchase_price_tl <= 0:
            eksik.append(f"{machine.name}: alış fiyatı (TL)")
        if machine.lifespan_hours <= 0:
            eksik.append(f"{machine.name}: tahmini ömür (saat)")
        if machine.power_consumption_kw <= 0:
            eksik.append(f"{machine.name}: ortalama güç (kW)")

    aktif_kargo = session.exec(
        select(ShippingRateHistory).where(ShippingRateHistory.is_active == True)  # noqa: E712
    ).first()
    if kargo_gerekli and not aktif_kargo:
        eksik.append("Kargo baremi (desi aralığı ve bedeli)")
    if not session.exec(select(Filament)).first():
        eksik.append("En az bir filament alımı")
    return eksik


def _hazir_degil(eksikler: List[str]) -> Dict[str, Any]:
    return {
        "status": "error",
        "message": "Fiyat üretilmedi: eksik değerleri kullanıcıya sor, varsayım yapma.",
        "eksikler": eksikler,
    }


def _desi_kargo(session: Session, desi: float) -> Optional[float]:
    baremler = session.exec(
        select(ShippingRateHistory)
        .where(ShippingRateHistory.is_active == True)  # noqa: E712
        .order_by(ShippingRateHistory.min_desi)
    ).all()
    for barem in baremler:
        if barem.min_desi <= desi <= barem.max_desi:
            return barem.cost_tl
    return None


def _makine_sec(session: Session, machine_id: Optional[int]) -> Any:
    """Makine kimliğini döndürür; makine belirsizse kullanıcıya sorulacak hata sözlüğünü döndürür."""
    try:
        return makine_coz(list(session.exec(select(Machine)).all()), machine_id)
    except ValueError as e:
        return {"status": "error", "message": str(e)}


@mcp.tool()
def hazirlik_durumu() -> Dict[str, Any]:
    """Fiyat hesabı için eksik ayarları listeler. İlk fiyat hesabından önce çağır, eksikleri kullanıcıya tek tek sor."""
    with Session(engine) as session:
        eksikler = _eksikler(session)
        return {"hazir": not eksikler, "eksikler": eksikler}


@mcp.tool()
def ayar_guncelle(
    electricity_price_per_kwh: Optional[float] = None,
    commission_rate_percent: Optional[float] = None,
    esnaf_muafiyeti_enabled: Optional[bool] = None,
    bank_withholding_rate_percent: Optional[float] = None,
    esnaf_yillik_limit_tl: Optional[float] = None,
    default_vat_rate_percent: Optional[float] = None,
    default_vat_enabled: Optional[bool] = None,
    default_target_margin_percent: Optional[float] = None,
    default_b2b_margin_percent: Optional[float] = None,
    default_shipping_cost: Optional[float] = None,
    kargo_esigi_tl: Optional[float] = None,
) -> Dict[str, Any]:
    """Kullanıcının bildirdiği gerçek genel ayarları kaydeder ve onaylı işaretler. Yalnızca kullanıcının söylediği değerleri gönder."""
    degerler = {k: v for k, v in locals().items() if v is not None}
    with Session(engine) as session:
        settings = session.get(GlobalSettings, 1)
        for key, value in degerler.items():
            setattr(settings, key, value)
            if not session.get(SettingConfirmation, key):
                session.add(SettingConfirmation(key=key))
        settings.updated_at = utc_now()
        session.add(settings)
        session.commit()
        return {"status": "success", "guncellenen": list(degerler), "eksikler": _eksikler(session)}


@mcp.tool()
def makine_guncelle(
    machine_id: Optional[int] = None,
    purchase_price_tl: Optional[float] = None,
    lifespan_hours: Optional[float] = None,
    power_consumption_kw: Optional[float] = None,
    total_worked_hours: Optional[float] = None,
    filament_slot_sayisi: Optional[int] = None,
    yuklu_filament_ids: Optional[List[Optional[int]]] = None,
) -> Dict[str, Any]:
    """Makinenin kullanıcıdan alınan alış fiyatı, ömür, güç, çalışma saati, filament yuva sayısı (1-4) ve yüklü filamentlerini günceller."""
    degerler = {
        k: v
        for k, v in {
            "purchase_price_tl": purchase_price_tl,
            "lifespan_hours": lifespan_hours,
            "power_consumption_kw": power_consumption_kw,
            "total_worked_hours": total_worked_hours,
        }.items()
        if v is not None
    }
    with Session(engine) as session:
        machine_id = _makine_sec(session, machine_id)
        if isinstance(machine_id, dict):
            return machine_id
        machine = session.get(Machine, machine_id)
        for key, value in degerler.items():
            setattr(machine, key, value)
        if filament_slot_sayisi is not None:
            machine.filament_slot_sayisi = max(1, min(4, int(filament_slot_sayisi)))
        if yuklu_filament_ids is not None:
            alanlar = ["yuklu_filament_id", "yuklu_filament_2_id", "yuklu_filament_3_id", "yuklu_filament_4_id"]
            for idx, alan in enumerate(alanlar):
                setattr(machine, alan, yuklu_filament_ids[idx] if idx < len(yuklu_filament_ids) else None)
        session.add(machine)
        session.commit()
        session.refresh(machine)
        return {"status": "success", "makine": machine.model_dump(), "eksikler": _eksikler(session)}


@mcp.tool()
def kargo_baremi_ekle(tier_name: str, min_desi: float, max_desi: float, cost_tl: float) -> Dict[str, Any]:
    """Kullanıcının bildirdiği kargo baremini ekler."""
    with Session(engine) as session:
        barem = ShippingRateHistory(
            tier_name=tier_name, min_desi=min_desi, max_desi=max_desi, cost_tl=cost_tl, is_active=True
        )
        session.add(barem)
        session.commit()
        session.refresh(barem)
        return {"status": "success", "barem": barem.model_dump(), "eksikler": _eksikler(session)}


@mcp.tool()
def b2b_iskonto_ekle(
    tier_name: str, min_quantity: int, discount_percent: float, max_quantity: Optional[int] = None
) -> Dict[str, Any]:
    """Kullanıcının bildirdiği B2B toplu alım iskonto baremini ekler."""
    with Session(engine) as session:
        barem = B2BDiscountTier(
            tier_name=tier_name,
            min_quantity=min_quantity,
            max_quantity=max_quantity,
            discount_percent=discount_percent,
        )
        session.add(barem)
        session.commit()
        session.refresh(barem)
        return {"status": "success", "barem": barem.model_dump()}


@mcp.tool()
def kategori_ekle(target_type: str, name: str) -> Dict[str, Any]:
    """Ürün veya malzeme kategorisi ekler. target_type: PRODUCT veya MATERIAL."""
    with Session(engine) as session:
        kategori = CategoryItem(target_type=target_type.upper(), name=name)
        session.add(kategori)
        session.commit()
        session.refresh(kategori)
        return {"status": "success", "kategori": kategori.model_dump()}


@mcp.tool()
def filament_alimi(filament_id: int, alinan_gr: float, price_per_kg_tl: Optional[float] = None) -> Dict[str, Any]:
    """Var olan filamentin yeni alımını stoğa ekler; fiyat verilirse birim fiyatı günceller."""
    if alinan_gr <= 0:
        return {"status": "error", "message": f"Alım miktarı sıfırdan büyük olmalı: {alinan_gr}"}
    with Session(engine) as session:
        filament = session.get(Filament, filament_id)
        if not filament:
            return {"status": "error", "message": f"Filament bulunamadı: {filament_id}"}
        filament.remaining_weight_gr = round(filament.remaining_weight_gr + alinan_gr, 2)
        if price_per_kg_tl is not None:
            filament.price_per_kg_tl = price_per_kg_tl
        session.add(filament)
        session.commit()
        session.refresh(filament)
        return {"status": "success", "filament": filament.model_dump()}


@mcp.tool()
def malzeme_alimi(material_id: int, alinan_miktar: float, unit_cost_tl: Optional[float] = None) -> Dict[str, Any]:
    """Var olan yardımcı malzemenin yeni alımını stoğa ekler; fiyat verilirse birim maliyeti günceller."""
    if alinan_miktar <= 0:
        return {"status": "error", "message": f"Alım miktarı sıfırdan büyük olmalı: {alinan_miktar}"}
    with Session(engine) as session:
        material = session.get(AuxiliaryMaterial, material_id)
        if not material:
            return {"status": "error", "message": f"Malzeme bulunamadı: {material_id}"}
        material.stock_quantity = round(material.stock_quantity + alinan_miktar, 2)
        if unit_cost_tl is not None:
            material.unit_cost_tl = unit_cost_tl
        session.add(material)
        session.commit()
        session.refresh(material)
        return {"status": "success", "malzeme": material.model_dump()}


@mcp.tool()
def urun_numune_guncelle(
    sku: str,
    print_time_hours: int,
    print_time_minutes: int,
    filament_bom_list: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Numune baskısının gerçek süre ve gramını reçeteye yazar, maliyeti yeniden hesaplar.
    - filament_bom_list: [{"filament_id": 1, "net_weight_gr": 120.0, "waste_rate_percent": 5.0}]
    """
    with Session(engine) as session:
        settings = session.get(GlobalSettings, 1)
        product = session.exec(select(Product).where(Product.sku == sku)).first()
        if not product:
            return {"status": "error", "message": f"Ürün bulunamadı: {sku}"}
        product.print_time_hours_int = print_time_hours
        product.print_time_minutes_int = print_time_minutes
        product.print_time_decimal_hours = hours_minutes_to_decimal(print_time_hours, print_time_minutes)
        session.add(product)
        for item in session.exec(
            select(ProductBOMFilament).where(ProductBOMFilament.product_id == product.id)
        ).all():
            session.delete(item)
        for f_item in filament_bom_list:
            session.add(
                ProductBOMFilament(
                    product_id=product.id,
                    filament_id=int(f_item["filament_id"]),
                    net_weight_gr=float(f_item["net_weight_gr"]),
                    waste_rate_percent=float(f_item["waste_rate_percent"]),
                )
            )
        session.commit()
        session.refresh(product)
        return get_enriched_product(session, product, settings)


@mcp.tool()
def uretim_emri_ac(
    sku: str, adet: int = 1, channel: str = "IKAS_B2C", machine_id: Optional[int] = None, notes: Optional[str] = None
) -> Dict[str, Any]:
    """
    Bildirilen sipariş için baskı kuyruğuna emir açar. Stok, baskı tamamlanınca düşülür.
    Makine verilmezse ürün reçetesindeki makine kullanılır.
    """
    with Session(engine) as session:
        product = session.exec(select(Product).where(Product.sku == sku)).first()
        if not product:
            return {"status": "error", "message": f"Ürün bulunamadı: {sku}"}
        machine_id = _makine_sec(session, machine_id if machine_id is not None else product.machine_id)
        if isinstance(machine_id, dict):
            return machine_id
        emir = ProductionQueueItem(
            product_id=product.id, machine_id=machine_id, quantity=adet, channel=channel, notes=notes
        )
        session.add(emir)
        session.commit()
        session.refresh(emir)
        return {"status": "success", "emir": emir.model_dump()}


@mcp.tool()
def uretim_emirleri(status: str = "BEKLIYOR") -> List[Dict[str, Any]]:
    """Baskı kuyruğundaki emirleri listeler. status: BEKLIYOR, ONAY_BEKLIYOR, DILIM_BEKLIYOR, BASILIYOR, HATA, TAMAMLANDI, IPTAL."""
    with Session(engine) as session:
        emirler = session.exec(
            select(ProductionQueueItem).where(ProductionQueueItem.status == status.upper())
        ).all()
        sonuc = []
        for emir in emirler:
            product = session.get(Product, emir.product_id)
            sonuc.append({**emir.model_dump(), "sku": product.sku, "urun": product.name})
        return sonuc


@mcp.tool()
def uretim_emri_tamamla(emir_id: int) -> Dict[str, Any]:
    """Baskısı biten emri tamamlar: filament ve malzeme stoğunu, makine saatini düşer ve üretim kaydı açar."""
    with Session(engine) as session:
        return istasyon.emir_tamamla(session, emir_id)


@mcp.tool()
def uretim_emri_iptal(emir_id: int, neden: str) -> Dict[str, Any]:
    """Baskıya girmemiş emri stok düşmeden iptal eder."""
    with Session(engine) as session:
        return istasyon.emir_iptal(session, emir_id, neden)


@mcp.tool()
def fire_kaydet(
    filament_id: int, gram: float, saat: int, dakika: int, machine_id: Optional[int] = None, notes: Optional[str] = None
) -> Dict[str, Any]:
    """Başarısız baskıyı fire olarak kaydeder; filamenti stoktan, süreyi makineden düşer."""
    from fastapi import HTTPException
    from main import ProductionOrderInput, create_production_order

    with Session(engine) as session:
        try:
            kayit = create_production_order(
                ProductionOrderInput(
                    order_type="WASTE",
                    machine_id=machine_id,
                    waste_hours=saat,
                    waste_minutes=dakika,
                    waste_filament_id=filament_id,
                    waste_grams=gram,
                    notes=notes,
                ),
                session,
            )
        except HTTPException as e:
            return {"status": "error", "message": str(e.detail)}
        return {"status": "success", "fire_kaydi": kayit.model_dump()}


@mcp.tool()
def get_erp_inventory_summary() -> Dict[str, Any]:
    """
    ERP'deki tüm genel parametreleri, makineleri (ve yüklü 4 filament yuvasını),
    filamentleri (firesiz saf birim maliyetli), yardımcı malzemeleri, malzeme/ambalaj gruplarını (Alt BOM)
    ve ürün reçetelerini (BOM) özetler.
    """
    with Session(engine) as session:
        settings = session.get(GlobalSettings, 1)
        filaments = session.exec(select(Filament)).all()
        materials = session.exec(select(AuxiliaryMaterial)).all()
        groups = session.exec(select(MaterialGroup)).all()
        machines = session.exec(select(Machine)).all()
        products = session.exec(select(Product)).all()

        return {
            "settings": settings.model_dump(mode="json") if settings else {},
            "machines": [
                {
                    "id": m.id,
                    "name": m.name,
                    "brand_model": m.brand_model,
                    "power_kw": m.power_consumption_kw,
                    "worked_hours": m.total_worked_hours,
                    "filament_slot_sayisi": m.filament_slot_sayisi,
                    "yuklu_filament_slotlari": [
                        m.yuklu_filament_id,
                        m.yuklu_filament_2_id,
                        m.yuklu_filament_3_id,
                        m.yuklu_filament_4_id,
                    ],
                }
                for m in machines
            ],
            "filaments": [
                {
                    "id": f.id,
                    "code": f.code,
                    "brand": f.brand,
                    "material_type": f.material_type,
                    "color_name": f.color_name,
                    "price_per_kg_tl": f.price_per_kg_tl,
                    "cost_per_gram_tl": round(f.price_per_kg_tl / 1000.0, 4),
                    "remaining_weight_gr": f.remaining_weight_gr,
                }
                for f in filaments
            ],
            "auxiliary_materials": [
                {
                    "id": mat.id,
                    "code": mat.code,
                    "name": mat.name,
                    "category": mat.category,
                    "unit": mat.unit,
                    "unit_cost_tl": mat.unit_cost_tl,
                    "stock_quantity": mat.stock_quantity,
                }
                for mat in materials
            ],
            "material_groups": [_enrich_material_group(session, g) for g in groups],
            "products_count": len(products),
        }


@mcp.tool()
def add_filament(
    code: str,
    brand: str,
    material_type: str,
    color_name: str,
    price_per_kg_tl: float,
    spool_weight_gr: float = 1000.0,
    remaining_weight_gr: float = 1000.0,
    color_hex: str = "#1F2937",
    min_stock_alert_gr: float = 250.0,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """
    ERP envanterine yeni bir filament ekler.
    NOT: Fire oranı filamentin kendisinde tutulmaz, ürün reçetesinde (BOM) tanımlanır!
    """
    with Session(engine) as session:
        fil = Filament(
            code=code,
            brand=brand,
            material_type=material_type,
            color_name=color_name,
            color_hex=color_hex,
            price_per_kg_tl=price_per_kg_tl,
            spool_weight_gr=spool_weight_gr,
            remaining_weight_gr=remaining_weight_gr,
            min_stock_alert_gr=min_stock_alert_gr,
            notes=notes,
        )
        session.add(fil)
        session.commit()
        session.refresh(fil)
        return {
            "status": "success",
            "message": f"{brand} {material_type} ({color_name}) filamenti ERP'ye eklendi.",
            "filament": fil.model_dump(),
            "cost_per_gram_tl": round(fil.price_per_kg_tl / 1000.0, 4),
        }


@mcp.tool()
def add_auxiliary_material(
    code: str,
    name: str,
    unit_cost_tl: float,
    category: str = "ELEKTRIK",
    unit: str = "adet",
    stock_quantity: float = 50.0,
    min_stock_alert: float = 10.0,
    supplier: Optional[str] = None,
) -> Dict[str, Any]:
    """
    ERP envanterine yardımcı malzeme / sarf bileşeni (LED, duy, kablo, adaptör, kargo kutusu, patpat, etiket vb.) ekler.
    NOT: Fire oranı malzemenin kendisinde değil, BOM (ürün reçetesi) içinde tanımlanır.
    Kategori seçenekleri: ELEKTRIK, AMBALAJ, MONTAJ, AKSESUAR, DIGER
    """
    with Session(engine) as session:
        mat = AuxiliaryMaterial(
            code=code,
            name=name,
            category=category.upper(),
            unit=unit,
            unit_cost_tl=unit_cost_tl,
            stock_quantity=stock_quantity,
            min_stock_alert=min_stock_alert,
            supplier=supplier,
        )
        session.add(mat)
        session.commit()
        session.refresh(mat)
        return {
            "status": "success",
            "message": f"'{name}' malzemesi ({unit_cost_tl} TL/{unit}) ERP envanterine eklendi.",
            "material": mat.model_dump(),
        }


@mcp.tool()
def bulk_add_materials(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Yapay zeka ile tek seferde birden fazla yardımcı malzemeyi (kutu, kablo, duy, LED, patpat vb.) ERP'ye girer.
    Her öğe şu alanları içerebilir: code, name, category, unit, unit_cost_tl, stock_quantity, supplier.
    """
    added = []
    with Session(engine) as session:
        for item in items:
            mat = AuxiliaryMaterial(
                code=item.get("code", f"MAT-{len(added)+100}"),
                name=item["name"],
                category=item.get("category", "DIGER").upper(),
                unit=item.get("unit", "adet"),
                unit_cost_tl=float(item["unit_cost_tl"]),
                stock_quantity=float(item.get("stock_quantity", 50.0)),
                min_stock_alert=float(item.get("min_stock_alert", 10.0)),
                supplier=item.get("supplier"),
            )
            session.add(mat)
            session.commit()
            session.refresh(mat)
            added.append(mat.model_dump())
    return {
        "status": "success",
        "added_count": len(added),
        "materials": added,
    }


@mcp.tool()
def malzeme_grubu_kaydet(
    code: str,
    name: str,
    items: List[Dict[str, Any]],
    category: str = "AMBALAJ_GRUBU",
    description: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Alt BOM / Ambalaj Grubu (ör. Ambalaj_Kucuk, Ambalaj_Orta, Ambalaj_Buyuk) oluşturur veya günceller.
    - items: [{"material_id": 1, "quantity": 1.0, "waste_rate_percent": 0.0}] veya material_code ile.
    """
    from main import MaterialGroupCreate, MaterialGroupItemInput, create_or_update_material_group

    with Session(engine) as session:
        code_to_id = {m.code: m.id for m in session.exec(select(AuxiliaryMaterial)).all()}
        parsed_items = []
        for it in items:
            mid = it.get("material_id")
            if mid is None and it.get("material_code"):
                mid = code_to_id.get(str(it["material_code"]))
            if mid is None:
                return {"status": "error", "message": f"Malzeme bulunamadı: {it}"}
            parsed_items.append(
                MaterialGroupItemInput(
                    material_id=int(mid),
                    quantity=float(it.get("quantity", 1.0)),
                    waste_rate_percent=float(it.get("waste_rate_percent", 0.0)),
                )
            )
        sonuc = create_or_update_material_group(
            MaterialGroupCreate(
                code=code,
                name=name,
                category=category,
                description=description,
                items=parsed_items,
            ),
            session,
        )
        return {"status": "success", "group": sonuc}


@mcp.tool()
def create_product_with_bom(
    sku: str,
    name: str,
    print_time_hours: int,
    print_time_minutes: int,
    size_x_mm: float,
    size_y_mm: float,
    size_z_mm: float,
    filament_bom_list: List[Dict[str, Any]],
    material_bom_list: Optional[List[Dict[str, Any]]] = None,
    group_bom_list: Optional[List[Dict[str, Any]]] = None,
    category: str = "B2C_DEKOR",
    variant_name: str = "Standart",
    machine_id: Optional[int] = None,
    target_margin_percent: float = 50.0,
    vat_enabled: bool = False,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Orca Slicer saat ve dakika verisini otomatik Saat + (Dakika/60) ondalıklı saate çevirerek
    yeni bir Ürün Reçetesi (BOM) oluşturur ve maliyet + Başa Baş Noktası + Satış Fiyatını hesaplar.
    - filament_bom_list: [{"filament_id": 1, "net_weight_gr": 120.0, "waste_rate_percent": 5.0}]
    - material_bom_list: [{"material_id": 1, "quantity": 1.0, "waste_rate_percent": 0.0}]
    - group_bom_list: [{"group_id": 1, "quantity": 1.0}] veya [{"group_code": "Ambalaj_Kucuk", "quantity": 1.0}]
    - machine_id: tek makine varsa otomatik gelir, birden fazlaysa kullanıcıya sor.
    """
    with Session(engine) as session:
        eksikler = _eksikler(session)
        if eksikler:
            return _hazir_degil(eksikler)
        machine_id = _makine_sec(session, machine_id)
        if isinstance(machine_id, dict):
            return machine_id
        settings = session.get(GlobalSettings, 1)
        dec_hours = hours_minutes_to_decimal(print_time_hours, print_time_minutes)
        prod = Product(
            sku=sku,
            name=name,
            category=category,
            variant_name=variant_name,
            machine_id=machine_id,
            print_time_hours_int=print_time_hours,
            print_time_minutes_int=print_time_minutes,
            print_time_decimal_hours=dec_hours,
            size_x_mm=size_x_mm,
            size_y_mm=size_y_mm,
            size_z_mm=size_z_mm,
            target_margin_percent=target_margin_percent,
            vat_enabled=vat_enabled,
            vat_rate_percent=settings.default_vat_rate_percent if settings else 20.0,
            notes=notes,
        )
        session.add(prod)
        session.commit()
        session.refresh(prod)

        for f_item in filament_bom_list:
            session.add(
                ProductBOMFilament(
                    product_id=prod.id,
                    filament_id=int(f_item["filament_id"]),
                    net_weight_gr=float(f_item["net_weight_gr"]),
                    waste_rate_percent=float(f_item.get("waste_rate_percent", 5.0)),
                )
            )

        for m_item in material_bom_list or []:
            session.add(
                ProductBOMMaterial(
                    product_id=prod.id,
                    material_id=int(m_item["material_id"]),
                    quantity=float(m_item.get("quantity", 1.0)),
                    waste_rate_percent=float(m_item.get("waste_rate_percent", 0.0)),
                )
            )

        for g_item in group_bom_list or []:
            gid = g_item.get("group_id")
            if gid is None and g_item.get("group_code"):
                grp = session.exec(select(MaterialGroup).where(MaterialGroup.code == str(g_item["group_code"]))).first()
                if grp:
                    gid = grp.id
            if gid is not None:
                session.add(
                    ProductBOMGroup(
                        product_id=prod.id,
                        group_id=int(gid),
                        quantity=float(g_item.get("quantity", 1.0)),
                    )
                )

        session.commit()
        session.refresh(prod)
        return get_enriched_product(session, prod, settings)


@mcp.tool()
def calculate_b2b_or_custom_quote(
    quantity: int,
    print_hours_per_unit: int,
    print_minutes_per_unit: int,
    net_grams_per_unit: float,
    bom_waste_percent: float = 5.0,
    extra_material_cost_per_unit_tl: float = 0.0,
    shipping_total_tl: Optional[float] = None,
    desi: Optional[float] = None,
    target_margin_percent: Optional[float] = None,
    discount_percent: float = 0.0,
    vat_enabled: Optional[bool] = None,
    machine_id: Optional[int] = None,
    filament_id: int = 1,
) -> Dict[str, Any]:
    """
    Özel kurumsal (B2B) veya B2C talepler için Başa Baş Noktası (Break-Even),
    Hedef Kâr Marjı, İskonto (%) ve KDV Aç/Kapat seçeneğiyle detaylı teklif hesaplar.
    Kargo için desi veya shipping_total_tl verilmelidir; marj ve KDV verilmezse ERP ayarları kullanılır.
    """
    with Session(engine) as session:
        eksikler = _eksikler(session, kargo_gerekli=shipping_total_tl is None)
        if eksikler:
            return _hazir_degil(eksikler)
        settings = session.get(GlobalSettings, 1)
        machine_id = _makine_sec(session, machine_id)
        if isinstance(machine_id, dict):
            return machine_id
        machine = session.get(Machine, machine_id)
        filament = session.get(Filament, filament_id)
        if not filament:
            return {"status": "error", "message": f"Filament bulunamadı: {filament_id}"}
        if shipping_total_tl is None:
            shipping_total_tl = _desi_kargo(session, desi) if desi is not None else None
        if shipping_total_tl is None:
            return {"status": "error", "message": "Kargo hesaplanamadı: geçerli desi veya shipping_total_tl ver."}
        if target_margin_percent is None:
            target_margin_percent = settings.default_target_margin_percent
        if vat_enabled is None:
            vat_enabled = settings.default_vat_enabled

        dec_hours = hours_minutes_to_decimal(print_hours_per_unit, print_minutes_per_unit)
        parts = session.exec(select(MachinePart).where(MachinePart.machine_id == machine.id)).all()
        m_hourly = calculate_machine_hourly_cost(
            machine, list(parts), settings.electricity_price_per_kwh
        )["total_hourly_cost_tl"]
        cost_per_gr = filament.price_per_kg_tl / 1000.0

        eff_grams = net_grams_per_unit * (1.0 + bom_waste_percent / 100.0)
        unit_prod = (
            (dec_hours * m_hourly)
            + (eff_grams * cost_per_gr)
            + extra_material_cost_per_unit_tl
        )
        total_prod = unit_prod * quantity

        pricing = kargo_esikli_fiyat(
            esik_tl=settings.kargo_esigi_tl,
            kargo_tl=shipping_total_tl,
            production_cost_tl=total_prod,
            commission_rate_percent=settings.commission_rate_percent,
            target_margin_percent=target_margin_percent,
            vat_enabled=vat_enabled,
            vat_rate_percent=settings.default_vat_rate_percent,
            discount_percent=discount_percent,
            bank_withholding_rate_percent=(
                settings.bank_withholding_rate_percent if settings.esnaf_muafiyeti_enabled else 0.0
            ),
        )
        return {
            "quantity": quantity,
            "decimal_hours_per_unit": dec_hours,
            "unit_production_cost_tl": round(unit_prod, 2),
            "total_production_cost_tl": round(total_prod, 2),
            "pricing": pricing,
        }



# ---------------------------------------------------------------------------
# Ürün Fabrikası araçları (Orca dilimleme + G-code maliyeti + ürün listesi)
# ---------------------------------------------------------------------------
import os as _os
import glob as _glob
import shutil as _shutil
import subprocess as _subprocess

import workbench_config as _cfg

_ORCA_PROFIL = _cfg.ORCA_PROFILES_DIR
_ORCA_BIN = _cfg.ORCA_BIN or _shutil.which("orcaslicer") or _os.path.expanduser("~/.local/bin/orcaslicer")
_MAKINE_JSON = _cfg.ORCA_MACHINE_PROFILE + ".json"


def _gcode_oku(yol: str) -> Dict[str, Any]:
    from slicer_parser import parse_gcode_content
    with open(yol, "r", encoding="utf-8", errors="ignore") as f:
        r = parse_gcode_content(f.read())
    r.pop("thumbnail_base64", None)
    r.pop("thumbnail", None)
    return r


@mcp.tool()
def orca_profilleri() -> Dict[str, Any]:
    """Ürün Fabrikası için hazır Orca proses ve filament profillerini listeler."""
    def ad(d):
        return sorted(_os.path.basename(x)[:-5] for x in _glob.glob(_os.path.join(_ORCA_PROFIL, d, "*.json")))
    return {"orca": _ORCA_BIN, "proses": ad("process"), "filament": ad("filament")}


@mcp.tool()
def dilimle(
    stl_yolu: str,
    profil: str = _cfg.ORCA_DEFAULT_PROCESS,
    filament: str = _cfg.ORCA_DEFAULT_FILAMENT,
    cikti_klasoru: Optional[str] = None,
) -> Dict[str, Any]:
    """
    STL'yi Orca Slicer komut satırıyla (WORKBENCH_ORCA_MACHINE profili) dilimler, G-code üretir ve
    süre / gram / boyut bilgisini döndürür. Profiller: orca_profilleri() ile görülebilir.
    Profil seçimi: Dekor (2 duvar %10), Standart (3 duvar %15), Dayanikli (4 duvar %25),
    Hizli 0.24 (büyük düz parçalar). Hepsinde üst yüzey ütüleme açık.
    """
    stl = _os.path.abspath(_os.path.expanduser(stl_yolu))
    if not _os.path.exists(stl):
        return {"status": "error", "message": f"STL bulunamadı: {stl}"}
    proses = _os.path.join(_ORCA_PROFIL, "process", profil + ".json")
    fil = _os.path.join(_ORCA_PROFIL, "filament", filament + ".json")
    makine = _os.path.join(_ORCA_PROFIL, "machine", _MAKINE_JSON)
    for x in (proses, fil, makine):
        if not _os.path.exists(x):
            return {"status": "error", "message": f"Profil yok: {x}"}
    out = cikti_klasoru or _os.path.join(_os.path.dirname(stl), "gcode", profil.replace(" ", "_"))
    _os.makedirs(out, exist_ok=True)
    cmd = [
        _ORCA_BIN, "--arrange", "1", "--slice", "0",
        "--load-settings", f"{makine};{proses}",
        "--load-filaments", fil,
        "--outputdir", out, stl,
    ]
    try:
        p = _subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except Exception as e:
        return {"status": "error", "message": f"Orca çalıştırılamadı: {e}", "komut": cmd}
    gcodes = sorted(_glob.glob(_os.path.join(out, "*.gcode")), key=_os.path.getmtime)
    if not gcodes:
        return {"status": "error", "message": "G-code oluşmadı", "komut": cmd,
                "stdout": p.stdout[-1500:], "stderr": p.stderr[-1500:]}
    hedef = _os.path.join(out, _os.path.splitext(_os.path.basename(stl))[0] + ".gcode")
    if gcodes[-1] != hedef:
        _shutil.move(gcodes[-1], hedef)
    return {"status": "success", "gcode": hedef, "profil": profil, "filament": filament,
            "ozet": _gcode_oku(hedef)}


@mcp.tool()
def gcode_maliyet(
    gcode_yolu: str,
    fire_yuzde: float,
    ek_malzeme_tl: float,
    filament_id: int = 1,
    machine_id: Optional[int] = None,
    desi: Optional[float] = None,
    kargo_tl: Optional[float] = None,
    hedef_marj_yuzde: Optional[float] = None,
    adet: int = 1,
) -> Dict[str, Any]:
    """
    G-code'dan süre ve gramı okuyup ERP'nin makine saat ücreti, filament, fire, komisyon,
    kargo ve marj ayarlarıyla birim maliyet ve önerilen satış fiyatını hesaplar (kayıt açmaz).
    - fire_yuzde ve ek_malzeme_tl (montaj + ambalaj) kullanıcının BOM cevabından gelir, varsayılanı yoktur.
    - Kargo için desi veya kargo_tl gerekir; KDV ve marj ERP ayarlarından gelir.
    """
    g = _gcode_oku(_os.path.expanduser(gcode_yolu))
    q = calculate_b2b_or_custom_quote(
        quantity=adet,
        print_hours_per_unit=int(g.get("hours", 0)),
        print_minutes_per_unit=int(g.get("minutes", 0)),
        net_grams_per_unit=float(g.get("filament_grams", 0.0)),
        bom_waste_percent=fire_yuzde,
        extra_material_cost_per_unit_tl=ek_malzeme_tl,
        shipping_total_tl=kargo_tl,
        desi=desi,
        target_margin_percent=hedef_marj_yuzde,
        machine_id=machine_id,
        filament_id=filament_id,
    )
    if q.get("status") == "error":
        return q
    return {"gcode_ozet": g, "maliyet": q}


@mcp.tool()
def urun_listesi() -> List[Dict[str, Any]]:
    """ERP'deki ürün reçetelerini maliyet ve fiyat bilgileriyle listeler."""
    from main import list_products
    with Session(engine) as session:
        return list_products(session)


# ---------------------------------------------------------------------------
# İstasyon araçları (onaylı G-code, baskı kuyruğu, gözetimsiz baskı koşulları)
# Kurallar istasyon_altyapi.py içindedir; buradakiler yalnızca oturum açar.
# ---------------------------------------------------------------------------
@mcp.tool()
def urun_gcode_onayla(sku: str, gcode_yolu: str) -> Dict[str, Any]:
    """
    Kullanıcının denetlediği G-code'u ürünün onaylı G-code'u yapar: dosyayı depoya değişmez kopyalar,
    SHA-256 özetini alır, süre ve gramı ürün kartına yazar. Yalnızca kullanıcı onayladıktan sonra çağır.
    """
    with Session(engine) as session:
        return istasyon.urun_gcode_onayla(session, sku, gcode_yolu)


@mcp.tool()
def emir_baskiya_al(emir_id: int) -> Dict[str, Any]:
    """Bekleyen emri BASILIYOR yapar ve başlama zamanını yazar. Makinede başka baskı varsa reddeder."""
    with Session(engine) as session:
        return istasyon.emir_baskiya_al(session, emir_id)


@mcp.tool()
def emir_hata(emir_id: int, gram: float, saat: int, dakika: int) -> Dict[str, Any]:
    """Başarısız baskıyı fire olarak kaydeder (harcanan gram ve süre) ve emri kuyruğa geri döndürür."""
    with Session(engine) as session:
        return istasyon.emir_hata(session, emir_id, gram, saat, dakika)


@mcp.tool()
def basilabilir_emir(machine_id: int, tabla_bos: bool, yazici_bosta: Optional[bool] = None) -> Dict[str, Any]:
    """
    Gözetimsiz baskı koşullarını denetler; hiçbir şey değiştirmez. Sıradaki emir için eksik her koşulu
    adıyla ve açıklamasıyla döndürür. yazici_bosta, yazıcının o anki durumudur (boşta ve hatasız).
    """
    with Session(engine) as session:
        return istasyon.basilabilir_emir(session, machine_id, tabla_bos, yazici_bosta)


@mcp.tool()
def ozel_emir_ac(
    sku: str, metin: str, adet: int = 1, channel: str = "IKAS_B2C",
    machine_id: Optional[int] = None, notes: Optional[str] = None,
) -> Dict[str, Any]:
    """Kişiye özel ürün için emir açar (müşterinin metniyle). Emir müşteri onayına kadar ONAY_BEKLIYOR kalır."""
    with Session(engine) as session:
        return istasyon.ozel_emir_ac(session, sku, metin, adet, channel, machine_id, notes)


@mcp.tool()
def musteri_onayi(emir_id: int) -> Dict[str, Any]:
    """Müşteri önizlemeyi onayladı: emri DILIM_BEKLIYOR yapar."""
    with Session(engine) as session:
        return istasyon.musteri_onayi(session, emir_id)


@mcp.tool()
def emir_gcode_bagla(emir_id: int, gcode_yolu: str) -> Dict[str, Any]:
    """Kullanıcının dilimlediği G-code'u kişiye özel emre bağlar ve emri baskı kuyruğuna (BEKLIYOR) alır."""
    with Session(engine) as session:
        return istasyon.emir_gcode_bagla(session, emir_id, gcode_yolu)


if __name__ == "__main__":
    mcp.run()
