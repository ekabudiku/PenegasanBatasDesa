# Toolkit Penegasan Batas Desa

Rilis perdana **Toolkit Batas Desa** terintegrasi untuk ArcGIS Desktop dan ArcGIS Pro. Rilis ini menyediakan aplikasi mandiri `ToolboxBatasDesa.exe` yang bertindak sebagai *installer* sekaligus *auto-updater* untuk memastikan Anda selalu terhubung dengan modul geoprocessing versi terbaru secara otomatis dari repositori GitHub.

**Modul dan Fitur Utama**
Toolkit ini menyediakan 5 alat pemrosesan yang dirancang berurutan sesuai dengan alur kerja penegasan batas desa:

* **Generate Titik Kartometrik:** Mengekstrak Titik Kartometrik (TK) secara otomatis dari simpul pertemuan batas poligon administrasi, merapatkan celah topologi antar-poligon, dan menghitung koordinat spasialnya. Serta memberikan penamaan seusai dengan dokumen acuan.


* **Auto-Generate Nama TK Biasa:** Melakukan penamaan pada SHP TK Biasa melalui algoritma hierarki batas, deteksi persinggungan garis pantai, dan penarikan Kode Desa (KDEPUM) secara spasial dengan radius 50 cm.


* **QC Topologi & Atribut TK:** Menjalankan *Quality Control* spasial yang tervalidasi ke dalam sesi *editing* untuk memeriksa *snapping* titik ke garis batas (toleransi 5 cm), serta memastikan kebenaran penamaan KDEPUM pada TK Simpul dan TK Biasa.


* **Urutkan & Ekspor Koordinat:** Mengurutkan titik-titik kartometrik secara otomatis mengikuti orientasi putaran garis batas desa (Counter clockwise) menggunakan parameter jarak (*measure on line*), lalu mengekspor seluruh koordinat Lintang, Bujur, dan UTM ke dalam format Excel.


* **Buat Laporan Deskripsi Batas:** Membangun narasi deskripsi lokasi Titik Kartometrik secara otomatis dengan mengekstrak relasi spasial titik terhadap fitur tata guna lahan, jaringan jalan, sungai, batas desa, dan garis pantai di sekitarnya, lengkap dengan kalkulasi arah mata angin menuju Excel.



**Panduan Download, Instalasi & Pembaruan**

1. Unduh file `ToolboxBatasDesa.exe` pada halaman [release](https://github.com/ekabudiku/PenegasanBatasDesa/releases/tag/BatasDesa).
2. Simpan file tersebut di PC/Laptop Anda (direkomendasikan diletakkan di *Desktop*).
3. Klik ganda (2x) aplikasi tersebut untuk mengunduh dan mengekstrak Toolkit secara otomatis ke direktori `Documents\Batas Desa`.
4. Buka perangkat lunak ArcGIS, klik kanan pada ArcToolbox -> pilih **Add Toolbox** -> Arahkan ke file `BatasDesa.pyt` di dalam folder instalasi tersebut.
5. Untuk mendapatkan pembaruan (*update*) sistem di masa mendatang, cukup tutup ArcGIS Anda lalu jalankan kembali `ToolboxBatasDesa.exe`.
