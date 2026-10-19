"""Bounded public web research. Search snippets are never nutrition evidence."""
import ipaddress
import json
import re
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit

import certifi
import urllib3
from bs4 import BeautifulSoup
from ddgs import DDGS

MAX_BYTES = 5 * 1024 * 1024
MAX_DOCS = 5


def public_target(url):
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise ValueError('Only public HTTP pages are supported')
    port = parts.port or (443 if parts.scheme == 'https' else 80)
    if port not in (80, 443):
        raise ValueError('Unsupported port')
    addresses = sorted({item[4][0] for item in socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)})
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise ValueError('Non-public network address')
    return parts, port, addresses[0]


def fetch(url):
    """Pin a validated public IP; validate redirects afresh, retaining TLS SNI."""
    for _ in range(4):
        parts, port, address = public_target(url)
        kwargs = dict(timeout=urllib3.Timeout(connect=6, read=10), retries=False, maxsize=1)
        if parts.scheme == 'https':
            pool = urllib3.HTTPSConnectionPool(address, port, server_hostname=parts.hostname,
                assert_hostname=parts.hostname, cert_reqs='CERT_REQUIRED', ca_certs=certifi.where(), **kwargs)
        else:
            pool = urllib3.HTTPConnectionPool(address, port, **kwargs)
        response = None
        try:
            path = urlunsplit(('', '', parts.path or '/', parts.query, ''))
            response = pool.urlopen('GET', path, headers={'Host': parts.netloc,
                'User-Agent': 'Mozilla/5.0 (compatible; NutritionLookup/1.0)', 'Accept-Encoding': 'identity'},
                redirect=False, preload_content=False, assert_same_host=False)
            if response.status in (301, 302, 303, 307, 308):
                url = urljoin(url, response.headers.get('Location', ''))
                continue
            if response.status != 200:
                raise ValueError('Page unavailable')
            if int(response.headers.get('Content-Length', '0')) > MAX_BYTES:
                raise ValueError('Page too large')
            body = response.read(MAX_BYTES + 1, decode_content=True)
            if len(body) > MAX_BYTES:
                raise ValueError('Page too large')
            return url, response.headers.get('Content-Type', ''), body
        finally:
            if response:
                response.close()
            pool.close()
    raise ValueError('Too many redirects')


def extract(url, content_type, body):
    if body.startswith(b'%PDF') or 'application/pdf' in content_type:
        # A malformed/compressed PDF must not consume the bot's memory indefinitely.
        proc = subprocess.run([sys.executable, __file__, '--pdf'], input=body,
                              capture_output=True, timeout=22, check=True)
        return proc.stdout.decode('utf-8'), []
    if 'html' not in content_type and 'text/plain' not in content_type:
        raise ValueError('Unsupported content')
    soup = BeautifulSoup(body, 'html.parser')
    links = []
    for a in soup.find_all(['a', 'iframe', 'embed', 'object']):
        href = a.get('href') or a.get('src') or a.get('data')
        if not href:
            continue
        target = urljoin(url, href).split('#')[0]
        if target.rstrip('/') == url.split('#')[0].rstrip('/'):
            continue
        label = a.get_text(' ', strip=True) + ' ' + target
        if re.search(r'nutrition|nutritional', label, re.I):
            links.append(target)
    for node in soup(['script', 'style', 'nav', 'footer']):
        node.decompose()
    links = sorted(dict.fromkeys(links), key=lambda link: '.pdf' not in link.lower())
    return soup.get_text(' ', strip=True)[:180000], links[:2]


def excerpts(text, dish):
    """Keep headers plus relevant table neighborhoods, preserving original text."""
    if len(text) <= 18000:
        return text
    terms = set(re.findall(r'[a-z]{3,}', dish.lower())) - {
        'one', 'from', 'with', 'full', 'order', 'restaurant', 'india', 'had', 'the', 'and', 'whole'}
    blocks = [(text[i:i+1800], i) for i in range(0, len(text), 1500)]
    ranked = sorted(blocks, key=lambda item: sum(min(item[0].lower().count(t), 3) for t in terms), reverse=True)
    selected = sorted({0, *(pos for _, pos in ranked[:9])})
    return '\n[excerpt]\n'.join(text[pos:pos+1800] for pos in selected)


def source_class(url, restaurant):
    """Conservative domain-name match, not a guarantee of publisher ownership.

    Unknown publisher domains can aid discovery but cannot establish published
    nutrition. This restricts source authority, not which restaurants are searched.
    """
    host = (urlsplit(url).hostname or '').lower()
    if any(host == domain or host.endswith('.' + domain) for domain in ('swiggy.com', 'zomato.com')):
        return 'merchant'
    brand = re.sub(r"[^a-z0-9 ]", '', restaurant.lower())
    words = [w for w in brand.split() if w not in ('india', 'bengaluru', 'bangalore', 'the', 'restaurant', 'restaurants')]
    joined = ''.join(words)
    candidates = {joined, joined + 'india'}
    for suffix in ('foods', 'palace', 'cafe', 'pizza'):
        if joined.endswith(suffix) and len(joined) > len(suffix) + 2:
            candidates.add(joined[:-len(suffix)])
        candidates.add(joined + suffix)
    labels = host.split('.')
    multipart = {('co', 'in'), ('co', 'uk'), ('com', 'au'), ('co', 'nz'), ('co', 'jp'), ('com', 'sg'), ('com', 'my')}
    offset = 3 if tuple(labels[-2:]) in multipart else 2
    label = labels[-offset] if len(labels) >= offset else ''
    return 'primary_candidate' if label in candidates and len(label) >= 3 else 'unverified_publisher'


def research(restaurant, dish, country='India'):
    """Search afresh for arbitrary restaurant names; never use a brand allowlist."""
    restaurant = re.sub(r'[^\w .&\'-]', ' ', restaurant)[:100]
    location = ''
    for suffix in (country, 'Bengaluru', 'Bangalore'):
        if restaurant.casefold().endswith(' ' + suffix.casefold()):
            restaurant = restaurant[:-len(suffix)].strip()
            location = suffix
    dish = re.sub(r'[^\w .&\'-]', ' ', dish)[:160]
    country = re.sub(r'[^\w -]', '', country)[:50] or 'India'
    started = time.monotonic()
    results, search_ok = [], False
    queries = [f'"{restaurant}" {country} nutrition pdf',
               f'"{restaurant}" {location or country} {dish} menu calories protein']
    for query in queries:
        try:
            hits = DDGS(timeout=12).text(query, region='in-en', max_results=5, backend='bing')
            results.extend(hits)
            search_ok = True
        except Exception:
            # An alternate backend helps when a search engine is unavailable.
            try:
                results.extend(DDGS(timeout=10).text(query, region='in-en', max_results=4, backend='yahoo'))
                search_ok = True
            except Exception:
                pass
    if country.casefold() == 'india' and not any(source_class(hit.get('href', ''), restaurant) == 'primary_candidate' for hit in results):
        try:
            results.extend(DDGS(timeout=12).text(f'"{restaurant}" site:in nutrition', region='in-en', max_results=4, backend='bing'))
        except Exception:
            pass
    brand_words = re.findall(r'[a-z0-9]{3,}', restaurant.lower())
    results.sort(key=lambda hit: (not any(word in urlsplit(hit.get('href', '')).netloc.lower() for word in brand_words), '.pdf' not in hit.get('href', '').lower()))
    queue, seen, docs = [], set(), []
    for hit in results:
        url = hit.get('href', '').split('#')[0].rstrip('/')
        if url and url not in queue:
            queue.append(url)
    attempts = 0
    while queue and len(docs) < MAX_DOCS and attempts < 9 and time.monotonic() - started < 90:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        attempts += 1
        try:
            final_url, kind, body = fetch(url)
            text, links = extract(final_url, kind, body)
            # Prefer linked primary nutrition documents over another search result.
            queue[0:0] = [link for link in links if link not in seen]
            if len(text) < 100:
                continue
            docs.append(dict(id=f'S{len(docs)+1}', url=final_url, source_class=source_class(final_url, restaurant),
                             text=excerpts(text, dish), retrieved_at=datetime.now(timezone.utc).date().isoformat()))
        except Exception:
            continue
    return dict(documents=docs, search_ok=search_ok)


if __name__ == '__main__' and '--pdf' in sys.argv:
    import io
    import resource
    from pypdf import PdfReader
    if sys.platform.startswith('linux'):
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
    reader = PdfReader(io.BytesIO(sys.stdin.buffer.read(MAX_BYTES + 1)))
    metadata = reader.metadata or {}
    output = ['PDF metadata (not proof of current menu): ' + json.dumps({key: str(metadata.get('/' + key, ''))[:200] for key in ('Title', 'CreationDate', 'ModDate')})]
    from itertools import islice
    for page in islice(reader.pages, 35):
        output.append(re.sub(r'[ \t]{2,}', ' ', page.extract_text(extraction_mode='plain'))[:30000])
        if sum(map(len, output)) > 180000:
            break
    sys.stdout.write('\n'.join(output)[:180000])
