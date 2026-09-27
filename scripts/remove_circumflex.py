import glob
import os

replacements = {
    'â': 'a',
    'î': 'i',
    'û': 'u',
    'Â': 'A',
    'Î': 'I',
    'Û': 'U',
}

files = sorted(glob.glob('*.html')) + sorted(glob.glob('src/**/*.js', recursive=True)) + sorted(glob.glob('src/**/*.css', recursive=True))

total_replaced = 0
for file_path in files:
    if not os.path.isfile(file_path):
        continue
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    original_content = content
    file_replaced = 0
    for circumflex, normal in replacements.items():
        count = content.count(circumflex)
        if count > 0:
            content = content.replace(circumflex, normal)
            file_replaced += count

    if file_replaced > 0:
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f'{file_path}: Replaced {file_replaced} circumflex characters.')
        total_replaced += file_replaced

print(f'\nDone! Total replaced across all files: {total_replaced}')
