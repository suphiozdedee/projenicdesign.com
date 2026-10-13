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
    const { lenis, raf } = smoothScroll();
    cineHero();
    cineWork();
    marquee();
    reveals();
    // Yazı tipleri yüklenince ölçüler değişir; tetik noktalarını yeniden hesapla
    document.fonts?.ready.then(() => ScrollTrigger.refresh());
    return () => {
      gsap.ticker.remove(raf); gsap.ticker.lagSmoothing(500, 33);
      lenis.off('scroll', ScrollTrigger.update); lenis.destroy(); window.lenis = null;
      root.classList.remove('motion-ready');
    };
  });
  // Sabitlenen projeler ayrı koşulda: hareket azaltılınca boyut değişse bile geri gelmez
  mm.add('(prefers-reduced-motion: no-preference) and (min-width: 701px)', () => pinPanels());
}

/* Ataletli yumuşak kaydırma (Lenis) — ScrollTrigger ile aynı saatte çalışır */
function smoothScroll() {
  const lenis = new Lenis({ lerp: 0.1, wheelMultiplier: 1, anchors: { offset: -80 } });
  lenis.on('scroll', ScrollTrigger.update);
  const raf = (t) => lenis.raf(t * 1000);
  gsap.ticker.add(raf);
  gsap.ticker.lagSmoothing(0);
  window.lenis = lenis;
  return { lenis, raf };
}


/* 5 · Referans bandı: ekrandayken akar, hızlı kaydırınca hızlanır; üzerine gelince / odakta durur */
function marquee() {
  const track = $('.dmarquee__track');
  if (!track) return;
  const sec = track.parentElement;
  const loop = gsap.to(track, { xPercent: -50, ease: 'none', duration: 34, repeat: -1, paused: true });
  loop.totalTime(loop.duration() * 100); // geri akarken sıfıra takılıp durmasın
  let dir = 1, hold = false;
  const settle = gsap.delayedCall(0.25, () => gsap.to(loop, { timeScale: dir, duration: 1.2, overwrite: true })).pause();
  const st = ScrollTrigger.create({
    trigger: track, start: 'top bottom', end: 'bottom top',
    onToggle: (self) => (self.isActive && !hold ? loop.play() : loop.pause()),
    onUpdate: (self) => {
      dir = self.direction || dir;
      gsap.to(loop, { timeScale: dir * (1 + Math.min(4, Math.abs(self.getVelocity()) / 400)), duration: 0.2, overwrite: true });
      settle.restart(true);
    }
  });
  ['mouseenter', 'focusin'].forEach((e) => sec.addEventListener(e, () => { hold = true; loop.pause(); }));
  ['mouseleave', 'focusout'].forEach((e) => sec.addEventListener(e, () => { hold = false; if (st.isActive) loop.play(); }));
}

/* 6 · Sayfa geneli: başlıklar maskeden yükselir, fotoğraflar perde gibi açılır */
function reveals() {
  const root = document.documentElement;
  $$('.t-h2, .projenic-h2, .case-next__title, .cta-band h2, .contact__title').forEach((el) => {
    SplitText.create(el, {
      type: 'lines', mask: 'lines', linesClass: 'rl', aria: 'auto', autoSplit: true,
      onSplit: (self) => gsap.from(self.lines, {
        yPercent: 110, duration: 1.1, ease: 'expo.out', stagger: 0.08,
        scrollTrigger: { trigger: el, start: 'top 88%', once: true }
      })
    });
  });

  $$('.svc__figure, .work-card__media, .case-gallery figure').forEach((box) => {
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
    gsap.set(rows.map((r) => r.firstElementChild), { y: 40, opacity: 0 });
    ScrollTrigger.batch(rows, {
      start: 'top 92%', once: true,
      onEnter: (batch) => gsap.timeline()
        .to(batch.map((r) => r.querySelector('.service-row__rule')), { scaleX: 1, duration: 0.9, ease: 'power3.out', stagger: 0.06 }, 0)
        .to(batch.map((r) => r.firstElementChild), { y: 0, opacity: 1, duration: 0.8, ease: 'expo.out', stagger: 0.06, clearProps: 'transform' }, 0.1)
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
  const img = $('img', media);
  // Her özelliğin tek sahibi: açılış görsele, kaydırma kapsayıcıya uygulanır (sıçrama olmaz)
  // Başlangıç durumları CSS'te ilk boyamadan önce gizli (.js .chero__…); burada yerine gelir
  section.classList.add('is-live'); // CSS yedek açılışını iptal et, GSAP üstlenir
  gsap.timeline({ defaults: { ease: 'expo.out' } })
    .fromTo(img, { scale: 1.25 }, { scale: 1.08, duration: 2.6 }, 0)
    .fromTo('.chero__title .line > span', { yPercent: 110 }, { yPercent: 0, duration: 1.4, stagger: 0.1 }, 0.15)
    .fromTo(['.chero__lead', '.chero__actions'], { y: 24 }, { y: 0, duration: 1.1, stagger: 0.08 }, 0.3);
  gsap.timeline({ scrollTrigger: { trigger: section, start: 'top top', end: 'bottom top', scrub: true } })
    .to(media, { yPercent: 18, scale: 1.1, ease: 'none' }, 0)
    .to('.chero__content', { yPercent: -30, opacity: 0, ease: 'none' }, 0);
}

/* 8 · Projeler: her tam ekran proje sabitlenir, bir sonraki üstüne kayar */
function cineWork() {
  $$('.cpanel').forEach((p) => {
    gsap.fromTo($('.cpanel__media', p), { scale: 1.2 }, { scale: 1, ease: 'none', scrollTrigger: { trigger: p, start: 'top bottom', end: 'top top', scrub: true } });
    gsap.from($$('.cpanel__title, .cpanel__meta', p), {
      y: 50, opacity: 0, duration: 1.1, ease: 'expo.out', stagger: 0.06,
      scrollTrigger: { trigger: $('.cpanel__content', p), start: 'top 92%', toggleActions: 'play none none reverse' }
    });
  });
}

/* Sabitlenen öğe değil, içindeki katman ölçeklenir (ScrollTrigger kuralı); yalnız transform + opacity */
function pinPanels() {
  $$('.cpanel').slice(0, -1).forEach((p) => {
    ScrollTrigger.create({ trigger: p, start: 'top top', pin: true, pinSpacing: false });
    gsap.timeline({ scrollTrigger: { trigger: p, start: 'top top', end: 'bottom top', scrub: true } })
      .fromTo($('.cpanel__inner', p), { scale: 1 }, { scale: 0.93, ease: 'none' }, 0)
      .fromTo($('.cpanel__media', p), { opacity: 1 }, { opacity: 0.7, ease: 'none' }, 0);
  });
}
