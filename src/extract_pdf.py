#!/usr/bin/env python3
"""Extract every PDF page without fixed header cropping or guessed paragraph joins."""
import argparse
from collections import Counter
import hashlib
import json
import logging
import re
import sys
from pathlib import Path
from source_common import normalize_glyphs, output_dir, save_json


def extract(path, out, render=False):
    from pypdf import PdfReader
    reader = PdfReader(path)
    if reader.is_encrypted and not reader.decrypt(''):
        raise ValueError('Password protected PDF; ask the user for an accessible copy.')
    pages, links, warnings, assets = [], [], [], []
    (out / 'images').mkdir()
    for i, page in enumerate(reader.pages, 1):
        raw_text = page.extract_text() or ''
        normalized = normalize_glyphs(raw_text)
        rec = {'page': i, 'width': float(page.mediabox.width), 'height': float(page.mediabox.height),
               'raw_text': raw_text, 'normalized_text': normalized, 'images': []}
        for annotation in page.get('/Annots', []):
            action = annotation.get_object().get('/A')
            action = action.get_object() if action else {}
            uri = str(action.get('/URI', ''))
            if uri.startswith(('https://', 'http://')) and uri not in links:
                links.append(uri)
        try:
            for j, im in enumerate(page.images, 1):
                ext = Path(im.name).suffix.lower()
                ext = ext if ext in ('.png', '.jpg', '.jpeg', '.jp2', '.tiff', '.tif', '.bmp') else '.bin'
                relative = f'images/page-{i:03d}-image-{j:02d}{ext}'
                (out / relative).write_bytes(im.data)
                info = {'path': relative, 'page': i, 'source_name': im.name,
                        'bytes': len(im.data), 'sha256': hashlib.sha256(im.data).hexdigest()}
                if im.image is not None:
                    info['width'], info['height'] = im.image.size
                assets.append(info)
                rec['images'].append(relative)
        except Exception as exc:
            warnings.append(f'Page {i}: image extraction incomplete ({type(exc).__name__}); inspect the page render.')
        pages.append(rec)
    # Flag pages consisting only of repeated print headers/footers, without
    # removing any source text. Repeated lines can also be legitimate content.
    def line_key(line):
        return re.sub(r'\d+', '#', re.sub(r'\s+', '', line))
    counts = Counter(key for page in pages for key in
                     {line_key(line) for line in page['normalized_text'].splitlines() if line.strip()})
    repeated = {key for key, count in counts.items() if len(pages) >= 3 and count >= max(3, (len(pages) + 1) // 2)}
    for page in pages:
        nonrepeated = ''.join(line for line in page['normalized_text'].splitlines() if line_key(line) not in repeated)
        if len(re.sub(r'\s+', '', nonrepeated)) < 30:
            warnings.append(f'Page {page["page"]}: little non-repeated selectable text; inspect for scans/images/blank media.')
    if render:
        try:
            import pdfplumber
            logging.getLogger('pdfminer').setLevel(logging.ERROR)
            (out / 'pages').mkdir()
            with pdfplumber.open(path) as pdf:
                for i, page in enumerate(pdf.pages, 1):
                    page.to_image(resolution=120).original.convert('RGB').save(out / 'pages' / f'page-{i:03d}.jpg', quality=90)
                    pages[i - 1]['render'] = f'pages/page-{i:03d}.jpg'
        except Exception as exc:
            warnings.append(f'Page rendering incomplete ({type(exc).__name__}); use the PDF skill renderer.')
    result = {'status': 'needs_visual_review', 'source_file': str(path.resolve()),
              'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
              'page_count': len(pages), 'metadata': {str(k): str(v) for k, v in (reader.metadata or {}).items()},
              'links': links, 'pages': pages, 'images': assets, 'warnings': warnings,
              'repeated_line_candidates': sorted(repeated)}
    save_json(out / 'source.json', result)
    (out / 'body.txt').write_text('\n\n'.join(f'===== PAGE {p["page"]} =====\n{p["normalized_text"]}' for p in pages), encoding='utf-8')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--file', required=True)
    parser.add_argument('--out-dir', required=True)
    parser.add_argument('--render', action='store_true')
    args = parser.parse_args()
    try:
        out = output_dir(args.out_dir)
        result = extract(Path(args.file).expanduser(), out, args.render)
        print(json.dumps({'status': result['status'], 'pages': result['page_count'],
                          'images': len(result['images']), 'warnings': result['warnings']}, ensure_ascii=False))
        return 0
    except Exception as exc:
        error = {'status': 'failed', 'message': str(exc)}
        if 'out' in locals():
            save_json(out / 'source.json', error)
        print(json.dumps(error, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    sys.exit(main())
