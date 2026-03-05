# Gateway Monitor Pro - Evolution Series (V18 - V101.7)

Repository ini mendokumentasikan evolusi sistem Gateway yang menghubungkan protokol **IEC 61850 (Client)** ke **IEC 60870-5-104 (Server)**. Proyek ini berfokus pada kecepatan transmisi data, akurasi waktu milidetik, dan antarmuka monitoring yang profesional.

## 📈 Roadmap Perkembangan Fitur

| Versi | Folder | Fitur Utama & Perubahan |
| :--- | :--- | :--- |
| **V18 - V19** | `V18`, `V19` | **Foundation:** Implementasi `aiohttp`, pemetaan "Signal Name" dari konfigurasi, dan sistem shutdown yang aman. |
| **V20 - V30** | `V20`, `V30` | **Time Precision:** Implementasi stempel waktu (timestamp) otomatis dari Gateway (V20) dan dukungan stempel waktu asli dari IED/SOE (V30). |
| **V40 - V50** | `V40`, `V50` | **Carbon UI:** Pengenalan tema gelap (Carbon Edition), fitur Event Log terpisah, dan perbaikan logika pengurutan data terbaru. |
| **V60** | `V60` | **Smart Storage:** Fitur otomatis simpan Report Control Block (RCB) ke `reports.ini` untuk koneksi yang lebih stabil dan permanen. |
| **V70** | `V70` | **Stability:** Refinement pada library caching dan optimalisasi memori untuk penggunaan jangka panjang. |
| **V101** | `V101` | **Professional Pro:** Fitur Filtered Logs (hanya mencatat sinyal penting), Heartbeat sistem dengan presisi ms, dan Dual Mode Tracker. |

---

## 🚀 Fitur Unggulan (Versi V101.7)

Versi terbaru (`v101.7.py`) mencakup semua fitur terbaik dari versi sebelumnya dengan peningkatan signifikan pada sisi performa:

* **Filtered Logging**: Hanya mencatat sinyal yang terdaftar di konfigurasi. Log tidak lagi dibanjiri oleh pesan otomatis dari library vendor, sehingga file `process.txt` tetap bersih dan relevan.
* **System Heartbeat & High Precision Clock**: Dashboard menampilkan detak jantung sistem (❤) dan jam sistem dengan akurasi milidetik untuk memastikan Gateway beroperasi secara real-time.
* **Dual Mode Tracker**: Menandai data secara otomatis apakah didapat melalui metode *Polling* atau *Report* (spontan), membantu teknisi dalam mendiagnosa IED.
* **Professional Carbon UI**: Dashboard modern berbasis web yang ringan, responsif, dan didesain khusus untuk monitor di Control Room.
* **Invalidation Logic**: Secara otomatis mengirimkan status `INVALID` ke SCADA/Server 104 jika koneksi ke IED terputus.

---

## 🛠 Persyaratan Sistem

Pastikan library berikut tersedia di lingkungan Python Anda:
- `websockets`
- `aiohttp`
- `configparser`
- **Library Vendor:** `libiec61850client_cached`, `libiec60870server`, `lib60870`, dan `lib61850`.

## ⚙️ Cara Penggunaan

1.  **Konfigurasi**: Sesuaikan file `config.local.ini` di folder versi yang digunakan. Format alamat MMS harus menyertakan nama sinyal untuk hasil terbaik di UI:
    ```ini
    [doublepointinformation]
    1001 = Nama_Sinyal_Anda : iec61850://192.168.1.10:102/IEDName/LLN0$ST$Loc$stVal
    ```
    *Gunakan suffix `:invers=true` jika ingin membalik logika status.*

2.  **Menjalankan Gateway**:
    Pindah ke folder versi terbaru (contoh V101) dan jalankan script:
    ```bash
    python3 v101.7.py
    ```

3.  **Monitoring**:
    Buka peramban (browser) dan akses: `http://localhost:8000`

## 📊 File Output
- `process.txt`: Mencatat aktivitas sistem dan perubahan nilai sinyal yang terfilter.
- `events.txt`: Log khusus untuk kejadian sinyal biner (DPI/SPI) dengan deskripsi status (Open/Close/Appear).

---
**Developer:** Ridwan Kurniawan  
**Status:** Professional Pro Edition (V101.7)
