# PlanicWorks & Projenic Design System Guidelines

## Repo-Local Skill Kullanımı

* Projeye ait kalıcı skill paketleri `.agents/skills/*/SKILL.md` altında tutulur ve GitHub ile sürümlenir.
* Bir görev bu skill'lerden birinin adı veya açıklamasıyla eşleştiğinde, işlem yapmadan önce ilgili `SKILL.md` dosyası tamamen okunmalıdır.
* Yalnız görevle ilgili skill veya skill'ler yüklenmelidir; alakasız tüm skill'ler aynı anda kullanılmamalıdır.
* Skill talimatları bu dosyadaki Projenic Design kurallarıyla birlikte uygulanır. Çelişki varsa daha güvenli ve daha dar kapsamlı kural geçerlidir.
* Repo içindeki mevcut skill'lere ek olarak aşağıdaki ortak paketler de sürümlenir:
  * `ui-ux-pro-max`
  * `graphify`
  * `design-system`
  * `ui-styling`
  * `frontend-ui-engineering`
  * `performance-optimization`
  * `browser-testing-with-devtools`
  * `security-and-hardening`
  * `test-driven-development`
  * `code-review-and-quality`
  * `code-simplification`
  * `codexkit-bug-hunt`
  * `codexkit-test-hardening`
  * `codexkit-root-cause-analyzer`
  * `iso-24495-style`

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
