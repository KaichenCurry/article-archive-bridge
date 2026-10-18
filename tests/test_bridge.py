import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
from prepare_article import prepare, normalize_url
from combine_exports import combine

READER = Path(__file__).resolve().parent.parent / '.upstream/wechat_article_to_markdown.py'
BODY = '状态与记忆需要独立验证。授权明确之后，才能执行外部操作。' * 6
URL = 'https://example.com/article?id=2'


def archive(body=BODY, url=URL, extra=''):
    return f'<!doctype html><!-- Page saved with SingleFile\n url: {url}\n saved date: 2026-10-18\n --><html><meta charset="utf-8"><title>原标题</title><h1 id="activity-name">原标题</h1><span id="js_name">作者</span><div id="js_content"><p>{body}</p>{extra}</div></html>'


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_archive_preserved_images_and_no_network(self):
        payload = b'fixture image bytes'
        data = base64.b64encode(payload).decode()
        raw = archive(extra=f'<img src="data:image/png;base64,{data}" data-src="https://example.com/stale.png">')
        path = self.root / 'saved.html'; path.write_text(raw)
        with patch('socket.socket', side_effect=AssertionError('Network use in offline importer')):
            result = prepare(path, self.root / 'out')
        self.assertEqual(result['identity']['url'], URL)
        self.assertEqual(result['input_kind'], 'singlefile_html')
        self.assertEqual((self.root / 'out/original.html').read_text(), raw)
        img = result['image_candidates'][0]
        self.assertEqual(img['status'], 'local_ready')
        self.assertEqual((self.root / 'out' / img['path']).read_bytes(), payload)

    def test_reader_metadata_and_cross_format_duplicate(self):
        htmlfile = self.root / 'a.html'; htmlfile.write_text(archive())
        prepare(htmlfile, self.root / 'a')
        mdfile = self.root / 'a.md'; mdfile.write_text(f'# 原标题\n\n> 公众号: 作者\n> 发布时间: 2026-10-18\n> 原文链接: {URL}\n\n---\n{BODY}')
        result = prepare(mdfile, self.root / 'b', compare_to=[self.root / 'a/source.json'])
        self.assertEqual(result['author'], '作者')
        self.assertEqual(result['published_at'], '2026-10-18')
        self.assertEqual((self.root / 'b/body.txt').read_text(), BODY)
        self.assertTrue(result['duplicate_candidates'][0]['same_body'])
        self.assertTrue(result['duplicate_candidates'][0]['same_source_url'])

    def test_external_image_reference_not_copied(self):
        (self.root / 'private.png').write_bytes(b'not part of the article')
        sub = self.root / 'input'; sub.mkdir()
        path = sub / 'a.md'; path.write_text(f'# 标题\n\n{BODY}\n\n![bad](../private.png)')
        result = prepare(path, self.root / 'out')
        self.assertEqual(result['image_candidates'][0]['status'], 'unavailable')
        self.assertFalse((self.root / 'out/images').exists())

    def test_source_id_parameters_not_removed(self):
        first = normalize_url('https://mp.weixin.qq.com/s?__biz=A&mid=1&idx=1&sn=B&scene=2')
        same = normalize_url('https://mp.weixin.qq.com/s?sn=B&idx=1&mid=1&__biz=A&scene=3')
        other = normalize_url('https://mp.weixin.qq.com/s?sn=B&idx=2&mid=1&__biz=A')
        self.assertEqual(first, same); self.assertNotEqual(first, other)

    def test_verification_page_not_article(self):
        path = self.root / 'gate.html'; path.write_text('<meta charset="utf-8"><title>环境异常</title><main>请完成验证</main>')
        self.assertEqual(prepare(path, self.root / 'out')['status'], 'blocked')
        with self.assertRaises(ValueError):
            combine(path, self.root / 'combined', READER)

    def test_existing_output_never_overwritten(self):
        path = self.root / 'a.html'; path.write_text(archive())
        out = self.root / 'out'; out.mkdir(); (out / 'keep').write_text('unchanged')
        with self.assertRaises(ValueError):
            prepare(path, out)
        self.assertEqual((out / 'keep').read_text(), 'unchanged')

    def test_mismatched_companion_rejected(self):
        source = self.root / 'a.md'; source.write_text(f'# 标题\n\n> 原文链接: {URL}\n\n---\n{BODY}')
        companion = self.root / 'b.html'; companion.write_text(archive(url='https://example.com/different'))
        with self.assertRaises(ValueError):
            prepare(source, self.root / 'out', archive_file=companion)

    def test_complete_combination(self):
        path = self.root / 'a.html'; path.write_text(archive())
        result = combine(path, self.root / 'out', READER)
        self.assertEqual(result['status'], 'extracted')
        self.assertTrue((self.root / 'out/article.md').exists())
        self.assertEqual((self.root / 'out/archive.html').read_bytes(), path.read_bytes())
        self.assertTrue(result['companion_archive']['identity_confirmed'])

    def test_hidden_dom_not_exposed_by_markdown(self):
        path = self.root / 'a.html'; path.write_text(archive(extra='<p style="display:none">HIDDEN_TRACKING_TEXT</p>'))
        combine(path, self.root / 'out', READER)
        self.assertNotIn('HIDDEN_TRACKING_TEXT', (self.root / 'out/article.md').read_text())
        self.assertIn('HIDDEN_TRACKING_TEXT', (self.root / 'out/archive.html').read_text())

    def test_empty_pdf_requires_visual_review(self):
        from pypdf import PdfWriter
        source = self.root / 'blank.pdf'
        writer = PdfWriter(); writer.add_blank_page(width=300, height=300)
        writer.write(source)
        result = prepare(source, self.root / 'out')
        self.assertEqual(result['status'], 'needs_visual_review')
        self.assertEqual(result['page_count'], 1)
        self.assertTrue(result['warnings'])

    def test_minified_html_table_survives_conversion(self):
        from bs4 import BeautifulSoup
        from markdown_it import MarkdownIt
        source = self.root / 'table.html'
        source.write_text(archive(extra='<table><tr><th>A<th>B<tr><td>C<td>D</table>'))
        combine(source, self.root / 'out', READER)
        rendered = MarkdownIt('commonmark').enable('table').render((self.root / 'out/article.md').read_text())
        table = BeautifulSoup(rendered, 'html.parser').find('table')
        self.assertEqual([[c.get_text(strip=True) for c in row.find_all(['th','td'])] for row in table.find_all('tr')], [['A','B'],['C','D']])


if __name__ == '__main__':
    unittest.main(verbosity=2)
