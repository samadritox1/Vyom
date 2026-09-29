import urllib.request, re, ssl
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
try:
    html = urllib.request.urlopen('https://mausam.imd.gov.in/', context=ctx).read().decode('utf-8')
    pdfs = set(re.findall(r'href=[\"\']([^\"\']+\.pdf)[\"\']', html, flags=re.IGNORECASE))
    with open('pdfs.txt', 'w', encoding='utf-8') as f:
        for p in pdfs: f.write(p + '\n')
except Exception as e:
    with open('pdfs.txt', 'w') as f: f.write(str(e))
