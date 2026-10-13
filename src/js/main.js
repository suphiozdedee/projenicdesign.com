/**
 * PROJENIC — site davranışları
 * Tema, navigasyon, hero slider, proje vitrini ve modalı, proje talep formu,
 * kaydırma koreografisi. Her modül kendi DOM'u yoksa sessizce çıkar.
 */


const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const storage = {
  get(key) { try { return localStorage.getItem(key); } catch { return null; } },
  set(key, val) { try { localStorage.setItem(key, val); } catch { /* gizli pencere */ } }
};

document.addEventListener('DOMContentLoaded', () => {
  initTheme();
  initActiveNav();
  initMobileMenu();
  initClocks();
  initHeroSlider();
  initDrawer();
  initPortfolioFilter();
  initProjectGallery();
  initBriefForms();
  initReveal();
  initProcessRail();
  // Hareket kodu (GSAP, Lenis) yalnız gerektiğinde yüklenir
  if (!matchMedia('(prefers-reduced-motion: reduce)').matches) import('./motion.js').then((mod) => mod.initMotion());
  tuckHeader();
  initImageProtection();
});

/* ── Ortak: odak tuzağı ve kaydırma kilidi ──────────────────────────────── */
const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([tabindex="-1"]), select, textarea, [tabindex]:not([tabindex="-1"])';

function trapFocus(container, event) {
  if (event.key !== 'Tab') return;
  const items = $$(FOCUSABLE, container).filter(el => el.offsetParent !== null);
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
}

let lockCount = 0;
function lockScroll(on) {
  lockCount = Math.max(0, lockCount + (on ? 1 : -1));
  document.body.classList.toggle('is-locked', lockCount > 0);
  if (window.lenis) (lockCount > 0 ? window.lenis.stop() : window.lenis.start());
}

/** Bir katmanı aç/kapat: hidden → görünür → .is-open (geçiş için bir kare bekler). */
function showLayer(el) {
  el.hidden = false;
  requestAnimationFrame(() => requestAnimationFrame(() => el.classList.add('is-open')));
}
function hideLayer(el) {
  el.classList.remove('is-open');
  const done = () => { if (!el.classList.contains('is-open')) el.hidden = true; };
  if (reduceMotion.matches) done();
  else setTimeout(done, 450);
}

/* ── 1. Tema ─────────────────────────────────────────────────────────────── */
function initTheme() {
  const buttons = $$('[data-theme-toggle]');
  const logos = $$('img.brand-logo');

  function apply(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    const dark = theme === 'dark';
    buttons.forEach(btn => {
      btn.setAttribute('aria-pressed', String(dark));
      btn.setAttribute('aria-label', dark ? 'Açık temaya geç' : 'Koyu temaya geç');
    });
    logos.forEach(img => { img.src = dark ? '/assets/images/logo-white.png' : '/assets/images/logo-black.png'; });
    const meta = $('meta[name="theme-color"]');
    if (meta) meta.content = dark ? '#121311' : '#FFFFFF';
  }

  apply(document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light');
  buttons.forEach(btn => btn.addEventListener('click', () => {
    const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    storage.set('projenic_theme', next);
    apply(next);
  }));
}

/* ── 2. Aktif sayfa işareti ──────────────────────────────────────────────── */
function initActiveNav() {
  const page = document.body.dataset.page || location.pathname.replace(/^\/|\.html$/g, '') || 'index';
  $$(`[data-nav="${page}"]`).forEach(a => a.setAttribute('aria-current', 'page'));
}

/* ── 3. Mobil menü ───────────────────────────────────────────────────────── */
function initMobileMenu() {
  const btn = $('[data-menu-toggle]');
  const menu = $('[data-mobile-menu]');
  if (!btn || !menu) return;

  const setOpen = (open) => {
    btn.setAttribute('aria-expanded', String(open));
    btn.setAttribute('aria-label', open ? 'Menüyü kapat' : 'Menüyü aç');
    document.body.classList.toggle('menu-open', open);
    if (open) { showLayer(menu); lockScroll(true); $('a', menu)?.focus(); }
    else { hideLayer(menu); lockScroll(false); }
  };
  const isOpen = () => btn.getAttribute('aria-expanded') === 'true';

  btn.addEventListener('click', () => setOpen(!isOpen()));
  menu.addEventListener('click', (e) => { if (e.target.closest('a')) setOpen(false); });
  document.addEventListener('keydown', (e) => {
    if (!isOpen()) return;
    if (e.key === 'Escape') { setOpen(false); btn.focus(); }
    else trapFocus($('[data-site-header]'), e);
  });
  window.matchMedia('(min-width: 1024px)').addEventListener('change', (mq) => { if (mq.matches && isOpen()) setOpen(false); });
}

/* ── 4. Footer saatleri (dakika hassasiyeti, sekme gizliyken durur) ───── */
function initClocks() {
  const clocks = [['clock-ist', 'Europe/Istanbul'], ['clock-dus', 'Europe/Berlin'], ['clock-dxb', 'Asia/Dubai']]
    .map(([id, tz]) => [document.getElementById(id), tz]).filter(([el]) => el);
  if (!clocks.length) return;
  const tick = () => {
    const now = new Date();
    clocks.forEach(([el, timeZone]) => {
      el.textContent = new Intl.DateTimeFormat('tr-TR', { timeZone, hour: '2-digit', minute: '2-digit' }).format(now);
    });
  };
  tick();
  let timer = setInterval(tick, 30000);
  document.addEventListener('visibilitychange', () => {
    clearInterval(timer);
    if (!document.hidden) { tick(); timer = setInterval(tick, 30000); }
  });
}

/* ── 5. Hero slider: gerçek projeler, durdurulabilir otomatik geçiş ────── */
function initHeroSlider() {
  const root = $('[data-hero-slider]');
  if (!root) return;
  const slides = $$('[data-slide]', root);
  const bar = $('.hero-progress__bar', root);
  const toggle = $('[data-hero-toggle]', root);
  const out = {
    client: $('[data-hero-client]', root),
    meta: $('[data-hero-meta]', root),
    area: $('[data-hero-area]', root),
    count: $('[data-hero-count]', root)
  };
  const DURATION = 6500;
  root.style.setProperty('--slide-ms', `${DURATION}ms`);

  let index = 0;
  let timer = null;
  let userPaused = reduceMotion.matches;   // azaltılmış harekette otomatik geçiş yok
  let hoverPaused = false;

  const hydrate = (slide) => {  // görseli ancak sıra gelince indir
    $$('[data-srcset]', slide).forEach(s => { s.srcset = s.dataset.srcset; s.removeAttribute('data-srcset'); });
    $$('img[data-src]', slide).forEach(img => { img.src = img.dataset.src; img.removeAttribute('data-src'); });
  };

  const restartBar = () => {
    if (!bar) return;
    bar.classList.remove('is-running');
    void bar.offsetWidth;
    if (!userPaused) bar.classList.add('is-running');
  };

  const schedule = () => {
    clearTimeout(timer);
    if (userPaused || hoverPaused || document.hidden) return;
    timer = setTimeout(() => go(index + 1), DURATION);
  };

  function go(next) {
    index = (next + slides.length) % slides.length;
    const slide = slides[index];
    hydrate(slide);
    hydrate(slides[(index + 1) % slides.length]);
    slides.forEach((s, i) => {
      const active = i === index;
      s.classList.toggle('is-active', active);
      s.setAttribute('aria-hidden', String(!active));
    });
    out.client.textContent = slide.dataset.client;
    out.meta.textContent = slide.dataset.meta;
    out.count.textContent = `${index + 1} / ${slides.length}`;
    if (out.area) {
      out.area.textContent = slide.dataset.area;
      const dim = out.area.parentElement;   // ölçü çizgisini yeniden çiz
      dim.classList.remove('is-redraw'); void dim.offsetWidth; dim.classList.add('is-redraw');
    }
    restartBar();
    schedule();
  }

  const setPaused = (paused) => {
    userPaused = paused;
    root.classList.toggle('is-paused', paused);
    toggle?.setAttribute('aria-pressed', String(paused));
    toggle?.setAttribute('aria-label', paused ? 'Otomatik geçişi başlat' : 'Otomatik geçişi durdur');
    if (toggle) {
      $('.i-pause', toggle).style.display = paused ? 'none' : '';
      $('.i-play', toggle).style.display = paused ? '' : 'none';
    }
    restartBar();
    schedule();
  };

  $('[data-hero-prev]', root)?.addEventListener('click', () => go(index - 1));
  $('[data-hero-next]', root)?.addEventListener('click', () => go(index + 1));
  toggle?.addEventListener('click', () => setPaused(!userPaused));

  // Fareyle üzerindeyken ya da klavye odağı içerideyken bekle
  const holdOn = () => { hoverPaused = true; root.classList.add('is-paused'); clearTimeout(timer); };
  const holdOff = () => { hoverPaused = false; if (!userPaused) root.classList.remove('is-paused'); schedule(); };
  root.addEventListener('mouseenter', holdOn);
  root.addEventListener('mouseleave', holdOff);
  root.addEventListener('focusin', holdOn);
  root.addEventListener('focusout', (e) => { if (!root.contains(e.relatedTarget)) holdOff(); });
  document.addEventListener('visibilitychange', () => (document.hidden ? clearTimeout(timer) : schedule()));

  // Dokunmatikte kaydırma ile geçiş
  let startX = null;
  const frame = $('.hero-media__frame', root);
  frame.addEventListener('touchstart', (e) => { startX = e.touches[0].clientX; }, { passive: true });
  frame.addEventListener('touchend', (e) => {
    if (startX === null) return;
    const dx = e.changedTouches[0].clientX - startX;
    if (Math.abs(dx) > 40) go(index + (dx < 0 ? 1 : -1));
    startX = null;
  }, { passive: true });

  // Görsele tıklamak proje dosyasını açar
  frame.addEventListener('click', () => { const url = PROJECT_PAGE[slides[index].dataset.project]; if (url) location.href = url; });
  frame.style.cursor = 'pointer';

  setPaused(userPaused);
  hydrate(slides[1]);
}

/* ── 6. Proje talebi: sayfada form varsa ona git, yoksa yan paneli aç ──── */
function initDrawer() {
  const drawer = $('#contact-drawer');
  const backdrop = $('#drawer-backdrop');
  const inlineForm = $('#talep [data-fair-brief-form]');
  let lastTrigger = null;

  const open = (trigger) => {
    lastTrigger = trigger;
    showLayer(backdrop);
    showLayer(drawer);
    lockScroll(true);
    setTimeout(() => $('input:not([type="hidden"]):not([tabindex="-1"])', drawer)?.focus({ preventScroll: true }), reduceMotion.matches ? 0 : 120);
  };
  const close = () => {
    if (!drawer?.classList.contains('is-open')) return;
    hideLayer(drawer);
    hideLayer(backdrop);
    lockScroll(false);
    lastTrigger?.focus?.();
  };

  document.addEventListener('click', (e) => {
    const trigger = e.target.closest('[data-open-drawer]');
    if (!trigger) return;
    if (inlineForm) {            // ana sayfa ve iletişim: formun kendisine kaydır
      e.preventDefault();
      if (window.lenis) window.lenis.scrollTo('#talep', { offset: -80 }); else $('#talep').scrollIntoView({ behavior: reduceMotion.matches ? 'auto' : 'smooth' });
      setTimeout(() => $('input', inlineForm)?.focus({ preventScroll: true }), reduceMotion.matches ? 0 : 600);
    } else if (drawer) {
      e.preventDefault();
      open(trigger);
    }                             // ikisi de yoksa bağlantı /iletisim.html#talep'e gider
  });

  if (!drawer || !backdrop) return;
  $$('[data-close-drawer]').forEach(btn => btn.addEventListener('click', close));
  document.addEventListener('keydown', (e) => {
    if (!drawer.classList.contains('is-open')) return;
    if (e.key === 'Escape') close();
    else trapFocus(drawer, e);
  });
  drawer.addEventListener('brief:sent', () => {});  // başarı mesajı formda kalır; panel açık kalır
}

/* ── 7. Proje filtresi ───────────────────────────────────────────────────── */
function initPortfolioFilter() {
  const buttons = $$('[data-filter]');
  const cards = $$('.portfolio-project-card');
  const empty = $('[data-work-empty]');
  if (!buttons.length || !cards.length) return;

  // Sayıları gerçek kartlardan hesapla
  $$('[data-filter-count]').forEach(el => {
    const f = el.dataset.filterCount;
    el.textContent = f === 'all' ? cards.length : cards.filter(c => c.dataset.category === f).length;
  });

  const apply = (filter) => {
    buttons.forEach(b => b.setAttribute('aria-pressed', String(b.dataset.filter === filter)));
    let shown = 0;
    cards.forEach(card => {
      const match = filter === 'all' || card.dataset.category === filter;
      card.hidden = !match;
      if (match) {
        shown++;
        card.classList.add('is-in');
        if (!reduceMotion.matches) card.animate([{ opacity: 0, transform: 'translateY(12px)' }, { opacity: 1, transform: 'none' }], { duration: 420, easing: 'cubic-bezier(0.16,1,0.3,1)', delay: Math.min(shown, 6) * 40, fill: 'backwards' });
      }
    });
    if (empty) empty.hidden = shown > 0;
  };

  buttons.forEach(b => b.addEventListener('click', () => apply(b.dataset.filter)));
  $('[data-filter-reset]')?.addEventListener('click', (e) => { e.preventDefault(); apply('all'); });
}

/* ── 8. Proje iç sayfası: küçük görsel → büyük alan ──────────────────────── */
const PROJECT_PAGE = { yamaha: '/proje-yamaha.html', mercedes: '/proje-mercedes.html', dardanel: '/proje-dardanel.html' };
const toWebp = (src, w = 640) => src.replace('/projects/', '/opt/').replace(/\.jpg$/, `-${w}.webp`);
const escapeHtml = (s) => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function initProjectGallery() {
  const main = $('[data-pp-main]');
  const thumbs = $$('[data-pp-thumb]');
  if (!main || !thumbs.length) return;
  const img = $('img', main);
  const source = $('source', main);
  let current = 0;

  const show = (i) => {
    current = (i + thumbs.length) % thumbs.length;
    const t = thumbs[current];
    thumbs.forEach(b => b.setAttribute('aria-pressed', String(b === t)));
    const apply = () => {
      source.srcset = `${toWebp(t.dataset.src, 640)} 640w, ${toWebp(t.dataset.src, 1024)} 1024w, ${toWebp(t.dataset.src, 1600)} 1600w`;
      img.src = t.dataset.src;
      img.alt = t.dataset.alt;
      const done = () => main.classList.remove('is-swapping');
      img.complete ? done() : img.addEventListener('load', done, { once: true });
    };
    if (reduceMotion.matches) apply();
    else { main.classList.add('is-swapping'); setTimeout(apply, 220); }
  };

  thumbs.forEach((b, i) => b.addEventListener('click', () => show(i)));
  document.addEventListener('keydown', (e) => {
    if (e.target.closest('input, textarea, select') || document.body.classList.contains('is-locked')) return;
    if (e.key === 'ArrowRight') show(current + 1);
    if (e.key === 'ArrowLeft') show(current - 1);
  });
}

/* ── 9. Proje talep formu ────────────────────────────────────────────────── */
const MESSAGES = {
  valueMissing: { company: 'Firma adını yazın.', name: 'Size nasıl hitap edeceğimizi yazın.', phone: 'Size ulaşabileceğimiz bir numara yazın.', email: 'E-posta adresinizi yazın.' },
  typeMismatch: 'Geçerli bir e-posta adresi yazın (ör. ad@firma.com).',
  patternMismatch: 'Telefon numarasını en az 10 rakamla yazın.'
};

function fieldMessage(input) {
  const v = input.validity;
  if (v.valueMissing) return MESSAGES.valueMissing[input.name] || 'Bu alanı doldurun.';
  if (v.typeMismatch) return MESSAGES.typeMismatch;
  if (v.patternMismatch) return MESSAGES.patternMismatch;
  return '';
}

function validateField(input) {
  const err = document.getElementById(`${input.id}-err`);
  const msg = input.checkValidity() ? '' : fieldMessage(input);
  input.setAttribute('aria-invalid', String(Boolean(msg)));
  if (err) {
    err.textContent = msg;
    err.hidden = !msg;
    if (msg) input.setAttribute('aria-describedby', err.id); else input.removeAttribute('aria-describedby');
  }
  return !msg;
}

function initBriefForms() {
  $$('form[data-fair-brief-form]').forEach(form => {
    const status = $('.form-status', form);
    const submit = $('button[type="submit"]', form);
    const required = $$('[required]', form);

    required.forEach(input => {
      input.addEventListener('blur', () => { if (input.value || input.getAttribute('aria-invalid') === 'true') validateField(input); });
      input.addEventListener('input', () => { if (input.getAttribute('aria-invalid') === 'true') validateField(input); });
    });

    const showStatus = (kind, html) => {
      status.className = `form-status form-status--${kind}`;
      status.innerHTML = html;
      status.hidden = false;
      status.focus({ preventScroll: true });
      status.scrollIntoView({ block: 'nearest', behavior: reduceMotion.matches ? 'auto' : 'smooth' });
    };

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const invalid = required.filter(input => !validateField(input));
      if (invalid.length) { invalid[0].focus(); return; }

      const data = Object.fromEntries(new FormData(form).entries());
      const lead = {
        company: data.company.trim(), name: data.name.trim(), phone: data.phone.trim(), email: data.email.trim(),
        fair: (data.fair || '').trim() || 'Belirtilmedi', area: (data.area || '').trim(), type: data.type,
        notes: (data.notes || '').trim(), website_trap: data.website_trap || ''
      };
      const waText = `Merhaba, projenicdesign.com üzerinden yazıyorum.\nFirma: ${lead.company}\nYetkili: ${lead.name}\nFuar/proje: ${lead.fair}\nAlan: ${lead.area || '-'} m²\nHizmet: ${lead.type}`;
      const waUrl = `https://wa.me/905512047851?text=${encodeURIComponent(waText)}`;

      submit.disabled = true;
      submit.setAttribute('aria-busy', 'true');
      const label = submit.textContent;
      submit.textContent = 'Gönderiliyor…';
      status.hidden = true;

      try {
        const res = await fetch(form.action, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
          body: JSON.stringify(lead)
        });
        const json = await res.json().catch(() => ({}));
        if (!res.ok || !json.success) throw new Error(json.message || `HTTP ${res.status}`);
        form.reset();
        required.forEach(i => i.removeAttribute('aria-invalid'));
        showStatus('success', `<strong>Talebiniz alındı, ${escapeHtml(lead.name.split(' ')[0])}.</strong>
          Proje yöneticimiz 24 saat içinde ${escapeHtml(lead.email)} adresine ya da telefonla dönüş yapacak.
          <span class="form-status__actions"><a href="${waUrl}" target="_blank" rel="noopener noreferrer">Beklemek istemiyorsanız WhatsApp'tan yazın ↗</a></span>`);
      } catch (err) {
        // Bilgiler formda kalır; ziyaretçiye çalışan iki yol gösterilir
        showStatus('error', `<strong>Talebiniz gönderilemedi.</strong>
          Bağlantı ya da sunucu kaynaklı bir sorun oluştu; yazdıklarınız formda duruyor. Tekrar deneyebilir veya bize doğrudan ulaşabilirsiniz.
          <span class="form-status__actions"><a href="${waUrl}" target="_blank" rel="noopener noreferrer">Bilgileri WhatsApp ile gönderin ↗</a><a href="tel:+905512047851">+90 551 204 78 51</a></span>`);
      } finally {
        submit.disabled = false;
        submit.removeAttribute('aria-busy');
        submit.textContent = label;
      }
    });
  });
}

/* ── 10. Kaydırmada belirme (tek seferlik) ───────────────────────────────── */
function initReveal() {
  const items = $$('[data-reveal]');
  if (!items.length || reduceMotion.matches || !('IntersectionObserver' in window)) {
    items.forEach(el => el.classList.add('is-in'));
    return;
  }
  // Ekranda zaten görünenler gizlenmeden kalsın: önce işaretle, sonra sınıfı ekle
  const vh = window.innerHeight;
  items.forEach(el => { if (el.getBoundingClientRect().top < vh * 0.92) el.classList.add('is-in'); });
  document.documentElement.classList.add('motion-ready');

  const io = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (!entry.isIntersecting) return;
      entry.target.classList.add('is-in');
      io.unobserve(entry.target);
    });
  }, { rootMargin: '0px 0px -8% 0px', threshold: 0 });
  items.filter(el => !el.classList.contains('is-in')).forEach(el => io.observe(el));
}

/* ── 11. Süreç çizgisi: görünen adıma kadar dolar ─────────────────────────── */
function initProcessRail() {
  const root = $('[data-process]');
  if (!root) return;
  const steps = $$('[data-step]', root);
  const rail = $('.process__rail-fill', root);
  if (!('IntersectionObserver' in window)) { steps.forEach(s => s.classList.add('is-active')); return; }
  document.documentElement.classList.add('motion-ready');

  const update = () => {
    const active = steps.filter(s => s.classList.contains('is-active'));
    const last = active[active.length - 1];
    if (!last) { rail.parentElement.style.setProperty('--progress', 0); return; }
    const top = root.getBoundingClientRect().top;
    const total = root.getBoundingClientRect().height || 1;
    const reach = last.getBoundingClientRect().top - top + 14;
    rail.parentElement.style.setProperty('--progress', Math.min(1, reach / total).toFixed(3));
  };

  const io = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      // Adım ekranın ortasına geldiğinde etkin; geri kaydırınca söner
      entry.target.classList.toggle('is-active', entry.isIntersecting || entry.boundingClientRect.top < 0);
    });
    update();
  }, { rootMargin: '0px 0px -45% 0px', threshold: 0 });
  steps.forEach(s => io.observe(s));
  window.addEventListener('resize', update, { passive: true });
}

/* ── 12. Görsel koruma (mevcut tercih: sağ tık / sürükleme kapalı) ───────── */
function initImageProtection() {
  const block = (e) => { if (e.target instanceof HTMLImageElement) e.preventDefault(); };
  document.addEventListener('contextmenu', block, { capture: true });
  document.addEventListener('dragstart', block, { capture: true });
}

/* Menü kaydırma yönüne göre gizlenir / görünür (yalnız transform) */
function tuckHeader() {
  const header = document.querySelector('[data-site-header]');
  if (!header) return;
  let last = scrollY, ticking = false;
  addEventListener('scroll', () => {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(() => {
      const y = scrollY, d = y - last;
      if (Math.abs(d) > 6) { header.classList.toggle('is-tucked', d > 0 && y > 160 && !document.body.classList.contains('is-locked')); last = y; }
      ticking = false;
    });
  }, { passive: true });
  header.addEventListener('focusin', () => header.classList.remove('is-tucked'));
}
