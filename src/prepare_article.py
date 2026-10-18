#!/usr/bin/env python3
"""Offline bridge for reader Markdown, SingleFile HTML, and PDF.

Does not launch either browser project, fetch URLs, or modify Feishu.
Requires lxml; Markdown also needs markdown-it-py and PDF needs pypdf.
"""
import argparse
import base64
import hashlib
import html
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qsl, unquote, unquote_to_bytes, urlencode, urlsplit, urlunsplit

from html_parser import parse_html
from source_common import compact, output_dir, save_json

MAX_FILE = 128 * 1024 * 1024
MAX_IMAGE = 25 * 1024 * 1024
IMAGE_EXT = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.avif', '.svg', '.bmp'}
DATA_EXT = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/gif': '.gif',
            'image/webp': '.webp', 'image/avif': '.avif', 'image/svg+xml': '.svg',
            'image/bmp': '.bmp'}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def http_url(value):
    value = html.unescape(value.strip())
    p = urlsplit(value)
    return value if p.scheme in ('http', 'https') and p.hostname and not p.username and not p.password else ''


def normalize_url(value):
    """Conservative identity; never resolve short links or discard article IDs."""
    value = http_url(value)
    if not value:
        return ''
    p = urlsplit(value)
    tracking = {'fbclid', 'gclid'}
    if p.hostname == 'mp.weixin.qq.com':
        tracking |= {'scene', 'from', 'isappinstalled'}
    pairs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() not in tracking]
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path,
                       urlencode(sorted(pairs)), ''))


def singlefile_url(raw):
    # SingleFile's header comment records the original URL, not its local path.
    head = raw[:65536].decode('utf-8', errors='replace')
    for comment in re.findall(r'<!--(.*?)-->', head, re.S):
        if re.search(r'Page saved with SingleFile', comment, re.I):
            m = re.search(r'^\s*url:\s*(\S+)', comment, re.M | re.I)
            return http_url(m.group(1)) if m else ''
    return ''


def markdown_input(raw):
    from markdown_it import MarkdownIt
    text = raw.decode('utf-8-sig')
    meta = {}
    # Strip only this upstream tool's known metadata header, never body quotes.
    header = re.match(r'\A# ([^\n]+)\n((?:\s*\n|> [^\n]*\n)*)---\s*\n', text)
    if header and re.search(r'^> (?:公众号|发布时间|原文链接):', header.group(2), re.M):
        meta['original_title'] = header.group(1).strip()
        fields = {'公众号': 'author', '发布时间': 'published_at', '原文链接': 'source_url'}
        for key, value in re.findall(r'^> (公众号|发布时间|原文链接):\s*(.*)$', header.group(2), re.M):
            meta[fields[key]] = value.strip()
        text = text[header.end():]
    parser = MarkdownIt('commonmark', {'html': True}).enable('table')
    rendered = '<html><meta charset="utf-8"><body><article>' + parser.render(text) + '</article></body></html>'
    return rendered.encode('utf-8'), meta


def collect_assets(result, source_path, out):
    """Copy only explicit in-folder images or embedded data; never fetch a URL."""
    root = source_path.parent.resolve()
    saved = {}
    for item in result.get('image_candidates', []):
        ref = item['url']
        try:
            if ref.startswith('data:'):
                header, payload = ref.split(',', 1)
                mime = header[5:].split(';')[0].lower()
                ext = DATA_EXT.get(mime)
                if not ext:
                    raise ValueError('unsupported embedded image format')
                data = (base64.b64decode(re.sub(r'\s+', '', payload), validate=True)
                        if ';base64' in header.lower() else unquote_to_bytes(payload))
                item['embedded_mime'] = mime
                item['url'] = ''  # Do not duplicate megabytes of base64 in JSON.
            elif urlsplit(ref).scheme in ('http', 'https') or ref.startswith('//'):
                item['status'] = 'remote_not_fetched'
                continue
            else:
                parsed = urlsplit(ref)
                local = (root / unquote(parsed.path)).resolve()
                if parsed.scheme or parsed.netloc or not local.is_relative_to(root):
                    raise ValueError('image outside the supplied article folder')
                ext = local.suffix.lower()
                if ext not in IMAGE_EXT or not local.is_file():
                    raise ValueError('missing local image or unsupported format')
                if local.stat().st_size > MAX_IMAGE:
                    raise ValueError('image exceeds 25 MiB')
                data = local.read_bytes()
            if not data or len(data) > MAX_IMAGE:
                raise ValueError('empty image or image exceeds 25 MiB')
            sha = digest(data)
            if sha not in saved:
                (out / 'images').mkdir(exist_ok=True)
                saved[sha] = f'images/{sha[:20]}{ext}'
                (out / saved[sha]).write_bytes(data)
            item.update(path=saved[sha], sha256=sha, bytes=len(data),
                        downloaded=False, status='local_ready')
        except (ValueError, OSError) as exc:
            if ref.startswith('data:'):
                item['url'] = ''
            item.update(status='unavailable', reason=str(exc))
    incomplete = [a for a in result.get('image_candidates', []) if a.get('status') != 'local_ready']
    if incomplete:
        result['warnings'].append(f'{len(incomplete)} image reference(s) unavailable locally; no network retrieval attempted.')


def compare_sources(identity, paths):
    matches = []
    for path in paths:
        prior = json.loads(Path(path).read_text(encoding='utf-8'))
        other = prior.get('identity', {})
        same_url = bool(identity['url'] and identity['url'] == other.get('url'))
        same_body = bool(identity['body_sha256'] and identity['body_sha256'] == other.get('body_sha256'))
        same_file = identity['file_sha256'] == other.get('file_sha256')
        if same_url or same_body or same_file:
            matches.append({'manifest': str(Path(path).resolve()), 'same_source_url': same_url,
                            'same_body': same_body, 'same_file': same_file,
                            'decision': 'duplicate_candidate' if same_body or same_file else 'same_source_review_content'})
    return matches


def prepare(path, destination, source_url='', compare_to=(), archive_file=None):
    path = Path(path).expanduser().resolve()
    if path.stat().st_size > MAX_FILE:
        raise ValueError('Input exceeds 128 MiB.')
    raw = path.read_bytes()
    suffix = path.suffix.lower()
    if suffix not in ('.html', '.htm', '.md', '.markdown', '.pdf'):
        raise ValueError('Use ordinary HTML, Markdown, or PDF; compressed SingleFile exports are not supported.')
    if source_url and not http_url(source_url):
        raise ValueError('--source-url must be an HTTP(S) source identifier, not a fetch request.')
    out = output_dir(destination)
    if suffix == '.pdf':
        from extract_pdf import extract
        result = extract(path, out)
        text = '\n\n'.join(p['normalized_text'] for p in result['pages'])
        result.update(input_kind='pdf', source_url=source_url, canonical_url=source_url)
    else:
        inferred = singlefile_url(raw) if suffix in ('.html', '.htm') else ''
        md_meta = {}
        parsed_raw = raw
        if suffix in ('.md', '.markdown'):
            parsed_raw, md_meta = markdown_input(raw)
            inferred = http_url(md_meta.get('source_url', ''))
        result = parse_html(parsed_raw, source_url or inferred, allow_local_assets=True)
        for key in ('original_title', 'author', 'published_at'):
            if md_meta.get(key):
                result[key] = md_meta[key]
        if source_url and inferred and normalize_url(source_url) != normalize_url(inferred):
            result['warnings'].append('Supplied source URL differs from file metadata; confirm article identity.')
            result['status'] = 'needs_review'
        result['embedded_source_url'] = inferred
        result['input_kind'] = ('wechat_reader_markdown' if md_meta else 'markdown') if suffix in ('.md', '.markdown') else ('singlefile_html' if b'SingleFile' in raw[:65536] else 'html')
        result['warnings'].append('Source archive retains styling; extracted text is not a pixel-perfect reproduction.')
        if suffix in ('.html', '.htm'):
            result['warnings'].append('CSS background images, compressed archives, and embedded media require visual review.')
        text = result.pop('text')
        (out / 'body.txt').write_text(text, encoding='utf-8')
        collect_assets(result, path, out)
    original = 'original' + suffix
    (out / original).write_bytes(raw)
    result.update(source_file=str(path), original_file=original)
    identity = {'url': normalize_url(result.get('canonical_url') or result.get('source_url', '')),
                'file_sha256': digest(raw), 'body_sha256': digest(compact(text).encode('utf-8')) if compact(text) else ''}
    result['identity'] = identity
    result['duplicate_candidates'] = compare_sources(identity, compare_to)
    result['warnings'].append('Duplicate candidates are local comparisons only; verify existing Feishu documents before skipping or creating.')
    if archive_file:
        archive = Path(archive_file).expanduser().resolve()
        if archive.suffix.lower() not in ('.html', '.htm') or archive.stat().st_size > MAX_FILE:
            raise ValueError('--archive-file requires ordinary HTML up to 128 MiB.')
        archive_raw = archive.read_bytes()
        archive_url = singlefile_url(archive_raw)
        archive_result = parse_html(archive_raw, archive_url, allow_local_assets=True)
        archive_identity = normalize_url(archive_result.get('canonical_url') or archive_url)
        if identity['url'] and archive_identity and identity['url'] != archive_identity:
            raise ValueError('Companion archive is for a different source URL; refusing to pair.')
        if archive_result['status'] in ('blocked', 'partial', 'needs_browser'):
            raise ValueError('Companion archive is not a usable article; refusing to pair.')
        (out / 'archive.html').write_bytes(archive_raw)
        result['companion_archive'] = {'path': 'archive.html', 'source_url': archive_url,
                                       'sha256': digest(archive_raw), 'identity_confirmed': bool(identity['url'] and archive_identity)}
        if not identity['url'] or not archive_identity:
            result['warnings'].append('Companion archive lacks a matching source URL; manually verify it belongs to this article.')
        if compact(text) != compact(archive_result['text']):
            result['warnings'].append('Companion archive text differs from primary text; compare content before faithful editing.')
    save_json(out / 'source.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--file', required=True, help='Local exported HTML / Markdown / PDF')
    parser.add_argument('--out-dir', required=True)
    parser.add_argument('--source-url', default='', help='Metadata only; never requested over the network')
    parser.add_argument('--compare-to', action='append', default=[], help='Existing local source.json to compare; repeatable')
    parser.add_argument('--archive-file', help='Optional matching SingleFile HTML retained beside Markdown')
    args = parser.parse_args()
    try:
        result = prepare(args.file, args.out_dir, args.source_url, args.compare_to, args.archive_file)
        print(json.dumps({k: result.get(k) for k in ('status', 'input_kind', 'original_title', 'identity', 'duplicate_candidates', 'warnings')}, ensure_ascii=False))
        return 0 if result['status'] == 'extracted' else 2
    except (ValueError, OSError, ImportError) as exc:
        print(json.dumps({'status': 'failed', 'message': str(exc)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    sys.exit(main())
