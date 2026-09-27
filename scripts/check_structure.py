import re

pages = {
    'index.html': 'c:/Users/Projenic_Design/Desktop/projenicdesign.com/index.html',
    'projeler.html': 'c:/Users/Projenic_Design/Desktop/projenicdesign.com/projeler.html',
    'yetkinlikler.html': 'c:/Users/Projenic_Design/Desktop/projenicdesign.com/yetkinlikler.html',
    'hakkimizda.html': 'c:/Users/Projenic_Design/Desktop/projenicdesign.com/hakkimizda.html',
    'iletisim.html': 'c:/Users/Projenic_Design/Desktop/projenicdesign.com/iletisim.html',
    'proje-aurora.html': 'c:/Users/Projenic_Design/Desktop/projenicdesign.com/proje-aurora.html',
}

findings = []

for name, path in pages.items():
    with open(path, 'r', encoding='utf-8') as f:
        html = f.read()

    # 1. Check Floating Header consistency
    header_m = re.search(r'<header[^>]*class="([^"]*)"', html)
    header_cls = header_m.group(1) if header_m else 'NO_HEADER'
    
    # Check inner nav container
    nav_container_m = re.search(r'<header.*?<div[^>]*class="([^"]*)"', html, re.DOTALL)
    nav_container_cls = nav_container_m.group(1) if nav_container_m else 'NO_NAV_CONTAINER'
    
    # 2. Check Drawer markup consistency
    has_drawer = 'id="projenic-drawer"' in html
    
    # 3. Check Main container class
    main_m = re.search(r'<main[^>]*class="([^"]*)"', html)
    main_cls = main_m.group(1) if main_m else 'NO_MAIN'
    
    # 4. Check footer consistency
    footer_m = re.search(r'<footer[^>]*class="([^"]*)"', html)
    footer_cls = footer_m.group(1) if footer_m else 'NO_FOOTER'
    
    # 5. Check cards with missing flex-1 / flex-col for height alignment
    grids = re.findall(r'<div[^>]*class="[^"]*grid[^"]*"[^>]*>', html)
    
    findings.append({
        'page': name,
        'header_cls': header_cls,
        'nav_container_cls': nav_container_cls,
        'has_drawer': has_drawer,
        'main_cls': main_cls,
        'footer_cls': footer_cls,
        'grids_count': len(grids)
    })

for f in findings:
    print(f"=== {f['page']} ===")
    print(f"  Main class: {f['main_cls']}")
    print(f"  Nav container: {f['nav_container_cls']}")
    print(f"  Footer class: {f['footer_cls'][:60]}...")
    print(f"  Has Drawer: {f['has_drawer']}")
    print(f"  Grids: {f['grids_count']}")
