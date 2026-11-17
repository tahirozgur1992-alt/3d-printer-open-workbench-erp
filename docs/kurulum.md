# Kurulum (temiz Ubuntu)

Ubuntu 24.04 (Python 3.12) ve Python 3.13 / 3.14 ile denendi. Python 3.10+ olan her Linux'ta
çalışmalıdır. Sistem paketleri dışında root gerekmez.

> English: [install.md](install.md)

## 1. Sistem paketleri

```bash
sudo apt update
sudo apt install -y git python3 python3-venv curl
```

İsteğe bağlı: [uv](https://docs.astral.sh/uv/) — varsa `install.sh` onu kullanır (daha hızlı).

## 2. Kodu alın ve kurun

```bash
cd ~
git clone <depo adresi> 3d-printer-open-workbench-erp
cd 3d-printer-open-workbench-erp
./install.sh --test
```

`install.sh` tekrar çalıştırılabilir (her `git pull` sonrası da çalıştırın):

1. `.venv/` oluşturur (varsa yeniden kullanır),
2. `requirements.lock` içindeki sabit sürümlü bağımlılıkları kurar (`--dev`/`--test` pytest ekler),
3. `.env.example` → `.env` kopyalar (var olan `.env`'e dokunmaz),
4. `data/` klasörünü ve veritabanını (`data/erp.db`) açar; sürüm yükseltmede eksik sütunları
   ekler, veriyi korur,
5. `--test` ile testleri çalıştırır (`39 passed` beklenir).

## 3. Çalıştırma

```bash
./run.sh                 # geliştirme: kod değişince yeniden yüklenir
./run.sh --no-reload     # sabit çalışma
```

Arayüz: <http://127.0.0.1:8000>, API: <http://127.0.0.1:8000/docs>.
Adres/port `.env` içinden (`WORKBENCH_ERP_HOST`, `WORKBENCH_ERP_PORT`). Önüne kimlik doğrulamalı bir
ters vekil koymadıkça `127.0.0.1`'de bırakın: **API'de oturum açma yoktur**.

## 4. systemd kullanıcı servisi

```bash
./install.sh --service
systemctl --user status workbench-erp
journalctl --user -u workbench-erp -f      # kayıtlar
sudo loginctl enable-linger "$USER"         # oturum kapanınca da çalışsın / açılışta başlasın
```

Birim dosyası `deploy/workbench-erp.service.in` şablonundan
`~/.config/systemd/user/workbench-erp.service` olarak yazılır ve depo klasöründeki `.env`'i okur.
`.env` değişince: `systemctl --user restart workbench-erp`.

## 5. İlk ayarlar

Yeni veritabanında yalnızca boş bir ayar satırı vardır: ERP fiyat uydurmaz. Gerçek değerlerinizi
arayüzden (Ayarlar, Makineler, Filamentler sekmeleri) veya API ile girin. Aşağıdaki sayılar
yalnızca örnektir.

**Ayarlar** (kWh elektrik fiyatı, mağaza/sanal POS komisyonu, KDV, marjlar, varsayılan kargo):

```bash
curl -X PUT http://127.0.0.1:8000/api/settings -H 'Content-Type: application/json' -d '{
  "brand_name": "Atölyem",
  "electricity_price_per_kwh": 5.0,
  "commission_rate_percent": 3.0,
  "default_vat_rate_percent": 20.0,
  "default_vat_enabled": false,
  "default_target_margin_percent": 50.0,
  "default_b2b_margin_percent": 35.0,
  "default_shipping_cost": 100.0,
  "kargo_esigi_tl": 0
}'
```

**Makine ekleme** (alış fiyatı, saat cinsinden ömür, ortalama güç kW):

```bash
curl -X POST http://127.0.0.1:8000/api/machines -H 'Content-Type: application/json' -d '{
  "name": "Yazıcı #1", "brand_model": "Elegoo Centauri Carbon",
  "purchase_price_tl": 30000, "lifespan_hours": 5000, "power_consumption_kw": 0.15
}'
```

**Filament makarası ekleme:**

```bash
curl -X POST http://127.0.0.1:8000/api/filaments -H 'Content-Type: application/json' -d '{
  "code": "FIL-PLA-SYH", "brand": "Örnek Marka", "material_type": "PLA",
  "color_name": "Siyah", "color_hex": "#111111", "price_per_kg_tl": 700,
  "spool_weight_gr": 1000, "remaining_weight_gr": 1000
}'
```

**Kargo baremi ekleme** (desi = En×Boy×Yükseklik cm / 3000):

```bash
curl -X POST http://127.0.0.1:8000/api/shipping-rates -H 'Content-Type: application/json' -d '{
  "tier_name": "Küçük paket (0-2 desi)", "min_desi": 0, "max_desi": 2, "cost_tl": 80
}'
```

Ardından ürün açın: Ürünler sekmesi → dilimlenmiş `.gcode` / `.gcode.3mf` yükleyin; süre, gram ve
ölçü dosyadan okunur.

### Claude ile kullanım (MCP)

```bash
claude mcp add workbench-erp -- "$HOME/3d-printer-open-workbench-erp/.venv/bin/python" \
  "$HOME/3d-printer-open-workbench-erp/mcp_server.py"
claude mcp list            # workbench-erp bağlı görünmeli
```

Claude'a *"ERP fiyat hesabına hazır mı?"* diye sorun — `hazirlik_durumu` aracı onaylamanız gereken
her değeri listeler; Claude size sorar ve `ayar_guncelle` ile kaydeder. Yalnız arayüzden kaydedilen
değerler MCP fiyat hesabı için "onaylı" sayılmaz; bir kez Claude üzerinden onaylayın.

### İsteğe bağlı: MCP ile OrcaSlicer dilimleme

`dilimle` aracı OrcaSlicer komut satırını çalıştırır. Profilleri `WORKBENCH_ORCA_PROFILES`
klasörüne koyun (varsayılan `./examples/orca`):

```
examples/orca/machine/<WORKBENCH_ORCA_MACHINE>.json      # OrcaSlicer'dan dışa aktarın (depoda yok)
examples/orca/filament/<WORKBENCH_ORCA_FILAMENT>.json    # OrcaSlicer'dan dışa aktarın (depoda yok)
examples/orca/process/Workbench Standart 0.20 Utulu.json # örnekler depoda
```

OrcaSlicer'da yazıcı / filament / proses seçip profili dışa aktarın (veya JSON'u
`~/.config/OrcaSlicer/user/` klasöründen kopyalayın). Bir sistem ön ayarını `inherits` eden
profilleri tüm anahtarları içerecek şekilde düzleştirin. `orcaslicer` PATH'te değilse `ORCA_BIN`
ayarlayın.

## Yedekleme

Her şey tek dosyadadır: `data/erp.db` (veya `WORKBENCH_ERP_DB`). Servis boştayken kopyalayın ya da
`sqlite3 data/erp.db ".backup data/erp-yedek.db"` kullanın. Onaylı G-code kopyaları veritabanının
yanındaki `data/gcode/` klasöründedir.

## Sorun giderme

| Belirti | Çözüm |
|---|---|
| `python3 -m venv` hata veriyor / `ensurepip is not available` | `sudo apt install python3-venv`, `.venv/` silin, `./install.sh` tekrar |
| 8000 portunda `Address already in use` | `.env` içinde `WORKBENCH_ERP_PORT=8010` yapın veya diğer süreci durdurun (`ss -ltnp | grep 8000`) |
| Arayüzde fiyat yok / MCP ayar eksik diyor | Ayarları doldurun, makine, filament ve kargo baremi ekleyin (bölüm 5); ayarları Claude ile onaylayın |
| G-code onayı reddedildi: "G-code başlığındaki yazıcı … değil" | G-code başka yazıcı adı taşıyor. `WORKBENCH_PRINTER_MODEL`'i yazıcı adınızın bir parçası yapın, yeniden başlatın |
| "G-code'dan süre ya da gram okunamadı" | OrcaSlicer'dan dilimlenmiş dosya (`.gcode` veya `.gcode.3mf`) verin, düz proje `.3mf` değil |
| `dilimle`: "Profil yok" | `WORKBENCH_ORCA_PROFILES/{machine,process,filament}` içinde JSON eksik; adları `orca_profilleri` aracıyla görün |
| Servis başlamıyor | `journalctl --user -u workbench-erp -e`; `.venv/` var mı (`./install.sh`) |
| Oturum kapanınca servis duruyor | `sudo loginctl enable-linger "$USER"` |
| Yanlış veritabanı kullanılıyor | Göreli `WORKBENCH_ERP_DB` depo klasörüne göre çözülür; açık olmak için mutlak yol verin |
| MCP bağlanmıyor | `claude mcp add` içinde mutlak yol kullanın; `.venv/bin/python mcp_server.py` ile deneyin (stdin bekler, Ctrl+C ile çıkın) |
