# PlanicWorks & Projenic Design System Guidelines

## Proje skill'leri

* Projeye ait skill'ler `.claude/skills/*/SKILL.md` altında tutulur ve GitHub ile sürümlenir:
  * `gsap-*` — GreenSock'un resmi GSAP skill'leri (MIT): ScrollTrigger, SplitText, performans.
  * `apparel-logo-mockup` — kıyafet üzerine logo baskısı (IOPaint/LaMa + displacement).
* Proje bilgisi `src/js/projects.js` içindedir; proje sayfaları `node scripts/build-project-pages.mjs` ile üretilir.
* Sitede yalnızca gerçek proje fotoğrafları kullanılır; STM, TCDD, ULAK Haberleşme ve Alp Havacılık hiçbir yerde yer almaz.

Tasarım kararlarında ve arayüz geliştirmelerinde aşağıdaki prensipler izlenmelidir:

## 1. Tasarım Referansları & Vizyon
* Behance, Mobbin ve Untitled UI gibi modern platformlardan ilham alınabilir.
* Ancak, **hazır bir temayı doğrudan kopyalayıp entegre etmek kesinlikle kabul edilmez.** Projenic Design, kendine özel bir mimari ve kurumsal kimliğe sahip olmalıdır.

## 2. Bileşen Bazlı Standartlaştırma (Component-Based Design System)
Aşağıdaki tüm temel öğeler, tek bir bütünleşik tasarım dili ve CSS değişkenleri altında standardize edilmelidir:
* **Navigation & Floating Island:** Sadeleşen, temiz ve tutarlı menü yapısı.
* **Card Yapıları:** Lüks cam efektleri (glassmorphism), concentric double-bezel kenarlıklar ve gölgeler.
* **Formlar & Inputlar:** Focus auraları ve premium input sınıflarının ortak kullanımı.
* **Modal Pencereler:** Yumuşak geçiş efektleri, erişilebilir dialog standartları.
* **Bildirimler & Toasts:** Kullanıcıya durum bildiren tutarlı toasters.

## 3. Kod Standartları
* Stil tanımlarında ad-hoc renkler yerine semantic token'lar (`bg-card`, `bg-background`, `border-border`, `text-muted` vb.) tercih edilmelidir.
* Yazı tiplerinde modern Plus Jakarta Sans / Inter tipografisi ve dengeli harf aralıkları (`tracking-[-0.03em]`) kullanılmalıdır.
* Uygulamanın sürdürülebilir, kurumsal ve modern standartlarda kalması için kod temizliğine özen gösterilmelidir.

## 4. Mimari & Performans Kuralları
* **Geometrik Keskinlik (Industrial Design)**: Çocuksu/oval bubbly tasarımlar kesinlikle yasaktır. Kartlarda ve sarmalayıcılarda concentric radius kuralları (`rounded-2xl` dış bezel, `rounded-xl` iç kart) uygulanmalıdır.
* **Tipografik Hizalama**: Finansal ve teknik veriler (bütçe, m², kur vb.) KESİNLİKLE `tabular-nums` ve `font-mono` sınıfları ile kodlanmalıdır.
* **Responsive Yükseklik ve Mobil Uyumluluk**: Mobil ekranlarda hiçbir taşma veya yatay kaydırma olmaksızın kusursuz görünmelidir.
* **Doğrulama (Verification) Zorunluluğu**: Yazılan kod yerel ortamda derlenmeden ve hatasız çalıştığı doğrulanmadan görev tamamlandı sayılmaz.

## Tasarım becerileri (.claude/skills)
Her tasarım değişikliğinden önce bunlara göre denetle:
- taste-skill, redesign-skill (Leonxlnx/taste-skill, MIT) — yapay zeka kalıplarını engelleyen kurallar
- impeccable (pbakaus/impeccable, Apache-2.0) — reference/craft-floor.md kalite tabanı
- hallmark (Nutlope/hallmark, MIT), ui-ux-pro-max (nextlevelbuilder, MIT)
- improve-animations, animate, review-animations, find-animation-opportunities, animation-performance, apple-design (emilkowalski/skills, MIT), gsap-* (greensock)
- motion-design (LottieFiles, MIT), design-motion-principles (kylezantos, MIT)
- modern-web-design, lightweight-3d-effects, threejs-webgl, barba-js, scroll-reveal-libraries, locomotive-scroll (freshtechbro/claudedesignskills, MIT)
