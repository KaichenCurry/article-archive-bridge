"""Offline HTML parser; no downloader, browser launch, or script execution."""
import codecs
import json
import re
from urllib.parse import urljoin, urlsplit

def clean_text(text):
    return re.sub(r'\s+', ' ', text or '').strip()


def parse_html(raw, base_url='', encoding=None, allow_local_assets=False):
    from lxml import html
    if not raw.strip():
        raise ValueError('Empty HTML response.')
    declared = re.search(br'charset\s*=\s*[\x22\x27]?\s*([A-Za-z0-9._-]+)', raw[:8192], re.I)
    encoding = declared.group(1).decode('ascii') if declared else encoding or 'utf-8'
    try:
        codecs.lookup(encoding)
    except LookupError:
        encoding = 'utf-8'
    parser = html.HTMLParser(encoding=encoding, no_network=True)
    tree = html.fromstring(raw, parser=parser, base_url=base_url or None)

    def meta(*names):
        for name in names:
            values = tree.xpath('//meta[@property=$n or @name=$n]/@content', n=name)
            if values:
                return clean_text(values[0])
        return ''

    title = meta('og:title', 'twitter:title') or clean_text(' '.join(tree.xpath('//h1[1]//text()')))
    title = title or clean_text(' '.join(tree.xpath('//title/text()')))
    author = meta('author', 'article:author')
    published = meta('article:published_time', 'datePublished', 'pubdate')
    if not author:
        author = clean_text(' '.join(tree.xpath('//*[@id="js_name" or @id="js_author_name"]//text()')))
    if not published:
        published = clean_text(' '.join(tree.xpath('//*[@id="publish_time"]//text()')))
    canonical = tree.xpath('//link[contains(concat(" ",normalize-space(@rel)," ")," canonical ")]/@href')
    canonical = urljoin(base_url, canonical[0]) if canonical else base_url
    if urlsplit(canonical).scheme not in ('http', 'https'):
        canonical = base_url

    structured = []
    def inspect_json(value):
        if isinstance(value, list):
            for item in value:
                inspect_json(item)
        elif isinstance(value, dict):
            kinds = value.get('@type', [])
            kinds = [kinds] if isinstance(kinds, str) else kinds
            if any(k in ('Article', 'NewsArticle', 'BlogPosting', 'TechArticle') for k in kinds):
                structured.append(value)
            if '@graph' in value:
                inspect_json(value['@graph'])
    for script in tree.xpath('//script[@type="application/ld+json"]'):
        try:
            inspect_json(json.loads(script.text or ''))
        except (ValueError, TypeError):
            pass

    paywalled = any(str(x.get('isAccessibleForFree', '')).lower() == 'false' for x in structured)
    for el in list(tree.xpath('//script|//style|//noscript|//nav|//footer|//aside|//form|//button|//template|//*[@hidden or @aria-hidden="true"]')):
        if el.getparent() is not None:
            el.drop_tree()
    # Remove inline-hidden tracking/duplicate content. External CSS needs browser review.
    for el in list(tree.xpath('//*[@style]')):
        if re.search(r'(?:display\s*:\s*none|visibility\s*:\s*hidden)', el.get('style'), re.I):
            if el.getparent() is not None:
                el.drop_tree()
    for br in tree.xpath('//br'):
        br.tail = '\n' + (br.tail or '')

    selectors = [
        ('wechat', '//*[@id="js_content"]'),
        ('article-body', '//*[@itemprop="articleBody"]'),
        ('article', '//article'),
        ('main', '//main|//*[@role="main"]'),
    ]
    container = None
    method = 'body-fallback'
    def score(el):
        body = clean_text(el.text_content())
        links = sum(len(clean_text(a.text_content())) for a in el.xpath('.//a'))
        return len(body) - 0.9 * links
    for name, xpath in selectors:
        candidates = tree.xpath(xpath)
        if candidates:
            candidate = max(candidates, key=score)
            if len(clean_text(candidate.text_content())) >= 30 or candidate.xpath('.//img'):
                container, method = candidate, name
                break
    if container is None:
        candidates = tree.xpath('//div|//section')
        candidates = [x for x in candidates if len(x.xpath('.//p')) >= 2]
        if candidates:
            container = max(candidates, key=score)
        else:
            bodies = tree.xpath('//body')
            container = bodies[0] if bodies else tree

    blocks = []
    assets = []
    asset_ids = {}

    def add_image(el):
        # A saved SingleFile page can retain data-src but put the actual embedded
        # image in src. Keep the live-web behavior unless parsing a local export.
        src = ((el.get('src') or el.get('data-src') or el.get('data-original'))
               if allow_local_assets else
               (el.get('data-src') or el.get('data-original') or el.get('src')))
        if not src and el.get('srcset'):
            src = el.get('srcset').split(',')[0].strip().split()[0]
        if not src:
            return
        if not allow_local_assets or src.startswith('//'):
            src = urljoin(base_url, src)
        scheme = urlsplit(src).scheme
        if scheme not in ('http', 'https') and not (allow_local_assets and scheme in ('', 'data')):
            return
        if src not in asset_ids:
            asset_ids[src] = len(assets)
            assets.append({'url': src, 'alt': clean_text(el.get('alt')), 'downloaded': False})
        blocks.append({'type': 'image', 'asset_index': asset_ids[src]})

    semantic = {'p', 'blockquote', 'pre', 'li', 'figcaption', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}
    def walk(el):
        tag = el.tag.lower() if isinstance(el.tag, str) else ''
        if tag == 'img':
            add_image(el)
            return
        if tag == 'table':
            rows = [[clean_text(c.text_content()) for c in tr.xpath('./th|./td')] for tr in el.xpath('.//tr')]
            blocks.append({'type': 'table', 'rows': [r for r in rows if r]})
            for im in el.xpath('.//img'):
                add_image(im)
            return
        if tag in semantic and not any(child.tag in semantic for child in el.iterdescendants()):
            text = el.text_content().strip() if tag == 'pre' else clean_text(el.text_content())
            if text:
                blocks.append({'type': tag, 'text': text})
            for im in el.xpath('.//img'):
                add_image(im)
            return
        inline_only = not any(child.tag in semantic or child.tag in {'div', 'section', 'article', 'main', 'ul', 'ol', 'table', 'figure'} for child in el)
        if inline_only:
            text = clean_text(el.text_content())
            if text:
                blocks.append({'type': 'p', 'text': text})
            for im in el.xpath('.//img'):
                add_image(im)
            return
        if clean_text(el.text):
            blocks.append({'type': 'p', 'text': clean_text(el.text)})
        for child in el:
            walk(child)
            if clean_text(child.tail):
                blocks.append({'type': 'p', 'text': clean_text(child.tail)})
    walk(container)

    def text_of(items):
        return '\n\n'.join(b.get('text', '\n'.join('\t'.join(row) for row in b.get('rows', [])))
                            for b in items if b.get('type') != 'image')
    text = text_of(blocks)
    best_json = max(structured, key=lambda x: len(str(x.get('articleBody', ''))), default={})
    json_body = best_json.get('articleBody', '')
    if not paywalled and isinstance(json_body, str) and len(json_body) > len(text) * 1.3 and len(json_body) > 100:
        blocks = [{'type': 'p', 'text': clean_text(t)} for t in json_body.splitlines() if clean_text(t)]
        text, method = text_of(blocks), 'json-ld-article'
    title = title or str(best_json.get('headline', ''))
    published = published or str(best_json.get('datePublished', ''))
    if not author:
        who = best_json.get('author', '')
        if isinstance(who, list):
            author = ', '.join(str(x.get('name', '')) if isinstance(x, dict) else str(x) for x in who)
        elif isinstance(who, dict):
            author = str(who.get('name', ''))
        elif isinstance(who, str):
            author = who

    n = len(re.sub(r'\s+', '', text))
    visible = clean_text(tree.text_content())
    gate = re.search(r'环境异常|访问过于频繁|请完成验证|安全验证|验证码|verify you are human|access denied|just a moment', title + ' ' + visible[:4000], re.I)
    login = re.search(r'登录后查看|登录后阅读全文|sign in to continue|subscribe to continue|subscriber.only', visible, re.I)
    status = 'extracted' if n >= 120 and method != 'body-fallback' else 'needs_review'
    warnings = ['Extraction is a candidate; verify ending, images, and source identity.']
    title_gate = re.search(r'环境异常|访问过于频繁|安全验证|verify you are human|access denied|just a moment', title, re.I)
    if gate and (title_gate or n < 120 or method == 'body-fallback'):
        status = 'blocked'
        warnings.append('Access/verification page detected; do not treat it as article content.')
    elif login or paywalled:
        status = 'partial'
        warnings.append('Login/subscription marker detected; content may be incomplete.')
    elif n < 30:
        status = 'needs_browser'
        warnings.append('No usable text body; the page may need rendering or image reading.')
    host = urlsplit(base_url).hostname or ''
    if any(host == domain or host.endswith('.' + domain) for domain in ('xiaohongshu.com', 'xhslink.com', 'x.com', 'twitter.com')):
        if status == 'extracted':
            status = 'needs_review'
        warnings.append('Social content requires browser checks for images, expanded text, or thread completeness.')
    return {'status': status, 'method': method, 'source_url': base_url,
            'canonical_url': canonical, 'original_title': title, 'author': author,
            'published_at': published, 'text_characters': n, 'blocks': blocks,
            'image_candidates': assets, 'warnings': warnings, 'text': text}

