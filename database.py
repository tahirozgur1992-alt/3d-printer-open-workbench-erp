import os
from sqlalchemy import inspect, text
from sqlmodel import SQLModel, Session, create_engine, select
from models import (
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
    PurchaseRecord,  # noqa: F401 — create_all için tabloyu kaydeder
)
from cost_engine import hours_minutes_to_decimal

from workbench_config import DB_PATH, DEMO_SEED, COMPANY_NAME
os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)


def get_session():
    with Session(engine) as session:
        yield session


# Sonradan eklenen sütunlar: (tablo, sütun, SQL tipi ve varsayılanı). Var olan kayıtlara dokunulmaz.
EK_SUTUNLAR = [
    ("globalsettings", "kargo_esigi_tl", "FLOAT NOT NULL DEFAULT 0"),
    ("globalsettings", "esnaf_muafiyeti_enabled", "BOOLEAN NOT NULL DEFAULT 0"),
    ("globalsettings", "bank_withholding_rate_percent", "FLOAT NOT NULL DEFAULT 0"),
    ("globalsettings", "esnaf_yillik_limit_tl", "FLOAT NOT NULL DEFAULT 1900000"),
    ("machine", "filament_slot_sayisi", "INTEGER NOT NULL DEFAULT 4"),
    ("machine", "yuklu_filament_id", "INTEGER"),
    ("machine", "yuklu_filament_2_id", "INTEGER"),
    ("machine", "yuklu_filament_3_id", "INTEGER"),
    ("machine", "yuklu_filament_4_id", "INTEGER"),
    ("product", "urun_tipi", "VARCHAR NOT NULL DEFAULT 'STANDART'"),
    ("product", "stl_path", "VARCHAR"),
    ("product", "gcode_path", "VARCHAR"),
    ("product", "gcode_sha256", "VARCHAR"),
    ("product", "gcode_onay_tarihi", "DATETIME"),
    ("product", "gozetimsiz_baski", "BOOLEAN NOT NULL DEFAULT 0"),
    ("productionorder", "snapshot_bank_withholding_rate_percent", "FLOAT NOT NULL DEFAULT 0"),
    ("productionorder", "bank_incoming_tl", "FLOAT NOT NULL DEFAULT 0"),
    ("productionorder", "bank_withholding_amount_tl", "FLOAT NOT NULL DEFAULT 0"),
    ("productionorder", "bank_net_payout_tl", "FLOAT NOT NULL DEFAULT 0"),
    ("productionqueueitem", "ozel_metin", "VARCHAR"),
    ("productionqueueitem", "gcode_path", "VARCHAR"),
    ("productionqueueitem", "musteri_onay_at", "DATETIME"),
    ("productionqueueitem", "baski_basladi_at", "DATETIME"),
]


def sema_guncelle(baglanti) -> list:
    """Eski veritabanına eksik sütunları ekler; eklenenleri döndürür."""
    eklenen = []
    denetci = inspect(baglanti)
    for tablo, sutun, tip in EK_SUTUNLAR:
        if not denetci.has_table(tablo):
            continue
        if sutun not in {s["name"] for s in denetci.get_columns(tablo)}:
            baglanti.execute(text(f"ALTER TABLE {tablo} ADD COLUMN {sutun} {tip}"))
            eklenen.append(f"{tablo}.{sutun}")
    return eklenen


def init_db_and_seed():
    SQLModel.metadata.create_all(engine)
    with engine.begin() as baglanti:
        sema_guncelle(baglanti)
    with Session(engine) as session:
        # Üretim veritabanına örnek veri yazılmaz; gerçek değerleri kullanıcı girer.
        # WORKBENCH_ERP_DEMO_SEED=1 ile aşağıdaki ÖRNEK (EXAMPLE) veriler yüklenir; fiyatlar uydurma, yuvarlak
        # değerlerdir ve gerçek bir tedarikçiyi/piyasayı temsil etmez.
        if not DEMO_SEED:
            if not session.get(GlobalSettings, 1):
                session.add(
                    GlobalSettings(
                        id=1,
                        brand_name=COMPANY_NAME,
                        electricity_price_per_kwh=0.0,
                        commission_rate_percent=0.0,
                        default_vat_rate_percent=0.0,
                        default_target_margin_percent=0.0,
                        default_b2b_margin_percent=0.0,
                        default_shipping_cost=0.0,
                    )
                )
                session.commit()
            return

        # 1. Genel Parametreler
        settings = session.get(GlobalSettings, 1)
        if not settings:
            settings = GlobalSettings(
                id=1,
                brand_name=COMPANY_NAME,
                electricity_price_per_kwh=5.0,
                commission_rate_percent=3.0,
                esnaf_muafiyeti_enabled=False,
                bank_withholding_rate_percent=0.0,
                esnaf_yillik_limit_tl=1900000.0,
                default_vat_rate_percent=20.0,
                default_vat_enabled=False,
                default_target_margin_percent=50.0,
                default_b2b_margin_percent=35.0,
                default_shipping_cost=100.0,
            )
            session.add(settings)
            session.commit()

        # 2. Kargo Baremleri
        existing_shipping = session.exec(select(ShippingRateHistory)).first()
        if not existing_shipping:
            tiers = [
                ShippingRateHistory(
                    tier_name="Küçük Paket (0-2 Desi)",
                    min_desi=0.0,
                    max_desi=2.0,
                    cost_tl=80.0,
                    is_active=True,
                    notes="Küçük masa düzenleyici, anahtarlık, küçük aksesuar",
                ),
                ShippingRateHistory(
                    tier_name="Standart Paket (2-5 Desi)",
                    min_desi=2.0,
                    max_desi=5.0,
                    cost_tl=100.0,
                    is_active=True,
                    notes="Standart vazo, masa lambası, abajur",
                ),
                ShippingRateHistory(
                    tier_name="Büyük / Kurumsal Koli (5-15 Desi)",
                    min_desi=5.0,
                    max_desi=15.0,
                    cost_tl=150.0,
                    is_active=True,
                    notes="Büyük aydınlatma, kurumsal 3D tabela / toplu kutu",
                ),
            ]
            for t in tiers:
                session.add(t)
            session.commit()

        # 2b. B2B Toplu Alım İskonto Baremleri
        existing_b2b = session.exec(select(B2BDiscountTier)).first()
        if not existing_b2b:
            b2b_tiers = [
                B2BDiscountTier(
                    tier_name="Başlangıç B2B (10-24 Adet)",
                    min_quantity=10,
                    max_quantity=24,
                    discount_percent=10.0,
                ),
                B2BDiscountTier(
                    tier_name="Kurumsal Bronz (25-49 Adet)",
                    min_quantity=25,
                    max_quantity=49,
                    discount_percent=15.0,
                ),
                B2BDiscountTier(
                    tier_name="Kurumsal Gümüş (50-99 Adet)",
                    min_quantity=50,
                    max_quantity=99,
                    discount_percent=25.0,
                ),
                B2BDiscountTier(
                    tier_name="Kurumsal Altın (100+ Adet)",
                    min_quantity=100,
                    max_quantity=None,
                    discount_percent=30.0,
                ),
            ]
            for bt in b2b_tiers:
                session.add(bt)
            session.commit()

        # 2c. Ürün ve Malzeme Kategorileri
        existing_cat = session.exec(select(CategoryItem)).first()
        if not existing_cat:
            default_cats = [
                CategoryItem(target_type="PRODUCT", name="B2C - Dekoratif Vazo & Obje"),
                CategoryItem(target_type="PRODUCT", name="B2C - Aydınlatma & Abajur"),
                CategoryItem(target_type="PRODUCT", name="B2C - Masaüstü Düzenleyici (Organizer)"),
                CategoryItem(target_type="PRODUCT", name="B2B - Kurumsal 3D Logo & Harf"),
                CategoryItem(target_type="PRODUCT", name="B2B - Kurumsal Ofis & Promosyon"),
                CategoryItem(target_type="MATERIAL", name="ELEKTRİK / LED / KABLO"),
                CategoryItem(target_type="MATERIAL", name="AMBALAJ / KARGO KUTUSU"),
                CategoryItem(target_type="MATERIAL", name="MONTAJ / YAPIŞTIRICI / VİDA"),
                CategoryItem(target_type="MATERIAL", name="AKSESUAR / AHŞAP / PLEKSİ"),
            ]
            for c in default_cats:
                session.add(c)
            session.commit()

        # 3. Örnek Makine ve Ömürlü Parçalar
        existing_machine = session.exec(select(Machine)).first()
        if not existing_machine:
            m1 = Machine(
                name="Example Printer #1 (sample)",
                brand_model="Elegoo Centauri Carbon",
                purchase_price_tl=30000.0,
                lifespan_hours=5000.0,
                power_consumption_kw=0.15,  # 150W ortalama (örnek)
                total_worked_hours=100.0,
                status="ACTIVE",
                notes="EXAMPLE machine — replace with your own printer, price and lifespan.",
            )
            session.add(m1)
            session.commit()
            session.refresh(m1)

            parts = [
                MachinePart(
                    machine_id=m1.id,
                    name="Hardened Steel Nozzle 0.4mm",
                    cost_tl=500.0,
                    lifespan_hours=600.0,
                    used_hours=420.0,
                    warning_threshold_percent=80.0,
                ),
                MachinePart(
                    machine_id=m1.id,
                    name="Textured PEI Tabla",
                    cost_tl=1000.0,
                    lifespan_hours=1200.0,
                    used_hours=420.0,
                    warning_threshold_percent=85.0,
                ),
                MachinePart(
                    machine_id=m1.id,
                    name="Extruder Dişli Seti",
                    cost_tl=800.0,
                    lifespan_hours=2000.0,
                    used_hours=420.0,
                    warning_threshold_percent=85.0,
                ),
                MachinePart(
                    machine_id=m1.id,
                    name="XY Kayış & Rulman Bakım Seti",
                    cost_tl=800.0,
                    lifespan_hours=2500.0,
                    used_hours=420.0,
                    warning_threshold_percent=85.0,
                ),
            ]
            for p in parts:
                session.add(p)
            session.commit()

        # 4. Örnek Filamentler (Fire oranı burada değil, BOM'dadır)
        existing_filament = session.exec(select(Filament)).first()
        if not existing_filament:
            filaments = [
                Filament(
                    code="FIL-PLA-MATBLK",
                    brand="Sample Brand",
                    material_type="Matte PLA",
                    color_name="Sample PLA Black",
                    color_hex="#1F2937",
                    price_per_kg_tl=700.0,
                    spool_weight_gr=1000.0,
                    remaining_weight_gr=1000.0,
                    min_stock_alert_gr=250.0,
                ),
                Filament(
                    code="FIL-PLA-WARMWHT",
                    brand="Sample Brand",
                    material_type="PLA",
                    color_name="Sample PLA Warm White",
                    color_hex="#F5F5F0",
                    price_per_kg_tl=700.0,
                    spool_weight_gr=1000.0,
                    remaining_weight_gr=1000.0,
                    min_stock_alert_gr=250.0,
                ),
                Filament(
                    code="FIL-PLA-MARBLE",
                    brand="Sample Brand",
                    material_type="Marble PLA",
                    color_name="Sample PLA Marble",
                    color_hex="#D6D3D1",
                    price_per_kg_tl=800.0,
                    spool_weight_gr=1000.0,
                    remaining_weight_gr=1000.0,
                    min_stock_alert_gr=250.0,
                ),
                Filament(
                    code="FIL-PETG-NAVY",
                    brand="Sample Brand",
                    material_type="PETG",
                    color_name="Sample PETG Navy",
                    color_hex="#1E3A8A",
                    price_per_kg_tl=700.0,
                    spool_weight_gr=1000.0,
                    remaining_weight_gr=1000.0,
                    min_stock_alert_gr=250.0,
                ),
            ]
            for f in filaments:
                session.add(f)
            session.commit()

        # 5. Örnek Yardımcı Malzeme & Sarf Envanteri
        existing_mat = session.exec(select(AuxiliaryMaterial)).first()
        if not existing_mat:
            materials = [
                AuxiliaryMaterial(
                    code="MAT-ELK-E27KABLO",
                    name="E27 Duy + 1.8m Anahtarlı Kablo Seti",
                    category="ELEKTRIK",
                    unit="adet",
                    unit_cost_tl=50.0,
                    stock_quantity=10.0,
                    min_stock_alert=10.0,
                ),
                AuxiliaryMaterial(
                    code="MAT-ELK-LEDAMPUL",
                    name="Günışığı (2700K) 4W LED Ampul",
                    category="ELEKTRIK",
                    unit="adet",
                    unit_cost_tl=30.0,
                    stock_quantity=10.0,
                    min_stock_alert=10.0,
                ),
                AuxiliaryMaterial(
                    code="MAT-ELK-SERITLED",
                    name="12V COB Şerit LED + Adaptör (B2B Logo İçin)",
                    category="ELEKTRIK",
                    unit="set",
                    unit_cost_tl=150.0,
                    stock_quantity=5.0,
                    min_stock_alert=5.0,
                ),
                AuxiliaryMaterial(
                    code="MAT-AMB-KUTU25",
                    name="Çift Oluklu Kargo Kutusu (22x22x28 cm)",
                    category="AMBALAJ",
                    unit="adet",
                    unit_cost_tl=20.0,
                    stock_quantity=20.0,
                    min_stock_alert=20.0,
                ),
                AuxiliaryMaterial(
                    code="MAT-AMB-PATPAT",
                    name="Balonlu Naylon + Kutu Etiketi (örnek)",
                    category="AMBALAJ",
                    unit="set",
                    unit_cost_tl=5.0,
                    stock_quantity=20.0,
                    min_stock_alert=20.0,
                ),
            ]
            for mat in materials:
                session.add(mat)
            session.commit()

        # 6. Örnek Ürün Reçetesi (BOM) -> 3 saat 55 dk = 3.92 saat kuralı ile
        existing_product = session.exec(select(Product)).first()
        if not existing_product:
            machine = session.exec(select(Machine)).first()
            fil_white = session.exec(
                select(Filament).where(Filament.code == "FIL-PLA-WARMWHT")
            ).first()
            fil_black = session.exec(
                select(Filament).where(Filament.code == "FIL-PLA-MATBLK")
            ).first()
            mat_duy = session.exec(
                select(AuxiliaryMaterial).where(
                    AuxiliaryMaterial.code == "MAT-ELK-E27KABLO"
                )
            ).first()
            mat_led = session.exec(
                select(AuxiliaryMaterial).where(
                    AuxiliaryMaterial.code == "MAT-ELK-LEDAMPUL"
                )
            ).first()
            mat_box = session.exec(
                select(AuxiliaryMaterial).where(
                    AuxiliaryMaterial.code == "MAT-AMB-KUTU25"
                )
            ).first()
            mat_wrap = session.exec(
                select(AuxiliaryMaterial).where(
                    AuxiliaryMaterial.code == "MAT-AMB-PATPAT"
                )
            ).first()

            p1 = Product(
                sku="SAMPLE-LAMP-001",
                name="Sample Table Lamp (example product)",
                category="B2C_AYDINLATMA",
                variant_name="Standart Boy (24cm) - Sıcak Beyaz",
                machine_id=machine.id if machine else None,
                source_file_name="sample_lamp.3mf",
                print_time_hours_int=3,
                print_time_minutes_int=55,
                print_time_decimal_hours=hours_minutes_to_decimal(3, 55),  # 3.92 saat
                size_x_mm=180.0,
                size_y_mm=180.0,
                size_z_mm=240.0,
                layer_height_mm=0.20,
                infill_percent=15.0,
                target_margin_percent=50.0,
                b2b_margin_percent=35.0,
                vat_enabled=False,
                vat_rate_percent=20.0,
                notes="Üst abajur şapkası Vase Mode (0.8mm duvar), alt gövde %15 Gyroid dolgu.",
            )
            session.add(p1)
            session.commit()
            session.refresh(p1)

            if fil_white and fil_black:
                session.add(
                    ProductBOMFilament(
                        product_id=p1.id,
                        filament_id=fil_white.id,
                        net_weight_gr=115.0,
                        waste_rate_percent=4.0,
                    )
                )
                session.add(
                    ProductBOMFilament(
                        product_id=p1.id,
                        filament_id=fil_black.id,
                        net_weight_gr=95.0,
                        waste_rate_percent=5.0,
                    )
                )

            for m_obj in [mat_duy, mat_led, mat_box, mat_wrap]:
                if m_obj:
                    session.add(
                        ProductBOMMaterial(
                            product_id=p1.id,
                            material_id=m_obj.id,
                            quantity=1.0,
                            waste_rate_percent=0.0,
                        )
                    )
            session.commit()
