/**
 * PROJENIC DESIGN SYSTEM & ART DIRECTION ENGINE
 * Brandbook V1 Implementation: White/Stone/Dark/Orange Palette,
 * Hero Slider, Project Detail Modal, Live Clocks, Drawer & Estimator
 */

document.addEventListener('DOMContentLoaded', () => {
  initThemeEngine();
  initLiveWorldClocks();
  initHeroSlider();
  initContactDrawer();
  initPortfolioFilter();
  initProjectDetailModal();
  initAuroraThumbHeroSync();
  initImageLightbox();
  initBriefSubmission();
  initSmoothScroll();
});

/* ── 1. DUAL THEME ENGINE (DEFAULT: LIGHT AS SPECIFIED) ───────────────────── */
function initThemeEngine() {
  const themeToggleBtns = document.querySelectorAll('#theme-toggle, [data-theme-toggle]');
  // Default to light theme as requested in the visual design prompt
  const savedTheme = localStorage.getItem('projenic_theme') || 'light';

  function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('projenic_theme', theme);

    themeToggleBtns.forEach(btn => {
      const label = btn.querySelector('.theme-label');
      if (label) {
        label.textContent = theme === 'light' ? 'GECE' : 'GÜNDÜZ';
      } else {
        btn.textContent = theme === 'light' ? 'TEMA: GECE' : 'TEMA: GÜNDÜZ';
      }
    });

    // Dynamic logo swapping based on surface ground truth
    // Light mode -> logo-black.png (#242623) | Dark mode -> logo-white.png
    const dynamicLogos = document.querySelectorAll('img.brand-logo:not([data-static-logo]), #header-logo:not([data-static-logo]), .footer-brand-logo:not([data-static-logo]), .brand-logo:not([data-static-logo])');
    dynamicLogos.forEach(img => {
      img.src = theme === 'light' ? '/assets/images/logo-black.png' : '/assets/images/logo-white.png';
    });
  }

  applyTheme(savedTheme);

  themeToggleBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      const current = document.documentElement.getAttribute('data-theme') || 'light';
      const next = current === 'light' ? 'dark' : 'light';
      applyTheme(next);
    });
  });
}

/* ── 2. LIVE WORLD CLOCKS (ISTANBUL, DÜSSELDORF, DUBAI) ───────────────────── */
function initLiveWorldClocks() {
  const istEl = document.getElementById('clock-ist');
  const dusEl = document.getElementById('clock-dus');
  const dxbEl = document.getElementById('clock-dxb');

  function updateClocks() {
    const now = new Date();
    const formatCity = (timeZone) => {
      try {
        return new Intl.DateTimeFormat('en-GB', {
          timeZone,
          hour: '2-digit',
          minute: '2-digit',
          second: '2-digit',
          hour12: false
        }).format(now);
      } catch (e) {
        return now.toTimeString().substring(0, 8);
      }
    };

    if (istEl) istEl.textContent = formatCity('Europe/Istanbul');
    if (dusEl) dusEl.textContent = formatCity('Europe/Berlin');
    if (dxbEl) dxbEl.textContent = formatCity('Asia/Dubai');
  }

  updateClocks();
  setInterval(updateClocks, 1000);
}

/* ── 3. HERO IMAGE SLIDER (08 / WEBSITE ART DIRECTION) ────────────────────── */
function initHeroSlider() {
  const heroImage = document.getElementById('hero-slider-img');
  const heroCounter = document.getElementById('hero-slider-counter');
  const prevBtn = document.getElementById('hero-slider-prev');
  const nextBtn = document.getElementById('hero-slider-next');

  if (!heroImage || !heroCounter) return;

  const slides = [
    {
      src: '/assets/images/brand/web-direction-hero.jpg',
      tag: 'DESIGN · PRODUCE · DELIVER · TOGETHER',
      caption: 'Fikirden deneyime. Markaları fuarlarda ve mimari yapılarda tasarlar, üretir ve hayata geçiririz.'
    },
    {
      src: '/assets/images/brand/architecture-white-space.jpg',
      tag: 'SPACES FOR A BRIGHTER TOMORROW',
      caption: 'Mimari disiplin ve net tasarım hiyerarşisiyle şekillenen yapılar.'
    },
    {
      src: '/assets/images/projects/hero-architecture.jpg',
      tag: 'MARKALAR İÇİN DAHA FAZLASI MÜMKÜN',
      caption: 'Fuar ve etkinlik alanlarında insan ile marka arasında güçlü bağ kuran deneyimler.'
    },
    {
      src: '/assets/images/brand/architecture-minimal-light.jpg',
      tag: 'MİMARİ DİSİPLİN VE SAHA GÜCÜ',
      caption: 'Tasarım vizyonunu ve teknik detayları sahada birebir gerçeğe dönüştürüyoruz.'
    }
  ];

  let currentIndex = 0;

  function renderSlide(index) {
    currentIndex = (index + slides.length) % slides.length;
    heroImage.src = slides[currentIndex].src;
    heroCounter.textContent = `0${currentIndex + 1} / 0${slides.length}`;
    
    const tagEl = document.getElementById('hero-slider-tag');
    if (tagEl) tagEl.textContent = slides[currentIndex].tag;
  }

  if (prevBtn) prevBtn.addEventListener('click', () => renderSlide(currentIndex - 1));
  if (nextBtn) nextBtn.addEventListener('click', () => renderSlide(currentIndex + 1));
}

/* ── 4. SLIDE-OUT CONTACT DRAWER ──────────────────────────────────────────── */
function initContactDrawer() {
  const drawer = document.getElementById('contact-drawer');
  const backdrop = document.getElementById('drawer-backdrop');
  const openTriggers = document.querySelectorAll('[data-open-drawer]');
  const closeTriggers = document.querySelectorAll('[data-close-drawer]');

  openTriggers.forEach(btn => {
    btn.addEventListener('click', (e) => {
      if (drawer && backdrop) {
        if (e) e.preventDefault();
        drawer.classList.add('is-open');
        backdrop.classList.add('is-open');
        document.body.style.overflow = 'hidden';
      } else if (btn.tagName !== 'A' || !btn.getAttribute('href')) {
        // Fallback for subpages: navigate directly to brief intake form
        window.location.href = '/iletisim.html';
      }
    });
  });

  if (!drawer || !backdrop) return;

  function closeDrawer() {
    drawer.classList.remove('is-open');
    backdrop.classList.remove('is-open');
    document.body.style.overflow = '';
  }

  closeTriggers.forEach(btn => btn.addEventListener('click', closeDrawer));
  backdrop.addEventListener('click', closeDrawer);

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && drawer.classList.contains('is-open')) {
      closeDrawer();
    }
  });
}

/* ── 5. PORTFOLIO FILTERING ────────────────────────────────────────────────── */
function initPortfolioFilter() {
  const filterBtns = document.querySelectorAll('.portfolio-filter-btn');
  const projectCards = document.querySelectorAll('.portfolio-project-card');

  if (!filterBtns.length || !projectCards.length) return;

  filterBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      filterBtns.forEach(b => {
        b.classList.remove('border-current', 'font-bold', 'opacity-100');
        b.classList.add('opacity-50');
      });
      btn.classList.add('border-current', 'font-bold', 'opacity-100');
      btn.classList.remove('opacity-50');

      const filter = btn.dataset.filter;

      projectCards.forEach(card => {
        if (filter === 'all' || card.dataset.category === filter) {
          card.style.display = '';
          card.style.opacity = '1';
        } else {
          card.style.display = 'none';
        }
      });
    });
  });
}

/* ── 6. PROJECT DETAIL MODAL (TECHNICAL SPECIFICATIONS & SCOPE) ──────────── */
const projectData = {
  ulak: {
    title: 'ULAK Haberleşme',
    type: 'FUAR STANDI / SAVUNMA & TELEKOMÜNİKASYON / TÜYAP / 2023',
    lead: 'Türkiye’nin milli haberleşme teknolojileri kuruluşu için tasarlanan, yüksek teknoloji ve kurumsal otoriteyi yansıtan anahtar teslim IDEF fuar standı.',
    heroImg: '/assets/images/projects/ulak-haberlesme-hero.jpg',
    client: 'ULAK Haberleşme A.Ş.',
    location: 'TÜYAP Fuar ve Kongre Merkezi (IDEF 2023)',
    year: '2023',
    scope: ['Mimari Stand Tasarımı', 'Sayısal Enstalasyon Duvarları', 'Statik Taşıyıcı Konstrüksiyon', 'Lake Ahşap İmalat', 'Yerinde Montaj'],
    gallery: [
      '/assets/images/projects/ulak-haberlesme-gallery-1.jpg',
      '/assets/images/projects/ulak-haberlesme-gallery-2.jpg',
      '/assets/images/projects/ulak-haberlesme-gallery-3.jpg',
      '/assets/images/projects/ulak-haberlesme-gallery-4.jpg'
    ]
  },
  yamaha: {
    title: 'Yamaha Marine',
    type: 'FUAR STANDI / DENİZCİLİK & MOTOR / BOAT SHOW / 2024',
    lead: 'Dünya denizcilik devi Yamaha için kurgulanan; ağır deniz motorlarını sergileyen güçlendirilmiş platform mimarisi ve lüks ağırlama üniteleri.',
    heroImg: '/assets/images/projects/aurora-hero-large.jpg',
    client: 'Yamaha Motor / Burla A.Ş.',
    location: 'İstanbul Marin Fuar Alanı (Bosphorus Boat Show)',
    year: '2024',
    scope: ['Fuar Stand Mimarisi', 'Ağır Yük Platformu Statik Çözümü', 'Monolitik Lake Bankolar', 'Özel Işık Kurgusu', 'Saha Kurulumu'],
    gallery: [
      '/assets/images/projects/aurora-gallery-1.jpg',
      '/assets/images/projects/aurora-gallery-2.jpg',
      '/assets/images/projects/aurora-gallery-3.jpg',
      '/assets/images/projects/aurora-gallery-4.jpg'
    ]
  },
  dardanel: {
    title: 'Dardanel',
    type: 'FUAR STANDI / GIDA & PERAKENDE / WORLDFOOD / 2024',
    lead: 'Türkiye’nin gıda devi Dardanel için lezzet, tazelik ve çağdaş mimariyi bir araya getiren interaktif tadım, şef mutfağı ve karşılama standı.',
    heroImg: '/assets/images/projects/aurora-gallery-1.jpg',
    client: 'Dardanel Önentaş Gıda San. A.Ş.',
    location: 'TÜYAP Fuar ve Kongre Merkezi (WorldFood Istanbul)',
    year: '2024',
    scope: ['Konsept Tasarım', 'Canlı Şef Mutfak İstasyonu', 'Işıklı Pleksi Teşhir Duvarları', 'Lake Marangozluk', 'Anahtar Teslim'],
    gallery: [
      '/assets/images/projects/aurora-gallery-2.jpg',
      '/assets/images/projects/aurora-gallery-3.jpg',
      '/assets/images/projects/aurora-gallery-4.jpg',
      '/assets/images/projects/ulak-haberlesme-gallery-1.jpg'
    ]
  },
  stm: {
    title: 'STM Savunma',
    type: 'FUAR STANDI / SAVUNMA SANAYİİ / SAHA EXPO / 2023',
    lead: 'Milli savunma sanayiinin öncülerinden STM için yüksek güvenlik ve prestij standartlarına uygun olarak üretilen fuar ve teknoloji standı.',
    heroImg: '/assets/images/projects/ulak-haberlesme-gallery-1.jpg',
    client: 'STM Savunma Teknolojileri Mühendislik A.Ş.',
    location: 'İstanbul Fuar Merkezi (SAHA Expo 2023)',
    year: '2023',
    scope: ['Fuar Standı Projelendirme', 'Özel Maket Teşhir Kaideleri', 'VIP Toplantı Salonu', 'Işıklı Karkas Sistemleri', 'Saha Yönetimi'],
    gallery: [
      '/assets/images/projects/ulak-haberlesme-gallery-2.jpg',
      '/assets/images/projects/ulak-haberlesme-gallery-3.jpg',
      '/assets/images/projects/ulak-haberlesme-gallery-4.jpg',
      '/assets/images/projects/ulak-haberlesme-hero.jpg'
    ]
  },
  mercedes: {
    title: 'Mercedes-Benz Türk',
    type: 'ETKİNLİK & STAND / OTOMOTİV / İSTANBUL / 2023',
    lead: 'Global otomotiv liderinin kurum kültürünü ve mühendislik mirasını yansıtan heykelsi, minimalist insan kaynakları ve deneyim standı.',
    heroImg: '/assets/images/projects/aurora-gallery-3.jpg',
    client: 'Mercedes-Benz Türk A.Ş.',
    location: 'İstanbul',
    year: '2023',
    scope: ['Etkinlik Mimarisi', 'Kurumsal Kimlik Entegrasyonu', 'Özel Ahşap Banko & Mobilya', 'Modüler Sahne', 'Hassas Montaj'],
    gallery: [
      '/assets/images/projects/aurora-gallery-1.jpg',
      '/assets/images/projects/aurora-gallery-2.jpg',
      '/assets/images/projects/aurora-gallery-4.jpg',
      '/assets/images/projects/aurora-gallery-3.jpg'
    ]
  },
  perotti: {
    title: 'Perotti Showroom',
    type: 'SHOWROOM & İÇ MİMARİ / ZÜCCACİYE & YAŞAM / İSTOÇ / 2024',
    lead: 'Lüks sofra ve ev yaşam ürünleri için monolitik koyu dokular ve gizli lineer aydınlatmayla tasarlanan prestijli perakende showroom alanı.',
    heroImg: '/assets/images/projects/aurora-gallery-4.jpg',
    client: 'Perotti Ev Gereçleri',
    location: 'İSTOÇ Ticaret Merkezi, İstanbul',
    year: '2024',
    scope: ['Showroom İç Mimari Konsept', 'Özel Teşhir Rafları', 'Gizli Lineer LED Aydınlatma', 'Lake Bankolar', 'Anahtar Teslim'],
    gallery: [
      '/assets/images/projects/aurora-gallery-1.jpg',
      '/assets/images/projects/aurora-gallery-2.jpg',
      '/assets/images/projects/aurora-gallery-3.jpg',
      '/assets/images/projects/aurora-gallery-4.jpg'
    ]
  }
};

// Aliases for seamless backward compatibility
projectData.aurora = projectData.ulak;
projectData.terra = projectData.yamaha;
projectData.momentum = projectData.dardanel;
projectData.nexus = projectData.stm;
projectData.atlas = projectData.mercedes;
projectData.forma = projectData.perotti;

function initProjectDetailModal() {
  const modal = document.getElementById('project-detail-modal');
  const closeBtns = document.querySelectorAll('[data-close-project-modal]');
  const triggers = document.querySelectorAll('[data-open-project]');

  if (!modal) return;

  function openProject(slug) {
    const data = projectData[slug] || projectData.aurora;

    const elTitle = document.getElementById('modal-project-title');
    const elType = document.getElementById('modal-project-type');
    const elLead = document.getElementById('modal-project-lead');
    const elHeroImg = document.getElementById('modal-project-hero-img');
    const elClient = document.getElementById('modal-project-client');
    const elLoc = document.getElementById('modal-project-location');
    const elYear = document.getElementById('modal-project-year');
    const elGalleryContainer = document.getElementById('modal-project-gallery');

    if (elTitle) elTitle.textContent = data.title;
    if (elType) elType.textContent = data.type;
    if (elLead) elLead.textContent = data.lead;
    if (elHeroImg && elHeroImg.parentElement) {
      if (data.heroImg) {
        elHeroImg.parentElement.style.display = '';
        elHeroImg.src = data.heroImg;
        elHeroImg.alt = data.title;
        elHeroImg.parentElement.setAttribute('data-lightbox', data.heroImg);
        elHeroImg.parentElement.setAttribute('data-caption', `${data.title} — Ana Görünüm`);
        elHeroImg.parentElement.classList.add('cursor-pointer');
      } else {
        elHeroImg.parentElement.style.display = 'none';
      }
    }
    if (elClient) elClient.textContent = data.client;
    if (elLoc) elLoc.textContent = data.location;
    if (elYear) elYear.textContent = data.year;

    if (elGalleryContainer && elGalleryContainer.parentElement) {
      if (data.gallery && data.gallery.length > 0) {
        elGalleryContainer.parentElement.style.display = '';
        elGalleryContainer.innerHTML = data.gallery.map((item, idx) => {
          const src = typeof item === 'string' ? item : item.src;
          const caption = typeof item === 'object' && item.caption ? item.caption : `${data.title} Detay 0${idx + 1}`;
          return `
            <div class="rounded-xl overflow-hidden border border-current/10 aspect-[16/10] bg-black/10 group shadow-sm relative cursor-pointer" data-lightbox="${src}" data-caption="${caption}">
              <img src="${src}" alt="${caption}" class="w-full h-full object-cover group-hover:scale-105 transition-transform duration-500">
              <div class="absolute inset-0 bg-gradient-to-t from-black/75 via-transparent to-transparent opacity-0 group-hover:opacity-100 transition-opacity flex items-end justify-between p-3.5">
                <span class="text-white text-xs font-sans font-medium drop-shadow">${caption}</span>
                <span class="bg-white/20 text-white font-mono text-[10px] px-2 py-0.5 rounded backdrop-blur-sm">Büyüt ↗</span>
              </div>
            </div>
          `;
        }).join('');
      } else {
        elGalleryContainer.parentElement.style.display = 'none';
      }
    }

    modal.classList.add('is-open');
    document.body.style.overflow = 'hidden';
  }

  function closeModal() {
    modal.classList.remove('is-open');
    document.body.style.overflow = '';
  }

  triggers.forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      const slug = btn.dataset.openProject || 'ulak';
      openProject(slug);
    });
  });

  const triggerImages = document.querySelectorAll('[data-open-project] img');
  triggerImages.forEach(img => {
    img.addEventListener('click', (e) => {
      const trigger = img.closest('[data-open-project]');
      if (!trigger) return;

      e.preventDefault();
      e.stopPropagation();
      const slug = trigger.dataset.openProject || 'ulak';
      openProject(slug);
    });
  });

  closeBtns.forEach(btn => btn.addEventListener('click', closeModal));

  modal.addEventListener('click', (e) => {
    if (e.target === modal) closeModal();
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && modal.classList.contains('is-open')) {
      closeModal();
    }
  });
}

function initAuroraThumbHeroSync() {
  const heroImg = document.querySelector('main section.my-12 img');
  const thumbCards = document.querySelectorAll('main section.my-16 [data-lightbox]');

  if (!heroImg || thumbCards.length === 0) return;

  thumbCards.forEach(card => {
    card.addEventListener('click', () => {
      const thumbSrc = card.dataset.lightbox || card.querySelector('img')?.src;
      if (!thumbSrc) return;

      const thumbAlt = card.querySelector('img')?.alt;
      heroImg.src = thumbSrc;
      if (thumbAlt) heroImg.alt = thumbAlt;
    });
  });
}

/* ── 6.1. FULLSCREEN IMAGE LIGHTBOX VIEWER ─────────────────────────────────── */
function initImageLightbox() {
  const lightbox = document.getElementById('image-lightbox-modal');
  const lightboxImg = document.getElementById('lightbox-img');
  const lightboxCaption = document.getElementById('lightbox-caption');
  const lightboxClose = document.getElementById('lightbox-close');

  if (!lightbox || !lightboxImg) return;

  function openLightbox(src, caption) {
    lightboxImg.src = src;
    if (lightboxCaption) lightboxCaption.textContent = caption || '';
    lightbox.classList.remove('hidden');
    lightbox.classList.add('flex');
  }

  function closeLightbox() {
    lightbox.classList.add('hidden');
    lightbox.classList.remove('flex');
    lightboxImg.src = '';
  }

  document.addEventListener('click', (e) => {
    const trigger = e.target.closest('[data-lightbox]');
    if (trigger) {
      e.preventDefault();
      const src = trigger.dataset.lightbox || trigger.querySelector('img')?.src;
      const caption = trigger.dataset.caption || trigger.querySelector('img')?.alt || '';
      if (src) openLightbox(src, caption);
    }
  });

  if (lightboxClose) {
    lightboxClose.addEventListener('click', closeLightbox);
  }

  lightbox.addEventListener('click', (e) => {
    if (e.target === lightbox || e.target.id === 'lightbox-close' || e.target.closest('#lightbox-close')) {
      closeLightbox();
    }
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !lightbox.classList.contains('hidden')) {
      closeLightbox();
    }
  });
}

/* ── 7. BRIEF FORM SUBMISSION & CONFIRMATION MODAL ────────────────────────── */
function showLeadConfirmationModal({ name, company, email, mailtoUrl, waUrl }) {
  let modal = document.getElementById('brief-confirmation-modal');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'brief-confirmation-modal';
    modal.className = 'fixed inset-0 z-[150] bg-black/80 backdrop-blur-md flex items-center justify-center p-4 transition-all duration-300 opacity-0 pointer-events-none';
    modal.setAttribute('role', 'dialog');
    modal.setAttribute('aria-modal', 'true');
    modal.innerHTML = `
      <div class="projenic-card max-w-lg w-full p-6 sm:p-8 space-y-6 relative transform scale-95 transition-transform duration-300">
        <button type="button" id="brief-modal-close" class="absolute top-5 right-5 p-2 rounded-full border border-current/10 hover:bg-current/10 transition" aria-label="Kapat">
          <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>
        </button>
        <div class="flex items-center gap-3 text-emerald-500">
          <span class="w-8 h-8 rounded-full bg-emerald-500/10 border border-emerald-500/20 flex items-center justify-center font-bold text-sm">✓</span>
          <span class="font-mono text-xs uppercase tracking-wider font-bold">Proje Briefi Başarıyla Alındı</span>
        </div>
        <div class="space-y-2">
          <h3 class="text-xl sm:text-2xl font-bold font-sans">Teşekkür Ederiz, Sayın <span id="brief-confirm-name"></span></h3>
          <p class="text-xs sm:text-sm opacity-80 leading-relaxed font-sans">
            Talebiniz doğrudan Proje Direktörümüz <strong>Suphi Bey</strong>'e (<span class="font-mono">suphi@projenicdesign.com</span>) iletildi. Mimari ve teknik ekibimiz 24 saat içinde 3D konsept taslak ve teknik şartname ile dönüş sağlayacaktır.
          </p>
        </div>
        <div class="pt-4 border-t border-current/10 flex flex-col sm:flex-row gap-3 font-mono text-xs">
          <a id="brief-confirm-wa" href="#" target="_blank" rel="noopener noreferrer" class="projenic-btn-solid text-center py-2.5 px-4 justify-center flex-1">
            WhatsApp ile Hızlı İletişim ↗
          </a>
          <button type="button" id="brief-confirm-ok" class="projenic-btn-outline text-center py-2.5 px-4 justify-center">
            Kapat
          </button>
        </div>
      </div>
    `;
    document.body.appendChild(modal);

    const closeBtn = modal.querySelector('#brief-modal-close');
    const okBtn = modal.querySelector('#brief-confirm-ok');
    const closeIt = () => {
      modal.classList.add('opacity-0', 'pointer-events-none');
      const card = modal.querySelector('.projenic-card');
      if (card) card.classList.add('scale-95');
      document.body.style.overflow = '';
    };
    if (closeBtn) closeBtn.addEventListener('click', closeIt);
    if (okBtn) okBtn.addEventListener('click', closeIt);
    modal.addEventListener('click', (e) => {
      if (e.target === modal) closeIt();
    });
  }

  const nameSpan = modal.querySelector('#brief-confirm-name');
  if (nameSpan) nameSpan.textContent = name || company || 'Yetkili';
  const waLink = modal.querySelector('#brief-confirm-wa');
  if (waLink) waLink.href = waUrl;

  modal.classList.remove('opacity-0', 'pointer-events-none');
  const card = modal.querySelector('.projenic-card');
  if (card) card.classList.remove('scale-95');
  document.body.style.overflow = 'hidden';
}

function initBriefSubmission() {
  const forms = document.querySelectorAll('form[data-fair-brief-form], #contact-full-form');
  if (!forms.length) return;

  forms.forEach(form => {
    form.addEventListener('submit', async (e) => {
      e.preventDefault();

      const submitBtn = form.querySelector('button[type="submit"]');
      const originalBtnHtml = submitBtn ? submitBtn.innerHTML : '';

      const company = form.querySelector('[name="company"], #c-company')?.value || '';
      const name = form.querySelector('[name="name"], #c-name')?.value || '';
      const phone = form.querySelector('[name="phone"], #c-phone')?.value || '';
      const email = form.querySelector('[name="email"], #c-email')?.value || '';
      const fair = form.querySelector('[name="fair"], #c-event')?.value || 'Genel Proje';
      const area = form.querySelector('[name="area"], #c-area')?.value || '';
      const type = form.querySelector('input[name="service"]:checked')?.value || form.querySelector('[name="type"]')?.value || 'Ahşap Lake Fuar Standı';
      const notes = form.querySelector('[name="notes"], #c-notes')?.value || '';

      const leadData = {
        company,
        name,
        phone,
        email,
        fair,
        area,
        type,
        notes,
        targetEmail: 'suphi@projenicdesign.com'
      };

      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.innerHTML = '<span>İletiliyor...</span>';
      }

      const mailSubject = `[Yeni Müşteri Briefi] ${company || name} — ${fair}`;
      const mailBody = `FİRMA: ${company}\nYETKİLİ: ${name}\nTELEFON: ${phone}\nE-POSTA: ${email}\nFUAR / PROJE: ${fair}\nALAN: ${area} m²\nHİZMET: ${type}\nNOTLAR: ${notes || 'Belirtilmedi'}`;
      const mailtoUrl = `mailto:suphi@projenicdesign.com?subject=${encodeURIComponent(mailSubject)}&body=${encodeURIComponent(mailBody)}`;

      const whatsappText = `*YENİ PROJE BRİEFİ (projenicdesign.com)*\n\n` +
        `• *Firma:* ${company}\n` +
        `• *Yetkili:* ${name}\n` +
        `• *Telefon:* ${phone}\n` +
        `• *E-Posta:* ${email}\n` +
        `• *Proje / Fuar:* ${fair}\n` +
        `• *Alan:* ${area} m²\n` +
        `• *Hizmet / Sistem:* ${type}\n` +
        `• *Notlar:* ${notes || 'Belirtilmedi'}\n\n` +
        `Lütfen 24 saat içinde 3D mimari taslak ve resmi teklif dosyasını iletiniz.`;
      const waUrl = `https://wa.me/905512047851?text=${encodeURIComponent(whatsappText)}`;

      try {
        const response = await fetch('/api/contact.php', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(leadData)
        });

        if (response.ok) {
          showLeadConfirmationModal({ name, company, email, mailtoUrl, waUrl });
          form.reset();
        } else {
          throw new Error('Server response not ok');
        }
      } catch (err) {
        // Fallback: Show confirmation modal with direct WhatsApp & background mailto trigger
        showLeadConfirmationModal({ name, company, email, mailtoUrl, waUrl });
        form.reset();
      } finally {
        if (submitBtn) {
          submitBtn.disabled = false;
          submitBtn.innerHTML = originalBtnHtml;
        }
        // Eğer çekmece formundan veya hızlı teklif modalından gönderildiyse kapat
        const drawer = document.getElementById('contact-drawer');
        const backdrop = document.getElementById('drawer-backdrop');
        if (drawer && drawer.classList.contains('is-open')) {
          drawer.classList.remove('is-open');
          if (backdrop) backdrop.classList.remove('is-open');
        }
        const quickQuoteModal = document.getElementById('quick-quote-modal');
        if (quickQuoteModal && quickQuoteModal.classList.contains('is-open')) {
          quickQuoteModal.classList.remove('is-open');
        }
      }
    });
  });
}

/* ── 10. SMOOTH SCROLL ────────────────────────────────────────────────────── */
function initSmoothScroll() {
  document.querySelectorAll('a[href^="#"]').forEach(anchor => {
    anchor.addEventListener('click', function (e) {
      const targetId = this.getAttribute('href');
      if (!targetId || targetId === '#' || targetId.startsWith('#!')) return;
      const targetEl = document.querySelector(targetId);
      if (targetEl) {
        e.preventDefault();
        targetEl.scrollIntoView({
          behavior: 'smooth'
        });
      }
    });
  });
}
