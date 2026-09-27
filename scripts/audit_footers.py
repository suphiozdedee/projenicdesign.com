import re, sys
sys.stdout.reconfigure(encoding='utf-8')

pages = ['index.html', 'projeler.html', 'yetkinlikler.html', 'hakkimizda.html', 'iletisim.html', 'proje-aurora.html']

for p in pages:
    with open(p, 'r', encoding='utf-8') as f:
        html = f.read()
    
    footer_m = re.search(r'<footer[^>]*>(.*?)</footer>', html, re.DOTALL)
    if not footer_m:
        print(f"=== {p} === NO FOOTER")
        continue
    
    footer = footer_m.group(1)
    headings = re.findall(r'<h[234][^>]*>(.*?)</h[234]>', footer, re.DOTALL)
    clean_h = [re.sub(r'<[^>]+>', ' ', h).strip() for h in headings]
    print(f"=== {p} === Headings: {clean_h}")
