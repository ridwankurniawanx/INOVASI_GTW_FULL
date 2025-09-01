# Perbandingan Fitur: v20.py vs v30.py (Versi Awal)

Berikut adalah tabel yang merangkum perbedaan utama dan peningkatan fungsionalitas dari `v20.py` ke `v30.py`.

## 🚀 Tabel Perbandingan

| Fitur | Deskripsi di `v20.py` (Versi Lama) | Deskripsi di `v30.py` (Versi Baru) | Manfaat Peningkatan |
| :--- | :--- | :--- | :--- |
| **Sumber Timestamp Data** | Timestamp untuk setiap data yang masuk **selalu diambil dari waktu sistem (gateway)** saat data tersebut diproses. | Timestamp **diprioritaskan dari laporan IEC 61850 (`report timestamp`)** jika tersedia. Jika tidak ada, baru menggunakan waktu sistem sebagai cadangan. | **Akurasi Data Lebih Tinggi.** Menggunakan timestamp asli dari IED (jika didukung) memastikan bahwa waktu kejadian data lebih presisi dan sesuai dengan waktu di perangkat, bukan waktu kapan data diterima oleh gateway. |
| **Struktur Kode & Fitur Lain** | - Menggunakan `aiohttp` & `asyncio`.<br>- UI Web dengan WebSocket.<br>- Mendukung "Signal Name" dari config.| - Struktur dasar dan fitur utama sama dengan v20. | `v30` merupakan penyempurnaan yang berfokus pada kualitas data, sambil mempertahankan semua fitur solid yang sudah ada di `v20`. |

## Kesimpulan

Pembaruan dari **v20** ke **v30** adalah langkah penting untuk meningkatkan **integritas dan akurasi data**. Dengan memprioritaskan *timestamp* yang berasal langsung dari perangkat, `v30` memastikan bahwa data yang dikirim ke sistem SCADA dan ditampilkan di UI memiliki stempel waktu yang lebih dapat diandalkan, yang sangat krusial untuk analisis kejadian dan pelacakan historis.
