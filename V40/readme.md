# Gateway Protokol IEC 61850 ke IEC 60870-5-104 - Versi 40

Selamat datang di versi 40 dari proyek Gateway Protokol! Versi ini merupakan rilis final yang menggabungkan serangkaian inovasi signifikan untuk meningkatkan keandalan, pengalaman pengguna, dan kemudahan dalam melakukan *troubleshooting*.

Gateway ini berfungsi sebagai jembatan antara perangkat IED (Intelligent Electronic Device) yang menggunakan protokol **IEC 61850** dengan sistem SCADA (Supervisory Control and Data Acquisition) yang berjalan di atas protokol **IEC 60870-5-104**.

## 🚀 Tabel Fitur Baru v40

| Fitur | Deskripsi Singkat | File Terkait | Manfaat Utama |
| :--- | :--- | :--- | :--- |
| **Arsitektur Asynchronous Penuh** | Gateway sepenuhnya berjalan di atas `asyncio`, membuatnya non-blocking dan sangat efisien. | `v40.py` | Performa tinggi, tidak ada IED lambat yang menghambat IED lain, skalabilitas. |
| **UI Monitoring Real-Time** | Antarmuka web modern dengan WebSocket untuk pembaruan data instan tanpa perlu refresh. | `v40.py` | Visualisasi status koneksi, nilai, dan sinyal secara langsung dan interaktif. |
| **Validasi Konfigurasi** | Saat startup, program memvalidasi alamat di `config.local.ini` terhadap model data IED. | `v40.py` | Mendeteksi kesalahan pengetikan alamat lebih awal, mempercepat *troubleshooting*. |
| **Caching Model IED Cerdas** | Proses *discovery* yang lama hanya dilakukan sekali, lalu disimpan ke `ied_model_cache.json`. | `libiec61850client_cached.py` | Waktu startup program menjadi super cepat, dari menit menjadi beberapa detik saja. |
| **Indikator Progres Discovery** | Menampilkan progres *discovery* per *Logical Device* (LD) dan *Logical Node* (LN) di CLI. | `libiec61850client_cached.py` | Memberikan umpan balik visual saat proses *discovery* yang lama sedang berjalan. |
| **Shutdown yang Andal** | Program dapat dimatikan dengan aman dan bersih menggunakan `Ctrl+C` satu kali. | `v40.py` | Mencegah data korup dan memastikan semua koneksi ditutup dengan benar. |

## Cara Menjalankan

1.  Pastikan semua dependensi Python telah terpasang.
2.  Sesuaikan pemetaan data Anda di dalam file `config.local.ini`.
3.  Jalankan skrip utama dari terminal:
    ```bash
    python3 v40.py
    ```
4.  Buka browser dan akses `http://localhost:8000` untuk melihat antarmuka monitoring.
