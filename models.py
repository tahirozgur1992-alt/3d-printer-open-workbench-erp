from datetime import datetime, timezone
from typing import Optional, List
from sqlmodel import SQLModel, Field, Relationship

from workbench_config import COMPANY_NAME


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class GlobalSettings(SQLModel, table=True):
    """1. Genel Parametreler (Merkezi Ayarlar)"""
    id: Optional[int] = Field(default=1, primary_key=True)
    brand_name: str = Field(default=COMPANY_NAME)
    electricity_price_per_kwh: float = Field(default=4.50, description="TL/kWh elektrik birim fiyatı")
    commission_rate_percent: float = Field(default=3.50, description="Mağaza (ör. ikas, Shopify) / Sanal POS toplam hasılat üzerinden komisyon (%)")
    esnaf_muafiyeti_enabled: bool = Field(default=False, description="Esnaf Vergi Muafiyeti Belgesi (GVK 9/10) aktif mi?")
    bank_withholding_rate_percent: float = Field(default=0.0, description="Banka hesabına para geldiğinde kesilen stopaj oranı (%4 standart, işçi varsa %2)")
    esnaf_yillik_limit_tl: float = Field(default=1900000.0, description="Esnaf Muafiyeti yıllık brüt satış hasılatı sınırı (TL)")
    default_vat_rate_percent: float = Field(default=20.0, description="Varsayılan KDV oranı (%)")
    default_vat_enabled: bool = Field(default=False, description="Varsayılan olarak KDV açık mı?")
    default_target_margin_percent: float = Field(default=50.0, description="Varsayılan B2C hedef kâr marjı (%)")
    default_b2b_margin_percent: float = Field(default=35.0, description="Varsayılan B2B hedef kâr marjı (%)")
    default_shipping_cost: float = Field(default=85.0, description="Aktif standart kargo bedeli (TL)")
    kargo_esigi_tl: float = Field(default=0.0, description="Sipariş bu tutara ulaşınca kargoyu satıcı öder, altında müşteri öder. 0 = eşik yok")
    updated_at: datetime = Field(default_factory=utc_now)


class ShippingRateHistory(SQLModel, table=True):
    """Kargo Baremleri ve Tarihçesi (Değiştirilen tarihten sonraki siparişlerde geçerli olur)"""
    id: Optional[int] = Field(default=None, primary_key=True)
    tier_name: str = Field(default="Standart Kargo (1-3 Desi)", description="Barem adı / Desi aralığı")
    min_desi: float = Field(default=0.0)
    max_desi: float = Field(default=5.0)
    cost_tl: float = Field(default=85.0, description="Kargo maliyeti (TL)")
    is_active: bool = Field(default=True)
    effective_from: datetime = Field(default_factory=utc_now, description="Geçerlilik başlangıç tarihi")
    notes: Optional[str] = Field(default=None)


class B2BDiscountTier(SQLModel, table=True):
    """B2B Toplu Alım Adet Bazlı İskonto Baremleri (Örn: 10-24 Adet %10, 25-49 Adet %18, 50+ Adet %25)"""
    id: Optional[int] = Field(default=None, primary_key=True)
    tier_name: str = Field(default="Bronz B2B (10-24 Adet)")
    min_quantity: int = Field(default=10, description="Minimum sipariş adedi (dahil)")
    max_quantity: Optional[int] = Field(default=24, description="Maksimum sipariş adedi (dahil, sınırsız ise None/boş)")
    discount_percent: float = Field(default=10.0, description="Uygulanacak B2B iskonto oranı (%)")
    is_active: bool = Field(default=True)
    updated_at: datetime = Field(default_factory=utc_now)


class CategoryItem(SQLModel, table=True):
    """Kullanıcının kendi ekleyebileceği Ürün ve Malzeme Kategorileri"""
    id: Optional[int] = Field(default=None, primary_key=True)
    target_type: str = Field(default="PRODUCT", index=True, description="PRODUCT (Ürün Kategorisi) veya MATERIAL (Yardımcı Malzeme Kategorisi)")
    name: str = Field(index=True, description="Kategori adı (Örn: B2C - Abajur & Aydınlatma, B2B - Kurumsal 3D Logo)")
    created_at: datetime = Field(default_factory=utc_now)


class Machine(SQLModel, table=True):
    """2. Makine Parkuru (Ana Makine Kartı)"""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, description="Örn: Printer #1")
    brand_model: str = Field(default="Generic FDM")
    purchase_price_tl: float = Field(default=38000.0, description="Makine alış bedeli (TL)")
    lifespan_hours: float = Field(default=5000.0, description="Tahmini toplam çalışma ömrü (Saat)")
    power_consumption_kw: float = Field(default=0.12, description="Saatlik ortalama elektrik tüketimi (kW) - Örn: 120W = 0.12 kW")
    total_worked_hours: float = Field(default=0.0, description="Bugüne kadar toplam çalıştığı saat (Ondalıklı)")
    status: str = Field(default="ACTIVE", description="ACTIVE, MAINTENANCE, OFFLINE")
    filament_slot_sayisi: int = Field(default=4, description="Makineye aynı anda yüklenebilen filament yuvası sayısı (örn: 4)")
    yuklu_filament_id: Optional[int] = Field(default=None, foreign_key="filament.id", description="1. yuvada takılı filament (gözetimsiz baskı koşulu)")
    yuklu_filament_2_id: Optional[int] = Field(default=None, foreign_key="filament.id", description="2. yuvada takılı filament")
    yuklu_filament_3_id: Optional[int] = Field(default=None, foreign_key="filament.id", description="3. yuvada takılı filament")
    yuklu_filament_4_id: Optional[int] = Field(default=None, foreign_key="filament.id", description="4. yuvada takılı filament")
    notes: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=utc_now)

    parts: List["MachinePart"] = Relationship(back_populates="machine", sa_relationship_kwargs={"cascade": "all, delete-orphan"})

    def yuklu_filamentler_listesi(self) -> List[Optional[int]]:
        tum = [
            self.yuklu_filament_id,
            self.yuklu_filament_2_id,
            self.yuklu_filament_3_id,
            self.yuklu_filament_4_id,
        ]
        n = max(1, min(4, int(self.filament_slot_sayisi or 4)))
        return tum[:n]


class MachinePart(SQLModel, table=True):
    """2b. Makineye Bağlı Ömürlü Parçalar (Nozzle, PEI Tabla, Extruder Dişlisi, Kayış vb.)"""
    id: Optional[int] = Field(default=None, primary_key=True)
    machine_id: int = Field(foreign_key="machine.id", index=True)
    name: str = Field(description="Örn: Hardened Steel Nozzle 0.4mm, Textured PEI Tabla")
    cost_tl: float = Field(default=600.0, description="Parça yenileme bedeli (TL)")
    lifespan_hours: float = Field(default=600.0, description="Parçanın tahmini toplam ömrü (Saat)")
    used_hours: float = Field(default=0.0, description="Son değişimden beri kullanılan süre (Ondalıklı saat)")
    warning_threshold_percent: float = Field(default=85.0, description="Ömrün yüzde kaçı dolunca bakım uyarısı versin?")
    last_replaced_at: datetime = Field(default_factory=utc_now)

    machine: Optional[Machine] = Relationship(back_populates="parts")


class Filament(SQLModel, table=True):
    """3a. Hammadde - Filament Envanteri (Fire oranı burada DEĞİL, BOM'dadır)"""
    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(index=True, description="Örn: FIL-PLA-BLK-01")
    brand: str = Field(default="Generic", description="Filament markası")
    material_type: str = Field(default="PLA+", description="Tür (PLA, PLA+, Matte PLA, Silk PLA, PETG, ASA, ABS)")
    color_name: str = Field(default="Mat Siyah", description="Renk adı")
    color_hex: str = Field(default="#1E1E1E", description="Görsel renk kodu")
    price_per_kg_tl: float = Field(default=650.0, description="1 Kg (1000 gr) alış fiyatı (TL)")
    spool_weight_gr: float = Field(default=1000.0, description="Tam makara gramajı (gr)")
    remaining_weight_gr: float = Field(default=1000.0, description="Kalan net filament gramajı (gr)")
    min_stock_alert_gr: float = Field(default=250.0, description="Kritik stok uyarı sınırı (gr)")
    notes: Optional[str] = Field(default=None)
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def cost_per_gram_tl(self) -> float:
        if self.spool_weight_gr <= 0:
            return 0.0
        return self.price_per_kg_tl / 1000.0


class AuxiliaryMaterial(SQLModel, table=True):
    """3b. Ek Bileşen & Sarf Malzeme Envanteri (LED, duy, kablo, adaptör, kargo kutusu, patpat, etiket vb.)"""
    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(index=True, description="Örn: MAT-LED-E27")
    name: str = Field(description="Malzeme adı (Örn: E27 Duy + Anahtarlı Kablo, 20x20x30 Kargo Kutusu)")
    category: str = Field(default="ELEKTRIK", description="ELEKTRIK, AMBALAJ, MONTAJ, AKSESUAR, DIGER")
    unit: str = Field(default="adet", description="Birim: adet, metre, cm, set, gr")
    unit_cost_tl: float = Field(default=45.0, description="Birim alış maliyeti (TL)")
    stock_quantity: float = Field(default=50.0, description="Mevcut stok miktarı")
    min_stock_alert: float = Field(default=10.0, description="Kritik stok uyarı sınırı")
    supplier: Optional[str] = Field(default=None, description="Tedarikçi / Not")
    updated_at: datetime = Field(default_factory=utc_now)


class Product(SQLModel, table=True):
    """4. Ürün Reçetesi (BOM), Orca Slicer Entegrasyonu & Saat Dönüşümü"""
    id: Optional[int] = Field(default=None, primary_key=True)
    sku: str = Field(index=True, description="Stok Kodu (Örn: PRT-VAZO-001)")
    name: str = Field(index=True, description="Ürün Adı (Örn: Nordic Dalgalı Dekoratif Vazo)")
    category: str = Field(default="B2C_DEKOR", description="B2C_DEKOR, B2C_AYDINLATMA, B2C_ORGANIZER, B2B_LOGO, B2B_OFIS")
    variant_name: Optional[str] = Field(default="Standart", description="Varyant (Örn: Büyük Boy 22cm / Işıklı)")
    machine_id: Optional[int] = Field(default=None, foreign_key="machine.id", description="Varsayılan üretim yazıcısı")

    # Orca Slicer / 3D Dosya Teknik Verileri
    source_file_name: Optional[str] = Field(default=None, description="STL / 3MF / GCODE dosya adı")
    print_time_hours_int: int = Field(default=0, description="Orca Slicer Saat (Örn: 3)")
    print_time_minutes_int: int = Field(default=0, description="Orca Slicer Dakika (Örn: 55)")
    print_time_decimal_hours: float = Field(default=0.0, description="Otomatik Ondalıklı Saat: Saat + (Dakika / 60) -> Örn: 3.92")

    size_x_mm: float = Field(default=0.0, description="Genişlik X (mm)")
    size_y_mm: float = Field(default=0.0, description="Derinlik Y (mm)")
    size_z_mm: float = Field(default=0.0, description="Yükseklik Z (mm)")
    layer_height_mm: float = Field(default=0.20, description="Katman yüksekliği (mm)")
    infill_percent: float = Field(default=15.0, description="Dolgu oranı (%)")

    # Fiyatlandırma & Kargo & KDV Tercihleri
    shipping_tier_id: Optional[int] = Field(default=None, foreign_key="shippingratehistory.id")
    custom_shipping_cost_tl: Optional[float] = Field(default=None, description="Özel kargo bedeli girilirse baremi ezer")
    target_margin_percent: float = Field(default=50.0, description="Hedef kâr marjı (%)")
    b2b_margin_percent: float = Field(default=35.0, description="B2B hedef kâr marjı (%)")
    vat_enabled: bool = Field(default=False, description="Bu üründe KDV açık mı?")
    vat_rate_percent: float = Field(default=20.0, description="KDV açık ise uygulanacak oran (%)")
    manual_sale_price_tl: Optional[float] = Field(default=None, description="Kullanıcı elle satış fiyatı sabitlemek isterse")

    # İstasyon: ürün tipi, onaylı G-code ve gözetimsiz baskı izni
    urun_tipi: str = Field(default="STANDART", description="STANDART veya KISIYE_OZEL")
    stl_path: Optional[str] = Field(default=None, description="Model dosyasının yolu (STL / 3MF)")
    gcode_path: Optional[str] = Field(default=None, description="Onaylı G-code'un depodaki değişmez kopyası")
    gcode_sha256: Optional[str] = Field(default=None, description="Onaylı G-code'un SHA-256 özeti")
    gcode_onay_tarihi: Optional[datetime] = Field(default=None)
    gozetimsiz_baski: bool = Field(default=False, description="Kullanıcı bu ürünün gözetimsiz basılmasına izin verdi mi?")

    thumbnail_base64: Optional[str] = Field(default=None, description="3MF/GCODE içinden çekilen 3D önizleme")
    notes: Optional[str] = Field(default=None, description="Tasarım / Üretim notları")
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    filament_items: List["ProductBOMFilament"] = Relationship(
        back_populates="product", sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
    material_items: List["ProductBOMMaterial"] = Relationship(
        back_populates="product", sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
    group_items: List["ProductBOMGroup"] = Relationship(
        back_populates="product", sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )


class MaterialGroup(SQLModel, table=True):
    """3c. Ambalaj & Malzeme Grupları (Alt BOM: İçeriği birden fazla yardımcı malzemeden oluşan paket/set)"""
    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(index=True, description="Örn: Ambalaj_Kucuk, Ambalaj_Orta, Ambalaj_Buyuk")
    name: str = Field(description="Grup Adı (Örn: Küçük Boy E-Ticaret Ambalaj Seti)")
    category: str = Field(default="AMBALAJ_GRUBU", description="AMBALAJ_GRUBU, MONTAJ_SETI, DIGER")
    description: Optional[str] = Field(default=None, description="Grup açıklaması")
    updated_at: datetime = Field(default_factory=utc_now)

    items: List["MaterialGroupItem"] = Relationship(
        back_populates="group", sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )


class MaterialGroupItem(SQLModel, table=True):
    """Ambalaj / Malzeme Grubunun içindeki alt malzemeler ve miktarları"""
    id: Optional[int] = Field(default=None, primary_key=True)
    group_id: int = Field(foreign_key="materialgroup.id", index=True)
    material_id: int = Field(foreign_key="auxiliarymaterial.id")
    quantity: float = Field(default=1.0, description="Grup başına kullanılan miktar (adet, metre vb.)")
    waste_rate_percent: float = Field(default=0.0, description="Fire oranı (%)")

    group: Optional[MaterialGroup] = Relationship(back_populates="items")


class ProductBOMGroup(SQLModel, table=True):
    """Ürünün reçetesine (BOM) eklenen Ambalaj / Malzeme Grubu (Alt BOM)"""
    id: Optional[int] = Field(default=None, primary_key=True)
    product_id: int = Field(foreign_key="product.id", index=True)
    group_id: int = Field(foreign_key="materialgroup.id")
    quantity: float = Field(default=1.0, description="Kullanılan grup adedi (örn: 1 paket Ambalaj_Kucuk)")

    product: Optional[Product] = Relationship(back_populates="group_items")


class ProductBOMFilament(SQLModel, table=True):
    """Üründe kullanılan filament(ler) ve BOM bazlı Fire Oranı (%)"""
    id: Optional[int] = Field(default=None, primary_key=True)
    product_id: int = Field(foreign_key="product.id", index=True)
    filament_id: int = Field(foreign_key="filament.id")
    net_weight_gr: float = Field(default=100.0, description="Orca Slicer'dan gelen net gramaj (gr)")
    waste_rate_percent: float = Field(default=5.0, description="Bu ürün reçetesine (BOM) özel fire oranı (%)")

    product: Optional[Product] = Relationship(back_populates="filament_items")


class ProductBOMMaterial(SQLModel, table=True):
    """Üründe kullanılan ekstra bileşenler / sarf malzemeler ve BOM bazlı Fire Oranı (%)"""
    id: Optional[int] = Field(default=None, primary_key=True)
    product_id: int = Field(foreign_key="product.id", index=True)
    material_id: int = Field(foreign_key="auxiliarymaterial.id")
    quantity: float = Field(default=1.0, description="Kullanılan miktar (adet, metre vb.)")
    waste_rate_percent: float = Field(default=0.0, description="Bu ürün reçetesine (BOM) özel fire oranı (%)")

    product: Optional[Product] = Relationship(back_populates="material_items")


class ProductionOrder(SQLModel, table=True):
    """Sipariş Üzerine Üretim (Print-on-Demand) & Fire Kayıtları (Tarihsel Snapshot ile)"""
    id: Optional[int] = Field(default=None, primary_key=True)
    order_code: str = Field(index=True, description="Sipariş / Üretim No (Örn: ORD-2026-001)")
    order_type: str = Field(default="PRODUCTION", description="PRODUCTION (Normal Sipariş), WASTE (Başarısız Baskı / Fire)")
    channel: str = Field(default="IKAS_B2C", description="IKAS_B2C, B2B_KURUMSAL, TRENDYOL, NUMUNE")
    product_id: Optional[int] = Field(default=None, foreign_key="product.id")
    product_name_snapshot: str = Field(default="")
    machine_id: int = Field(foreign_key="machine.id")
    quantity: int = Field(default=1, description="Üretilen adet")

    # Gerçekleşen süre ve maliyetlerin o günkü Snapshot'ı (İleride kargo/elektrik değişse bile geçmiş sipariş bozulmaz)
    total_print_hours_decimal: float = Field(default=0.0, description="Toplam harcanan ondalıklı baskı saati")
    total_filament_used_gr: float = Field(default=0.0, description="Fire dahil toplam düşülen filament (gr)")
    snapshot_electricity_price: float = Field(default=0.0)
    snapshot_machine_hourly_cost: float = Field(default=0.0)
    snapshot_production_cost_tl: float = Field(default=0.0)
    snapshot_shipping_cost_tl: float = Field(default=0.0, description="Sipariş tarihindeki geçerli kargo bedeli")
    snapshot_commission_rate_percent: float = Field(default=0.0)
    snapshot_bank_withholding_rate_percent: float = Field(default=0.0, description="Sipariş tarihindeki banka stopaj oranı (%)")
    vat_enabled: bool = Field(default=False, description="Bu siparişte KDV var mıydı?")
    vat_rate_percent: float = Field(default=0.0)
    unit_sale_price_tl: float = Field(default=0.0)
    total_revenue_tl: float = Field(default=0.0, description="Toplam tahsilat / hasılat (brüt)")
    bank_incoming_tl: float = Field(default=0.0, description="Komisyon düşüldükten sonra bankaya gelen tutar (TL)")
    bank_withholding_amount_tl: float = Field(default=0.0, description="Bankanın kestiği stopaj kesintisi (TL)")
    bank_net_payout_tl: float = Field(default=0.0, description="Stopaj kesildikten sonra bankada kalan net nakit (TL)")
    total_net_profit_tl: float = Field(default=0.0, description="Komisyon, stopaj, kargo, üretim ve KDV sonrası net kâr")

    notes: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=utc_now)


class SettingConfirmation(SQLModel, table=True):
    """Gerçek değeri kullanıcı tarafından onaylanmış genel ayarlar"""
    key: str = Field(primary_key=True)
    confirmed_at: datetime = Field(default_factory=utc_now)


class ProductionQueueItem(SQLModel, table=True):
    """Baskısı bekleyen sipariş; stok ve makine saati baskı bitince düşülür"""
    id: Optional[int] = Field(default=None, primary_key=True)
    product_id: int = Field(foreign_key="product.id")
    machine_id: int = Field(foreign_key="machine.id")
    quantity: int = Field(default=1)
    channel: str = Field(default="IKAS_B2C")
    status: str = Field(
        default="BEKLIYOR",
        description="BEKLIYOR, ONAY_BEKLIYOR, DILIM_BEKLIYOR, BASILIYOR, HATA, TAMAMLANDI, IPTAL",
    )
    production_order_id: Optional[int] = Field(default=None, foreign_key="productionorder.id")
    notes: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=utc_now)
    completed_at: Optional[datetime] = Field(default=None)
    # İstasyon: kişiye özel hat ve baskı izleme
    ozel_metin: Optional[str] = Field(default=None, description="Kişiye özel üründe müşterinin istediği metin")
    gcode_path: Optional[str] = Field(default=None, description="Bu emre özel G-code (kişiye özel ürün)")
    musteri_onay_at: Optional[datetime] = Field(default=None)
    baski_basladi_at: Optional[datetime] = Field(default=None)


class QuoteRecord(SQLModel, table=True):
    """B2B ve Özel Tasarım Hızlı Teklif Hesaplayıcı Kayıtları"""
    id: Optional[int] = Field(default=None, primary_key=True)
    quote_code: str = Field(index=True, description="Örn: TEKLIF-2026-001")
    customer_name: str = Field(default="Kurumsal Müşteri")
    project_title: str = Field(default="Özel 3D Kurumsal Logo / Harf")
    quantity: int = Field(default=10)
    machine_id: Optional[int] = Field(default=None, foreign_key="machine.id")
    print_hours_per_unit: int = Field(default=2)
    print_minutes_per_unit: int = Field(default=30)
    decimal_hours_per_unit: float = Field(default=2.5)
    filament_id: Optional[int] = Field(default=None, foreign_key="filament.id")
    net_grams_per_unit: float = Field(default=85.0)
    bom_waste_percent: float = Field(default=5.0)
    extra_material_cost_per_unit_tl: float = Field(default=25.0)
    design_setup_fee_tl: float = Field(default=0.0, description="Tek seferlik 3D modelleme/kalıp ücreti (TL)")
    shipping_total_tl: float = Field(default=120.0)

    target_margin_percent: float = Field(default=45.0)
    discount_percent: float = Field(default=10.0, description="Müşteriye uygulanan iskonto (%)")
    vat_enabled: bool = Field(default=False, description="Teklifte KDV dahil mi?")
    vat_rate_percent: float = Field(default=20.0)

    break_even_total_tl: float = Field(default=0.0, description="Sıfır zarar (Başa baş) toplam tutar")
    final_offer_total_tl: float = Field(default=0.0, description="Müşteriye sunulan nihai toplam teklif")
    net_profit_total_tl: float = Field(default=0.0, description="Net toplam kâr")
    created_at: datetime = Field(default_factory=utc_now)


class PurchaseRecord(SQLModel, table=True):
    """Hammadde / malzeme alım kaydı (fatura veya alım listesi) — stok girişi ve maliyet izlenebilirliği."""
    id: Optional[int] = Field(default=None, primary_key=True)
    item_type: str = Field(index=True, description="FILAMENT veya MALZEME")
    item_id: int = Field(index=True, description="Filament.id veya AuxiliaryMaterial.id")
    item_label: str = Field(default="", description="Okunur ad (marka tür renk / malzeme adı)")
    quantity: float = Field(description="Filament: gram; malzeme: kendi birimi")
    unit: str = Field(default="gr")
    unit_price_tl: float = Field(default=0.0, description="Filament: TL/kg; malzeme: TL/birim (KDV dahil)")
    total_tl: float = Field(default=0.0)
    supplier: Optional[str] = Field(default=None)
    invoice_no: Optional[str] = Field(default=None, index=True)
    invoice_date: Optional[str] = Field(default=None, description="YYYY-AA-GG")
    source: Optional[str] = Field(default=None, description="Kaynak dosya / kanal (ör. telegram:fatura.pdf)")
    created_new_item: bool = Field(default=False, description="Bu alımla yeni kalem oluşturuldu mu")
    price_before_tl: Optional[float] = Field(default=None)
    price_after_tl: Optional[float] = Field(default=None)
    stock_before: Optional[float] = Field(default=None)
    stock_after: Optional[float] = Field(default=None)
    created_at: datetime = Field(default_factory=utc_now)
