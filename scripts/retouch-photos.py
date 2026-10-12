"""Proje fotoğraflarına tutarlı rötuş: beyaz dengesi, ışık/gölge, gürültü, renk, ton eşitleme.

Kullanım (proje dışı bir Python ortamında):
  uv pip install opencv-python-headless numpy color-matcher
  python scripts/retouch-photos.py <girdi_klasörü> <çıktı_klasörü> [referans.jpg]

Adımlar:
  1. Hafif gürültü temizleme (telefon gürültüsü ve aşırı keskinleştirme izleri)
  2. Gri dünya beyaz dengesi (%60 ölçekli: ortam ışığının karakteri korunur)
  3. Yerel ton dengeleme (CLAHE, %45 karışım): kapalı gölgeler açılır, patlak ışıklar toplanır
  4. Yumuşak S eğrisi ve ışıklarda yumuşak geçiş
  5. Doygunluk: genel -%3, mor/macenta aralığında -%25 (halı, LED taşmaları); marka kırmızıları korunur
  6. (İsteğe bağlı) color-matcher ile referansa eşitleme. Bu sette kırmızıları bozduğu için kullanılmıyor.
"""
import sys
from pathlib import Path

import cv2
import numpy as np

try:
    from color_matcher import ColorMatcher
except ImportError:  # eşitleme isteğe bağlı
    ColorMatcher = None


def white_balance(img, strength=0.6):
    """Beyaz dengesi yalnızca nötr ve açık piksellerden (beyaz duvar, tavan) ölçülür;
    halı, logo gibi büyük renkli alanlar ölçüme girmez."""
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB).astype(np.float32)
    L, a, b = lab[..., 0], lab[..., 1] - 128, lab[..., 2] - 128
    neutral = (L > 150) & (np.hypot(a, b) < 18)
    if neutral.sum() < 2000:
        return img
    lab[..., 1] -= a[neutral].mean() * strength
    lab[..., 2] -= b[neutral].mean() * strength
    return cv2.cvtColor(np.clip(lab, 0, 255).astype(np.uint8), cv2.COLOR_LAB2BGR)


def tone(img):
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    L = lab[..., 0]
    clahe = cv2.createCLAHE(clipLimit=1.6, tileGridSize=(8, 8)).apply(L)
    L = cv2.addWeighted(L, 0.55, clahe, 0.45, 0).astype(np.float32) / 255
    # Yumuşak S eğrisi + ışıklarda yumuşak geçiş (patlamayı önler)
    L = L + 0.10 * (L - 0.5) * (1 - np.abs(2 * L - 1))
    L = np.where(L > 0.88, 0.88 + (L - 0.88) * 0.6, L)
    lab[..., 0] = np.clip(L * 255, 0, 255).astype(np.uint8)
    return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)


def saturation(img):
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV).astype(np.float32)
    h, s = hsv[..., 0], hsv[..., 1]
    factor = np.full_like(s, 0.97)
    magenta = (h >= 135) & (h <= 168)            # OpenCV ton ölçeği 0–180; kırmızılar (0–8, 170–180) korunur
    factor[magenta] = 0.75
    hsv[..., 1] = s * factor
    return cv2.cvtColor(np.clip(hsv, 0, 255).astype(np.uint8), cv2.COLOR_HSV2BGR)


def retouch(img, ref=None):
    img = cv2.fastNlMeansDenoisingColored(img, None, 3, 3, 7, 21)
    img = white_balance(img)
    img = tone(img)
    img = saturation(img)
    if ref is not None and ColorMatcher is not None:
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        ref_rgb = cv2.cvtColor(ref, cv2.COLOR_BGR2RGB)
        matched = ColorMatcher().transfer(src=rgb, ref=ref_rgb, method='mkl')
        matched = cv2.cvtColor(np.clip(matched, 0, 255).astype(np.uint8), cv2.COLOR_RGB2BGR)
        img = cv2.addWeighted(img, 0.7, matched, 0.3, 0)
    return img


def main():
    src, out = Path(sys.argv[1]), Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    ref = None
    if len(sys.argv) > 3:
        ref = retouch(cv2.imread(sys.argv[3]))   # referans da aynı zincirden geçer
    for f in sorted(src.glob('*.jpg')):
        img = cv2.imread(str(f))
        cv2.imwrite(str(out / f.name), retouch(img, ref), [cv2.IMWRITE_JPEG_QUALITY, 90])
        print('✓', f.name)


if __name__ == '__main__':
    main()
