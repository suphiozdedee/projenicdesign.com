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
    hero(mm);
    cineHero();
    cineWork(mm);
    manifesto();
    story(mm);
    gallery(mm);
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

/* 1 · Açılış: satırlar yükselir; kaydırınca kart tam ekrana büyür */
function hero(mm) {
  const section = $('[data-dhero]');
  if (!section) return;
  gsap.from('.dhero__title .line > span', { yPercent: 110, duration: 1.3, ease: 'expo.out', stagger: 0.09, delay: 0.1 });
  gsap.from('.dhero__kicker', { opacity: 0, y: 12, duration: 1, ease: 'expo.out' });
  gsap.from('.dhero__side', { opacity: 0, y: 18, duration: 1.1, ease: 'expo.out', delay: 0.6 });

  mm.add('(min-width: 801px)', () => {
    const card = $('.dhero__card', section);
    const frame = $('.dhero__frame', section);
    const frameClip = () => {
      const f = frame.getBoundingClientRect();
      const h = section.getBoundingClientRect();
      return `inset(${f.top - h.top}px ${h.right - f.right}px ${h.bottom - f.bottom}px ${f.left - h.left}px round 22px)`;
    };
    card.classList.add('is-clip');
    gsap.set(card, { clipPath: frameClip() });
    gsap.from(card, { opacity: 0, duration: 1.3, ease: 'expo.out', delay: 0.3 });
    gsap.fromTo($('img', card), { scale: 1.18 }, { scale: 1.08, duration: 2.2, ease: 'expo.out', delay: 0.3 });

    const tl = gsap.timeline({
      scrollTrigger: { trigger: section, start: 'top top', end: '+=100%', scrub: 0.8, pin: true, invalidateOnRefresh: true }
    });
    tl.fromTo(card, { clipPath: () => frameClip() }, { clipPath: 'inset(0px 0px 0px 0px round 0px)', ease: 'power2.inOut', duration: 1, immediateRender: false }, 0)
      .to($('img', card), { scale: 1, ease: 'none', duration: 1 }, 0)
      .to('.dhero__title .line, .dhero__kicker', { y: -40, opacity: 0, ease: 'power2.in', duration: 0.3, stagger: 0.03 }, 0)
      .to(['.dhero__side', '.scroll-hint'], { opacity: 0, y: -20, duration: 0.3 }, 0)
      .to('.dhero__overlay', { opacity: 1, duration: 0.2 }, 0.8)
      .fromTo('.dhero__overlay .ddim', { '--d': 0 }, { '--d': 1, duration: 0.25, ease: 'power2.out' }, 0.8);

    return () => { card.classList.remove('is-clip'); gsap.set(card, { clearProps: 'all' }); };
  });
}

/* 2 · Manifesto: kelimeler okundukça koyulaşır */
function manifesto() {
  const el = $('[data-manifesto]');
  if (!el) return;
  SplitText.create(el, {
    type: 'words', wordsClass: 'word', aria: 'none',
    onSplit(self) {
      return gsap.to(self.words, {
        color: getComputedStyle(el).color, stagger: 0.1, ease: 'none',
        scrollTrigger: { trigger: el, start: 'top 78%', end: 'bottom 55%', scrub: true }
      });
    }
  });
}

/* 3 · Boş zeminden açılışa: sabitlenen bölümde fotoğraflar sırayla açılır */
function story(mm) {
  const section = $('.story');
  if (!section) return;
  mm.add('(min-width: 1000px)', () => {
    const figs = $$('.story__stack figure', section);
    const steps = $$('.story__step', section);
    const count = $('[data-count]', section);
    gsap.set(steps.slice(1), { yPercent: 30, opacity: 0 });
    const tl = gsap.timeline({
      scrollTrigger: {
        trigger: section, start: 'top top', end: '+=' + (figs.length - 1) * 70 + '%', scrub: 0.8, pin: '.story__pin',
        onUpdate: (self) => { count.textContent = String(Math.min(figs.length, Math.floor(self.progress * figs.length) + 1)).padStart(2, '0'); }
      }
    });
    tl.to('.story__bar b', { scaleX: 1, ease: 'none', duration: figs.length - 1 }, 0);
    figs.slice(1).forEach((fig, i) => {
      tl.to(fig, { clipPath: 'inset(0% 0 0 0)', ease: 'power2.inOut', duration: 1 }, i)
        .fromTo($('img', fig), { scale: 1.22 }, { scale: 1, ease: 'power2.out', duration: 1 }, i)
        .to(steps[i], { yPercent: -30, opacity: 0, duration: 0.35 }, i + 0.15)
        .fromTo(steps[i + 1], { yPercent: 30, opacity: 0 }, { yPercent: 0, opacity: 1, duration: 0.35, immediateRender: false }, i + 0.55);
    });
    return () => gsap.set([...steps, ...figs], { clearProps: 'all' });
  });
}

/* 4 · Yatay galeri: dikey kaydırma yatay harekete dönüşür, ortadaki kart büyür */
function gallery(mm) {
  const section = $('.dwork');
  if (!section) return;
  mm.add('(min-width: 900px)', () => {
    const track = $('.dwork__track', section);
    const distance = () => Math.max(0, track.scrollWidth - innerWidth);
    if (distance() < innerWidth * 0.25) { section.classList.add('is-static'); return () => section.classList.remove('is-static'); }
    const scrollTween = gsap.to(track, {
      x: () => -distance(), ease: 'none',
      scrollTrigger: { trigger: section, pin: '.dwork__pin', start: 'top top', end: () => '+=' + distance(), scrub: 0.8, invalidateOnRefresh: true }
    });
    $$('.proj__img', track).forEach((img) => {
      gsap.timeline({ scrollTrigger: { trigger: img, containerAnimation: scrollTween, start: 'left right', end: 'right left', scrub: true } })
        .to(img, { scale: 1, ease: 'power1.out', duration: 0.5 })
        .to(img, { scale: 0.92, ease: 'power1.in', duration: 0.5 });
    });
  });
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
    gsap.set(rows, { '--draw': 0 });
    ScrollTrigger.batch(rows, {
      start: 'top 88%', once: true,
      onEnter: (batch) => gsap.timeline()
        .to(batch, { '--draw': 1, duration: 1, ease: 'power3.inOut', stagger: 0.12 }, 0)
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
    .from(['.chero__kicker', '.chero__lead', '.chero__actions'], { y: 24, opacity: 0, duration: 1.1, stagger: 0.08 }, 0.6);
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
      gsap.to(p, { scale: 0.9, filter: 'brightness(0.45)', borderRadius: 24, ease: 'none',
        scrollTrigger: { trigger: p, start: 'top top', end: 'bottom top', scrub: true } });
    });
  });
}
