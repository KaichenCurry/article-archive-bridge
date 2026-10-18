"""Local-only comparison. Every capture target is our 127.0.0.1 server.

SingleFile is evaluated as an HTML archiver, reader as Markdown, bridge as both.
Online WeChat reachability, captcha handling and account access are NOT tested.
"""
import argparse
import base64
import difflib
import functools
import hashlib
import html
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import random
import re
import signal
import statistics
import subprocess
import sys
import threading
import time

from bs4 import BeautifulSoup
from markdown_it import MarkdownIt
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
READER = ROOT / '.upstream/wechat_article_to_markdown.py'
SF = ROOT / 'node_modules/single-file-cli/single-file-node.js'
PYTHON = sys.executable
MD = MarkdownIt('commonmark', {'html': True}).enable('table')


def compact(text):
    return re.sub(r'\s+', '', text)


def run(command, log, timeout=45):
    start = time.perf_counter()
    with log.open('w') as fp:
        proc = subprocess.Popen(command, stdout=fp, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait()
            code = 124
    return {'exit_code': code, 'seconds': time.perf_counter() - start}


def fixture_cases(folder, base, private_root=None):
    cases = []
    phrase = '状态记录发生了什么，记忆保存有用经验，授权决定哪些操作可以执行。'
    body = '<p>' + phrase * 6 + '</p>'
    blue = io.BytesIO(); Image.new('RGB', (96, 64), '#246bce').save(blue, 'PNG')
    png = blue.getvalue(); (folder / 'chart.png').write_bytes(png)
    inline = 'data:image/png;base64,' + base64.b64encode(png).decode()
    cases_data = [
        ('plain', '<h2>基本概念</h2>' + body, None, [], ''),
        ('formatting', '<h2>强调与引用</h2><p><strong>授权边界</strong>需要独立检查。</p><blockquote>长期运行不等于无限授权。</blockquote>' + body, None, [], ''),
        ('code', body + '<section class="code-snippet__fix"><pre data-lang="python"><code>state = {"step": 1}</code><code>print(state["step"])</code></pre></section>', None, [], ''),
        ('table', body + '<table><tr><th>能力</th><th>条件</th></tr><tr><td>执行</td><td>授权</td></tr></table>', None, [], ''),
        ('images', body + f'<img alt="蓝色图表" src="{base}/chart.png" data-src="{base}/chart.png"><img alt="内嵌图表" src="{inline}">', None, [hashlib.sha256(png).hexdigest()] * 2, ''),
        ('hidden', body + '<p style="display:none">HIDDEN_TRACKING_TEXT</p>', body, [], ''),
        ('delayed', body, body, [], '<script>const c=document.getElementById("js_content"); const b=c.innerHTML;c.innerHTML="";setTimeout(()=>c.innerHTML=b,600);</script>'),
        ('generic_article', body, None, [], ''),
    ]
    for name, content, expected, images, script in cases_data:
        title = 'Local fixture ' + name
        tag = '<article>' if name == 'generic_article' else '<div id="js_content">'
        close = '</article>' if name == 'generic_article' else '</div>'
        raw = f'<!doctype html><html><head><meta charset="utf-8"><title>{title}</title><style>body{{font-family:sans-serif;max-width:720px;margin:auto}}img{{max-width:96px}}</style></head><body><h1 id="activity-name">{title}</h1><span id="js_name">Fixture Author</span><span id="publish_time">2026-10-18</span>{tag}{content}{close}<footer>OUTSIDE_ARTICLE_NAVIGATION</footer>{script}</body></html>'
        (folder / f'{name}.html').write_text(raw)
        expected = BeautifulSoup(expected or content, 'html.parser').get_text()
        cases.append({'id': name, 'valid': True, 'expected': expected, 'images': images, 'private': False})
    for name, raw in [
        ('verification', '<title>环境异常</title><main>请完成验证后访问</main>'),
        ('empty_shell', '<title>Loading</title><div id="app"></div>')]:
        (folder / f'{name}.html').write_text('<meta charset="utf-8">' + raw)
        cases.append({'id': name, 'valid': False, 'expected': '', 'images': [], 'private': False})
    if private_root:
        for i in (1, 2):
            paragraphs = json.loads((Path(private_root) / f'source-paragraphs-{i:02d}.json').read_text())
            name = f'provided_pdf_text_{i:02d}'
            raw = '<meta charset="utf-8"><title>Private provided PDF text</title><h1 id="activity-name">Private provided PDF text</h1><span id="js_name">Provided source</span><div id="js_content">' + ''.join('<p>' + html.escape(p) + '</p>' for p in paragraphs) + '</div>'
            (folder / f'{name}.html').write_text(raw)
            cases.append({'id': name, 'valid': True, 'expected': ''.join(paragraphs), 'images': [], 'private': True})
    return cases


def image_hash(ref, parent):
    try:
        if ref.startswith('data:'):
            header, payload = ref.split(',', 1)
            if ';base64' not in header:
                return None
            data = base64.b64decode(payload)
        elif not re.match(r'^[a-z]+:', ref):
            data = (parent / ref).read_bytes()
        else:
            return None
        return hashlib.sha256(data).hexdigest()
    except Exception:
        return None


def evaluate(case, artifact, method):
    if not artifact or not artifact.exists():
        return {'artifact': False, 'text_recall': 0.0, 'text_precision': 0.0, 'text_exact': False, 'images_expected': len(case['images']), 'images_retained': 0, 'complete': False}
    raw = artifact.read_text()
    if method != 'singlefile':
        header = re.match(r'\A# [^\n]+\n((?:\s*\n|> [^\n]*\n)*)---\s*\n', raw)
        if header:
            raw = raw[header.end():]
        tree = BeautifulSoup(MD.render(raw), 'html.parser')
    else:
        soup = BeautifulSoup(raw, 'html.parser')
        tree = soup.select_one('#js_content') or soup.select_one('article')
        if tree is None:
            tree = BeautifulSoup('', 'html.parser')
        # Evaluate the visible archive text, not hidden source DOM retained by SF.
        for element in list(tree.select('[hidden], [aria-hidden="true"], [style]')):
            if element.parent is not None and (element.has_attr('hidden') or element.get('aria-hidden') == 'true'
                    or re.search(r'display\s*:\s*none|visibility\s*:\s*hidden', element.get('style', ''), re.I)):
                element.decompose()
    for tag in tree.select('script,style'):
        tag.decompose()
    actual = compact(tree.get_text())
    expected = compact(case['expected'])
    matched = sum(x.size for x in difflib.SequenceMatcher(None, expected, actual, autojunk=False).get_matching_blocks()) if expected else 0
    hashes = [image_hash(x.get('src', ''), artifact.parent) for x in tree.find_all('img')]
    retained = 0
    for target in case['images']:
        if target in hashes:
            hashes.remove(target); retained += 1
    return {'artifact': True, 'text_recall': matched / len(expected) if expected else None,
            'text_precision': matched / len(actual) if actual else 0.0,
            'text_exact': actual == expected, 'images_expected': len(case['images']),
            'images_retained': retained,
            'complete': bool(expected and actual == expected and retained == len(case['images']))}


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out-dir', required=True)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--private-root')
    parser.add_argument('--cases', help='Comma-separated subset for smoke checks')
    args = parser.parse_args()
    out = Path(args.out_dir).resolve(); out.mkdir(parents=True, exist_ok=False)
    fixtures = out / 'fixtures'; fixtures.mkdir()
    server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(QuietHandler, directory=str(fixtures)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_port}'
    cases = fixture_cases(fixtures, base, args.private_root)
    if args.cases:
        cases = [x for x in cases if x['id'] in args.cases.split(',')]
    records = []
    # Sequential captures prevent concurrent browsers from biasing timings.
    # Deterministic random ordering reduces systematic first/last position bias.
    jobs = [(rep, case, method) for rep in range(args.repeats) for case in cases for method in ('reader', 'singlefile', 'combined')]
    random.Random(20261018).shuffle(jobs)
    try:
        for number, (rep, case, method) in enumerate(jobs, 1):
            dest = out / f'{case["id"]}-{method}-{rep}'; dest.mkdir()
            url = base + '/' + case['id'] + '.html'
            if method == 'reader':
                capture = run([PYTHON, str(ROOT / 'benchmarks/reader_local_test.py'), url, str(dest / 'capture'), str(READER)], dest / 'capture.log')
                artifact = next((dest / 'capture').glob('*/*.md'), None)
                total = capture['seconds']
            else:
                archive = dest / 'capture.html'
                capture = run(['node', str(SF), url, str(archive), '--group-duplicate-images=false'], dest / 'capture.log')
                total = capture['seconds']; artifact = archive
                if method == 'combined':
                    bridge = run([PYTHON, str(ROOT / 'src/combine_exports.py'), '--html-file', str(archive), '--out-dir', str(dest / 'combined'), '--reader-module', str(READER)], dest / 'bridge.log') if capture['exit_code'] == 0 else {'exit_code': capture['exit_code'], 'seconds': 0}
                    artifact = dest / 'combined/article.md'
                    total += bridge['seconds']
            record = {'case': case['id'], 'private': case['private'], 'valid_article': case['valid'], 'method': method, 'repeat': rep,
                      'seconds': total, 'capture_exit_code': capture['exit_code'], **evaluate(case, artifact, method)}
            if method == 'combined':
                record.update(bridge_exit_code=bridge['exit_code'], bridge_seconds=bridge['seconds'])
            record['declared_success'] = capture['exit_code'] == 0 and (method != 'combined' or bridge['exit_code'] == 0)
            records.append(record)
            (out / 'records.json').write_text(json.dumps(records, ensure_ascii=False, indent=2))
            print(f'{number}/{len(jobs)} {method:10} {case["id"]:24} {total:.2f}s complete={record["complete"]} exit={capture["exit_code"]}', flush=True)
    finally:
        server.shutdown(); server.server_close()
    summary = {'scope': 'loopback fixtures only; online WeChat success rate unverified', 'repeats': args.repeats, 'cases': len(cases), 'results': {}}
    for method in ('reader', 'singlefile', 'combined'):
        good = [r for r in records if r['method'] == method and r['valid_article']]
        bad = [r for r in records if r['method'] == method and not r['valid_article']]
        summary['results'][method] = {
            'valid_runs': len(good), 'complete_runs': sum(r['complete'] for r in good),
            'declared_success_runs': sum(r['declared_success'] for r in good),
            'mean_text_recall': statistics.mean(r['text_recall'] for r in good),
            'mean_text_precision': statistics.mean(r['text_precision'] for r in good),
            'images_expected': sum(r['images_expected'] for r in good),
            'images_retained': sum(r['images_retained'] for r in good),
            'median_seconds_valid': statistics.median(r['seconds'] for r in good),
            'negative_runs': len(bad), 'negative_reported_success': sum(r['declared_success'] for r in bad),
            'cases': {c['id']: {'complete': sum(r['complete'] for r in good if r['case'] == c['id']),
                                'median_seconds': statistics.median(r['seconds'] for r in records if r['method'] == method and r['case'] == c['id'])}
                      for c in cases}}
    (out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
