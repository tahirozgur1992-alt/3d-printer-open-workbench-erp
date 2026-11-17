# 3D Printer Open Workbench ERP

> 3D Printer Open Workbench ailesi: [Bot + ajanlar](https://github.com/3d-printer-open-workbench/3d-printer-open-workbench-bot) · **ERP** · [Studio](https://github.com/3d-printer-open-workbench/3d-printer-open-workbench-studio)

3B baskı atölyesi için küçük, kendi bilgisayarınızda çalışan bir ERP: filamentin ve parçaların
maliyetini, her baskının G-code'a göre gerçek maliyetini, ne kadara satılması gerektiğini ve baskı
kuyruğunda ne olduğunu bilir. Tek makinede çalışır (FastAPI + SQLite); web arayüzü, REST API ve
Claude'un (Claude Code, Claude Desktop veya herhangi bir MCP istemcisi) kullanabileceği bir
**MCP sunucusu** vardır.

> English: [README.md](README.md)

## Neler yapar

| Alan | Özellikler |
|---|---|
| **Malzeme ve stok** | Filament makaraları (kg fiyatı, kalan gram, kritik stok uyarısı), yardımcı malzemeler (LED, kutu, vida…), tedarikçi/fatura no'lu alım kayıtları, malzeme grupları (setler) |
| **Makineler** | Alış fiyatı, ömür, güç → saatlik makine maliyeti; ömürlü parçalar (nozul, tabla…) ve değişim uyarısı; yuva başına yüklü filament (4'e kadar) |
| **BOM (ürün reçetesi)** | Ürün başına filament (net gram + fire %), malzeme, malzeme grubu, baskı süresi, ölçü, katman, doluluk |
| **G-code'dan maliyet** | OrcaSlicer `.gcode` / `.gcode.3mf` okur: süre, gram (ekstrüder bazlı), ölçü, katman, küçük resim. Süre kuralı: 3 sa 55 dk = 3,92 sa |
| **Fiyatlandırma** | Amortisman + elektrik + yıpranma + filament + malzeme + fire; ardından komisyon, KDV (isteğe bağlı), hedef marj, desi kargo baremleri, kargo eşiği, B2B adet iskonto baremleri |
| **Teklifler** | G-code'dan ya da elle girilen saat/gramdan B2B / özel teklif, kayıtlı |
| **Üretim** | Stoktan düşen üretim emirleri, fire/başarısız baskı kaydı, durumlu baskı kuyruğu (bekliyor → müşteri onayı → dilim → basılıyor → tamam / hata / iptal), ürün başına onaylı G-code (SHA-256 kilitli), "gözetimsiz basılabilir mi?" denetimi |
| **Dışa aktarım** | Toplu yüklemeye uygun ürün CSV'si (ör. ikas ve benzeri mağazalar), ürünün gerçek rakamlarıyla metin/pazar araştırması için "AI paketi" istemi |
| **MCP araçları** | ~30 araç: envanter özeti, filament/malzeme ekleme, alım, reçeteli ürün, teklif, G-code maliyeti, OrcaSlicer komut satırıyla dilimleme, üretim emirleri, kuyruk |

Para alanları `*_tl` adını taşır; tek bir para birimi olarak kullanabilirsiniz.

## Hızlı başlangıç

```bash
git clone <bu depo> 3d-printer-open-workbench-erp
cd 3d-printer-open-workbench-erp
./install.sh            # venv + sabit sürümlü bağımlılıklar + .env + boş veritabanı
./run.sh                # http://127.0.0.1:8000  (API belgeleri: /docs)
```

Önce bakmak mı istiyorsunuz? Örnek veriyle başlatın (açıkça "örnek" etiketli, uydurma yuvarlak
fiyatlar): `WORKBENCH_ERP_DEMO_SEED=1 WORKBENCH_ERP_DB=./data/demo.db ./run.sh`

Temiz Ubuntu'da adım adım kurulum, systemd servisi ve ilk ayarlar:
**[docs/kurulum.md](docs/kurulum.md)** (English: [docs/install.md](docs/install.md)).

## Yapılandırma

Tüm ayarlar ortam değişkenidir; `install.sh`, `.env.example` dosyasını `.env` olarak kopyalar ve
açılışta okunur (ortamda zaten tanımlı değişken önceliklidir).

| Değişken | Varsayılan | Anlamı |
|---|---|---|
| `WORKBENCH_ERP_DB` | `./data/erp.db` | SQLite veritabanı yolu |
| `WORKBENCH_COMPANY_NAME` | `Open Workbench` | Arayüzde görünen ad (Ayarlar'dan da değişir) |
| `WORKBENCH_ERP_DEMO_SEED` | `0` | `1` = boş veritabanına örnek veri yükle |
| `WORKBENCH_ERP_HOST` / `_PORT` | `127.0.0.1` / `8000` | Web sunucusu adresi |
| `WORKBENCH_PRINTER_MODEL` | `Centauri Carbon` | Onaylı G-code bu yazıcı adını içermeli |
| `WORKBENCH_MACHINE_NAME` | `Elegoo Centauri Carbon` | Varsayılan makine kartının adı |
| `WORKBENCH_ORCA_PROFILES` | `./examples/orca` | OrcaSlicer profil klasörü (`machine/`, `process/`, `filament/`) |
| `WORKBENCH_ORCA_MACHINE` / `_PROCESS` / `_FILAMENT` | `.env.example`'a bakın | Dilimleme için varsayılan profil adları |
| `ORCA_BIN` | PATH'teki `orcaslicer` | OrcaSlicer çalıştırılabilir dosyası |

Elegoo Centauri Carbon + OrcaSlicer değerleri yalnızca örnektir; kendi yazıcınızı girin.
`examples/orca/process/` örnek proses profilleri içerir; makine ve filament profilleri depoda
yoktur, kendi OrcaSlicer'ınızdan dışa aktarın (kurulum rehberine bakın).

## API özeti

Etkileşimli belge: `http://127.0.0.1:8000/docs`. Uç noktaların listesi için
[README.md → API overview](README.md#api-overview).

## MCP kurulumu (Claude Code)

```bash
claude mcp add workbench-erp -- "$PWD/.venv/bin/python" "$PWD/mcp_server.py"
# farklı veritabanı ile:
claude mcp add workbench-erp -e WORKBENCH_ERP_DB="$PWD/data/erp.db" -- "$PWD/.venv/bin/python" "$PWD/mcp_server.py"
```

Sonra Claude'a örneğin *"Stokta azalan ne var?"*, *"Kg'ı 700'den 1 kg siyah PLA ekle"*,
*"Bu G-code'un maliyetini %5 fire ve 20 ambalajla hesapla"* diyebilirsiniz. İlk fiyat hesabından
önce `hazirlik_durumu` aracı onaylamanız gereken ayarları (elektrik, komisyon, KDV, marjlar, kargo)
listeler; ERP bu değerleri asla uydurmaz.

## Birlikte çalıştığı projeler (isteğe bağlı)

- **3d-printer-open-workbench-bot** — bu ERP ile konuşan Telegram botu (stok, alım, teklif,
  baskı durumu).
- **3d-printer-open-workbench-studio** — parametrik 3B ürün üreteçleri ve baskı denetimleri;
  çıkan G-code burada maliyetlendirilip onaylanabilir.

Her proje tek başına çalışır; ERP'nin onlara bağımlılığı yoktur.

## Güvenlik

ERP yazıcıya hiçbir komut göndermez. Kuyruktaki "basılıyor" durumları yalnızca kayıttır;
`basilabilir` yalnızca koşulları denetler, hiçbir şeyi değiştirmez.

## Testler

```bash
./install.sh --test      # ya da: .venv/bin/python -m pytest -q
```

Testler geçici veritabanında çalışır, verinize dokunmaz.

## Sorumluluk reddi

Bu yazılım girdiğiniz rakamlardan maliyet ve önerilen fiyat hesaplar. **Vergi, muhasebe veya hukuk
danışmanlığı değildir.** KDV, esnaf muafiyeti ve banka stopajı gibi seçenekler yalnızca ayarlanabilir
yüzdelerdir; size uygulanan kuralları bir mali müşavirle doğrulayın.

## Lisans

MIT — [LICENSE](LICENSE).
