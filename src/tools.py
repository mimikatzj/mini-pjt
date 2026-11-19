"""도메인 도구: 외부와 주고받는 기능을 모은다.

현재(2-8): 기사 URL 본문 추출 (fetch_article_text) + 주소 차단 (check_url).
M4에서 추가 예정: RSS 수집, DB 접근, 메일 발송.

주소 차단 규칙 (SERVICE.md 10.2 입력 보안 규칙, SSRF 방지):
- http, https만 허용, 포트는 80·443만
- 호스트 이름을 실제 IP로 바꿔(DNS) 모든 IP가 공인 인터넷 주소일 때만 허용
  (사설망 10/8·172.16/12·192.168/16, 루프백 127/8·::1, 링크로컬 169.254/16(클라우드 메타데이터), 예약·멀티캐스트 등 차단)
- 리다이렉트는 자동으로 따라가지 않고, 이동할 때마다 같은 검사를 다시 한다
한계: 검사 시점과 실제 접속 시점 사이에 DNS 응답이 바뀌는 공격(DNS 리바인딩)까지 완전히 막지는 못한다.
"""

import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from src import config

ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_PORTS = {80, 443}
MAX_REDIRECTS = 5
TIMEOUT_SECONDS = 10
MAX_BYTES = 2_000_000
USER_AGENT = "Mozilla/5.0 (compatible; security-checklist-agent/1.0)"  # 일반 클라이언트 이름 (개인정보 없음)

# 기사 본문 영역 표시 (우선순위 순). schema.org 표준 표시 → 국내 언론사 CMS에서 흔한 id/class
BODY_SELECTORS = [
    "[itemprop=articleBody]",
    "#article-view-content-div",  # 국내 언론사 공통 CMS (보안뉴스 등)
    "#articleBody", "#article_body", "#articeBody", "#news_body_area", "#newsct_article", "#dic_area",
    ".article_body", ".article-body-content", ".news_content", ".view_con",
    ".article-content", ".articleBody", ".post-content", ".entry-content",  # 해외 사이트·블로그형
]


class BlockedURL(Exception):
    """보안 규칙상 접속하지 않는 주소."""


class FetchFailed(Exception):
    """접속은 했지만 본문을 가져오지 못함 (오류 응답, HTML 아님, 본문 너무 짧음 등)."""


def check_url(url: str) -> None:
    """접속해도 되는 주소인지 검사한다. 안 되면 BlockedURL을 던진다."""
    parts = urlsplit(url)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise BlockedURL(f"허용되지 않는 주소 형식: {parts.scheme}")
    host = parts.hostname
    if not host:
        raise BlockedURL("호스트가 없는 주소")
    try:
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    except ValueError:
        raise BlockedURL("잘못된 포트")
    if port not in ALLOWED_PORTS:
        raise BlockedURL(f"허용되지 않는 포트: {port}")
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise FetchFailed(f"주소를 찾을 수 없음: {host}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if getattr(ip, "ipv4_mapped", None):  # ::ffff:127.0.0.1 같은 표기
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            raise BlockedURL(f"사내망·로컬·예약 주소로 연결됨: {host} → {ip}")


def _get(url: str) -> requests.Response:
    """주소 검사 → 요청을 리다이렉트마다 반복한다."""
    for _ in range(MAX_REDIRECTS + 1):
        check_url(url)
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_SECONDS,
                            allow_redirects=False, stream=True)
        if resp.is_redirect or resp.is_permanent_redirect:
            url = urljoin(url, resp.headers.get("Location", ""))
            resp.close()
            continue
        return resp
    raise FetchFailed("리다이렉트가 너무 많음")


def _read_limited(resp: requests.Response) -> bytes:
    data = b""
    for chunk in resp.iter_content(64 * 1024):
        data += chunk
        if len(data) > MAX_BYTES:
            raise FetchFailed("페이지가 너무 큼")
    return data


def extract_article(html: str) -> tuple[str, str]:
    """HTML에서 (제목, 본문)을 뽑는다. 메뉴·광고·스크립트를 지우고 본문 문단이 가장 많은 영역을 고른다."""
    soup = BeautifulSoup(html, "html.parser")
    og = soup.find("meta", property="og:title")
    title = (og.get("content") if og else None) or (soup.title.get_text() if soup.title else "")

    for tag in soup(["script", "style", "noscript", "iframe", "form", "nav", "header", "footer", "aside", "button", "svg"]):
        tag.decompose()

    best = None
    # 1순위: 본문을 정확히 가리키는 표시. 위에서부터 차례로 보고, 본문 길이가 충분한 첫 영역을 쓴다.
    for selector in BODY_SELECTORS:
        el = soup.select_one(selector)
        if el and len(el.get_text(" ", strip=True)) >= config.MIN_ARTICLE_CHARS:
            best = el
            break
    # 2순위: <p>(문단)가 가장 많은 블록. 3순위: <article> 중 가장 긴 것.
    if best is None:
        parents = [p.parent for p in soup.find_all("p") if p.parent]
        if parents:
            best = max(set(parents), key=lambda el: sum(len(p.get_text(strip=True)) for p in el.find_all("p", recursive=False)))
    if best is None:
        best = max(soup.find_all("article"), key=lambda el: len(el.get_text(" ", strip=True)), default=soup.body or soup)

    lines = [ln.strip() for ln in best.get_text("\n").splitlines()]
    body = "\n".join(ln for ln in lines if len(ln) > 1)
    return title.strip(), body


def fetch_article_text(url: str) -> str:
    """기사 URL에서 "제목 + 본문" 텍스트를 가져온다.

    BlockedURL: 보안 규칙상 차단 / FetchFailed: 본문을 가져오지 못함
    """
    try:
        resp = _get(url)
    except requests.RequestException as e:
        raise FetchFailed(f"접속 실패: {type(e).__name__}")
    with resp:
        if resp.status_code != 200:
            raise FetchFailed(f"응답 오류: HTTP {resp.status_code}")
        if "html" not in resp.headers.get("Content-Type", "").lower():
            raise FetchFailed(f"HTML 페이지가 아님: {resp.headers.get('Content-Type')}")
        data = _read_limited(resp)
    html = data.decode(resp.encoding or resp.apparent_encoding or "utf-8", errors="replace")
    title, body = extract_article(html)
    if len(body) < config.MIN_ARTICLE_CHARS:
        raise FetchFailed("본문이 너무 짧음 (로그인이 필요하거나 자바스크립트로 그리는 페이지일 수 있음)")
    return f"{title}\n\n{body}"[: config.QUERY_MAX_CHARS]
