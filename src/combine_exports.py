#!/usr/bin/env python3
"""Convert a supplied HTML archive with the upstream reader's pure parser.

This bridge never calls the reader's browser or image downloader. It preserves
the archive and uses only images already embedded in, or beside, the input.
"""
import argparse
import importlib.util
import json
import re
from pathlib import Path

from bs4 import BeautifulSoup
from prepare_article import collect_assets, prepare, singlefile_url
from html_parser import parse_html
from source_common import output_dir, save_json


def load_reader(path):
    spec = importlib.util.spec_from_file_location('upstream_reader', path)
    reader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reader)
    return reader


def combine(file, out_dir, reader_path):
    path = Path(file).resolve()
    raw = path.read_bytes()
    source_url = singlefile_url(raw)
    validation = parse_html(raw, source_url, allow_local_assets=True)
    if validation['status'] in ('blocked', 'partial', 'needs_browser'):
        raise ValueError('Archive does not contain a usable complete article: ' + validation['status'])
    out = output_dir(out_dir)
    reader = load_reader(reader_path)
    # SingleFile minifies optional closing tags; lxml recovers table structure.
    soup = BeautifulSoup(raw, 'lxml')
    content = soup.select_one('#js_content')
    if content is None:
        content = soup.select_one('article')
        if content is None:
            raise ValueError('No article container in supplied archive.')
        content['id'] = 'js_content'
    # SingleFile may retain hidden DOM nodes. Do not make them visible in MD.
    for element in list(content.select('[hidden], [aria-hidden="true"], [style]')):
        if element.parent is not None and (element.has_attr('hidden') or element.get('aria-hidden') == 'true'
                or re.search(r'display\s*:\s*none|visibility\s*:\s*hidden', element.get('style', ''), re.I)):
            element.decompose()
    # Keep the actual saved image, not stale WeChat lazy-loading URLs.
    refs = []
    images = content.find_all('img')
    for img in images:
        refs.append({'url': img.get('src') or img.get('data-src') or '', 'alt': img.get('alt', '')})
    asset_result = {'image_candidates': refs, 'warnings': []}
    collect_assets(asset_result, path, out)
    for img, asset in zip(images, refs):
        for attr in ('data-src', 'data-original', 'srcset'):
            img.attrs.pop(attr, None)
        if asset.get('path'):
            img['src'] = asset['path']
    meta = reader.extract_metadata(soup, raw.decode('utf-8', errors='replace'))
    meta['title'] = meta.get('title') or validation['original_title']
    meta['publish_time'] = meta.get('publish_time') or validation.get('published_at', '')
    meta['source_url'] = source_url or validation.get('canonical_url', '')
    content_html, codes, _ = reader.process_content(soup)
    markdown = reader.build_markdown(meta, reader.convert_to_markdown(content_html, codes))
    md_path = out / 'article.md'
    md_path.write_text(markdown, encoding='utf-8')
    (out / 'archive.html').write_bytes(raw)
    result = prepare(md_path, out / 'prepared', archive_file=out / 'archive.html')
    result['warnings'].extend(asset_result['warnings'])
    save_json(out / 'prepared/source.json', result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--html-file', required=True)
    parser.add_argument('--out-dir', required=True)
    parser.add_argument('--reader-module', required=True)
    args = parser.parse_args()
    try:
        result = combine(args.html_file, args.out_dir, args.reader_module)
        print(json.dumps({'status': result['status'], 'warnings': result['warnings']}, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False))
        raise SystemExit(2)
