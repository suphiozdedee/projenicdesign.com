import re, os

pages = ['index.html', 'projeler.html', 'yetkinlikler.html', 'hakkimizda.html', 'iletisim.html', 'proje-aurora.html']

for p in pages:
    print(f"\n==================== {p} ====================")
    with open(p, 'r', encoding='utf-8') as f:
        content = f.read()

    # 1. Check container widths
    max_w = set(re.findall(r'max-w-[a-zA-Z0-9]+', content))
    print(f"Max-widths used: {sorted(list(max_w))}")

    # 2. Check header nav structure
    nav_match = re.search(r'<header[^>]*>.*?</header>', content, re.DOTALL)
    if nav_match:
        nav_classes = re.findall(r'class="([^"]*)"', nav_match.group(0))
        print(f"Header classes: {nav_classes[0] if nav_classes else 'none'}")

    # 3. Check section paddings
    section_paddings = re.findall(r'<section[^>]*class="([^"]*)"', content)
    print(f"Sections count: {len(section_paddings)}")
    for i, s in enumerate(section_paddings):
        py = [c for c in s.split() if c.startswith('py-') or 'py-' in c]
        px = [c for c in s.split() if c.startswith('px-') or 'px-' in c]
        print(f"  Section {i+1}: py={py}, px={px}")

    # 4. Check for nested radiuses (Concentric geometry)
    radii = re.findall(r'rounded-[a-zA-Z0-9]+', content)
    radii_counts = {}
    for r in radii:
        radii_counts[r] = radii_counts.get(r, 0) + 1
    print(f"Radii distribution: {radii_counts}")

    # 5. Check button / link heights
    h_classes = set(re.findall(r'\bh-(?:8|9|10|11|12|14|16)\b', content))
    print(f"Standard heights used: {sorted(list(h_classes))}")
