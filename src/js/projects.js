/* Proje verisi: scripts/build-project-pages.mjs bu dosyadan proje-*.html sayfalarını üretir. Rakam ve tarihler mevcut siteden aynen alınmıştır. */
export const projectData = {
  yamaha: {
    title: 'Yamaha Marine',
    type: 'FUAR STANDI / BOAT SHOW / İSTANBUL',
    lead: 'Ağır deniz motorları sergilemesi için tasarlanan güçlendirilmiş platform mimarisi, monolitik bankolar ve VIP ağırlama salonu.',
    heroImg: '/assets/images/projects/yamaha-gallery-2.jpg',
    heroCaption: 'Yamaha Marine — Bosphorus Boat Show Standı',
    client: 'Yamaha Motor / Burla A.Ş.',
    location: 'İstanbul Marin Fuar Alanı (Bosphorus Boat Show)',
    year: '2024',
    area: '',
    fabrication: 'Ağır Yük Podyumu & Özel Lake Bankolar',
    scope: ['Marin Stand Mimarisi', 'Ağır Yük Podyum Statik Çözümü', 'Özel Lake Karşılama Bankosu', 'Özel Işık Kurgusu', 'Saha Kurulumu'],
    pillars: [
      { step: '01 / MİMARİ KONSEPT', title: 'Marin Dinamizmi & Prestij', desc: 'Denizcilik ruhunu yansıtan akışkan çizgiler, prestijli VIP salonu ve yüksek tavanlı açık sergileme aksı.' },
      { step: '02 / ATÖLYE İMALATI', title: 'Güçlendirilmiş Platform', desc: 'Tonlarca ağırlıktaki dıştan takma motorları güvenle sergileyen özel çelik konstrüksiyon podyum imalatı.' },
      { step: '03 / SAHA TESLİMİ', title: 'Kusursuz Fuar Açılışı', desc: 'Marin fuar alanında vinç operasyonları ve platform montajı hatasız tamamlanarak anahtar teslim sunuldu.' }
    ],
    gallery: [
      { src: '/assets/images/projects/yamaha-gallery-1.jpg', caption: 'Ana Podyum ve Ağır Motor Teşhir Alanı' },
      { src: '/assets/images/projects/yamaha-gallery-3.jpg', caption: 'Özel Işıklandırmalı Ürün Kaideleri' },
      { src: '/assets/images/projects/yamaha-gallery-4.jpg', caption: 'Genel Stand Perspektifi ve VIP Alanı' }
    ]
  },
  dardanel: {
    title: 'Dardanel',
    type: 'FUAR STANDI / WORLDFOOD / İSTANBUL',
    lead: 'Canlı şef mutfağı, interaktif tadım alanları ve modern teşhir kurgusuyla tasarlanan fuar standı.',
    heroImg: '/assets/images/projects/dardanel-gallery-3.jpg',
    heroCaption: 'Dardanel — WorldFood Fuar Standı & Tadım İstasyonu',
    client: 'Dardanel Önentaş Gıda San. A.Ş.',
    location: 'TÜYAP Fuar ve Kongre Merkezi (WorldFood)',
    year: '2024',
    area: '',
    fabrication: 'Canlı Şef Mutfağı & Pleksi Teşhir Duvarları',
    scope: ['Konsept Stand Tasarımı', 'Canlı Şef Mutfak İstasyonu', 'Işıklı Pleksi Teşhir Duvarları', 'Özel Marangozluk', 'Anahtar Teslim'],
    pillars: [
      { step: '01 / MİMARİ KONSEPT', title: 'Gastronomi ve Mimari Buluşması', desc: 'Ziyaretçiyi içine çeken koku, tat ve görsel deneyimi entegre eden açık mutfak ve tadım adaları.' },
      { step: '02 / ATÖLYE İMALATI', title: 'Hijyenik ve Fonksiyonel İmalat', desc: 'Paslanmaz tezgah entegrasyonu, su ve elektrik altyapısına uygun özel üretim ahşap modüller.' },
      { step: '03 / SAHA TESLİMİ', title: 'Eksiksiz Gastronomi Standı', desc: 'Havalandırma ve pişirme donanımları test edilerek açılış saatinde tam kapasite hazır hale getirildi.' }
    ],
    gallery: [
      { src: '/assets/images/projects/dardanel-gallery-2.jpg', caption: 'Dardanel standı, karşılama bankosu ve oturma alanı' },
      { src: '/assets/images/projects/dardanel-gallery-4.jpg', caption: 'Dardanel standı, köşe görünüm' },
      { src: '/assets/images/projects/dardanel-gallery-1.jpg', caption: 'Dardanel standı, koridordan görünüm' }
    ]
  },
  mercedes: {
    title: 'Mercedes-Benz Türk',
    type: 'ETKİNLİK STANDI / OTOMOTİV / İSTANBUL',
    lead: 'Global otomotiv liderinin mühendislik ve inovasyon vizyonunu yansıtan minimalist ahşap hacimler, görüşme masaları ve akustik bölmeler.',
    heroImg: '/assets/images/projects/mercedes-benz-hero.jpg',
    heroCaption: 'Mercedes-Benz Türk — Özel Etkinlik ve Deneyim Standı',
    client: 'Mercedes-Benz Türk A.Ş.',
    location: 'İstanbul',
    year: '2023',
    area: '',
    fabrication: 'Minimalist Ahşap Karkas & Akustik Paneller',
    scope: ['Etkinlik Mimarisi', 'Kurumsal Kimlik Entegrasyonu', 'Özel Banko & Mobilya', 'Modüler Podyum', 'Hassas Montaj'],
    pillars: [
      { step: '01 / MİMARİ KONSEPT', title: 'Minimalist Lüks & Hassasiyet', desc: 'Mercedes-Benz tasarım felsefesine uygun yalın formlar, mat koyu yüzeyler ve keskin aydınlatma detayları.' },
      { step: '02 / ATÖLYE İMALATI', title: 'Özel Ahşap ve Metal İmalat', desc: 'Kendi atölyemizde üretilen özel tasarım görüşme bankoları, akustik paneller ve kusursuz yüzey kalitesi.' },
      { step: '03 / SAHA TESLİMİ', title: 'Hızlı ve Sessiz Kurulum', desc: 'Etkinlik mekanında gece vardiyasında kusursuz montaj tamamlanarak sabah lansmana hazır edildi.' }
    ],
    gallery: [
      { src: '/assets/images/projects/mercedes-benz-gallery-2.jpg', caption: 'Mercedes-Benz Türk standı, karşılama bankosu' },
      { src: '/assets/images/projects/mercedes-benz-gallery-3.jpg', caption: 'Mercedes-Benz Türk standı, yan görünüm' },
      { src: '/assets/images/projects/mercedes-benz-gallery-4.jpg', caption: 'Mercedes-Benz Türk standı, önden görünüm' }
    ]
  }
};

