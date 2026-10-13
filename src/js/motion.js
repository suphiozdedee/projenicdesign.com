/**
 * Yön D — kaydırmaya bağlı sinematik hareketler (GSAP ScrollTrigger + SplitText).
 * Kurallar (.claude/skills/gsap-*): yalnızca transform / opacity / clip-path animasyonu,
 * ScrollTrigger yalnız üst düzey tween/timeline'larda, yatay galeri ease:"none".
 * Azaltılmış hareket tercihinde hiçbir şey çalışmaz; içerik statik ve tam görünür kalır.
 */
import { gsap } from 'gsap';
import { ScrollTrigger } from 'gsap/ScrollTrigger';
import { SplitText } from 'gsap/SplitText';
import Lenis from 'lenis';

gsap.registerPlugin(ScrollTrigger, SplitText);

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

export function initMotion() {
  const root = document.documentElement;
  const mm = gsap.matchMedia();

  mm.add('(prefers-reduced-motion: no-preference)', () => {
    root.classList.add('motion-ready');
    const lenis = smoothScroll();
    cineHero();
    cineWork(mm);
    marquee();
    reveals();
    // Yazı tipleri yüklenince ölçüler değişir; tetik noktalarını yeniden hesapla
    document.fonts?.ready.then(() => ScrollTrigger.refresh());
    return () => { root.classList.remove('motion-ready'); lenis.destroy(); window.lenis = null; };
  });
}

/* Ataletli yumuşak kaydırma (Lenis) — ScrollTrigger ile aynı saatte çalışır */
function smoothScroll() {
  const lenis = new Lenis({ lerp: 0.1, wheelMultiplier: 1, anchors: { offset: -80 } });
  lenis.on('scroll', ScrollTrigger.update);
  gsap.ticker.add((t) => lenis.raf(t * 1000));
  gsap.ticker.lagSmoothing(0);
  window.lenis = lenis;
  return lenis;
}





/* 5 · Referans bandı: sürekli akar, hızlı kaydırınca hızlanır */
function marquee() {
  const track = $('.dmarquee__track');
  if (!track) return;
  const loop = gsap.to(track, { xPercent: -50, ease: 'none', duration: 34, repeat: -1 });
  ScrollTrigger.create({
    trigger: track, start: 'top bottom', end: 'bottom top',
    onToggle: (self) => (self.isActive ? loop.play() : loop.pause()),
    onUpdate: (self) => {
      const boost = 1 + Math.min(5, Math.abs(self.getVelocity()) / 400);
      gsap.to(loop, { timeScale: (self.direction || 1) * boost, duration: 0.2, overwrite: true });
      gsap.to(loop, { timeScale: self.direction || 1, duration: 1.2, delay: 0.25 });
    }
  });
}

/* 6 · Sayfa geneli: başlıklar maskeden yükselir, fotoğraflar perde gibi açılır */
function reveals() {
  const root = document.documentElement;
  $$('.t-h2, .projenic-h2, .pp-title, .case-next__title, .cta-band h2, .contact__title, .footer-wordmark').forEach((el) => {
    SplitText.create(el, {
      type: 'lines', mask: 'lines', linesClass: 'rl', aria: 'auto', autoSplit: true,
      onSplit: (self) => gsap.from(self.lines, {
        yPercent: 110, duration: 1.1, ease: 'expo.out', stagger: 0.08,
        scrollTrigger: { trigger: el, start: 'top 88%', once: true }
      })
    });
  });

  $$('.svc__figure, .work-card__media, .pp-main, .case-gallery figure').forEach((box) => {
    const img = $('img', box);
    gsap.fromTo(box, { clipPath: 'inset(18% 8% 18% 8% round 22px)' }, {
      clipPath: 'inset(0% 0% 0% 0% round 22px)', ease: 'power3.out', duration: 1.4,
      scrollTrigger: { trigger: box, start: 'top 85%', once: true }
    });
    if (img) gsap.fromTo(img, { scale: 1.25, yPercent: -6 }, {
      scale: 1.05, yPercent: 6, ease: 'none',
      scrollTrigger: { trigger: box, start: 'top bottom', end: 'bottom top', scrub: true }
    });
  });

  const rows = $$('.service-row');
  if (rows.length) {
    // Çizgi ayrı bir öğe: CSS değişkeniyle değil, doğrudan scaleX ile çizilir
    const lines = rows.map((r) => { const l = document.createElement('span'); l.className = 'service-row__rule'; l.setAttribute('aria-hidden', 'true'); r.append(l); return l; });
    root.classList.add('rows-drawn');
    gsap.set(lines, { scaleX: 0 });
    ScrollTrigger.batch(rows, {
      start: 'top 88%', once: true,
      onEnter: (batch) => gsap.timeline()
        .to(batch.map((r) => r.querySelector('.service-row__rule')), { scaleX: 1, duration: 1, ease: 'power3.inOut', stagger: 0.12 }, 0)
        .from(batch.map((r) => r.firstElementChild), { y: 40, opacity: 0, duration: 0.9, ease: 'expo.out', stagger: 0.12, clearProps: 'transform' }, 0.15)
    });
  }

  const proj = $$('.proj');
  if (proj.length && innerWidth < 900) {
    proj.forEach((p) => gsap.from(p, { y: 60, opacity: 0, duration: 1, ease: 'expo.out', scrollTrigger: { trigger: p, start: 'top 90%', once: true } }));
  }
}

/* 7 · Tam ekran açılış: satırlar yükselir, fotoğraf yavaşça yaklaşır; kaydırınca derinleşir */
function cineHero() {
  const section = $('[data-chero]');
  if (!section) return;
  const media = $('.chero__media', section);
  gsap.timeline({ defaults: { ease: 'expo.out' } })
    .fromTo(media, { scale: 1.25 }, { scale: 1.08, duration: 2.6 }, 0)
    .from('.chero__title .line > span', { yPercent: 110, duration: 1.4, stagger: 0.1 }, 0.25)
    .from(['.chero__lead', '.chero__actions'], { y: 24, opacity: 0, duration: 1.1, stagger: 0.08 }, 0.6);
  gsap.timeline({ scrollTrigger: { trigger: section, start: 'top top', end: 'bottom top', scrub: true } })
    .to(media, { yPercent: 18, scale: 1.18, ease: 'none' }, 0)
    .to('.chero__content', { yPercent: -30, opacity: 0, ease: 'none' }, 0)
    .to('.chero__shade', { opacity: 1.0, ease: 'none' }, 0);
}

/* 8 · Projeler: her tam ekran proje sabitlenir, bir sonraki üstüne kayar; alttaki küçülüp kararır */
function cineWork(mm) {
  const panels = $$('.cpanel');
  if (!panels.length) return;
  panels.forEach((p) => {
    const img = $('.cpanel__media', p);
    gsap.fromTo(img, { scale: 1.2 }, { scale: 1, ease: 'none', scrollTrigger: { trigger: p, start: 'top bottom', end: 'top top', scrub: true } });
    gsap.from($$('.cpanel__no, .cpanel__title, .cpanel__meta', p), {
      y: 50, opacity: 0, duration: 1.1, ease: 'expo.out', stagger: 0.08,
      scrollTrigger: { trigger: p, start: 'top 45%', toggleActions: 'play none none reverse' }
    });
  });
  mm.add('(min-width: 701px)', () => {
    panels.slice(0, -1).forEach((p) => {
      ScrollTrigger.create({ trigger: p, start: 'top top', pin: true, pinSpacing: false });
      // Yalnız transform + opacity (GPU): sahne geri çekilir, fotoğraf koyu zemine doğru söner
      gsap.timeline({ scrollTrigger: { trigger: p, start: 'top top', end: 'bottom top', scrub: true } })
        .fromTo(p, { scale: 1 }, { scale: 0.93, ease: 'none' }, 0)
        .fromTo($('.cpanel__media', p), { opacity: 1 }, { opacity: 0.7, ease: 'none' }, 0);
    });
  });
}

/* Menü kaydırma yönüne göre gizlenir / görünür (yalnız transform) */
export function tuckHeader() {
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
