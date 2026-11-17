from typing import Dict, Any, List, Optional
from models import (
    GlobalSettings,
    Machine,
    MachinePart,
    Filament,
    AuxiliaryMaterial,
    MaterialGroup,
    Product,
    ProductBOMFilament,
    ProductBOMMaterial,
    ProductBOMGroup,
)


def makine_coz(machines: List[Machine], machine_id: Optional[int] = None) -> int:
    """Makine verilmediyse tek makineyi seçer; birden fazlaysa varsayım yapmaz, seçim ister."""
    if machine_id is not None:
        if not any(m.id == machine_id for m in machines):
            raise ValueError(f"Makine bulunamadı: {machine_id}")
        return machine_id
    if not machines:
        raise ValueError("Kayıtlı makine yok, önce makine ekleyin.")
    if len(machines) > 1:
        adlar = ", ".join(f"{m.id}: {m.name}" for m in machines)
        raise ValueError(f"Birden fazla makine var, makine seçin ({adlar}).")
    return machines[0].id


def hours_minutes_to_decimal(hours: int, minutes: int) -> float:
    """
    Orca Slicer Baskı Süresi Dönüşüm Kuralı:
    Saat + (Dakika / 60) -> Ondalıklı Saat (Örn: 3 saat 55 dk -> 3.92 saat)
    """
    total_hours = float(hours) + (float(minutes) / 60.0)
    return round(total_hours, 2)


def decimal_to_hours_minutes_str(decimal_hours: float) -> str:
    """Ondalıklı saati hem '3s 55dk' hem de '3.92 saat' olarak okunaklı döndürür."""
    total_minutes = int(round(decimal_hours * 60))
    h = total_minutes // 60
    m = total_minutes % 60
    return f"{h} saat {m} dk ({decimal_hours:.2f} saat)"


def calculate_machine_hourly_cost(
    machine: Machine, parts: List[MachinePart], electricity_price_per_kwh: float
) -> Dict[str, Any]:
    """
    Saatlik Toplam Makine Gideri:
    Saatlik Ana Makine Amortismanı + Saatlik Parça Yıpranma Payları Toplamı + Saatlik Elektrik Tüketim Maliyeti
    """
    hourly_depreciation = (
        machine.purchase_price_tl / machine.lifespan_hours
        if machine.lifespan_hours > 0
        else 0.0
    )

    parts_breakdown = []
    hourly_parts_wear_total = 0.0
    warnings = []

    for part in parts:
        part_hourly = part.cost_tl / part.lifespan_hours if part.lifespan_hours > 0 else 0.0
        hourly_parts_wear_total += part_hourly
        remaining_hours = max(0.0, part.lifespan_hours - part.used_hours)
        usage_percent = (
            (part.used_hours / part.lifespan_hours) * 100.0 if part.lifespan_hours > 0 else 0.0
        )
        needs_maintenance = usage_percent >= part.warning_threshold_percent

        if needs_maintenance:
            warnings.append(
                f"{machine.name} -> {part.name}: Ömrün %{usage_percent:.1f}'i doldu (Kalan: {remaining_hours:.1f} saat)"
            )

        parts_breakdown.append(
            {
                "id": part.id,
                "name": part.name,
                "cost_tl": round(part.cost_tl, 2),
                "lifespan_hours": round(part.lifespan_hours, 2),
                "used_hours": round(part.used_hours, 2),
                "remaining_hours": round(remaining_hours, 2),
                "usage_percent": round(usage_percent, 1),
                "hourly_wear_tl": round(part_hourly, 4),
                "warning_threshold_percent": part.warning_threshold_percent,
                "needs_maintenance": needs_maintenance,
            }
        )

    hourly_electricity_cost = machine.power_consumption_kw * electricity_price_per_kwh
    total_hourly_cost = hourly_depreciation + hourly_parts_wear_total + hourly_electricity_cost

    machine_remaining_hours = max(0.0, machine.lifespan_hours - machine.total_worked_hours)
    machine_usage_percent = (
        (machine.total_worked_hours / machine.lifespan_hours) * 100.0
        if machine.lifespan_hours > 0
        else 0.0
    )

    return {
        "machine_id": machine.id,
        "machine_name": machine.name,
        "hourly_depreciation_tl": round(hourly_depreciation, 2),
        "hourly_parts_wear_tl": round(hourly_parts_wear_total, 2),
        "hourly_electricity_tl": round(hourly_electricity_cost, 2),
        "total_hourly_cost_tl": round(total_hourly_cost, 2),
        "machine_remaining_hours": round(machine_remaining_hours, 2),
        "machine_usage_percent": round(machine_usage_percent, 1),
        "parts": parts_breakdown,
        "maintenance_warnings": warnings,
    }


def calculate_pricing_from_cost(
    production_cost_tl: float,
    shipping_cost_tl: float,
    commission_rate_percent: float,
    target_margin_percent: float,
    vat_enabled: bool = False,
    vat_rate_percent: float = 20.0,
    discount_percent: float = 0.0,
    manual_sale_price_tl: Optional[float] = None,
    bank_withholding_rate_percent: float = 0.0,
) -> Dict[str, Any]:
    """
    Finansal Hesaplama Kuralları:
    1. Komisyon her zaman toplam hasılat (nihai satış fiyatı) üzerinden kesilir.
    2. Esnaf Muafiyeti (GVK 9/10) aktifse, komisyon sonrası banka hesabına aktarılan tutar
       üzerinden banka stopajı (varsayılan %4, işçi varsa %2) otomatik kesilir.
    3. KDV (açıksa) en son matrah üzerinden eklenir ve toplam tahsilatı oluşturur.
    4. Başa Baş Noktası (Break-Even): Net kârın tam 0.00 TL olduğu minimum satış fiyatıdır.
    5. Hedef Kâr Marjı (%): Satış matrahı (KDV hariç hasılat) üzerinden hedeflenen net kâr oranıdır.
       Formül:
       Etkin_Kesinti = (1 + KDV) * [Komisyon + (1 - Komisyon) * BankaStopajı]
       Matrah = (Üretim + Kargo) / (1 - Etkin_Kesinti - HedefKarMarjı)
    """
    base_cost = production_cost_tl + shipping_cost_tl
    comm_ratio = max(0.0, min(0.95, commission_rate_percent / 100.0))
    stopaj_ratio = max(0.0, min(0.50, bank_withholding_rate_percent / 100.0))
    vat_ratio = (vat_rate_percent / 100.0) if vat_enabled else 0.0
    margin_ratio = max(0.0, min(0.90, target_margin_percent / 100.0))
    disc_ratio = max(0.0, min(0.95, discount_percent / 100.0))

    # POS komisyonu S_gross üzerinden kesilir, kalan (1 - comm_ratio) * S_gross ticari hesaba yatar.
    # Banka ise hesaba yatan bu tutar üzerinden stopaj_ratio oranında stopaj keser.
    combined_deduction_on_gross = comm_ratio + ((1.0 - comm_ratio) * stopaj_ratio)
    effective_comm_on_net = (1.0 + vat_ratio) * combined_deduction_on_gross

    # 1. BAŞA BAŞ NOKTASI (Break-Even Point - Sıfır Zarar Fiyatı)
    # S_net_breakeven - (S_net_breakeven * effective_comm_on_net) - base_cost = 0
    be_denominator = max(0.05, 1.0 - effective_comm_on_net)
    break_even_net_tl = base_cost / be_denominator
    break_even_vat_tl = break_even_net_tl * vat_ratio
    break_even_gross_tl = break_even_net_tl + break_even_vat_tl
    break_even_commission_tl = break_even_gross_tl * comm_ratio
    break_even_bank_incoming_tl = break_even_gross_tl - break_even_commission_tl
    break_even_stopaj_tl = break_even_bank_incoming_tl * stopaj_ratio

    # 2. HEDEF KÂR MARJINA GÖRE TAVSİYE EDİLEN SATIŞ FİYATI
    # Hedef: Net Kâr = S_net * margin_ratio
    # S_net * (1 - effective_comm_on_net - margin_ratio) = base_cost
    target_denominator = 1.0 - effective_comm_on_net - margin_ratio
    if target_denominator <= 0.03:
        # Çok yüksek marj girildiyse güvenli sınır
        target_denominator = 0.03

    recommended_net_before_discount = base_cost / target_denominator
    recommended_gross_before_discount = recommended_net_before_discount * (1.0 + vat_ratio)

    # İskonto veya Manuel Fiyat Uygulaması
    if manual_sale_price_tl is not None and manual_sale_price_tl > 0:
        final_gross_tl = manual_sale_price_tl
        final_net_tl = final_gross_tl / (1.0 + vat_ratio)
        final_vat_tl = final_gross_tl - final_net_tl
    else:
        final_net_tl = recommended_net_before_discount * (1.0 - disc_ratio)
        final_vat_tl = final_net_tl * vat_ratio
        final_gross_tl = final_net_tl + final_vat_tl

    # Gerçekleşen Komisyon, Bankaya Gelen, Banka Stopajı, Net Nakit ve Net Kâr
    commission_amount_tl = final_gross_tl * comm_ratio
    bank_incoming_tl = final_gross_tl - commission_amount_tl
    bank_withholding_amount_tl = bank_incoming_tl * stopaj_ratio
    bank_net_payout_tl = bank_incoming_tl - bank_withholding_amount_tl
    net_profit_tl = final_net_tl - commission_amount_tl - bank_withholding_amount_tl - base_cost
    actual_margin_percent = (net_profit_tl / final_net_tl * 100.0) if final_net_tl > 0 else 0.0
    roi_markup_percent = (net_profit_tl / base_cost * 100.0) if base_cost > 0 else 0.0

    return {
        "production_cost_tl": round(production_cost_tl, 2),
        "shipping_cost_tl": round(shipping_cost_tl, 2),
        "total_base_cost_tl": round(base_cost, 2),
        "vat_enabled": vat_enabled,
        "vat_rate_percent": round(vat_rate_percent if vat_enabled else 0.0, 2),
        "commission_rate_percent": round(commission_rate_percent, 2),
        "bank_withholding_rate_percent": round(bank_withholding_rate_percent, 2),
        "target_margin_percent": round(target_margin_percent, 2),
        "discount_percent": round(discount_percent, 2),
        # Başa Baş Noktası (Sıfır Zarar)
        "break_even_net_tl": round(break_even_net_tl, 2),
        "break_even_vat_tl": round(break_even_vat_tl, 2),
        "break_even_gross_tl": round(break_even_gross_tl, 2),
        "break_even_commission_tl": round(break_even_commission_tl, 2),
        "break_even_stopaj_tl": round(break_even_stopaj_tl, 2),
        # İskontosuz Tavsiye Fiyat
        "recommended_net_tl": round(recommended_net_before_discount, 2),
        "recommended_gross_tl": round(recommended_gross_before_discount, 2),
        # Nihai Satış Fiyatı (Matrah + KDV = Toplam Hasılat)
        "final_net_matrah_tl": round(final_net_tl, 2),
        "final_vat_tl": round(final_vat_tl, 2),
        "final_sale_price_gross_tl": round(final_gross_tl, 2),
        "commission_amount_tl": round(commission_amount_tl, 2),
        "bank_incoming_tl": round(bank_incoming_tl, 2),
        "bank_withholding_amount_tl": round(bank_withholding_amount_tl, 2),
        "bank_net_payout_tl": round(bank_net_payout_tl, 2),
        "net_profit_tl": round(net_profit_tl, 2),
        "actual_margin_percent": round(actual_margin_percent, 1),
        "roi_markup_percent": round(roi_markup_percent, 1),
        "is_below_break_even": round(final_gross_tl, 2) < round(break_even_gross_tl, 2),
    }


def kargo_esikli_fiyat(esik_tl: float, kargo_tl: float, **fiyat_alanlari) -> Dict[str, Any]:
    """Sipariş tutarı eşiğin altındaysa kargoyu müşteri öder, satıcıya maliyeti yoktur; eşik ve üstünde satıcı öder."""
    musteri_oder = calculate_pricing_from_cost(shipping_cost_tl=0.0, **fiyat_alanlari)
    if esik_tl > 0 and musteri_oder["final_sale_price_gross_tl"] < esik_tl:
        return musteri_oder
    return calculate_pricing_from_cost(shipping_cost_tl=kargo_tl, **fiyat_alanlari)


def calculate_product_bom_cost(
    product: Product,
    settings: GlobalSettings,
    machine: Optional[Machine],
    machine_parts: List[MachinePart],
    filaments_map: Dict[int, Filament],
    materials_map: Dict[int, AuxiliaryMaterial],
    shipping_cost_tl: float,
    groups_map: Optional[Dict[int, MaterialGroup]] = None,
) -> Dict[str, Any]:
    """
    Ürünün tam reçete (BOM) maliyetini, ondalıklı baskı süresini ve B2C / B2B fiyatlarını hesaplar.
    Fire oranı malzemenin kendisinde değil, doğrudan BOM kalemlerindedir!
    """
    decimal_hours = hours_minutes_to_decimal(
        product.print_time_hours_int, product.print_time_minutes_int
    )

    # 1. Makine Saatlik Gideri x Ondalıklı Baskı Saati
    machine_hourly_info = None
    machine_hourly_rate = 0.0
    if machine:
        machine_hourly_info = calculate_machine_hourly_cost(
            machine, machine_parts, settings.electricity_price_per_kwh
        )
        machine_hourly_rate = machine_hourly_info["total_hourly_cost_tl"]

    total_machine_print_cost = machine_hourly_rate * decimal_hours

    # 2. Filament BOM Maliyeti (BOM üzerindeki Fire Oranı ile)
    filament_breakdown = []
    total_filament_cost = 0.0
    total_filament_net_gr = 0.0
    total_filament_with_waste_gr = 0.0

    for f_item in product.filament_items:
        fil = filaments_map.get(f_item.filament_id)
        if not fil:
            continue
        effective_gr = f_item.net_weight_gr * (1.0 + (f_item.waste_rate_percent / 100.0))
        cost_per_gr = fil.price_per_kg_tl / 1000.0
        item_cost = effective_gr * cost_per_gr

        total_filament_net_gr += f_item.net_weight_gr
        total_filament_with_waste_gr += effective_gr
        total_filament_cost += item_cost

        filament_breakdown.append(
            {
                "bom_item_id": f_item.id,
                "filament_id": fil.id,
                "filament_name": f"{fil.brand} {fil.material_type} - {fil.color_name}",
                "color_hex": fil.color_hex,
                "net_weight_gr": round(f_item.net_weight_gr, 2),
                "waste_rate_percent": round(f_item.waste_rate_percent, 1),
                "effective_weight_gr": round(effective_gr, 2),
                "cost_per_gram_tl": round(cost_per_gr, 4),
                "total_cost_tl": round(item_cost, 2),
            }
        )

    # 3. Ek Bileşen & Sarf Malzeme BOM Maliyeti (BOM üzerindeki Fire Oranı ile)
    material_breakdown = []
    total_material_cost = 0.0

    for m_item in product.material_items:
        mat = materials_map.get(m_item.material_id)
        if not mat:
            continue
        effective_qty = m_item.quantity * (1.0 + (m_item.waste_rate_percent / 100.0))
        item_cost = effective_qty * mat.unit_cost_tl
        total_material_cost += item_cost

        material_breakdown.append(
            {
                "bom_item_id": m_item.id,
                "material_id": mat.id,
                "material_name": mat.name,
                "category": mat.category,
                "unit": mat.unit,
                "quantity": round(m_item.quantity, 2),
                "waste_rate_percent": round(m_item.waste_rate_percent, 1),
                "effective_quantity": round(effective_qty, 2),
                "unit_cost_tl": round(mat.unit_cost_tl, 2),
                "total_cost_tl": round(item_cost, 2),
            }
        )

    # 3b. Ambalaj / Malzeme Grubu (Alt BOM) Maliyeti
    group_breakdown = []
    total_group_cost = 0.0
    if groups_map:
        for g_item in getattr(product, "group_items", []) or []:
            grp = groups_map.get(g_item.group_id)
            if not grp:
                continue
            grp_unit_cost = 0.0
            child_items_info = []
            for child in grp.items:
                c_mat = materials_map.get(child.material_id)
                if not c_mat:
                    continue
                c_eff = child.quantity * (1.0 + (child.waste_rate_percent / 100.0))
                c_cost = c_eff * c_mat.unit_cost_tl
                grp_unit_cost += c_cost
                child_items_info.append(
                    {
                        "material_id": c_mat.id,
                        "material_name": c_mat.name,
                        "unit": c_mat.unit,
                        "quantity": round(child.quantity, 2),
                        "unit_cost_tl": round(c_mat.unit_cost_tl, 2),
                        "line_cost_tl": round(c_cost, 2),
                    }
                )
            grp_total_cost = grp_unit_cost * g_item.quantity
            total_group_cost += grp_total_cost
            total_material_cost += grp_total_cost
            group_breakdown.append(
                {
                    "bom_group_id": g_item.id,
                    "group_id": grp.id,
                    "group_code": grp.code,
                    "group_name": grp.name,
                    "quantity": round(g_item.quantity, 2),
                    "unit_cost_tl": round(grp_unit_cost, 2),
                    "total_cost_tl": round(grp_total_cost, 2),
                    "items": child_items_info,
                }
            )

    # Toplam Net Üretim Maliyeti
    production_cost_tl = total_machine_print_cost + total_filament_cost + total_material_cost
    effective_shipping = (
        product.custom_shipping_cost_tl
        if product.custom_shipping_cost_tl is not None
        else shipping_cost_tl
    )
    effective_stopaj = (
        settings.bank_withholding_rate_percent
        if getattr(settings, "esnaf_muafiyeti_enabled", False)
        else 0.0
    )

    # B2C Fiyatlandırma (ürüne özel kargo girildiyse eşik uygulanmaz)
    b2c_pricing = kargo_esikli_fiyat(
        esik_tl=0.0 if product.custom_shipping_cost_tl is not None else settings.kargo_esigi_tl,
        kargo_tl=effective_shipping,
        production_cost_tl=production_cost_tl,
        commission_rate_percent=settings.commission_rate_percent,
        target_margin_percent=product.target_margin_percent,
        vat_enabled=product.vat_enabled,
        vat_rate_percent=product.vat_rate_percent,
        manual_sale_price_tl=product.manual_sale_price_tl,
        bank_withholding_rate_percent=effective_stopaj,
    )

    # B2B Kademeli Fiyatlandırma (10+, 25+, 50+ Adet Simülasyonu)
    b2b_base_pricing = calculate_pricing_from_cost(
        production_cost_tl=production_cost_tl,
        shipping_cost_tl=effective_shipping / 10.0,  # Toplu gönderimde birim başına düşen kargo
        commission_rate_percent=settings.commission_rate_percent,
        target_margin_percent=product.b2b_margin_percent,
        vat_enabled=product.vat_enabled,
        vat_rate_percent=product.vat_rate_percent,
        bank_withholding_rate_percent=effective_stopaj,
    )

    return {
        "product_id": product.id,
        "sku": product.sku,
        "name": product.name,
        "category": product.category,
        "variant_name": product.variant_name,
        "dimensions_mm": {
            "x": product.size_x_mm,
            "y": product.size_y_mm,
            "z": product.size_z_mm,
            "formatted": f"{product.size_x_mm:.0f} x {product.size_y_mm:.0f} x {product.size_z_mm:.0f} mm",
        },
        "print_time": {
            "hours": product.print_time_hours_int,
            "minutes": product.print_time_minutes_int,
            "decimal_hours": decimal_hours,
            "formatted": decimal_to_hours_minutes_str(decimal_hours),
        },
        "machine": {
            "id": machine.id if machine else None,
            "name": machine.name if machine else "Makine Seçilmedi",
            "hourly_rate_tl": round(machine_hourly_rate, 2),
            "print_machine_cost_tl": round(total_machine_print_cost, 2),
        },
        "filaments": filament_breakdown,
        "total_filament_net_gr": round(total_filament_net_gr, 2),
        "total_filament_with_waste_gr": round(total_filament_with_waste_gr, 2),
        "total_filament_cost_tl": round(total_filament_cost, 2),
        "materials": material_breakdown,
        "material_groups": group_breakdown,
        "total_group_cost_tl": round(total_group_cost, 2),
        "total_material_cost_tl": round(total_material_cost, 2),
        "production_cost_tl": round(production_cost_tl, 2),
        "shipping_cost_tl": b2c_pricing["shipping_cost_tl"],
        "kargo_bedeli_tl": round(effective_shipping, 2),
        "b2c_pricing": b2c_pricing,
        "b2b_unit_pricing_10plus": b2b_base_pricing,
    }
