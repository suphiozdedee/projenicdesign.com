import re, sys
sys.stdout.reconfigure(encoding='utf-8')

pages = ['index.html', 'projeler.html', 'yetkinlikler.html', 'hakkimizda.html', 'iletisim.html', 'proje-aurora.html']

for p in pages:
    with open(p, 'r', encoding='utf-8') as f:
        html = f.read()
    
    header_m = re.search(r'<header[^>]*>(.*?)</header>', html, re.DOTALL)
    if not header_m:
        print(f"=== {p} === NO HEADER")
        continue
    
    header = header_m.group(1)
    
    # Extract nav links
    nav_m = re.search(r'<nav[^>]*>(.*?)</nav>', header, re.DOTALL)
    nav_links = []
    if nav_m:
        for a_href, a_text in re.findall(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', nav_m.group(1), re.DOTALL):
            t = re.sub(r'<[^>]+>', ' ', a_text).strip()
            # deduplicate repeated rollover words
            words = t.split()
            first_word = words[0] if words else ''
            nav_links.append((a_href, first_word))
            
    # Extract action buttons
    action_m = re.search(r'<div class="nav-actions[^"]*"[^>]*>(.*?)</div>\s*</div>', header, re.DOTALL)
    action_summary = []
    if action_m:
        buttons = re.findall(r'<(?:button|a)[^>]*class="([^"]*)"[^>]*>(.*?)</(?:button|a)>', action_m.group(1), re.DOTALL)
        for b_cls, b_content in buttons:
            text = re.sub(r'<[^>]+>', ' ', b_content).strip()
            action_summary.append((text, 'btn-solid' if 'btn-solid' in b_cls else 'btn-outline' if 'btn-outline' in b_cls else 'other'))
            
    print(f"=== {p} ===")
    print(f"  Nav links: {nav_links}")
    print(f"  Action buttons: {action_summary}")
