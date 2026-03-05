# Gateway Monitor Pro - V101 Series (Professional Edition)

Repository ini berisi evolusi kode Gateway yang menghubungkan protokol **IEC 61850 (Client)** ke **IEC 60870-5-104 (Server)**. Seri V101 berfokus pada pembaruan Antarmuka Pengguna (UI) bertema **Carbon Dark**, stabilitas sinkronisasi data, dan presisi waktu milidetik.

## 🚀 Fitur Utama
- **Protokol:** Konversi data MMS (IEC 61850) ke IOA (IEC 104).
- **UI Carbon Edition:** Tampilan dashboard modern berbasis web (HTML5/AIOHTTP) dengan tema gelap.
- **WebSocket Real-time:** Pembaruan data instan tanpa perlu refresh halaman.
- **Status Mapping:** Deteksi otomatis tipe data SPI (Single), DPI (Double), dan MEAS (Measurement).
- **Invalidation Logic:** Secara otomatis mengirim status `INVALID` ke SCADA 104 jika koneksi ke IED terputus.
- **Logging Ganda:** Pemisahan antara log proses sistem (`process.txt`) dan log kejadian sinyal (`events.txt`).

## 📂 Daftar Versi & Perubahan (Changelog)

| Versi | Nama File | Fokus Pembaruan |
| :--- | :--- | :--- |
| **V101.0** | `v101.py` | Rilis UI Carbon awal, perbaikan sorting tabel, dan pembersihan nilai angka (Clean Values). |
| **V101.1** | `v101.1.py` | Penambahan fitur **Dot Lamp** (indikator lampu status) dan pemetaan deskripsi status (Open/Close/Appear). |
| **V101.3** | `v101.3.py` | Penambahan **Mode Tracking** untuk membedakan data yang didapat via *Polling* atau *Report*. |
| **V101.4** | `v101.4.py` | **Fix Time Update:** Perbaikan sinkronisasi waktu pada tabel IED agar diperbarui secara real-time. |
| **V101.5** | `v101.5.py` | Penambahan **System Heartbeat** dengan jam sistem presisi milidetik (ms) di header UI. |
| **V101.6** | `v101.6.py` | Refinemen pada monitoring milidetik dan optimalisasi beban WebSocket. |
| **V101.7** | `v101.7.py` | **Filtered Logs:** Fitur untuk mematikan log sampah dari library vendor dan hanya mencatat sinyal yang terdaftar di konfigurasi. |

## 🛠 Persyaratan Sistem
Pastikan library berikut sudah terpasang di environment Python Anda:
- `websockets`
- `aiohttp`
- `configparser`
- Library Vendor: `libiec61850client_cached`, `libiec60870server`, `lib60870`, `lib61850`.

## ⚙️ Konfigurasi
Gateway ini menggunakan file `config.local.ini` untuk pemetaan sinyal. Contoh format:
```ini
[doublepointinformation]
1001 = Nama_Sinyal : iec61850://192.168.1.10:102/IEDName/LLN0$ST$Loc$stVal
