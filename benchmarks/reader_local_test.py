"""Test the upstream fetch function ONLY against our loopback fixture server."""
import asyncio
import importlib.util
import sys
from pathlib import Path
from urllib.parse import urlsplit

url, dest, source = sys.argv[1:]
parsed = urlsplit(url)
if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1':
    raise SystemExit('This benchmark accepts only its local fixture server.')
spec = importlib.util.spec_from_file_location('reader', source)
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)
asyncio.run(reader.fetch_article(url, Path(dest)))
