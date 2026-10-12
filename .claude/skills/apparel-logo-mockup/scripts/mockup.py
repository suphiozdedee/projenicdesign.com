"""Kıyafet üzerine gerçekçi logo baskısı: IOPaint (LaMa) ile temizleme + displacement/ışık eşlemeli baskı.

Ayrıntılar ve örnek komut: ../SKILL.md
"""
import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np


def parse_box(s):
    x1, y1, x2, y2 = (int(v) for v in s.split(','))
    return x1, y1, x2, y2


def build_erase_mask(img, boxes, thresh, grow):
    """Kutular içindeki parlak (yazı) pikselleri maskeler; LaMa'nın kenar izlerini de silmesi için genişletir."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    mask = np.zeros(gray.shape, np.uint8)
    for x1, y1, x2, y2 in boxes:
        roi = gray[y1:y2, x1:x2]
        # Kumaşın yerel parlaklığına göre eşik: koyu kumaş üzerindeki açık yazı
        local = max(thresh, int(np.percentile(roi, 35)) + 40)
        mask[y1:y2, x1:x2] = (roi > local).astype(np.uint8) * 255
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * grow + 1, 2 * grow + 1))
    return cv2.dilate(mask, k)


def lama_inpaint(img, mask, iopaint_bin, device):
    """IOPaint CLI ile LaMa inpainting (iopaint run --model=lama)."""
    tmp = Path(tempfile.mkdtemp(prefix='mockup-'))
    try:
        (tmp / 'in').mkdir(); (tmp / 'mask').mkdir(); (tmp / 'out').mkdir()
        cv2.imwrite(str(tmp / 'in' / 'img.png'), img)
        cv2.imwrite(str(tmp / 'mask' / 'img.png'), mask)
        cmd = [iopaint_bin, 'run', '--model=lama', f'--device={device}',
               f'--image={tmp / "in"}', f'--mask={tmp / "mask"}', f'--output={tmp / "out"}']
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out = cv2.imread(str(tmp / 'out' / 'img.png'))
        if out is None:
            raise RuntimeError('IOPaint çıktı üretmedi')
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def load_logo_alpha(path):
    """Logonun şeklini 0–1 alfa olarak döndürür (şeffaf PNG ya da koyu zemin üstü beyaz logo)."""
    logo = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if logo is None:
        sys.exit(f'Logo okunamadı: {path}')
    if logo.ndim == 3 and logo.shape[2] == 4:
        a = logo[..., 3].astype(np.float32) / 255
    else:
        g = cv2.cvtColor(logo, cv2.COLOR_BGR2GRAY) if logo.ndim == 3 else logo
        a = g.astype(np.float32) / 255
    ys, xs = np.where(a > 0.02)
    return a[ys.min():ys.max() + 1, xs.min():xs.max() + 1]


def place_logo(alpha, shape, center, width, rotate, skew):
    """Logoyu hedef genişlik, döndürme ve yatay perspektifle tuval üzerine yerleştirir."""
    # Önce kenar yumuşatmalı küçült (INTER_AREA), sonra perspektif: keskin ama tırtıksız harfler
    h0, w0 = alpha.shape
    tw = max(int(round(width * 2)), 8)
    alpha = cv2.resize(alpha, (tw, max(int(round(h0 * tw / w0)), 2)), interpolation=cv2.INTER_AREA)
    alpha = np.pad(alpha, 4)
    h, w = alpha.shape
    scale = width / (w - 8)
    lw, lh = w * scale, h * scale
    cx, cy = center
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    # Yatay perspektif: bir kenar hafif uzun, diğeri kısa (sırtın kameraya göre dönmesi)
    dst = np.float32([
        [-lw / 2, -lh / 2 * (1 - skew)], [lw / 2, -lh / 2 * (1 + skew)],
        [lw / 2, lh / 2 * (1 + skew)], [-lw / 2, lh / 2 * (1 - skew)]])
    t = np.deg2rad(rotate)
    rot = np.float32([[np.cos(t), -np.sin(t)], [np.sin(t), np.cos(t)]])
    dst = dst @ rot.T + np.float32([cx, cy])
    M = cv2.getPerspectiveTransform(src, dst)
    return cv2.warpPerspective(alpha, M, (shape[1], shape[0]), flags=cv2.INTER_AREA)


def print_on_fabric(base, alpha, strength, ink, opacity):
    """Displacement map ile kıvrımlara bükme + kumaş ışığıyla gölgeleme + doku."""
    gray = cv2.cvtColor(base, cv2.COLOR_BGR2GRAY).astype(np.float32)
    ys, xs = np.where(alpha > 0.01)
    pad = 40
    y1, y2 = max(ys.min() - pad, 0), min(ys.max() + pad, gray.shape[0])
    x1, x2 = max(xs.min() - pad, 0), min(xs.max() + pad, gray.shape[1])

    # 1) Displacement: büyük kıvrımlar için bulanık parlaklık haritasının gradyanı
    height = cv2.GaussianBlur(gray, (0, 0), 7)
    gx = cv2.Sobel(height, cv2.CV_32F, 1, 0, ksize=5)
    gy = cv2.Sobel(height, cv2.CV_32F, 0, 1, ksize=5)
    norm = max(np.abs(gx[y1:y2, x1:x2]).max(), np.abs(gy[y1:y2, x1:x2]).max(), 1e-3)
    mx, my = np.meshgrid(np.arange(gray.shape[1], dtype=np.float32), np.arange(gray.shape[0], dtype=np.float32))
    warped = cv2.remap(alpha, mx - gx / norm * strength, my - gy / norm * strength, cv2.INTER_LINEAR)

    # 2) Işık: kumaşın yerel parlaklığı (kıvrım tepeleri açık, çukurlar koyu)
    shade = cv2.GaussianBlur(gray, (0, 0), 3)
    region = shade[y1:y2, x1:x2]
    rel = shade / max(float(np.median(region[warped[y1:y2, x1:x2] > 0.3])) if (warped[y1:y2, x1:x2] > 0.3).any() else float(np.median(region)), 1.0)
    light = np.clip(0.86 + 0.14 * rel, 0.78, 1.04)

    # 3) Doku: kumaş örgüsünün ince detayı mürekkebin içinden hafifçe görünür
    # JPEG gürültüsünü değil örgüyü almak için önce hafif yumuşat
    soft = cv2.GaussianBlur(gray, (0, 0), 0.8)
    detail = soft - cv2.GaussianBlur(soft, (0, 0), 2.5)
    texture = 1 + np.clip(detail / 90.0, -0.035, 0.035)

    # Kenarlar: serigrafi mürekkebi gibi çok hafif yumuşak
    a = (np.clip(warped, 0, 1) * opacity)[..., None]
    ink_col = np.clip(ink * light * texture, 0, 255)[..., None]
    # Kırık beyaz mürekkep (hafif sıcak), sahnenin renk ısısına uyum
    ink_rgb = np.concatenate([ink_col * 0.97, ink_col * 0.985, ink_col], axis=2)
    out = base.astype(np.float32) * (1 - a) + ink_rgb * a
    return np.clip(out, 0, 255).astype(np.uint8)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--image', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--logo', required=True)
    p.add_argument('--erase-box', action='append', type=parse_box, default=[])
    p.add_argument('--center', required=True, type=lambda s: tuple(float(v) for v in s.split(',')))
    p.add_argument('--width', required=True, type=float)
    p.add_argument('--rotate', type=float, default=0.0)
    p.add_argument('--skew', type=float, default=0.0)
    p.add_argument('--strength', type=float, default=3.0)
    p.add_argument('--ink', type=float, default=228.0)
    p.add_argument('--opacity', type=float, default=0.93)
    p.add_argument('--thresh', type=int, default=70)
    p.add_argument('--grow', type=int, default=4)
    p.add_argument('--iopaint', default=str(Path(sys.executable).with_name('iopaint')))
    p.add_argument('--device', default='cpu')
    p.add_argument('--debug-dir')
    args = p.parse_args()

    img = cv2.imread(args.image)
    if img is None:
        sys.exit(f'Görsel okunamadı: {args.image}')
    base = img
    if args.erase_box:
        mask = build_erase_mask(img, args.erase_box, args.thresh, args.grow)
        base = lama_inpaint(img, mask, args.iopaint, args.device)
        if args.debug_dir:
            Path(args.debug_dir).mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(Path(args.debug_dir) / 'mask.png'), mask)
            cv2.imwrite(str(Path(args.debug_dir) / 'clean.png'), base)

    alpha = place_logo(load_logo_alpha(args.logo), base.shape, args.center, args.width, args.rotate, args.skew)
    result = print_on_fabric(base, alpha, args.strength, args.ink, args.opacity)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(args.out, result, [cv2.IMWRITE_JPEG_QUALITY, 92])
    print('✓', args.out)


if __name__ == '__main__':
    main()
