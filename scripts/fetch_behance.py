import urllib.request
import re
import json

req = urllib.request.Request(
    'https://www.behance.net/gallery/247310965/Studio-Namma-Website',
    headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
)
try:
    with urllib.request.urlopen(req) as response:
        html = response.read().decode('utf-8', errors='ignore')
        imgs = re.findall(r'https://mir-s3-cdn-cf\.behance\.net/project_modules/[^"\'\s<>]+', html)
        unique_imgs = list(dict.fromkeys(imgs))
        print(f"Total unique images: {len(unique_imgs)}")
        for img in unique_imgs:
            print(img)
except Exception as e:
    print("Error:", e)
