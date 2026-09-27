import os, sys
sys.stdout.reconfigure(encoding='utf-8')
from PIL import Image

base = r'D:\PROJENIC BRAND REBUILD\projenic_eski\_projenic_fotoğraflar'
dest_dir = r'c:\Users\Projenic_Design\Desktop\projenicdesign.com\public\assets\images\projects'
os.makedirs(dest_dir, exist_ok=True)

# Selected authentic projects mapping
project_specs = {
    'alp-havacilik': {
        'folder': 'alp havacılık_win fuarı_2024',
        'hero': 'IMG_9943.jpg',
        'gallery': ['IMG_9944.jpg', 'IMG_9945.jpg', 'IMG_9946.jpg', 'IMG_9947.jpg']
    },
    'ulak-haberlesme': {
        'folder': 'ulak haberleşme_idef_2023',
        'hero': 'IMG_5040.JPG',
        'gallery': ['IMG_5042.JPG', 'IMG_5043.JPG', 'IMG_5146.JPG', 'IMG_5150.JPG']
    },
    'yamaha': {
        'folder': 'yamaha_boatshow_2024',
        'hero': 'IMG_7770.jpg',
        'gallery': ['IMG_7813.jpg', 'IMG_7816.jpg', 'IMG_7847.jpg', 'IMG_7852.jpg']
    },
    'dardanel': {
        'folder': 'dardanel_worldfood_2024',
        'hero': 'IMG_9525.jpg',
        'gallery': ['IMG_9529.jpg', 'IMG_9531.jpg', 'IMG_9532.jpg', 'IMG_9534.jpg']
    },
    'mercedes-benz': {
        'folder': 'mercedes benz-türk_ik standı_2023',
        'hero': 'IMG_7259.JPG',
        'gallery': ['IMG_7260.JPG', 'IMG_7261.JPG', 'IMG_7262.JPG', 'IMG_7259.JPG']
    },
    'perotti': {
        'folder': 'perotti_istoç showroom_2024',
        'hero': 'IMG_0342.jpg',
        'gallery': ['IMG_0344.jpg', 'IMG_0348.jpg', 'IMG_0350.jpg', 'IMG_9494.jpg']
    }
}

def process_and_save(src_path, dest_path, max_w=1920):
    with Image.open(src_path) as im:
        if im.mode in ('RGBA', 'P'):
            im = im.convert('RGB')
        w, h = im.size
        if w > max_w:
            new_h = int(h * (max_w / w))
            im = im.resize((max_w, new_h), Image.Resampling.LANCZOS)
        im.save(dest_path, 'JPEG', quality=86, optimize=True)
        print(f'Processed: {os.path.basename(dest_path)} ({im.size[0]}x{im.size[1]} - {os.path.getsize(dest_path)//1024} KB)')

for slug, data in project_specs.items():
    folder_path = os.path.join(base, data['folder'])
    # Hero
    hero_src = os.path.join(folder_path, data['hero'])
    hero_dest = os.path.join(dest_dir, f'{slug}-hero.jpg')
    process_and_save(hero_src, hero_dest, max_w=1920)

    # Gallery
    for idx, g_name in enumerate(data['gallery'], start=1):
        g_src = os.path.join(folder_path, g_name)
        g_dest = os.path.join(dest_dir, f'{slug}-gallery-{idx}.jpg')
        process_and_save(g_src, g_dest, max_w=1920)

print("\nALL 30 AUTHENTIC PROJECT PHOTOS PROCESSED SUCCESSFULLY!")
