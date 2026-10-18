"""Download a pinned public parser source for local conversion and tests only."""
from hashlib import sha256
from pathlib import Path
from urllib.request import urlopen

REVISION = '50b7e63c11a880c991815e4bae6a4a9fb161785a'
EXPECTED_SHA256 = 'ad1140f23d49a3276ed6f4ac9b08050367bbe64a5f1634cf53f2b8b160319424'
URL = f'https://raw.githubusercontent.com/jackwener/wechat-article-to-markdown/{REVISION}/wechat_article_to_markdown.py'
dest = Path(__file__).resolve().parent.parent / '.upstream/wechat_article_to_markdown.py'
dest.parent.mkdir(exist_ok=True)
with urlopen(URL, timeout=30) as response:
    content = response.read()
if sha256(content).hexdigest() != EXPECTED_SHA256:
    raise SystemExit('Upstream hash mismatch; no file was written.')
dest.write_bytes(content)
print(f'Saved pinned upstream parser: {dest.name}; SHA-256: {sha256(content).hexdigest()}')
