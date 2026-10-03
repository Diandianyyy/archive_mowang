"""Publish only APT indexes; serve packages from this archive's GitHub commit."""
import bz2
import gzip
import hashlib
import html
import lzma
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import quote

repository = os.environ['GITHUB_REPOSITORY']
commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
base = f'https://raw.githubusercontent.com/{repository}/{commit}/'
index = Path('Packages').read_text(encoding='utf-8')
index = re.sub(r'^Filename: (.+)$', lambda m: 'Filename: ' + base + quote(m[1].removeprefix('./'), safe='/'), index, flags=re.M)
payload = index.encode('utf-8')
site = Path('_site')
site.mkdir(exist_ok=True)
files = {'Packages': payload, 'Packages.xz': lzma.compress(payload),
         'Packages.bz2': bz2.compress(payload), 'Packages.gz': gzip.compress(payload, mtime=0)}
for name, data in files.items():
    (site / name).write_bytes(data)
release = Path('Release').read_text(encoding='utf-8').rstrip() + '\nSHA256:\n'
for name, data in files.items():
    release += f' {hashlib.sha256(data).hexdigest()} {len(data)} {name}\n'
(site / 'Release').write_text(release, encoding='utf-8')
count = len(re.findall(r'^Package:', index, flags=re.M))
(site / 'index.html').write_text(
    '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    '<title>Mowang 源备份</title><h1>Mowang 源备份</h1>'
    f'<p>已备份 {count} 个安装包。将当前页面地址添加到包管理器即可使用。</p>'
    '<p>设置为每 6 小时检查更新（GitHub 调度可能延迟），保留已备份的历史版本。</p>'
    f'<p><a href="https://github.com/{html.escape(repository)}">查看备份仓库</a></p></html>',
    encoding='utf-8')
print(f'Published index for {count} packages at commit {commit}')
