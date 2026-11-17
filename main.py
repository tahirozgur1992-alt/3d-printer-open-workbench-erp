import io
import csv
from contextlib import asynccontextmanager
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from database import init_db_and_seed, get_session
from models import (
    utc_now,
    GlobalSettings,
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
    ProductionOrder,
    ProductionQueueItem,
    QuoteRecord,
    PurchaseRecord,
)
from cost_engine import (
    makine_coz,
    hours_minutes_to_decimal,
    calculate_machine_hourly_cost,
    calculate_pricing_from_cost,
    kargo_esikli_fiyat,
    calculate_product_bom_cost,
)
from slicer_parser import parse_3d_or_slicer_file, parse_duration_string
import istasyon_altyapi as istasyon


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db_and_seed()
    yield


app = FastAPI(
    title="3D Printer Open Workbench ERP — Maliyet / Üretim Yönetim Sistemi",
    description="B2C/B2B 3D Baskı Üretim, BOM, Makine Amortismanı, Orca Slicer ve 3 Modüllü AI Veri Motoru",
    version="1.1.0",
    lifespan=lifespan,
)


# --- Pydantic İstek Şemaları ---
class SettingsUpdate(BaseModel):
    brand_name: str
    electricity_price_per_kwh: float
    commission_rate_percent: float
    esnaf_muafiyeti_enabled: Optional[bool] = None
    bank_withholding_rate_percent: Optional[float] = None
    esnaf_yillik_limit_tl: Optional[float] = None
    default_vat_rate_percent: float
    default_vat_enabled: bool
    default_target_margin_percent: float
    default_b2b_margin_percent: float
    default_shipping_cost: float
    kargo_esigi_tl: Optional[float] = None


class ShippingRateCreate(BaseModel):
    tier_name: str
    min_desi: float = 0.0
    max_desi: float = 5.0
    cost_tl: float
    notes: Optional[str] = None


class B2BDiscountTierCreate(BaseModel):
    tier_name: str
    min_quantity: int
    max_quantity: Optional[int] = None
    discount_percent: float


class CategoryCreate(BaseModel):
    target_type: str = "PRODUCT"  # PRODUCT veya MATERIAL
    name: str


class MachineCreate(BaseModel):
    name: str
    brand_model: str
    purchase_price_tl: float
    lifespan_hours: float
    power_consumption_kw: float
    total_worked_hours: float = 0.0
    filament_slot_sayisi: int = 4
    yuklu_filament_id: Optional[int] = None
    yuklu_filament_2_id: Optional[int] = None
    yuklu_filament_3_id: Optional[int] = None
    yuklu_filament_4_id: Optional[int] = None
    notes: Optional[str] = None


class YukluFilamentInput(BaseModel):
    slot: int = 1
    filament_id: Optional[int] = None
    filament_ids: Optional[List[Optional[int]]] = None
    filament_slot_sayisi: Optional[int] = None


class GcodeYoluInput(BaseModel):
    gcode_yolu: str


class EmirCreate(BaseModel):
    product_id: int
    quantity: int = 1
    machine_id: Optional[int] = None
    channel: str = "IKAS_B2C"
    ozel_metin: Optional[str] = None
    notes: Optional[str] = None


class EmirHataInput(BaseModel):
    gram: float = 0.0
    saat: int = 0
    dakika: int = 0


class EmirIptalInput(BaseModel):
    neden: str = "Kullanıcı iptal etti"


class MachinePartCreate(BaseModel):
    machine_id: int
    name: str
    cost_tl: float
    lifespan_hours: float
    used_hours: float = 0.0
    warning_threshold_percent: float = 85.0


class FilamentCreate(BaseModel):
    code: str
    brand: str
    material_type: str
    color_name: str
    color_hex: str = "#1F2937"
    price_per_kg_tl: float
    spool_weight_gr: float = 1000.0
    remaining_weight_gr: float = 1000.0
    min_stock_alert_gr: float = 250.0
    notes: Optional[str] = None


class AlimGirdisi(BaseModel):
    """Var olan kaleme alım. Filament: miktar gram, birim_fiyat TL/kg. Malzeme: miktar kendi biriminde,
    birim_fiyat TL/birim. Fiyatlar KDV dahil (esnaf muafiyeti: KDV indirilemez, maliyete girer)."""
    miktar: float
    birim_fiyat_tl: Optional[float] = None
    tedarikci: Optional[str] = None
    fatura_no: Optional[str] = None
    fatura_tarihi: Optional[str] = None
    kaynak: Optional[str] = None
    yeni_kalem: bool = False


def agirlikli_fiyat(eski_stok: float, eski_fiyat: float, yeni_miktar: float, yeni_fiyat: Optional[float]) -> float:
    """Hareketli ağırlıklı ortalama maliyet: kalan stok eski fiyatla, yeni alım yeni fiyatla."""
    if yeni_fiyat is None:
        return eski_fiyat
    eski_stok = max(0.0, eski_stok)
    toplam = eski_stok + yeni_miktar
    if toplam <= 0 or eski_stok == 0:
        return round(yeni_fiyat, 4)
    return round((eski_stok * eski_fiyat + yeni_miktar * yeni_fiyat) / toplam, 4)


class AuxiliaryMaterialCreate(BaseModel):
    code: str
    name: str
    category: str = "ELEKTRİK / LED / KABLO"
    unit: str = "adet"
    unit_cost_tl: float
    stock_quantity: float = 50.0
    min_stock_alert: float = 10.0
    supplier: Optional[str] = None


class BOMFilamentInput(BaseModel):
    filament_id: int
    net_weight_gr: float
    waste_rate_percent: float = 5.0


class BOMMaterialInput(BaseModel):
    material_id: int
    quantity: float = 1.0
    waste_rate_percent: float = 0.0


class MaterialGroupItemInput(BaseModel):
    material_id: int
    quantity: float = 1.0
    waste_rate_percent: float = 0.0


class MaterialGroupCreate(BaseModel):
    code: str
    name: str
    category: str = "AMBALAJ_GRUBU"
    description: Optional[str] = None
    items: List[MaterialGroupItemInput] = []


class BOMGroupInput(BaseModel):
    group_id: int
    quantity: float = 1.0


class ProductCreateUpdate(BaseModel):
    sku: str
    name: str
    category: str = "B2C - Dekoratif Vazo & Obje"
    variant_name: Optional[str] = "Standart"
    machine_id: Optional[int] = None
    source_file_name: Optional[str] = None
    print_time_hours_int: int = 0
    print_time_minutes_int: int = 0
    size_x_mm: float = 0.0
    size_y_mm: float = 0.0
    size_z_mm: float = 0.0
    layer_height_mm: float = 0.20
    infill_percent: float = 15.0
    shipping_tier_id: Optional[int] = None
    custom_shipping_cost_tl: Optional[float] = None
    target_margin_percent: float = 50.0
    b2b_margin_percent: float = 35.0
    vat_enabled: bool = False
    vat_rate_percent: float = 20.0
    manual_sale_price_tl: Optional[float] = None
    thumbnail_base64: Optional[str] = None
    notes: Optional[str] = None
    urun_tipi: str = "STANDART"
    stl_path: Optional[str] = None
    gozetimsiz_baski: bool = False
    filaments: List[BOMFilamentInput] = []
    materials: List[BOMMaterialInput] = []
    groups: List[BOMGroupInput] = []


class QuoteSimulationInput(BaseModel):
    customer_name: str = "Kurumsal Müşteri"
    project_title: str = "Özel 3D Baskı Projesi"
    quantity: int = 10
    machine_id: Optional[int] = None
    print_hours_per_unit: int = 2
    print_minutes_per_unit: int = 30
    filament_id: Optional[int] = None
    net_grams_per_unit: float = 85.0
    bom_waste_percent: float = 5.0
    extra_material_cost_per_unit_tl: float = 0.0
    design_setup_fee_tl: float = 0.0
    shipping_total_tl: float = 85.0
    target_margin_percent: float = 45.0
    discount_percent: float = 0.0
    vat_enabled: bool = False
    vat_rate_percent: float = 20.0
    save_quote: bool = False


class ProductionOrderInput(BaseModel):
    order_type: str = "PRODUCTION"  # PRODUCTION veya WASTE (Fire)
    channel: str = "IKAS_B2C"
    product_id: Optional[int] = None
    machine_id: Optional[int] = None
    quantity: int = 1
    vat_enabled: bool = False
    custom_shipping_cost_tl: Optional[float] = None
    custom_unit_sale_price_tl: Optional[float] = None
    waste_hours: int = 0
    waste_minutes: int = 0
    waste_filament_id: Optional[int] = None
    waste_grams: float = 0.0
    notes: Optional[str] = None


def _load_groups_map(session: Session) -> Dict[int, MaterialGroup]:
    groups_map = {g.id: g for g in session.exec(select(MaterialGroup)).all()}
    for g in groups_map.values():
        _ = g.items
    return groups_map


# --- Yardımcı Fonksiyon (N+1 Sorgusuz Önbellekli Zenginleştirici) ---
def get_enriched_product(
    session: Session,
    product: Product,
    settings: GlobalSettings,
    filaments_map: Optional[Dict[int, Filament]] = None,
    materials_map: Optional[Dict[int, AuxiliaryMaterial]] = None,
    b2b_tiers: Optional[List[B2BDiscountTier]] = None,
    groups_map: Optional[Dict[int, MaterialGroup]] = None,
) -> Dict[str, Any]:
    machine = session.get(Machine, product.machine_id) if product.machine_id else None
    parts = (
        session.exec(select(MachinePart).where(MachinePart.machine_id == machine.id)).all()
        if machine
        else []
    )
    if filaments_map is None:
        filaments_map = {f.id: f for f in session.exec(select(Filament)).all()}
    if materials_map is None:
        materials_map = {m.id: m for m in session.exec(select(AuxiliaryMaterial)).all()}
    if groups_map is None:
        groups_map = _load_groups_map(session)
    if b2b_tiers is None:
        b2b_tiers = list(
            session.exec(select(B2BDiscountTier).order_by(B2BDiscountTier.min_quantity.asc())).all()
        )

    shipping_cost = settings.default_shipping_cost
    if product.shipping_tier_id:
        tier = session.get(ShippingRateHistory, product.shipping_tier_id)
        if tier:
            shipping_cost = tier.cost_tl

    calc = calculate_product_bom_cost(
        product=product,
        settings=settings,
        machine=machine,
        machine_parts=list(parts),
        filaments_map=filaments_map,
        materials_map=materials_map,
        shipping_cost_tl=shipping_cost,
        groups_map=groups_map,
    )

    b2b_tiers_breakdown = []
    prod_cost = calc["production_cost_tl"]
    eff_ship = calc["kargo_bedeli_tl"]
    eff_stopaj = settings.bank_withholding_rate_percent if settings.esnaf_muafiyeti_enabled else 0.0

    for bt in b2b_tiers:
        rep_qty = max(1, bt.min_quantity)
        unit_ship_in_bulk = eff_ship / rep_qty
        tier_pricing = calculate_pricing_from_cost(
            production_cost_tl=prod_cost,
            shipping_cost_tl=unit_ship_in_bulk,
            commission_rate_percent=settings.commission_rate_percent,
            target_margin_percent=product.target_margin_percent,
            vat_enabled=product.vat_enabled,
            vat_rate_percent=product.vat_rate_percent,
            discount_percent=bt.discount_percent,
            bank_withholding_rate_percent=eff_stopaj,
        )
        b2b_tiers_breakdown.append(
            {
                "tier_id": bt.id,
                "tier_name": bt.tier_name,
                "min_quantity": bt.min_quantity,
                "max_quantity": bt.max_quantity,
                "discount_percent": bt.discount_percent,
                "unit_break_even_tl": tier_pricing["break_even_gross_tl"],
                "unit_sale_price_tl": tier_pricing["final_sale_price_gross_tl"],
                "unit_net_profit_tl": tier_pricing["net_profit_tl"],
                "is_below_break_even": tier_pricing["is_below_break_even"],
            }
        )

    calc["b2b_tiers_breakdown"] = b2b_tiers_breakdown
    calc["thumbnail_base64"] = product.thumbnail_base64
    calc["source_file_name"] = product.source_file_name
    calc["layer_height_mm"] = product.layer_height_mm
    calc["infill_percent"] = product.infill_percent
    calc["notes"] = product.notes
    calc["urun_tipi"] = product.urun_tipi
    calc["stl_path"] = product.stl_path
    calc["gozetimsiz_baski"] = product.gozetimsiz_baski
    calc["gcode_path"] = product.gcode_path
    calc["gcode_sha256"] = product.gcode_sha256
    calc["gcode_onay_tarihi"] = product.gcode_onay_tarihi.isoformat() if product.gcode_onay_tarihi else None
    calc["vat_enabled"] = product.vat_enabled
    calc["vat_rate_percent"] = product.vat_rate_percent
    calc["target_margin_percent"] = product.target_margin_percent
    calc["b2b_margin_percent"] = product.b2b_margin_percent
    calc["shipping_tier_id"] = product.shipping_tier_id
    calc["custom_shipping_cost_tl"] = product.custom_shipping_cost_tl
    calc["manual_sale_price_tl"] = product.manual_sale_price_tl
    return calc


# --- 1. GENEL ÖZET PANELİ (DASHBOARD) & MERKEZİ PARAMETRELER ---
@app.get("/api/dashboard")
def get_dashboard(session: Session = Depends(get_session)):
    settings = session.get(GlobalSettings, 1)
    machines = session.exec(select(Machine)).all()
    filaments = session.exec(select(Filament)).all()
    materials = session.exec(select(AuxiliaryMaterial)).all()
    products = session.exec(select(Product).order_by(Product.created_at.desc())).all()
    orders = session.exec(select(ProductionOrder).order_by(ProductionOrder.created_at.desc())).all()
    quotes = session.exec(select(QuoteRecord).order_by(QuoteRecord.created_at.desc())).all()

    maintenance_warnings = []
    machine_cards = []
    for m in machines:
        parts = session.exec(select(MachinePart).where(MachinePart.machine_id == m.id)).all()
        m_cost = calculate_machine_hourly_cost(m, list(parts), settings.electricity_price_per_kwh)
        machine_cards.append(m_cost)
        maintenance_warnings.extend(m_cost["maintenance_warnings"])

    low_filaments = [
        {
            "id": f.id,
            "name": f"{f.brand} {f.material_type} - {f.color_name}",
            "remaining_gr": round(f.remaining_weight_gr, 1),
            "min_alert_gr": f.min_stock_alert_gr,
        }
        for f in filaments
        if f.remaining_weight_gr <= f.min_stock_alert_gr
    ]

    low_materials = [
        {
            "id": mat.id,
            "name": mat.name,
            "stock": mat.stock_quantity,
            "unit": mat.unit,
            "min_alert": mat.min_stock_alert,
        }
        for mat in materials
        if mat.stock_quantity <= mat.min_stock_alert
    ]

    total_filament_stock_gr = sum(f.remaining_weight_gr for f in filaments)
    total_filament_value_tl = sum(
        (f.remaining_weight_gr / 1000.0) * f.price_per_kg_tl for f in filaments
    )
    total_material_value_tl = sum(m.stock_quantity * m.unit_cost_tl for m in materials)
    total_inventory_value_tl = total_filament_value_tl + total_material_value_tl

    total_revenue = sum(o.total_revenue_tl for o in orders if o.order_type == "PRODUCTION")
    total_profit = sum(o.total_net_profit_tl for o in orders)
    total_print_hours = sum(o.total_print_hours_decimal for o in orders)

    current_year = utc_now().year
    prod_orders = [o for o in orders if o.order_type == "PRODUCTION"]
    ytd_gross_revenue = sum(
        o.total_revenue_tl
        for o in prod_orders
        if o.created_at and o.created_at.year == current_year
    )
    total_bank_withholding = sum(o.bank_withholding_amount_tl for o in prod_orders)
    total_bank_net_payout = sum(o.bank_net_payout_tl for o in prod_orders)
    annual_limit = settings.esnaf_yillik_limit_tl or 1900000.0
    limit_usage_pct = (ytd_gross_revenue / annual_limit * 100.0) if annual_limit > 0 else 0.0
    if settings.esnaf_muafiyeti_enabled and limit_usage_pct >= 80.0:
        maintenance_warnings.append(
            f"Esnaf Muafiyeti Yıllık Hasılat Sınırı %{limit_usage_pct:.1f} doldu "
            f"({ytd_gross_revenue:.0f} / {annual_limit:.0f} TL)!"
        )

    filaments_map = {f.id: f for f in filaments}
    materials_map = {m.id: m for m in materials}
    groups_map = _load_groups_map(session)
    b2b_tiers = list(
        session.exec(select(B2BDiscountTier).order_by(B2BDiscountTier.min_quantity.asc())).all()
    )

    product_summaries = []
    for p in products:
        enr = get_enriched_product(session, p, settings, filaments_map, materials_map, b2b_tiers, groups_map)
        product_summaries.append(
            {
                "product_id": p.id,
                "sku": p.sku,
                "name": p.name,
                "category": p.category,
                "decimal_hours": enr["print_time"]["decimal_hours"],
                "formatted_time": enr["print_time"]["formatted"],
                "production_cost_tl": enr["production_cost_tl"],
                "break_even_tl": enr["b2c_pricing"]["break_even_gross_tl"],
                "b2c_sale_price_tl": enr["b2c_pricing"]["final_sale_price_gross_tl"],
                "b2c_net_profit_tl": enr["b2c_pricing"]["net_profit_tl"],
                "b2b_10plus_price_tl": enr["b2b_unit_pricing_10plus"]["final_sale_price_gross_tl"],
                "vat_enabled": p.vat_enabled,
            }
        )

    return {
        "settings": settings,
        "counts": {
            "machines": len(machines),
            "filaments": len(filaments),
            "materials": len(materials),
            "products": len(products),
            "orders": len(orders),
            "quotes": len(quotes),
        },
        "inventory_summary": {
            "total_filament_stock_kg": round(total_filament_stock_gr / 1000.0, 2),
            "total_filament_value_tl": round(total_filament_value_tl, 2),
            "total_material_value_tl": round(total_material_value_tl, 2),
            "total_inventory_value_tl": round(total_inventory_value_tl, 2),
        },
        "financial_summary": {
            "total_revenue_tl": round(total_revenue, 2),
            "total_net_profit_tl": round(total_profit, 2),
            "total_print_hours_decimal": round(total_print_hours, 2),
        },
        "esnaf_muafiyeti_summary": {
            "enabled": settings.esnaf_muafiyeti_enabled,
            "stopaj_rate_percent": round(
                settings.bank_withholding_rate_percent if settings.esnaf_muafiyeti_enabled else 0.0, 2
            ),
            "annual_limit_tl": round(annual_limit, 2),
            "current_year": current_year,
            "ytd_gross_revenue_tl": round(ytd_gross_revenue, 2),
            "remaining_limit_tl": round(max(0.0, annual_limit - ytd_gross_revenue), 2),
            "limit_usage_percent": round(limit_usage_pct, 2),
            "total_bank_withholding_tl": round(total_bank_withholding, 2),
            "total_bank_net_payout_tl": round(total_bank_net_payout, 2),
        },
        "maintenance_warnings": maintenance_warnings,
        "low_filaments": low_filaments,
        "low_materials": low_materials,
        "machine_cards": machine_cards,
        "product_summaries": product_summaries,
        "recent_orders": orders[:5],
        "recent_quotes": quotes[:5],
    }


@app.get("/api/settings")
def read_settings(session: Session = Depends(get_session)):
    settings = session.get(GlobalSettings, 1)
    shipping_rates = session.exec(
        select(ShippingRateHistory).order_by(ShippingRateHistory.effective_from.desc())
    ).all()
    b2b_tiers = session.exec(
        select(B2BDiscountTier).order_by(B2BDiscountTier.min_quantity.asc())
    ).all()
    categories = session.exec(
        select(CategoryItem).order_by(CategoryItem.target_type, CategoryItem.name)
    ).all()
    return {
        "settings": settings,
        "shipping_rates": shipping_rates,
        "b2b_tiers": b2b_tiers,
        "categories": categories,
    }


@app.post("/api/categories")
def create_category(payload: CategoryCreate, session: Session = Depends(get_session)):
    clean_name = payload.name.strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="Kategori adı boş olamaz")
    t_type = payload.target_type.upper()
    existing = session.exec(
        select(CategoryItem).where(
            CategoryItem.target_type == t_type,
            CategoryItem.name == clean_name,
        )
    ).first()
    if existing:
        return existing

    cat = CategoryItem(
        target_type=t_type,
        name=clean_name,
        created_at=utc_now(),
    )
    session.add(cat)
    session.commit()
    session.refresh(cat)
    return cat


@app.delete("/api/categories/{cat_id}")
def delete_category(cat_id: int, session: Session = Depends(get_session)):
    cat = session.get(CategoryItem, cat_id)
    if not cat:
        raise HTTPException(status_code=404, detail="Kategori bulunamadı")
    session.delete(cat)
    session.commit()
    return {"ok": True}


@app.put("/api/settings")
def update_settings(payload: SettingsUpdate, session: Session = Depends(get_session)):
    settings = session.get(GlobalSettings, 1)
    for k, v in payload.model_dump(exclude_none=True).items():
        setattr(settings, k, v)
    settings.updated_at = utc_now()
    session.add(settings)
    session.commit()
    session.refresh(settings)
    return settings


@app.post("/api/shipping-rates")
def add_or_update_shipping_rate(payload: ShippingRateCreate, session: Session = Depends(get_session)):
    """Yeni kargo baremi ekler ve eklenen tarihten sonraki siparişlerde geçerli olur."""
    rate = ShippingRateHistory(
        tier_name=payload.tier_name,
        min_desi=payload.min_desi,
        max_desi=payload.max_desi,
        cost_tl=payload.cost_tl,
        is_active=True,
        effective_from=utc_now(),
        notes=payload.notes,
    )
    session.add(rate)
    session.commit()
    session.refresh(rate)
    return rate


@app.delete("/api/shipping-rates/{rate_id}")
def delete_shipping_rate(rate_id: int, session: Session = Depends(get_session)):
    rate = session.get(ShippingRateHistory, rate_id)
    if not rate:
        raise HTTPException(status_code=404, detail="Kargo baremi bulunamadı")
    session.delete(rate)
    session.commit()
    return {"ok": True}


def recete_bagini_denetle(ad: str, urunler: list) -> None:
    """Reçetede kullanılan kalem silinirse ürün maliyeti sessizce düşeceği için silmeyi durdurur."""
    if urunler:
        adlar = ", ".join(sorted({u.sku for u in urunler}))
        raise HTTPException(status_code=409, detail=f"{ad} silinemedi, şu ürünlerin reçetesinde: {adlar}")


def makine_sec(session: Session, machine_id: Optional[int]) -> int:
    try:
        return makine_coz(list(session.exec(select(Machine)).all()), machine_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def siradaki_kod(session: Session, alan, onek: str, hane: int) -> str:
    """Silinen kayıtlar yüzünden numara tekrarlanmasın diye en büyük numaradan devam eder."""
    kodlar = session.exec(select(alan).where(alan.startswith(onek))).all()
    numaralar = [int(k[len(onek):]) for k in kodlar if k[len(onek):].isdigit()]
    return f"{onek}{max(numaralar, default=0) + 1:0{hane}d}"


# --- 1b. B2B TOPLU ALIM İSKONTO BAREMLERİ ---
@app.get("/api/b2b-tiers")
def list_b2b_tiers(session: Session = Depends(get_session)):
    return session.exec(
        select(B2BDiscountTier).order_by(B2BDiscountTier.min_quantity.asc())
    ).all()


@app.post("/api/b2b-tiers")
def create_b2b_tier(payload: B2BDiscountTierCreate, session: Session = Depends(get_session)):
    tier = B2BDiscountTier(
        tier_name=payload.tier_name,
        min_quantity=payload.min_quantity,
        max_quantity=payload.max_quantity,
        discount_percent=payload.discount_percent,
        is_active=True,
        updated_at=utc_now(),
    )
    session.add(tier)
    session.commit()
    session.refresh(tier)
    return tier


@app.put("/api/b2b-tiers/{tier_id}")
def update_b2b_tier(
    tier_id: int, payload: B2BDiscountTierCreate, session: Session = Depends(get_session)
):
    tier = session.get(B2BDiscountTier, tier_id)
    if not tier:
        raise HTTPException(status_code=404, detail="B2B baremi bulunamadı")
    tier.tier_name = payload.tier_name
    tier.min_quantity = payload.min_quantity
    tier.max_quantity = payload.max_quantity
    tier.discount_percent = payload.discount_percent
    tier.updated_at = utc_now()
    session.add(tier)
    session.commit()
    session.refresh(tier)
    return tier


@app.delete("/api/b2b-tiers/{tier_id}")
def delete_b2b_tier(tier_id: int, session: Session = Depends(get_session)):
    tier = session.get(B2BDiscountTier, tier_id)
    if not tier:
        raise HTTPException(status_code=404, detail="B2B baremi bulunamadı")
    session.delete(tier)
    session.commit()
    return {"ok": True}


# --- 2. MAKİNE PARKURU VE ÖMÜRLÜ PARÇALAR ---
@app.get("/api/machines")
def list_machines(session: Session = Depends(get_session)):
    settings = session.get(GlobalSettings, 1)
    machines = session.exec(select(Machine)).all()
    result = []
    for m in machines:
        parts = session.exec(select(MachinePart).where(MachinePart.machine_id == m.id)).all()
        calc = calculate_machine_hourly_cost(m, list(parts), settings.electricity_price_per_kwh)
        result.append(
            {
                **m.model_dump(),
                "cost_breakdown": calc,
            }
        )
    return result


@app.post("/api/machines")
def create_machine(payload: MachineCreate, session: Session = Depends(get_session)):
    m = Machine(**payload.model_dump())
    session.add(m)
    session.commit()
    session.refresh(m)
    return m


@app.delete("/api/machines/{machine_id}")
def delete_machine(machine_id: int, session: Session = Depends(get_session)):
    m = session.get(Machine, machine_id)
    if not m:
        raise HTTPException(status_code=404, detail="Makine bulunamadı")
    kayitlar = session.exec(select(ProductionOrder).where(ProductionOrder.machine_id == machine_id)).all()
    if kayitlar:
        raise HTTPException(
            status_code=409,
            detail=f"{m.name} silinemez: {len(kayitlar)} üretim kaydı var, üretim geçmişi değiştirilemez.",
        )
    urunler = session.exec(select(Product).where(Product.machine_id == machine_id)).all()
    emirler = session.exec(select(ProductionQueueItem).where(ProductionQueueItem.machine_id == machine_id)).all()
    if urunler or emirler:
        raise HTTPException(
            status_code=409,
            detail=f"{m.name} silinemedi: {len(urunler)} ürün ve {len(emirler)} emir bu makineye bağlı.",
        )
    for q in session.exec(select(QuoteRecord).where(QuoteRecord.machine_id == machine_id)).all():
        q.machine_id = None
        session.add(q)
    session.delete(m)
    session.commit()
    return {"ok": True}


@app.post("/api/machine-parts")
def add_machine_part(payload: MachinePartCreate, session: Session = Depends(get_session)):
    part = MachinePart(**payload.model_dump())
    session.add(part)
    session.commit()
    session.refresh(part)
    return part


@app.post("/api/machine-parts/{part_id}/reset")
def reset_machine_part_maintenance(part_id: int, session: Session = Depends(get_session)):
    """Bakım yapıldı / Parça yenilendi: Kullanılan saati 0'a sıfırlar."""
    part = session.get(MachinePart, part_id)
    if not part:
        raise HTTPException(status_code=404, detail="Parça bulunamadı")
    part.used_hours = 0.0
    part.last_replaced_at = utc_now()
    session.add(part)
    session.commit()
    session.refresh(part)
    return part


@app.delete("/api/machine-parts/{part_id}")
def delete_machine_part(part_id: int, session: Session = Depends(get_session)):
    part = session.get(MachinePart, part_id)
    if not part:
        raise HTTPException(status_code=404, detail="Parça bulunamadı")
    session.delete(part)
    session.commit()
    return {"ok": True}


# --- 3. HAMMADDE (FİLAMENT) & YARDIMCI MALZEME ENVANTERİ ---
@app.get("/api/filaments")
def list_filaments(session: Session = Depends(get_session)):
    filaments = session.exec(select(Filament).order_by(Filament.brand, Filament.color_name)).all()
    return [
        {
            **f.model_dump(),
            "cost_per_gram_tl": round(f.price_per_kg_tl / 1000.0, 4),
        }
        for f in filaments
    ]


@app.post("/api/filaments")
def create_filament(payload: FilamentCreate, session: Session = Depends(get_session)):
    f = Filament(**payload.model_dump())
    session.add(f)
    session.commit()
    session.refresh(f)
    return f


@app.post("/api/filaments/{filament_id}/alim")
def filament_alim(filament_id: int, alim: AlimGirdisi, session: Session = Depends(get_session)):
    if alim.miktar <= 0:
        raise HTTPException(status_code=400, detail="Alım miktarı (gram) sıfırdan büyük olmalı")
    f = session.get(Filament, filament_id)
    if not f:
        raise HTTPException(status_code=404, detail="Filament bulunamadı")
    onceki_stok, onceki_fiyat = f.remaining_weight_gr, f.price_per_kg_tl
    f.price_per_kg_tl = agirlikli_fiyat(onceki_stok, onceki_fiyat, alim.miktar, alim.birim_fiyat_tl)
    f.remaining_weight_gr = round(onceki_stok + alim.miktar, 2)
    f.updated_at = utc_now()
    kayit = PurchaseRecord(
        item_type="FILAMENT", item_id=f.id, item_label=f"{f.brand} {f.material_type} {f.color_name}",
        quantity=alim.miktar, unit="gr", unit_price_tl=alim.birim_fiyat_tl or onceki_fiyat,
        total_tl=round(alim.miktar / 1000.0 * (alim.birim_fiyat_tl or onceki_fiyat), 2),
        supplier=alim.tedarikci, invoice_no=alim.fatura_no, invoice_date=alim.fatura_tarihi, source=alim.kaynak,
        created_new_item=alim.yeni_kalem, price_before_tl=onceki_fiyat, price_after_tl=f.price_per_kg_tl,
        stock_before=onceki_stok, stock_after=f.remaining_weight_gr,
    )
    session.add(f)
    session.add(kayit)
    session.commit()
    session.refresh(f)
    session.refresh(kayit)
    return {"filament": f, "alim": kayit}


class RenkGirdisi(BaseModel):
    color_hex: str
    kaynak: Optional[str] = None


@app.patch("/api/filaments/{filament_id}/renk")
def filament_renk(filament_id: int, girdi: RenkGirdisi, session: Session = Depends(get_session)):
    """Filament rengini (HEX) düzeltir — ör. bir bottan ekran görüntüsüyle ölçülen değer."""
    import re as _re

    hx = girdi.color_hex.strip().upper()
    if not _re.fullmatch(r"#[0-9A-F]{6}", hx):
        raise HTTPException(status_code=400, detail="Renk #RRGGBB biçiminde olmalı")
    f = session.get(Filament, filament_id)
    if not f:
        raise HTTPException(status_code=404, detail="Filament bulunamadı")
    eski = f.color_hex
    f.color_hex = hx
    session.add(f)
    session.commit()
    session.refresh(f)
    return {"filament": f, "eski_hex": eski}


@app.delete("/api/filaments/{filament_id}")
def delete_filament(filament_id: int, session: Session = Depends(get_session)):
    f = session.get(Filament, filament_id)
    if not f:
        raise HTTPException(status_code=404, detail="Filament bulunamadı")
    urunler = session.exec(
        select(Product).join(ProductBOMFilament).where(ProductBOMFilament.filament_id == filament_id)
    ).all()
    recete_bagini_denetle(f"{f.brand} {f.color_name}", urunler)
    for q in session.exec(select(QuoteRecord).where(QuoteRecord.filament_id == filament_id)).all():
        q.filament_id = None
        session.add(q)
    session.delete(f)
    session.commit()
    return {"ok": True}


@app.get("/api/materials")
def list_materials(session: Session = Depends(get_session)):
    return session.exec(
        select(AuxiliaryMaterial).order_by(AuxiliaryMaterial.category, AuxiliaryMaterial.name)
    ).all()


@app.post("/api/materials")
def create_material(payload: AuxiliaryMaterialCreate, session: Session = Depends(get_session)):
    mat = AuxiliaryMaterial(**payload.model_dump())
    session.add(mat)
    session.commit()
    session.refresh(mat)
    return mat


@app.post("/api/materials/{mat_id}/alim")
def malzeme_alim(mat_id: int, alim: AlimGirdisi, session: Session = Depends(get_session)):
    if alim.miktar <= 0:
        raise HTTPException(status_code=400, detail="Alım miktarı sıfırdan büyük olmalı")
    m = session.get(AuxiliaryMaterial, mat_id)
    if not m:
        raise HTTPException(status_code=404, detail="Malzeme bulunamadı")
    onceki_stok, onceki_fiyat = m.stock_quantity, m.unit_cost_tl
    m.unit_cost_tl = agirlikli_fiyat(onceki_stok, onceki_fiyat, alim.miktar, alim.birim_fiyat_tl)
    m.stock_quantity = round(onceki_stok + alim.miktar, 2)
    m.updated_at = utc_now()
    kayit = PurchaseRecord(
        item_type="MALZEME", item_id=m.id, item_label=m.name, quantity=alim.miktar, unit=m.unit,
        unit_price_tl=alim.birim_fiyat_tl or onceki_fiyat,
        total_tl=round(alim.miktar * (alim.birim_fiyat_tl or onceki_fiyat), 2),
        supplier=alim.tedarikci, invoice_no=alim.fatura_no, invoice_date=alim.fatura_tarihi, source=alim.kaynak,
        created_new_item=alim.yeni_kalem, price_before_tl=onceki_fiyat, price_after_tl=m.unit_cost_tl,
        stock_before=onceki_stok, stock_after=m.stock_quantity,
    )
    session.add(m)
    session.add(kayit)
    session.commit()
    session.refresh(m)
    session.refresh(kayit)
    return {"malzeme": m, "alim": kayit}


@app.get("/api/alimlar")
def alim_listesi(fatura_no: Optional[str] = None, limit: int = 200, session: Session = Depends(get_session)):
    q = select(PurchaseRecord).order_by(PurchaseRecord.created_at.desc()).limit(limit)
    if fatura_no:
        q = q.where(PurchaseRecord.invoice_no == fatura_no)
    return session.exec(q).all()


@app.delete("/api/materials/{mat_id}")
def delete_material(mat_id: int, session: Session = Depends(get_session)):
    mat = session.get(AuxiliaryMaterial, mat_id)
    if not mat:
        raise HTTPException(status_code=404, detail="Malzeme bulunamadı")
    urunler = session.exec(
        select(Product).join(ProductBOMMaterial).where(ProductBOMMaterial.material_id == mat_id)
    ).all()
    recete_bagini_denetle(mat.name, urunler)
    gruplar = session.exec(
        select(MaterialGroup).join(MaterialGroupItem).where(MaterialGroupItem.material_id == mat_id)
    ).all()
    if gruplar:
        adlar = ", ".join(sorted({g.code for g in gruplar}))
        raise HTTPException(status_code=409, detail=f"{mat.name} silinemedi, şu ambalaj/malzeme gruplarında kullanılıyor: {adlar}")
    session.delete(mat)
    session.commit()
    return {"ok": True}


def _enrich_material_group(session: Session, grp: MaterialGroup, materials_map: Optional[Dict[int, AuxiliaryMaterial]] = None) -> Dict[str, Any]:
    if materials_map is None:
        materials_map = {m.id: m for m in session.exec(select(AuxiliaryMaterial)).all()}
    items_out = []
    total_cost = 0.0
    for it in grp.items:
        mat = materials_map.get(it.material_id)
        if not mat:
            continue
        eff_q = it.quantity * (1.0 + it.waste_rate_percent / 100.0)
        line_cost = eff_q * mat.unit_cost_tl
        total_cost += line_cost
        items_out.append(
            {
                "id": it.id,
                "material_id": mat.id,
                "material_code": mat.code,
                "material_name": mat.name,
                "unit": mat.unit,
                "quantity": it.quantity,
                "waste_rate_percent": it.waste_rate_percent,
                "unit_cost_tl": round(mat.unit_cost_tl, 2),
                "line_cost_tl": round(line_cost, 2),
            }
        )
    return {
        "id": grp.id,
        "code": grp.code,
        "name": grp.name,
        "category": grp.category,
        "description": grp.description,
        "unit_cost_tl": round(total_cost, 2),
        "items": items_out,
    }


@app.get("/api/material-groups")
def list_material_groups(session: Session = Depends(get_session)):
    groups = session.exec(select(MaterialGroup).order_by(MaterialGroup.code)).all()
    materials_map = {m.id: m for m in session.exec(select(AuxiliaryMaterial)).all()}
    return [_enrich_material_group(session, g, materials_map) for g in groups]


@app.post("/api/material-groups")
def create_or_update_material_group(payload: MaterialGroupCreate, session: Session = Depends(get_session)):
    existing = session.exec(select(MaterialGroup).where(MaterialGroup.code == payload.code.strip())).first()
    if existing:
        existing.name = payload.name
        existing.category = payload.category
        existing.description = payload.description
        existing.updated_at = utc_now()
        session.add(existing)
        for old_it in session.exec(select(MaterialGroupItem).where(MaterialGroupItem.group_id == existing.id)).all():
            session.delete(old_it)
        grp = existing
    else:
        grp = MaterialGroup(
            code=payload.code.strip(),
            name=payload.name,
            category=payload.category,
            description=payload.description,
            updated_at=utc_now(),
        )
        session.add(grp)
    session.commit()
    session.refresh(grp)
    for it in payload.items:
        session.add(
            MaterialGroupItem(
                group_id=grp.id,
                material_id=it.material_id,
                quantity=it.quantity,
                waste_rate_percent=it.waste_rate_percent,
            )
        )
    session.commit()
    session.refresh(grp)
    return _enrich_material_group(session, grp)


@app.delete("/api/material-groups/{group_id}")
def delete_material_group(group_id: int, session: Session = Depends(get_session)):
    grp = session.get(MaterialGroup, group_id)
    if not grp:
        raise HTTPException(status_code=404, detail="Malzeme grubu bulunamadı")
    urunler = session.exec(
        select(Product).join(ProductBOMGroup).where(ProductBOMGroup.group_id == group_id)
    ).all()
    recete_bagini_denetle(grp.code, urunler)
    session.delete(grp)
    session.commit()
    return {"ok": True}


# --- 4. ORCA SLICER DOSYA OKUYUCU (.GCODE / .GCODE.3MF) ---
@app.post("/api/slicer/parse-file")
async def upload_and_parse_slicer_file(file: UploadFile = File(...)):
    data = await file.read()
    try:
        parsed = parse_3d_or_slicer_file(file.filename or "model.gcode", data)
        return parsed
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/slicer/convert-time")
def convert_slicer_time(hours: int = 0, minutes: int = 0, text: Optional[str] = None):
    if text:
        h, m, dec = parse_duration_string(text)
        return {"hours": h, "minutes": m, "decimal_hours": dec}
    dec = hours_minutes_to_decimal(hours, minutes)
    return {"hours": hours, "minutes": minutes, "decimal_hours": dec}


# --- 5. ÜRÜN KATALOG VE REÇETE (BOM) YÖNETİMİ ---
@app.get("/api/products")
def list_products(session: Session = Depends(get_session)):
    settings = session.get(GlobalSettings, 1)
    products = session.exec(select(Product).order_by(Product.created_at.desc())).all()
    filaments_map = {f.id: f for f in session.exec(select(Filament)).all()}
    materials_map = {m.id: m for m in session.exec(select(AuxiliaryMaterial)).all()}
    b2b_tiers = list(
        session.exec(select(B2BDiscountTier).order_by(B2BDiscountTier.min_quantity.asc())).all()
    )
    groups_map = _load_groups_map(session)
    return [
        get_enriched_product(session, p, settings, filaments_map, materials_map, b2b_tiers, groups_map)
        for p in products
    ]


def urun_alanlari(session: Session, payload: ProductCreateUpdate) -> dict:
    if payload.urun_tipi not in istasyon.URUN_TIPLERI:
        raise HTTPException(status_code=400, detail=f"Ürün tipi {' ya da '.join(istasyon.URUN_TIPLERI)} olmalı.")
    if payload.gozetimsiz_baski and payload.urun_tipi != "STANDART":
        raise HTTPException(status_code=400, detail="Kişiye özel ürün gözetimsiz basılamaz.")
    return {
        "urun_tipi": payload.urun_tipi,
        "stl_path": payload.stl_path or None,
        "gozetimsiz_baski": payload.gozetimsiz_baski,
        "sku": payload.sku,
        "name": payload.name,
        "category": payload.category,
        "variant_name": payload.variant_name,
        "machine_id": makine_sec(session, payload.machine_id),
        "print_time_hours_int": payload.print_time_hours_int,
        "print_time_minutes_int": payload.print_time_minutes_int,
        "print_time_decimal_hours": hours_minutes_to_decimal(
            payload.print_time_hours_int, payload.print_time_minutes_int
        ),
        "size_x_mm": payload.size_x_mm,
        "size_y_mm": payload.size_y_mm,
        "size_z_mm": payload.size_z_mm,
        "layer_height_mm": payload.layer_height_mm,
        "infill_percent": payload.infill_percent,
        "shipping_tier_id": payload.shipping_tier_id,
        "custom_shipping_cost_tl": payload.custom_shipping_cost_tl,
        "target_margin_percent": payload.target_margin_percent,
        "b2b_margin_percent": payload.b2b_margin_percent,
        "vat_enabled": payload.vat_enabled,
        "vat_rate_percent": payload.vat_rate_percent,
        "manual_sale_price_tl": payload.manual_sale_price_tl,
        "notes": payload.notes,
    }


@app.post("/api/products")
def create_product(payload: ProductCreateUpdate, session: Session = Depends(get_session)):
    prod = Product(
        **urun_alanlari(session, payload),
        source_file_name=payload.source_file_name,
        thumbnail_base64=payload.thumbnail_base64,
    )
    session.add(prod)
    session.commit()
    session.refresh(prod)
    return recete_yaz(session, prod, payload)


@app.put("/api/products/{product_id}")
def update_product(product_id: int, payload: ProductCreateUpdate, session: Session = Depends(get_session)):
    """Reçeteyi yalnızca bundan sonraki üretimler için değiştirir; geçmiş kayıtlar kendi değerlerini saklar."""
    prod = session.get(Product, product_id)
    if not prod:
        raise HTTPException(status_code=404, detail="Ürün bulunamadı")
    for alan, deger in urun_alanlari(session, payload).items():
        setattr(prod, alan, deger)
    if payload.source_file_name:
        prod.source_file_name = payload.source_file_name
    if payload.thumbnail_base64:
        prod.thumbnail_base64 = payload.thumbnail_base64
    session.add(prod)
    for item in session.exec(select(ProductBOMFilament).where(ProductBOMFilament.product_id == product_id)).all():
        session.delete(item)
    for item in session.exec(select(ProductBOMMaterial).where(ProductBOMMaterial.product_id == product_id)).all():
        session.delete(item)
    for item in session.exec(select(ProductBOMGroup).where(ProductBOMGroup.product_id == product_id)).all():
        session.delete(item)
    return recete_yaz(session, prod, payload)


def recete_yaz(session: Session, prod: Product, payload: ProductCreateUpdate):
    for f_in in payload.filaments:
        session.add(
            ProductBOMFilament(
                product_id=prod.id,
                filament_id=f_in.filament_id,
                net_weight_gr=f_in.net_weight_gr,
                waste_rate_percent=f_in.waste_rate_percent,
            )
        )
    for m_in in payload.materials:
        session.add(
            ProductBOMMaterial(
                product_id=prod.id,
                material_id=m_in.material_id,
                quantity=m_in.quantity,
                waste_rate_percent=m_in.waste_rate_percent,
            )
        )
    for g_in in payload.groups:
        session.add(
            ProductBOMGroup(
                product_id=prod.id,
                group_id=g_in.group_id,
                quantity=g_in.quantity,
            )
        )
    session.commit()
    session.refresh(prod)
    settings = session.get(GlobalSettings, 1)
    return get_enriched_product(session, prod, settings)


@app.put("/api/products/{product_id}/toggle-vat")
def toggle_product_vat(
    product_id: int, vat_enabled: bool, session: Session = Depends(get_session)
):
    prod = session.get(Product, product_id)
    if not prod:
        raise HTTPException(status_code=404, detail="Ürün bulunamadı")
    prod.vat_enabled = vat_enabled
    prod.updated_at = utc_now()
    session.add(prod)
    session.commit()
    session.refresh(prod)
    settings = session.get(GlobalSettings, 1)
    return get_enriched_product(session, prod, settings)


@app.delete("/api/products/{product_id}")
def delete_product(product_id: int, session: Session = Depends(get_session)):
    prod = session.get(Product, product_id)
    if not prod:
        raise HTTPException(status_code=404, detail="Ürün bulunamadı")
    if session.exec(select(ProductionOrder).where(ProductionOrder.product_id == product_id)).first():
        raise HTTPException(
            status_code=409,
            detail=f"{prod.name} silinemez: üretim geçmişi var. Reçetesini düzenleyebilirsiniz.",
        )
    emirler = session.exec(
        select(ProductionQueueItem).where(ProductionQueueItem.product_id == product_id)
    ).all()
    bekleyen = [e for e in emirler if e.status == "BEKLIYOR"]
    if bekleyen:
        raise HTTPException(
            status_code=409,
            detail=f"{prod.name} silinemedi: {len(bekleyen)} bekleyen üretim emri var. Önce emirleri iptal edin.",
        )
    for emir in emirler:
        session.delete(emir)
    session.delete(prod)
    session.commit()
    return {"ok": True}


# --- 6. B2B & ÖZEL TASARIM HIZLI TEKLİF HESAPLAYICI (QUOTATION) ---
@app.post("/api/quotes/calculate")
def calculate_or_save_quote(
    payload: QuoteSimulationInput, session: Session = Depends(get_session)
):
    settings = session.get(GlobalSettings, 1)
    dec_hours_unit = hours_minutes_to_decimal(
        payload.print_hours_per_unit, payload.print_minutes_per_unit
    )
    total_print_hours = dec_hours_unit * payload.quantity

    machine = session.get(Machine, makine_sec(session, payload.machine_id))
    parts = (
        session.exec(select(MachinePart).where(MachinePart.machine_id == machine.id)).all()
        if machine
        else []
    )
    m_hourly = (
        calculate_machine_hourly_cost(machine, list(parts), settings.electricity_price_per_kwh)[
            "total_hourly_cost_tl"
        ]
        if machine
        else 0.0
    )

    filament = session.get(Filament, payload.filament_id) if payload.filament_id else None
    if not filament:
        filament = session.exec(select(Filament)).first()
    cost_per_gr = (filament.price_per_kg_tl / 1000.0) if filament else 0.65

    effective_grams_unit = payload.net_grams_per_unit * (1.0 + payload.bom_waste_percent / 100.0)
    unit_filament_cost = effective_grams_unit * cost_per_gr
    unit_machine_cost = dec_hours_unit * m_hourly
    unit_production_cost = (
        unit_machine_cost + unit_filament_cost + payload.extra_material_cost_per_unit_tl
    )

    total_production_cost = (
        unit_production_cost * payload.quantity
    ) + payload.design_setup_fee_tl

    eff_stopaj = settings.bank_withholding_rate_percent if settings.esnaf_muafiyeti_enabled else 0.0
    pricing = calculate_pricing_from_cost(
        production_cost_tl=total_production_cost,
        shipping_cost_tl=payload.shipping_total_tl,
        commission_rate_percent=settings.commission_rate_percent,
        target_margin_percent=payload.target_margin_percent,
        vat_enabled=payload.vat_enabled,
        vat_rate_percent=payload.vat_rate_percent,
        discount_percent=payload.discount_percent,
        bank_withholding_rate_percent=eff_stopaj,
    )

    qty = max(1, payload.quantity)
    unit_break_even_tl = round(pricing["break_even_gross_tl"] / qty, 2)
    unit_final_offer_tl = round(pricing["final_sale_price_gross_tl"] / qty, 2)

    saved_id = None
    if payload.save_quote:
        q_rec = QuoteRecord(
            quote_code=siradaki_kod(session, QuoteRecord.quote_code, f"TEKLIF-{utc_now().year}-", 3),
            customer_name=payload.customer_name,
            project_title=payload.project_title,
            quantity=payload.quantity,
            machine_id=machine.id if machine else None,
            print_hours_per_unit=payload.print_hours_per_unit,
            print_minutes_per_unit=payload.print_minutes_per_unit,
            decimal_hours_per_unit=dec_hours_unit,
            filament_id=filament.id if filament else None,
            net_grams_per_unit=payload.net_grams_per_unit,
            bom_waste_percent=payload.bom_waste_percent,
            extra_material_cost_per_unit_tl=payload.extra_material_cost_per_unit_tl,
            design_setup_fee_tl=payload.design_setup_fee_tl,
            shipping_total_tl=payload.shipping_total_tl,
            target_margin_percent=payload.target_margin_percent,
            discount_percent=payload.discount_percent,
            vat_enabled=payload.vat_enabled,
            vat_rate_percent=payload.vat_rate_percent,
            break_even_total_tl=pricing["break_even_gross_tl"],
            final_offer_total_tl=pricing["final_sale_price_gross_tl"],
            net_profit_total_tl=pricing["net_profit_tl"],
        )
        session.add(q_rec)
        session.commit()
        session.refresh(q_rec)
        saved_id = q_rec.id

    return {
        "saved_quote_id": saved_id,
        "quantity": payload.quantity,
        "decimal_hours_per_unit": dec_hours_unit,
        "total_print_hours_decimal": round(total_print_hours, 2),
        "effective_grams_per_unit": round(effective_grams_unit, 2),
        "total_filament_grams_with_waste": round(effective_grams_unit * qty, 2),
        "unit_machine_cost_tl": round(unit_machine_cost, 2),
        "unit_filament_cost_tl": round(unit_filament_cost, 2),
        "unit_production_cost_tl": round(unit_production_cost, 2),
        "total_production_cost_tl": round(total_production_cost, 2),
        "unit_break_even_gross_tl": unit_break_even_tl,
        "unit_final_offer_gross_tl": unit_final_offer_tl,
        "pricing": pricing,
    }


@app.get("/api/quotes")
def list_saved_quotes(session: Session = Depends(get_session)):
    return session.exec(select(QuoteRecord).order_by(QuoteRecord.created_at.desc())).all()


@app.delete("/api/quotes/{quote_id}")
def delete_quote(quote_id: int, session: Session = Depends(get_session)):
    q = session.get(QuoteRecord, quote_id)
    if not q:
        raise HTTPException(status_code=404, detail="Teklif bulunamadı")
    session.delete(q)
    session.commit()
    return {"ok": True}


# --- 7. ÜRETİM EMİRLERİ, STOKTAN / PARÇA ÖMRÜNDEN DÜŞME VE FİRE KAYDI ---
@app.get("/api/orders")
def list_production_orders(session: Session = Depends(get_session)):
    return session.exec(
        select(ProductionOrder).order_by(ProductionOrder.created_at.desc())
    ).all()


def stok_yeterli_mi(session: Session, product: Product, qty: int) -> None:
    """Reçetenin stoğu karşılamadığı kalemleri bulur; biri bile yetmezse hiçbir şey değişmeden hata verir."""
    filament_gr: dict = {}
    for f_bom in product.filament_items:
        used_gr = f_bom.net_weight_gr * (1.0 + f_bom.waste_rate_percent / 100.0) * qty
        filament_gr[f_bom.filament_id] = filament_gr.get(f_bom.filament_id, 0.0) + used_gr

    malzeme_adet: dict = {}
    for m_bom in product.material_items:
        used_qty = m_bom.quantity * (1.0 + m_bom.waste_rate_percent / 100.0) * qty
        malzeme_adet[m_bom.material_id] = malzeme_adet.get(m_bom.material_id, 0.0) + used_qty
    for g_bom in getattr(product, "group_items", []) or []:
        grp = session.get(MaterialGroup, g_bom.group_id)
        if grp:
            for c_it in grp.items:
                used_qty = c_it.quantity * (1.0 + c_it.waste_rate_percent / 100.0) * g_bom.quantity * qty
                malzeme_adet[c_it.material_id] = malzeme_adet.get(c_it.material_id, 0.0) + used_qty

    eksikler = []
    for filament_id, gerekli in filament_gr.items():
        fil = session.get(Filament, filament_id)
        if fil and round(gerekli, 2) > fil.remaining_weight_gr:
            eksikler.append(
                f"{fil.brand} {fil.color_name}: gereken {gerekli:.2f} g, stok {fil.remaining_weight_gr:.2f} g, "
                f"eksik {gerekli - fil.remaining_weight_gr:.2f} g"
            )
    for material_id, gerekli in malzeme_adet.items():
        mat = session.get(AuxiliaryMaterial, material_id)
        if mat and round(gerekli, 2) > mat.stock_quantity:
            eksikler.append(
                f"{mat.name}: gereken {gerekli:.2f} {mat.unit}, stok {mat.stock_quantity:.2f} {mat.unit}, "
                f"eksik {gerekli - mat.stock_quantity:.2f} {mat.unit}"
            )
    if eksikler:
        raise HTTPException(status_code=409, detail="Stok yetersiz, önce alımı girin. " + "; ".join(eksikler))


@app.post("/api/orders")
def create_production_order(
    payload: ProductionOrderInput, session: Session = Depends(get_session)
):
    settings = session.get(GlobalSettings, 1)
    machine_id = payload.machine_id
    if machine_id is None and payload.order_type != "WASTE" and payload.product_id:
        urun = session.get(Product, payload.product_id)
        machine_id = urun.machine_id if urun else None
    machine = session.get(Machine, makine_sec(session, machine_id))

    parts = session.exec(
        select(MachinePart).where(MachinePart.machine_id == machine.id)
    ).all()
    m_cost_info = calculate_machine_hourly_cost(
        machine, list(parts), settings.electricity_price_per_kwh
    )
    hourly_rate = m_cost_info["total_hourly_cost_tl"]

    order_code = siradaki_kod(session, ProductionOrder.order_code, f"ORD-{utc_now().year}-", 4)

    if payload.order_type == "WASTE":
        dec_hours = hours_minutes_to_decimal(payload.waste_hours, payload.waste_minutes)
        fil_cost = 0.0
        if payload.waste_filament_id and payload.waste_grams > 0:
            fil = session.get(Filament, payload.waste_filament_id)
            if fil:
                if payload.waste_grams > fil.remaining_weight_gr:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"Stok yetersiz, önce alımı girin. {fil.brand} {fil.color_name}: fire {payload.waste_grams:.2f} g, "
                            f"stok {fil.remaining_weight_gr:.2f} g"
                        ),
                    )
                fil.remaining_weight_gr = round(fil.remaining_weight_gr - payload.waste_grams, 2)
                fil_cost = payload.waste_grams * (fil.price_per_kg_tl / 1000.0)
                session.add(fil)

        machine.total_worked_hours += dec_hours
        session.add(machine)
        for pt in parts:
            pt.used_hours += dec_hours
            session.add(pt)

        prod_loss = (dec_hours * hourly_rate) + fil_cost
        order = ProductionOrder(
            order_code=order_code,
            order_type="WASTE",
            channel=payload.channel,
            product_id=payload.product_id,
            product_name_snapshot="Başarısız Baskı / Fire Kaydı",
            machine_id=machine.id,
            quantity=1,
            total_print_hours_decimal=dec_hours,
            total_filament_used_gr=payload.waste_grams,
            snapshot_electricity_price=settings.electricity_price_per_kwh,
            snapshot_machine_hourly_cost=hourly_rate,
            snapshot_production_cost_tl=round(prod_loss, 2),
            snapshot_shipping_cost_tl=0.0,
            snapshot_commission_rate_percent=0.0,
            vat_enabled=False,
            vat_rate_percent=0.0,
            unit_sale_price_tl=0.0,
            total_revenue_tl=0.0,
            total_net_profit_tl=round(-prod_loss, 2),
            notes=payload.notes,
        )
        session.add(order)
        session.commit()
        session.refresh(order)
        return order

    if not payload.product_id:
        raise HTTPException(status_code=400, detail="Üretim için bir ürün seçilmelidir")
    product = session.get(Product, payload.product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Ürün bulunamadı")

    enriched = get_enriched_product(session, product, settings)
    qty = max(1, payload.quantity)
    total_hours_dec = round(enriched["print_time"]["decimal_hours"] * qty, 2)
    total_filament_gr = round(enriched["total_filament_with_waste_gr"] * qty, 2)
    stok_yeterli_mi(session, product, qty)

    machine.total_worked_hours = round(machine.total_worked_hours + total_hours_dec, 2)
    session.add(machine)
    for pt in parts:
        pt.used_hours = round(pt.used_hours + total_hours_dec, 2)
        session.add(pt)

    for f_bom in product.filament_items:
        fil = session.get(Filament, f_bom.filament_id)
        if fil:
            used_gr = f_bom.net_weight_gr * (1.0 + f_bom.waste_rate_percent / 100.0) * qty
            fil.remaining_weight_gr = max(0.0, round(fil.remaining_weight_gr - used_gr, 2))
            session.add(fil)

    for m_bom in product.material_items:
        mat = session.get(AuxiliaryMaterial, m_bom.material_id)
        if mat:
            used_qty = m_bom.quantity * (1.0 + m_bom.waste_rate_percent / 100.0) * qty
            mat.stock_quantity = max(0.0, round(mat.stock_quantity - used_qty, 2))
            session.add(mat)

    for g_bom in getattr(product, "group_items", []) or []:
        grp = session.get(MaterialGroup, g_bom.group_id)
        if grp:
            for c_it in grp.items:
                mat = session.get(AuxiliaryMaterial, c_it.material_id)
                if mat:
                    used_qty = c_it.quantity * (1.0 + c_it.waste_rate_percent / 100.0) * g_bom.quantity * qty
                    mat.stock_quantity = max(0.0, round(mat.stock_quantity - used_qty, 2))
                    session.add(mat)

    elle_kargo = payload.custom_shipping_cost_tl is not None or product.custom_shipping_cost_tl is not None
    total_prod_cost = enriched["production_cost_tl"] * qty
    eff_stopaj = settings.bank_withholding_rate_percent if settings.esnaf_muafiyeti_enabled else 0.0

    pricing_order = kargo_esikli_fiyat(
        esik_tl=0.0 if elle_kargo else settings.kargo_esigi_tl,
        kargo_tl=(
            payload.custom_shipping_cost_tl
            if payload.custom_shipping_cost_tl is not None
            else enriched["kargo_bedeli_tl"]
        ),
        production_cost_tl=total_prod_cost,
        commission_rate_percent=settings.commission_rate_percent,
        target_margin_percent=product.target_margin_percent,
        vat_enabled=payload.vat_enabled,
        vat_rate_percent=product.vat_rate_percent,
        manual_sale_price_tl=(
            payload.custom_unit_sale_price_tl * qty
            if payload.custom_unit_sale_price_tl
            else None
        ),
        bank_withholding_rate_percent=eff_stopaj,
    )

    shipping_cost = pricing_order["shipping_cost_tl"]

    order = ProductionOrder(
        order_code=order_code,
        order_type="PRODUCTION",
        channel=payload.channel,
        product_id=product.id,
        product_name_snapshot=f"{product.name} ({product.variant_name})",
        machine_id=machine.id,
        quantity=qty,
        total_print_hours_decimal=total_hours_dec,
        total_filament_used_gr=total_filament_gr,
        snapshot_electricity_price=settings.electricity_price_per_kwh,
        snapshot_machine_hourly_cost=hourly_rate,
        snapshot_production_cost_tl=round(total_prod_cost, 2),
        snapshot_shipping_cost_tl=round(shipping_cost, 2),
        snapshot_commission_rate_percent=settings.commission_rate_percent,
        snapshot_bank_withholding_rate_percent=eff_stopaj,
        vat_enabled=payload.vat_enabled,
        vat_rate_percent=product.vat_rate_percent if payload.vat_enabled else 0.0,
        unit_sale_price_tl=round(pricing_order["final_sale_price_gross_tl"] / qty, 2),
        total_revenue_tl=pricing_order["final_sale_price_gross_tl"],
        bank_incoming_tl=pricing_order["bank_incoming_tl"],
        bank_withholding_amount_tl=pricing_order["bank_withholding_amount_tl"],
        bank_net_payout_tl=pricing_order["bank_net_payout_tl"],
        total_net_profit_tl=pricing_order["net_profit_tl"],
        notes=payload.notes,
    )
    session.add(order)
    session.commit()
    session.refresh(order)
    return order


# --- 7b. İSTASYON: ONAYLI G-CODE, YÜKLÜ FİLAMENT VE BASKI KUYRUĞU ---
def _sonuc(sonuc: dict) -> dict:
    """İstasyon işlevinin hata sözlüğünü ekranın beklediği HTTP hatasına çevirir."""
    if sonuc.get("status") == "error":
        raise HTTPException(status_code=409, detail=sonuc["message"])
    return sonuc


@app.post("/api/products/{product_id}/gcode-onayla")
def onayla_urun_gcode(product_id: int, payload: GcodeYoluInput, session: Session = Depends(get_session)):
    prod = session.get(Product, product_id)
    if not prod:
        raise HTTPException(status_code=404, detail="Ürün bulunamadı")
    return _sonuc(istasyon.urun_gcode_onayla(session, prod.sku, payload.gcode_yolu))


@app.put("/api/machines/{machine_id}/yuklu-filament")
def yuklu_filament_yaz(machine_id: int, payload: YukluFilamentInput, session: Session = Depends(get_session)):
    m = session.get(Machine, machine_id)
    if not m:
        raise HTTPException(status_code=404, detail="Makine bulunamadı")
    if payload.filament_slot_sayisi is not None:
        m.filament_slot_sayisi = max(1, min(4, int(payload.filament_slot_sayisi)))
        for idx in range(m.filament_slot_sayisi + 1, 5):
            setattr(m, "yuklu_filament_id" if idx == 1 else f"yuklu_filament_{idx}_id", None)
    if payload.filament_ids is not None:
        for idx in range(1, 5):
            fid = payload.filament_ids[idx - 1] if idx - 1 < len(payload.filament_ids) else None
            if fid is not None and not session.get(Filament, fid):
                raise HTTPException(status_code=404, detail=f"Filament bulunamadı: {fid}")
            setattr(m, "yuklu_filament_id" if idx == 1 else f"yuklu_filament_{idx}_id", fid)
    elif "filament_id" in payload.model_fields_set:
        if payload.filament_id is not None and not session.get(Filament, payload.filament_id):
            raise HTTPException(status_code=404, detail="Filament bulunamadı")
        slot_no = max(1, min(4, int(payload.slot or 1)))
        alan = "yuklu_filament_id" if slot_no == 1 else f"yuklu_filament_{slot_no}_id"
        setattr(m, alan, payload.filament_id)
    session.add(m)
    session.commit()
    session.refresh(m)
    return m


@app.post("/api/machines/centauri-karti")
def centauri_karti_ac(session: Session = Depends(get_session)):
    return istasyon.makine_karti_hazirla(session)


@app.get("/api/queue")
def list_queue(status: Optional[str] = None, session: Session = Depends(get_session)):
    return istasyon.emir_listesi(session, status)


@app.post("/api/queue")
def create_queue_item(payload: EmirCreate, session: Session = Depends(get_session)):
    prod = session.get(Product, payload.product_id)
    if not prod:
        raise HTTPException(status_code=404, detail="Ürün bulunamadı")
    if prod.urun_tipi == "KISIYE_OZEL":
        return _sonuc(
            istasyon.ozel_emir_ac(
                session, prod.sku, payload.ozel_metin or "", payload.quantity,
                payload.channel, payload.machine_id, payload.notes,
            )
        )
    return _sonuc(
        istasyon.emir_ac(session, prod.sku, payload.quantity, payload.channel, payload.machine_id, payload.notes)
    )


@app.post("/api/queue/{emir_id}/baskiya-al")
def queue_baskiya_al(emir_id: int, session: Session = Depends(get_session)):
    return _sonuc(istasyon.emir_baskiya_al(session, emir_id))


@app.post("/api/queue/{emir_id}/musteri-onayi")
def queue_musteri_onayi(emir_id: int, session: Session = Depends(get_session)):
    return _sonuc(istasyon.musteri_onayi(session, emir_id))


@app.post("/api/queue/{emir_id}/gcode-bagla")
def queue_gcode_bagla(emir_id: int, payload: GcodeYoluInput, session: Session = Depends(get_session)):
    return _sonuc(istasyon.emir_gcode_bagla(session, emir_id, payload.gcode_yolu))


@app.post("/api/queue/{emir_id}/hata")
def queue_hata(emir_id: int, payload: EmirHataInput, session: Session = Depends(get_session)):
    return _sonuc(istasyon.emir_hata(session, emir_id, payload.gram, payload.saat, payload.dakika))


@app.post("/api/queue/{emir_id}/tamamla")
def queue_tamamla(emir_id: int, session: Session = Depends(get_session)):
    return _sonuc(istasyon.emir_tamamla(session, emir_id))


@app.post("/api/queue/{emir_id}/iptal")
def queue_iptal(emir_id: int, payload: EmirIptalInput, session: Session = Depends(get_session)):
    return _sonuc(istasyon.emir_iptal(session, emir_id, payload.neden))


@app.get("/api/queue/basilabilir")
def queue_basilabilir(
    machine_id: int, tabla_bos: bool = False, yazici_bosta: Optional[bool] = None,
    session: Session = Depends(get_session),
):
    return _sonuc(istasyon.basilabilir_emir(session, machine_id, tabla_bos, yazici_bosta))


# --- 8. 3 MODÜLLÜ YAPAY ZEKA (MODÜL 1-2-3) & İKAS CSV ÇIKTI PAKETİ ---
@app.get("/api/products/{product_id}/ai-bundle")
def generate_ai_and_ikas_bundle(product_id: int, session: Session = Depends(get_session)):
    settings = session.get(GlobalSettings, 1)
    product = session.get(Product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Ürün bulunamadı")

    enriched = get_enriched_product(session, product, settings)
    fil_names = ", ".join(f["filament_name"] for f in enriched["filaments"]) or "PLA+"
    mat_parts = [m["material_name"] for m in enriched["materials"]] + [
        f"{g['group_code']} ({g['unit_cost_tl']} TL)" for g in enriched.get("material_groups", [])
    ]
    mat_names = ", ".join(mat_parts) or "Standart Kutu"
    dims = enriched["dimensions_mm"]
    b2c = enriched["b2c_pricing"]
    b2b = enriched["b2b_unit_pricing_10plus"]
    stopaj_note = (
        f"- **Esnaf Vergi Muafiyeti Banka Stopajı (GVK 9/10):** %{settings.bank_withholding_rate_percent} "
        f"(Bankaya Gelen: {b2c.get('bank_incoming_tl', 0)} TL → Stopaj: -{b2c.get('bank_withholding_amount_tl', 0)} TL → Bankaya Net: {b2c.get('bank_net_payout_tl', 0)} TL)"
        if settings.esnaf_muafiyeti_enabled
        else "- **Esnaf Vergi Muafiyeti Stopajı:** Kapalı (%0)"
    )

    claude_module1_prompt = f"""# {settings.brand_name} ERP -> MODÜL 1 (CLAUDE PRO İKAS SEO & PAZAR ANALİZİ VERİ PAKETİ)

## 1. ÜRÜN TEKNİK KİMLİĞİ (ERP & Orca Slicer Çıktısı)
- **Stok Kodu (SKU):** {product.sku}
- **Ürün Adı:** {product.name} ({product.variant_name})
- **Kategori:** {product.category}
- **Ürün Boyutları (X-Y-Z):** {dims['x']:.0f} mm (Genişlik) x {dims['y']:.0f} mm (Derinlik) x {dims['z']:.0f} mm (Yükseklik) -> ({dims['x']/10:.1f} x {dims['y']/10:.1f} x {dims['z']/10:.1f} cm)
- **Kullanılan Hammadde (Filament):** {fil_names} (Net: {enriched['total_filament_net_gr']} gr / BOM Fire Dahil: {enriched['total_filament_with_waste_gr']} gr)
- **Ek Bileşenler (BOM):** {mat_names}
- **Üretim Süresi:** {enriched['print_time']['formatted']}
- **Tasarım / Üretim Notu:** {product.notes or 'Sipariş üzerine sıfır kusur toleransıyla üretilir.'}

## 2. MALİYET VE FİYATLANDIRMA SINIRLARI (ERP Hesaplaması)
- **Net Üretim Maliyeti (Makine + Elektrik + Parça Yıpranması + BOM):** {enriched['production_cost_tl']} TL
- **Kargo Baremi:** {enriched['shipping_cost_tl']} TL
- **İkas / Sanal POS Komisyon Oranı:** %{settings.commission_rate_percent} (Toplam hasılat üzerinden)
{stopaj_note}
- **KDV Durumu:** {'Açık (%' + str(product.vat_rate_percent) + ')' if product.vat_enabled else 'KDV Yok (%0)'}
- **🔴 Başa Baş Noktası (Sıfır Zarar Fiyatı):** {b2c['break_even_gross_tl']} TL
- **🟢 ERP Hedef B2C Satış Fiyatı (%{product.target_margin_percent} Hedef Marj):** **{b2c['final_sale_price_gross_tl']} TL** (Net Kâr: {b2c['net_profit_tl']} TL)
- **🔵 ERP B2B Kurumsal Toplu Alım Birim Fiyatı (10+ Adet):** **{b2b['final_sale_price_gross_tl']} TL**

## 3. GÖREVİN (MODÜL 1 -> MODÜL 2 & 3 ÇIKTILARI)
1. **Canlı Pazar & Rakip Analizi:** Türkiye pazarında (Trendyol, İkas mağazaları, Etsy) bu boyut ve kategorideki ürünlerin fiyat bandını ERP fiyatımızla ({b2c['final_sale_price_gross_tl']} TL) kıyasla.
2. **İkas SEO Paketi:** Yüksek dönüşümlü Ürün Başlığı, B2C duygusal + teknik satış açıklaması (boyutları cm olarak geçir), B2B kurumsal toplu sipariş notu, SEO Meta Title ve Meta Description üret.
3. **Modül 2 (Google AI Pro / Imagen / Whisk) Sanal Stüdyo Komutları:** Ham 3D model görselini gerçekçi filament dokulu ({fil_names}) stüdyo fotoğrafına dönüştürmek için 3 farklı sahne (1. Minimalist İskandinav yaşam alanı, 2. Yakın çekim katman/doku detayı, 3. Kurumsal/Modern ofis masası) İngilizce Image-to-Image promptu yaz.
4. **Modül 3 (Instagram Reels & Karusel) Senaryosu:** 5 saniyelik Veo video komutu, dikkat çekici ilk saniye Hook metni ve gerçek aranma hacimli Türkçe niş hashtag'leri hazırla."""

    return {
        "product": enriched,
        "claude_module1_prompt": claude_module1_prompt,
    }


@app.get("/api/export/ikas-csv")
def export_ikas_csv(session: Session = Depends(get_session)):
    """Tüm ürünleri İkas Toplu Ürün Yükleme uyumlu CSV formatında dışa aktarır."""
    settings = session.get(GlobalSettings, 1)
    products = session.exec(select(Product)).all()
    filaments_map = {f.id: f for f in session.exec(select(Filament)).all()}
    materials_map = {m.id: m for m in session.exec(select(AuxiliaryMaterial)).all()}
    groups_map = _load_groups_map(session)
    b2b_tiers = list(
        session.exec(select(B2BDiscountTier).order_by(B2BDiscountTier.min_quantity.asc())).all()
    )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "SKU",
            "Ürün Adı",
            "Varyant",
            "Kategori",
            "Boyutlar (cm)",
            "Baskı Süresi (Ondalıklı Saat)",
            "Net Gramaj (gr)",
            "Üretim Maliyeti (TL)",
            "Başa Baş Fiyatı (TL)",
            "B2C Satış Fiyatı (TL)",
            "B2B 10+ Birim Fiyatı (TL)",
            "KDV Durumu",
            "Açıklama / Not",
        ]
    )

    for p in products:
        enr = get_enriched_product(session, p, settings, filaments_map, materials_map, b2b_tiers, groups_map)
        d = enr["dimensions_mm"]
        cm_str = f"{d['x']/10:.1f}x{d['y']/10:.1f}x{d['z']/10:.1f} cm"
        writer.writerow(
            [
                p.sku,
                p.name,
                p.variant_name or "Standart",
                p.category,
                cm_str,
                enr["print_time"]["decimal_hours"],
                enr["total_filament_net_gr"],
                enr["production_cost_tl"],
                enr["b2c_pricing"]["break_even_gross_tl"],
                enr["b2c_pricing"]["final_sale_price_gross_tl"],
                enr["b2b_unit_pricing_10plus"]["final_sale_price_gross_tl"],
                f"%{p.vat_rate_percent}" if p.vat_enabled else "KDV Yok",
                p.notes or "",
            ]
        )

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue().encode("utf-8-sig")]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=workbench_ikas_urunler.csv"},
    )


import os as _os
_APP_DIR = _os.path.dirname(_os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=_os.path.join(_APP_DIR, "static")), name="static")


@app.get("/")
def serve_spa():
    return FileResponse(_os.path.join(_APP_DIR, "static", "index.html"))
