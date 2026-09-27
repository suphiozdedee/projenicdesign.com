import sys
sys.stdout.reconfigure(encoding='utf-8')
from bs4 import BeautifulSoup
import glob, os

html_files = sorted(glob.glob('*.html'))
print(f'Found {len(html_files)} files: {html_files}\n')

for f in html_files:
    print(f'================ {f} ================')
    with open(f, 'r', encoding='utf-8') as fp:
        soup = BeautifulSoup(fp.read(), 'html.parser')
    
    buttons = soup.find_all('button')
    print(f'Buttons ({len(buttons)}):')
    for b in buttons:
        b_id = b.get('id', '')
        b_type = b.get('type', '')
        b_cls = ' '.join(b.get('class', [])) if isinstance(b.get('class'), list) else b.get('class', '')
        b_txt = b.get_text(strip=True)[:40]
        data_attrs = [f'{k}="{v}"' for k, v in b.attrs.items() if k.startswith('data-') or k.startswith('aria-')]
        print(f'  [BTN] id="{b_id}" type="{b_type}" class="{b_cls[:35]}" | data: {data_attrs} | txt: "{b_txt}"')

    btn_links = soup.find_all('a')
    action_links = []
    for a in btn_links:
        cls = ' '.join(a.get('class', [])) if isinstance(a.get('class'), list) else a.get('class', '')
        href = a.get('href', '')
        if any(x in cls for x in ['btn', 'button', 'projenic-btn', 'rounded']) or href.startswith('#') or href.startswith('javascript:'):
            action_links.append(a)
    print(f'\nAction Links ({len(action_links)}):')
    for a in action_links:
        a_id = a.get('id', '')
        a_href = a.get('href', '')
        a_txt = a.get_text(strip=True)[:40]
        a_cls = ' '.join(a.get('class', [])) if isinstance(a.get('class'), list) else a.get('class', '')
        data_attrs = [f'{k}="{v}"' for k, v in a.attrs.items() if k.startswith('data-')]
        print(f'  [LINK] id="{a_id}" href="{a_href}" class="{a_cls[:35]}" | data: {data_attrs} | txt: "{a_txt}"')
    print()
