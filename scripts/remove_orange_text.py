import re

def clean_file(path):
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()

    # 1. Replace group-hover:text-[#E84E1C] and hover:text-[#E84E1C]
    content = re.sub(r'\bgroup-hover:text-\[#E84E1C\]\b', '', content)
    content = re.sub(r'\bhover:text-\[#E84E1C\]\b', 'hover:opacity-75', content)

    # 2. Specific text color replacements
    content = content.replace('text-[#E84E1C]">GECE<', 'text-[var(--text)] opacity-80">GECE<')
    content = content.replace('text-[#E84E1C]">GÜNDÜZ<', 'text-[var(--text)] opacity-80">GÜNDÜZ<')
    
    # Links
    content = content.replace('class="projenic-link text-[#E84E1C]"', 'class="projenic-link"')
    
    # Section eyebrow titles
    content = content.replace('tracking-widest text-[#E84E1C] font-bold block mb-2', 'tracking-widest text-[var(--text)] opacity-75 font-semibold block mb-2')
    content = content.replace('tracking-widest text-[#E84E1C] font-bold block', 'tracking-widest text-[var(--text)] opacity-75 font-semibold block')
    
    # Steps / Numbers / Project headers
    content = content.replace('text-[#E84E1C] font-bold block">01', 'text-[var(--muted)] font-mono text-xs font-semibold block">01')
    content = content.replace('text-[#E84E1C] font-bold block">02', 'text-[var(--muted)] font-mono text-xs font-semibold block">02')
    content = content.replace('text-[#E84E1C] font-bold block">03', 'text-[var(--muted)] font-mono text-xs font-semibold block">03')
    content = content.replace('text-[#E84E1C] font-bold block">04', 'text-[var(--muted)] font-mono text-xs font-semibold block">04')
    content = content.replace('text-[#E84E1C] font-bold block">05', 'text-[var(--muted)] font-mono text-xs font-semibold block">05')
    content = content.replace('text-[#E84E1C] font-bold block">ADIM 01<', 'text-[var(--muted)] font-mono text-xs font-semibold block">ADIM 01<')
    content = content.replace('text-[#E84E1C] font-bold block">ADIM 02<', 'text-[var(--muted)] font-mono text-xs font-semibold block">ADIM 02<')
    content = content.replace('text-[#E84E1C] font-bold block">ADIM 03<', 'text-[var(--muted)] font-mono text-xs font-semibold block">ADIM 03<')
    content = content.replace('text-[#E84E1C] font-bold block">ADIM 04', 'text-[var(--muted)] font-mono text-xs font-semibold block">ADIM 04')
    content = content.replace('text-[#E84E1C] font-bold block">ADIM 05<', 'text-[var(--muted)] font-mono text-xs font-semibold block">ADIM 05<')

    # Phone / Price / General text-[#E84E1C]
    content = content.replace('text-[#E84E1C] tabular-nums', 'text-[var(--text)] tabular-nums')
    content = content.replace('text-[#E84E1C] font-semibold', 'text-[var(--text)] font-semibold')
    content = content.replace('text-[#E84E1C] font-bold', 'text-[var(--text)] font-bold')
    content = content.replace('text-[#E84E1C] uppercase font-bold', 'text-[var(--muted)] uppercase font-semibold')
    content = content.replace('text-[#E84E1C] uppercase font-semibold', 'text-[var(--muted)] uppercase font-semibold')
    content = content.replace('text-[#E84E1C] font-mono', 'text-[var(--muted)] font-mono')
    content = content.replace('text-[#E84E1C]', 'text-[var(--text)]')

    # Floating hover pills
    content = content.replace('bg-[#E84E1C] px-3 py-1 rounded', 'bg-[var(--text)] text-[var(--bg)] px-3 py-1 rounded')

    # Bullet dots: replace text-[#E84E1C] bullets with clean neutral or small round dot
    content = content.replace('<span class="text-[#E84E1C]">•</span>', '<span class="inline-block w-1.5 h-1.5 rounded-full bg-[var(--text)] opacity-40"></span>')

    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f'Cleaned {path}')

for p in ['index.html', 'projeler.html', 'proje-aurora.html']:
    clean_file(p)
