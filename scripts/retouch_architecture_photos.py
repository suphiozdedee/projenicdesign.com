import os, sys
sys.stdout.reconfigure(encoding='utf-8')
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
import numpy as np

base = r'D:\PROJENIC BRAND REBUILD\projenic_eski\_projenic_fotoğraflar'
dest_dir = r'c:\Users\Projenic_Design\Desktop\projenicdesign.com\public\assets\images\projects'
os.makedirs(dest_dir, exist_ok=True)

# Curated selections for the 6 projects
selections = {
    'ulak-haberlesme': {
        'folder': 'ulak haberleşme_idef_2023',
        'hero': 'IMG_5040.JPG',       # Wide majestic perspective of IDEF stand
        'gallery': [
            ('IMG_5042.JPG', 'Giriş ve Taşıyıcı Karkas Mimarisi'),
            ('IMG_5043.JPG', 'VIP Toplantı Alanı ve Aydınlatma Detayları'),
            ('IMG_5146.JPG', 'Sayısal Enstalasyon ve Logo Duvarı'),
            ('IMG_5150.JPG', 'İnteraktif Ürün Teşhir Masaları ve Bankolar')
        ]
    },
    'yamaha': {
        'folder': 'yamaha_boatshow_2024',
        'hero': 'IMG_7770.jpg',       # Wide open boat show perspective
        'gallery': [
            ('IMG_7813.jpg', 'Geniş Açık Alan Platform Mimarisi'),
            ('IMG_7816.jpg', 'Motor Teşhir Kaideleri ve Özel Aydınlatma'),
            ('IMG_7847.jpg', 'VIP Karşılama ve Ağırlama Bölümü'),
            ('IMG_7852.jpg', 'Ağır Yük Zemin Güçlendirmesi ve Lake Detaylar')
        ]
    },
    'dardanel': {
        'folder': 'dardanel_worldfood_2024',
        'hero': 'IMG_9525.jpg',       # Main facade with glowing sign
        'gallery': [
            ('IMG_9529.jpg', 'Canlı Şef Mutfak İstasyonu ve Tadım Barı'),
            ('IMG_9531.jpg', 'Işıklı Ürün Teşhir Rafları ve Vitrin'),
            ('IMG_9532.jpg', 'B2B Toplantı Bölümü ve Özel Marangozluk'),
            ('IMG_9534.jpg', 'Karşılama Bankosu ve Pleksi Detaylar')
        ]
    },
    'stm-savunma': {
        'folder': 'stm_sahaexpo_2023',
        'hero': 'GXXJ9781.JPEG',      # Main impressive stand view
        'gallery': [
            ('CNNL5724.JPEG', 'Milli Savunma Fuar Standı Ana Cephe'),
            ('GOLE9342.JPEG', 'Maket Teşhir Kaideleri ve Zemin Aydınlatması'),
            ('GOUV7583.JPEG', 'VIP Karşılama ve Görüşme Alanı'),
            ('HPAJ5757.JPEG', 'Statik Karkas ve Tavan Aydınlatma Izgarası')
        ]
    },
    'mercedes-benz': {
        'folder': 'mercedes benz-türk_ik standı_2023',
        'hero': 'IMG_7259.JPG',       # Full stand view
        'gallery': [
            ('IMG_7260.JPG', 'Giriş Totemi ve Minimalist Hacimler'),
            ('IMG_7261.JPG', 'Kariyer Görüşme Masaları ve Akustik Bölmeler'),
            ('IMG_7262.JPG', 'Özel Ahşap Lake ve Metal Birleşim Noktaları'),
            ('IMG_7259.JPG', 'Genel Alan Perspektifi ve Işık Kurgusu')
        ]
    },
    'perotti': {
        'folder': 'perotti_istoç showroom_2024',
        'hero': 'IMG_0342.jpg',       # Spectacular wide corridor of luxury showroom
        'gallery': [
            ('IMG_0344.jpg', 'Ana Galeri Koridoru ve Işıklı Raf Sistemleri'),
            ('IMG_0348.jpg', 'Özel Lake Sunum Bankoları ve Ürün Teşhiri'),
            ('IMG_0350.jpg', 'VIP Müşteri Ağırlama ve Toplantı Alanı'),
            ('IMG_9494.jpg', 'Tavan Lineer Işık Kanalları ve Doku Detayı')
        ]
    }
}

def studio_photoshop_retouch(im, is_hero=False):
    """
    Applies high-end architectural post-processing:
    1. White balance & tone curve normalization (neutralizes harsh yellow/green hall lighting)
    2. Deep velvety black level enhancement (matching Projenic stone/dark theme)
    3. Micro-contrast & edge clarity filter (UnsharpMask)
    4. Subtle luxury warmth & saturation calibration
    5. Soft 3% architectural radial vignette
    """
    if im.mode != 'RGB':
        im = im.convert('RGB')
    
    # 1. Architectural Crop: 16:10 for cards and heroes
    target_ratio = 16.0 / 10.0
    w, h = im.size
    current_ratio = w / float(h)
    
    if current_ratio > target_ratio:
        # Too wide -> crop sides
        new_w = int(h * target_ratio)
        left = (w - new_w) // 2
        im = im.crop((left, 0, left + new_w, h))
    elif current_ratio < target_ratio:
        # Too tall -> crop top & bottom (bias slightly to bottom so stand is centered and ceiling truss is minimized)
        new_h = int(w / target_ratio)
        top = int((h - new_h) * 0.4) # Keep slightly lower
        im = im.crop((0, top, w, top + new_h))
    
    # Resize to standard ultra-crisp web dimensions (1920x1200)
    im = im.resize((1920, 1200), Image.Resampling.LANCZOS)
    
    # 2. Tone and Color Grading with NumPy
    arr = np.array(im, dtype=np.float32)
    
    # Auto-levels / Black point stretch: eliminate washed-out haze
    p2 = np.percentile(arr, 1.5)
    p98 = np.percentile(arr, 99.0)
    arr = np.clip((arr - p2) / (p98 - p2) * 255.0, 0, 255)
    
    # Color temperature balancing: slightly reduce greenish-yellow hall cast
    arr[:, :, 1] *= 0.985 # Green subtle pull
    arr[:, :, 2] = np.clip(arr[:, :, 2] * 1.025, 0, 255) # Blue subtle lift for crisp neutral whites
    
    # Soft vignette on edges
    Y, X = np.ogrid[:1200, :1920]
    center_y, center_x = 600, 960
    max_dist = np.sqrt(center_x**2 + center_y**2)
    dist = np.sqrt((X - center_x)**2 + (Y - center_y)**2)
    vignette = 1.0 - 0.12 * (dist / max_dist)**2
    for c in range(3):
        arr[:, :, c] = np.clip(arr[:, :, c] * vignette, 0, 255)
        
    retouched = Image.fromarray(arr.astype(np.uint8))
    
    # 3. Contrast & Vibrance
    enh_contrast = ImageEnhance.Contrast(retouched)
    retouched = enh_contrast.enhance(1.08)
    
    enh_color = ImageEnhance.Color(retouched)
    retouched = enh_color.enhance(1.05)
    
    # 4. Micro-Contrast & Edge Sharpness (Unsharp Mask)
    retouched = retouched.filter(ImageFilter.UnsharpMask(radius=2.0, percent=125, threshold=2))
    
    return retouched

print("Starting Studio Architectural Retouch Pipeline...")

for slug, pdata in selections.items():
    folder_path = os.path.join(base, pdata['folder'])
    
    # Process Hero
    hero_src = os.path.join(folder_path, pdata['hero'])
    hero_dest = os.path.join(dest_dir, f'{slug}-hero.jpg')
    with Image.open(hero_src) as im:
        hero_im = studio_photoshop_retouch(im, is_hero=True)
        hero_im.save(hero_dest, 'JPEG', quality=88, optimize=True)
        print(f'✓ [HERO RETOUCHED] {slug}-hero.jpg ({hero_im.size[0]}x{hero_im.size[1]} - {os.path.getsize(hero_dest)//1024} KB)')
        
    # Process Gallery
    for idx, (g_file, caption) in enumerate(pdata['gallery'], start=1):
        g_src = os.path.join(folder_path, g_file)
        g_dest = os.path.join(dest_dir, f'{slug}-gallery-{idx}.jpg')
        with Image.open(g_src) as im:
            g_im = studio_photoshop_retouch(im, is_hero=False)
            g_im.save(g_dest, 'JPEG', quality=88, optimize=True)
            print(f'  ↳ [GALLERY RETOUCHED {idx}/4] {slug}-gallery-{idx}.jpg: {caption}')

print("\nSTUDIO RETOUCH COMPLETED! 30 PROFESSIONAL ARCHITECTURAL PHOTOS READY.")
