# Gateway Protokol IEC 61850 ke IEC 60870-5-104 - Versi 100

Selamat datang di **Versi 100**, rilis "Centennial" dari proyek Gateway Protokol! Versi ini merupakan evolusi puncak yang menggabungkan efisiensi tinggi, stabilitas pelaporan data, dan pengalaman pemantauan yang sangat bersih.

Gateway ini berfungsi sebagai jembatan cerdas antara Intelligent Electronic Device (IED) berbasis **IEC 61850** dengan sistem SCADA atau Master Station yang menggunakan protokol **IEC 60870-5-104**.

## 🚀 Tabel Fitur Unggulan v100

| Fitur | Deskripsi Singkat | Manfaat Utama |
| :--- | :--- | :--- |
| **Change-of-State (COS) Filter** | Filter cerdas yang hanya meneruskan data ke Terminal/Web jika terjadi perubahan nilai. | Mengurangi beban CPU dan trafik bandwidth secara drastis; Log menjadi sangat informatif. |
| **Smart Throttled Polling** | Mekanisme pembatasan laju baca data (MMS Read) otomatis setiap 5 detik per data point. | Menjamin IED tidak mengalami overload trafik namun data tetap terbarui secara konsisten. |
| **Robust Reporting Engine** | Logika penanganan Report (Dataset/RCB) dengan proteksi referensi memori objek. | Menjamin pengiriman data event-driven yang sangat cepat tanpa ada risiko kehilangan data point. |
| **Arsitektur Asynchronous v2** | Pemrosesan paralel berbasis `asyncio` yang dioptimalkan untuk skalabilitas banyak IED. | Performa non-blocking; IED yang lambat tidak akan mengganggu aliran data IED lainnya. |
| **Caching & Discovery Cerdas** | Proses discovery struktur IED disimpan permanen dalam `ied_model_cache.json`. | Startup kilat dalam hitungan detik setelah discovery pertama selesai dilakukan. |
| **UI Dashboard Real-Time** | Antarmuka monitoring web modern dengan fitur *Fix Sorting* dan WebSocket. | Visualisasi status IOA, nilai IED, dan kesehatan koneksi secara terurut dan instan. |

## 🛠 Prinsip Kerja Sistem

Gateway v100 bekerja dengan skema **Priority-First Logic**:
1.  **Prioritas Utama (Reporting):** Gateway mencoba mendaftarkan diri ke *Report Control Block* (RCB) pada IED. Jika berhasil, data dikirim secara instan oleh IED hanya saat ada perubahan (*event-driven*).
2.  **Fallback Otomatis (Polling):** Jika IED menolak permintaan Reporting (misal slot RCB penuh), sistem secara otomatis beralih ke mode **Polling 5 Detik**.
3.  **Filtrasi Data:** Semua data dari kedua jalur tersebut disaring melalui *COS Filter*. Hanya perubahan nilai yang akan memicu update ke dashboard web dan log terminal.

## 💻 Cara Menjalankan

1.  **Prasyarat:**
    * Pastikan library `libiec61850` sudah terpasang.
    * Python versi 3.10 ke atas.

2.  **Konfigurasi:**
    Sesuaikan pemetaan alamat (Mapping IEC 61850 ke IOA 104) di dalam file `config.local.ini`.

3.  **Eksekusi:**
    Jalankan skrip utama dari terminal:
    ```bash
    python3 main_v100.py
    ```

4.  **Monitoring:**
    Akses dashboard visual melalui browser di alamat: `http://localhost:8000`

## 📝 Catatan Rilis v100
* **Perbaikan:** Menghilangkan error *AttributeError* pada pemanggilan daftar IED terdaftar.
* **Perbaikan:** Menstabilkan jalur *Reporting* agar tidak ada data point yang hilang saat startup.
* **Optimasi:** Log terminal kini jauh lebih bersih, hanya menampilkan baris `CHANGE` saat status IED benar-benar berubah.

---
*Dikembangkan untuk keandalan tinggi dan efisiensi monitoring sistem tenaga listrik.*
