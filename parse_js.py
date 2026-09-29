import re
import esprima

with open('index.html', 'r', encoding='utf-8') as f:
    content = f.read()

scripts = re.findall(r'<script>(.*?)</script>', content, re.DOTALL)
for i, s in enumerate(scripts):
    try:
        esprima.parseScript(s)
        print(f'Script {i} OK')
    except Exception as e:
        print(f'Script {i} Syntax Error: {e}')
