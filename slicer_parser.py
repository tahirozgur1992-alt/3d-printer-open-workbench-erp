import re
import zipfile
import io
import base64
from typing import Dict, Any, Tuple, List
from cost_engine import hours_minutes_to_decimal


def parse_duration_string(duration_str: str) -> Tuple[int, int, float]:
    """
    Orca Slicer G-Code içindeki süre metnini (Örn: '1d 2h 15m 30s', '3h 55m 12s', '48m 19s')
    (saat_int, dakika_int, ondalikli_saat) olarak ayrıştırır.
    Baskı Süresi Dönüşüm Kuralı: Saat + (Dakika / 60) -> Ondalıklı Saat (Örn: 3s 55dk = 3.92 saat)
    """
    text = duration_str.lower().strip()
    days = 0
    hours = 0
    minutes = 0
    seconds = 0

    d_match = re.search(r"(\d+)\s*(?:d|gün|gun)", text)
    h_match = re.search(r"(\d+)\s*(?:h|saat|sa)", text)
    m_match = re.search(r"(\d+)\s*(?:m|dk|dakika|min)", text)
    s_match = re.search(r"(\d+)\s*(?:s|sn|sec|saniye)(?![a-z])", text)

    if d_match:
        days = int(d_match.group(1))
    if h_match:
        hours = int(h_match.group(1))
    if m_match:
        minutes = int(m_match.group(1))
    if s_match:
        seconds = int(s_match.group(1))

    total_hours = days * 24 + hours
    if seconds >= 30:
        minutes += 1
    if minutes >= 60:
        total_hours += minutes // 60
        minutes = minutes % 60

    decimal_h = hours_minutes_to_decimal(total_hours, minutes)
    return total_hours, minutes, decimal_h


def parse_gcode_content(gcode_text: str) -> Dict[str, Any]:
    """
    Orca Slicer G-Code (.gcode) içerisinden BOM için gerekli tüm verileri %100 hassasiyetle çeker:
    - Baskı süresi (saat, dakika ve Saat + Dakika/60 ondalıklı saat)
    - Toplam ve renk bazlı filament gramajı (gr)
    - Gerçek model X, Y, Z (mm) boyutları (Purge line / başlangıç çizgisini hariç tutar)
    - Katman yüksekliği (mm), Dolgu oranı (%), Duvar sayısı, Filament türü
    - Gömülü 3D Thumbnail (Base64 PNG)
    """
    result: Dict[str, Any] = {
        "hours": 0,
        "minutes": 0,
        "decimal_hours": 0.0,
        "filament_grams": 0.0,
        "filament_grams_per_extruder": [],
        "filament_type_hint": None,
        "size_x_mm": 0.0,
        "size_y_mm": 0.0,
        "size_z_mm": 0.0,
        "layer_height_mm": 0.20,
        "infill_percent": 15.0,
        "wall_loops": 2,
        "thumbnail_base64": None,
        "estimation_mode": "EXACT_GCODE",
    }

    # 1. Gömülü 3D Thumbnail (Orca Slicer HEADER_BLOCK / THUMBNAIL_BLOCK)
    thumb_matches = re.findall(
        r";\s*thumbnail begin \d+x\d+ \d+\r?\n(.*?);\s*thumbnail end",
        gcode_text,
        re.DOTALL | re.IGNORECASE,
    )
    if thumb_matches:
        raw_b64 = thumb_matches[-1]
        clean_b64 = "".join(
            line.strip().lstrip(";").strip() for line in raw_b64.splitlines()
        )
        if clean_b64:
            result["thumbnail_base64"] = f"data:image/png;base64,{clean_b64}"

    # 2. Baskı Süresi (Orca Slicer hem üstte model printing time hem altta estimated printing time verir)
    time_patterns = [
        r";\s*total estimated time\s*[:=]\s*([^\r\n;]+)",
        r";\s*estimated printing time \(normal mode\)\s*[:=]\s*([^\r\n;]+)",
        r";\s*model printing time\s*[:=]\s*([^\r\n;]+)",
    ]
    for pat in time_patterns:
        tm = re.search(pat, gcode_text, re.IGNORECASE)
        if tm:
            h, m, dec = parse_duration_string(tm.group(1))
            if dec > 0 or h > 0 or m > 0:
                result["hours"] = h
                result["minutes"] = m
                result["decimal_hours"] = dec
                break

    # 3. Filament Gramajı (; total filament used [g] = 142.35 veya ; filament used [g] = 120.5, 21.8)
    total_gram_match = re.search(
        r";\s*total filament used \[g\]\s*[:=]\s*([^\r\n;]+)",
        gcode_text,
        re.IGNORECASE,
    )
    per_ext_match = re.search(
        r";\s*filament used \[g\]\s*[:=]\s*([^\r\n;]+)",
        gcode_text,
        re.IGNORECASE,
    )

    if per_ext_match:
        ext_vals = [
            round(float(x.strip()), 2)
            for x in per_ext_match.group(1).split(",")
            if re.match(r"^\d+(?:\.\d+)?$", x.strip()) and float(x.strip()) > 0
        ]
        if ext_vals:
            result["filament_grams_per_extruder"] = ext_vals
            result["filament_grams"] = round(sum(ext_vals), 2)

    if total_gram_match and result["filament_grams"] == 0.0:
        t_vals = [
            float(x.strip())
            for x in total_gram_match.group(1).split(",")
            if re.match(r"^\d+(?:\.\d+)?$", x.strip())
        ]
        if t_vals:
            result["filament_grams"] = round(sum(t_vals), 2)

    # 4. Z Yüksekliği, Katman Yüksekliği, Dolgu Oranı, Duvar Sayısı, Filament Türü
    max_z_match = re.search(
        r";\s*max_z_height\s*[:=]\s*(\d+(?:\.\d+)?)", gcode_text, re.IGNORECASE
    )
    if max_z_match:
        result["size_z_mm"] = round(float(max_z_match.group(1)), 1)

    lh_match = re.search(
        r";\s*layer_height\s*[:=]\s*(\d+(?:\.\d+)?)", gcode_text, re.IGNORECASE
    )
    if lh_match:
        result["layer_height_mm"] = float(lh_match.group(1))

    infill_match = re.search(
        r";\s*sparse_infill_density\s*[:=]\s*(\d+(?:\.\d+)?)%?",
        gcode_text,
        re.IGNORECASE,
    )
    if infill_match:
        result["infill_percent"] = float(infill_match.group(1))

    wall_match = re.search(
        r";\s*wall_loops\s*[:=]\s*(\d+)", gcode_text, re.IGNORECASE
    )
    if wall_match:
        result["wall_loops"] = int(wall_match.group(1))

    ftype_match = re.search(
        r";\s*filament_type\s*[:=]\s*([^\r\n]+)", gcode_text, re.IGNORECASE
    )
    if ftype_match:
        result["filament_type_hint"] = ftype_match.group(1).strip().split(";")[0].strip()

    # 5. Model X-Y-Z Boyutları (Orca Slicer 오브젝트/Katman ekstrüzyon hareketlerinden, purge line hariç)
    min_x = min_y = float("inf")
    max_x = max_y = float("-inf")
    max_z = 0.0
    in_model_printing = False

    coord_re = re.compile(
        r"^G[01]\s+.*?X([-+]?\d+(?:\.\d+)?)\s+Y([-+]?\d+(?:\.\d+)?).*?E([-+]?\d+(?:\.\d+)?)"
    )
    z_re = re.compile(r"^G[01]\s+.*?Z([-+]?\d+(?:\.\d+)?)")

    for line in gcode_text.splitlines():
        if line.startswith(";"):
            u = line.upper()
            if (
                "LAYER_CHANGE" in u
                or "CHANGE_LAYER" in u
                or "OBJECT_ID" in u
                or " printing object " in line
                or "TYPE:OUTER WALL" in u
                or "TYPE:INNER WALL" in u
                or "TYPE:SKIRT" in u
            ):
                in_model_printing = True
            elif "EXECUTABLE_BLOCK_END" in u or "END GCODE" in u:
                in_model_printing = False
            continue

        if line.startswith(("G0 ", "G1 ")):
            zm = z_re.match(line)
            if zm:
                zv = float(zm.group(1))
                if zv < 400.0 and zv > max_z:
                    max_z = zv

            if in_model_printing:
                cm = coord_re.match(line)
                if cm:
                    xv = float(cm.group(1))
                    yv = float(cm.group(2))
                    ev = float(cm.group(3))
                    if ev > 0:
                        if xv < min_x:
                            min_x = xv
                        if xv > max_x:
                            max_x = xv
                        if yv < min_y:
                            min_y = yv
                        if yv > max_y:
                            max_y = yv

    if min_x != float("inf"):
        result["size_x_mm"] = round(max_x - min_x, 1)
        result["size_y_mm"] = round(max_y - min_y, 1)
    if result["size_z_mm"] == 0.0 and max_z > 0:
        result["size_z_mm"] = round(max_z, 1)

    return result


def parse_3d_or_slicer_file(filename: str, data: bytes) -> Dict[str, Any]:
    """
    Orca Slicer'dan dışa aktarılan .gcode veya dilimlenmiş .gcode.3mf dosyasını okur
    ve BOM alanlarını dolduracak veriyi döndürür.
    """
    lower = filename.lower()
    if lower.endswith(".gcode"):
        parsed = parse_gcode_content(data.decode("utf-8", errors="ignore"))
        parsed["source_type"] = "GCODE"
        parsed["filename"] = filename
        return parsed
    elif lower.endswith(".3mf"):
        with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
            names = zf.namelist()
            for candidate in names:
                if candidate.lower().endswith(".gcode"):
                    gcode_text = zf.read(candidate).decode("utf-8", errors="ignore")
                    parsed = parse_gcode_content(gcode_text)
                    parsed["source_type"] = "GCODE_3MF"
                    parsed["filename"] = filename
                    return parsed
        raise ValueError(
            "Bu .3mf dosyası dilimlenmiş G-Code içermiyor. Lütfen Orca Slicer'dan 'Export G-code (.gcode)' veya 'Export plate sliced file (.gcode.3mf)' olarak çıktısını yükleyin."
        )
    else:
        raise ValueError(
            f"Lütfen Orca Slicer'dan aldığınız .gcode (veya .gcode.3mf) dosyasını yükleyin ({filename})."
        )
