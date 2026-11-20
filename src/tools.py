"""도메인 도구: 외부와 주고받는 기능을 모은다.

- 기사 URL 본문 추출 (fetch_article_text) + 주소 차단 (check_url) — 2-8
- 기사 수집 (collect_all): RSS와 개인정보위 보도자료 목록 — 4-1
- DB 접근 (connect_db, load_records, load_checklist_with_changes): 누적 기록 읽기 — 4-2
- DB 쓰기 (save_run, prune_old_rows): commit_records 노드(4-9)만 부른다 (CLAUDE.md 구현 원칙)
- 메일 발송 (send_mail): Gmail SMTP, HTML·텍스트 multipart — 4-8
- 점검 에이전트 도구 (search_checklist, get_checklist_item): 질의 모드 ReAct 점검이 고르는 읽기 전용 @tool — 5-3

주소 차단 규칙 (SERVICE.md 10.2 입력 보안 규칙, SSRF 방지):
- http, https만 허용, 포트는 80·443만
- 호스트 이름을 실제 IP로 바꿔(DNS) 모든 IP가 공인 인터넷 주소일 때만 허용
  (사설망 10/8·172.16/12·192.168/16, 루프백 127/8·::1, 링크로컬 169.254/16(클라우드 메타데이터), 예약·멀티캐스트 등 차단)
- 리다이렉트는 자동으로 따라가지 않고, 이동할 때마다 같은 검사를 다시 한다
한계: 검사 시점과 실제 접속 시점 사이에 DNS 응답이 바뀌는 공격(DNS 리바인딩)까지 완전히 막지는 못한다.
"""

import hashlib
import ipaddress
import json
import re
import socket
import smtplib
import sqlite3
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import feedparser
import requests
from bs4 import BeautifulSoup
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from src import config
from src.retriever import load_checklist, normalize_id, search, to_document

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


# ── 기사 수집 (4-1) ───────────────────────────────────
KST = timezone(timedelta(hours=9))


def _fetch_bytes(url: str) -> bytes:
    """출처 페이지·피드를 가져온다. 출처 주소도 주소 차단 검사를 거친다."""
    try:
        resp = _get(url)
    except requests.RequestException as e:
        raise FetchFailed(f"접속 실패: {type(e).__name__}")
    with resp:
        if resp.status_code != 200:
            raise FetchFailed(f"응답 오류: HTTP {resp.status_code}")
        return _read_limited(resp)


def parse_date(raw: str) -> datetime | None:
    """피드·게시판의 날짜 문자열을 시간대가 있는 datetime으로 바꾼다. 시간대가 없으면 한국 시간으로 본다.

    예: "Thu, 19 Nov 2026 04:33:46 +0000" (RFC 822), "2026-11-19 12:25:00", "2026-11-19"
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(raw.replace("/", "-").replace(".", "-"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=KST)


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", BeautifulSoup(html or "", "html.parser").get_text(" ")).strip()


def make_item(source: dict, title: str, url: str, published: datetime | None, snippet: str = "") -> dict:
    """수집 기사 1건. id는 URL로 만든다 (같은 URL이면 같은 id)."""
    return {
        "id": hashlib.sha1(url.encode("utf-8")).hexdigest()[:16],
        "source": source["name"], "track": source["track"], "region": source["region"],
        "title": title.strip(), "url": url,
        "published": published.isoformat() if published else "",
        "snippet": snippet[:500],  # RSS 설명(본문 앞부분). 선별에 쓰고, 본문은 선별된 기사만 가져온다
    }


def collect_rss(source: dict) -> list[dict]:
    feed = feedparser.parse(_fetch_bytes(source["url"]))
    if feed.bozo and not feed.entries:
        raise FetchFailed(f"RSS 해석 실패: {type(feed.bozo_exception).__name__}")
    items = []
    prefix = source.get("link_prefix")  # 분류별 RSS가 없는 사이트: 전체 피드에서 지정 메뉴 주소의 기사만 남긴다
    for e in feed.entries:
        if not e.get("link") or not e.get("title"):
            continue
        if prefix and not e["link"].startswith(prefix):
            continue
        published = parse_date(e.get("published") or e.get("updated") or "")
        items.append(make_item(source, e["title"], e["link"], published, _text(e.get("summary", ""))))
    return items


def collect_pipc_board(source: dict) -> list[dict]:
    """개인정보보호위원회 보도자료 목록 표를 읽는다 (RSS 없음). 열: 번호 | 제목 | 작성부서 | 작성일 | 첨부 | 조회수.

    목록은 번호순이라 작성일이 섞여 있으므로 모든 행의 날짜를 본다. 표 구조가 바뀌면 FetchFailed로 알린다.
    """
    soup = BeautifulSoup(_fetch_bytes(source["url"]).decode("utf-8", errors="replace"), "html.parser")
    rows = soup.select("table tbody tr")
    items = []
    for tr in rows:
        tds = tr.find_all("td")
        link = tr.select_one("td.boardTitle a[href]")
        if len(tds) < 4 or not link:
            continue
        title = re.sub(r"\s+N$", "", link.get_text(" ", strip=True))  # 제목 끝의 새 글 표시 "N" 제거
        items.append(make_item(source, title, urljoin(source["url"], link["href"]), parse_date(tds[3].get_text(strip=True)),
                               snippet=f"작성부서: {tds[2].get_text(strip=True)}"))
    if rows and not items:
        raise FetchFailed("목록 표 구조가 예상과 다름 (사이트 개편 가능성)")
    return items


MSIT_MD_LINK = re.compile(r"fn_download\('(\d+)',\s*'(\d+)',\s*'md'\)")
# "2026. 11. 19.(목) 09:00", "2026.11.18.(수) 07시", "2026. 11. 17.(화)" — 시각은 있을 수도 없을 수도 있다
MSIT_DATE = re.compile(r"(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.?\s*(?:\([^)]*\))?\s*(?:(\d{1,2})(?::(\d{2})|시))?")


def _dates(text: str) -> list[datetime]:
    out = []
    for y, mo, d, h, mi in MSIT_DATE.findall(text):
        try:
            out.append(datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), tzinfo=KST))
        except ValueError:
            continue
    return out


def msit_release_date(md: str) -> datetime | None:
    """보도자료 머리 표의 "| 보도 시점 | … | 배포 | …" 줄에서 배포 일시를 돌려준다.

    - 기본: "배포" 칸의 날짜. 없으면 보도 시점의 첫 날짜.
    - 배포 날짜가 보도 시점보다 30일 넘게 앞서면 원문 오타로 보고 보도 시점 날짜를 쓴다
      (실제 사례: 보도 시점 2026.11.18, 배포 2025.11.17).
    """
    line = next((ln for ln in md.splitlines() if "배포" in ln and MSIT_DATE.search(ln)), "")
    if not line:
        return None
    head, _, tail = line.rpartition("| 배포")  # 마지막 "배포" 칸 기준으로 앞(보도 시점)과 뒤(배포)를 나눈다
    release, embargo = _dates(tail), _dates(head)
    if release and embargo and (embargo[0] - release[0]).days > 30:
        return embargo[0]
    return (release or embargo or [None])[0]


def msit_press_text(detail_url: str) -> tuple[datetime | None, str]:
    """과기정통부 보도자료 상세 페이지에서 MD 첨부(보도자료 전문 텍스트)를 읽어 (배포 일시, 본문)을 돌려준다.

    상세 페이지 본문은 첨부 문서를 보여 주는 뷰어(iframe)라 HTML에 없고, 첨부 중 MD 파일이 같은 내용의 텍스트다
    (공공누리 제1유형, 출처 표시). 파일은 메모리에서만 읽고 디스크에 저장하지 않는다. MD가 없으면 (None, "").
    """
    detail = BeautifulSoup(_fetch_bytes(detail_url).decode("utf-8", errors="replace"), "html.parser")
    link = detail.find("a", onclick=MSIT_MD_LINK)
    if not link or not link.get("href"):
        return None, ""
    md = _fetch_bytes(urljoin(detail_url, link["href"])).decode("utf-8", errors="replace")
    published = msit_release_date(md)
    lines = [ln for ln in md.splitlines() if not ln.lstrip().startswith(">")]  # 맨 위 이용조건 안내 제거
    body = re.sub(r"\|?-{3,}\|?", " ", "\n".join(lines))                       # 마크다운 표 구분선 제거
    body = re.sub(r"[ \t|]+", " ", body)
    body = re.sub(r"\n\s*\n+", "\n", body).strip()
    return published, body


def collect_msit_board(source: dict) -> list[dict]:
    """과학기술정보통신부 보도자료 목록을 읽는다 (RSS 없음). 항목: div.board_list > div.toggle > a[href] > p.title.

    목록의 등록일 칸은 자바스크립트가 채워서 HTML에 없다 → 글마다 상세 페이지의 MD 첨부에서 배포 일시와 본문을 얻는다.
    MD가 없는 글(일부 소속기관 소식)은 날짜 없이 제목만 수집하고, 새 글 여부는 중복 제거(이미 보낸 URL)가 가린다.
    글 하나의 상세 읽기가 실패해도 그 글만 제목으로 남기고 계속한다. 목록 구조가 바뀌면 FetchFailed로 알린다.
    """
    soup = BeautifulSoup(_fetch_bytes(source["url"]).decode("utf-8", errors="replace"), "html.parser")
    rows = [t for t in soup.select("div.board_list div.toggle") if "thead" not in (t.get("class") or [])]
    items = []
    for row in rows:
        link, title = row.select_one("a[href]"), row.select_one("p.title")
        if not link or not title or "view.do" not in link["href"]:
            continue
        url = urljoin(source["url"], link["href"])
        try:
            published, body = msit_press_text(url)
        except (FetchFailed, BlockedURL):
            published, body = None, ""
        item = make_item(source, re.sub(r"\s+", " ", title.get_text(" ", strip=True)), url, published, snippet=body[:500])
        if body:
            item["body"] = body[: config.QUERY_MAX_CHARS]  # 본문을 이미 얻었으므로 4-5에서 다시 가져오지 않는다
        items.append(item)
    if rows and not items:
        raise FetchFailed("목록 구조가 예상과 다름 (사이트 개편 가능성)")
    return items


COLLECTORS = {"rss": collect_rss, "pipc_board": collect_pipc_board, "msit_board": collect_msit_board}


def collect_all(sources: list[dict] | None = None, now: datetime | None = None) -> tuple[list[dict], list[dict], list[dict]]:
    """모든 출처에서 수집한다. 출처 하나가 실패해도 나머지는 계속한다 (CLAUDE.md 구현 원칙).

    최근 config.COLLECT_RECENT_DAYS일 안의 기사만 남긴다. 날짜를 알 수 없는 기사는 남긴다 (중복 제거가 걸러낸다).
    돌려주는 값: (기사 목록, 실패 목록 [{source, error}], 출처별 통계 [{source, region, fetched, recent}])
    """
    sources = sources if sources is not None else config.SOURCES
    cutoff = (now or datetime.now(KST)) - timedelta(days=config.COLLECT_RECENT_DAYS)
    items, failures, stats = [], [], []
    for source in sources:
        try:
            fetched = COLLECTORS[source["type"]](source)
        except (FetchFailed, BlockedURL, KeyError) as e:
            failures.append({"source": source["name"], "error": f"{type(e).__name__}: {e}"})
            continue
        except Exception as e:  # 예상 못 한 오류도 그 출처만 실패로 처리한다
            failures.append({"source": source["name"], "error": f"{type(e).__name__}: {e}"})
            continue
        recent = [it for it in fetched if not it["published"] or datetime.fromisoformat(it["published"]) >= cutoff]
        items.extend(recent)
        stats.append({"source": source["name"], "track": source["track"], "region": source["region"],
                      "fetched": len(fetched), "recent": len(recent)})
    return items, failures, stats


# ── DB: 누적 기록 (4-2, SERVICE.md 14장) ──────────────────
# 표 만들기는 매번 해도 안전하다 (IF NOT EXISTS). 기록 쓰기는 commit_records(4-9)만 한다.
SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (              -- 처리한 URL과 회신 메일 (보관 90일)
    url        TEXT PRIMARY KEY,
    kind       TEXT NOT NULL DEFAULT 'article', -- article / mail(2차)
    region     TEXT,
    first_seen TEXT NOT NULL                   -- YYYY-MM-DD
);
CREATE TABLE IF NOT EXISTS items (             -- 메일로 보낸 기사 (보관 1년, 제안 근거는 유지)
    id         TEXT PRIMARY KEY,               -- URL 해시 (tools.make_item)
    url        TEXT NOT NULL UNIQUE,
    source     TEXT, region TEXT, title TEXT,
    category   TEXT, event_key TEXT,
    summary    TEXT,
    key_info   TEXT,                           -- 핵심 정보 JSON (원인·대책·변경조항 등)
    published  TEXT,
    sent_date  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coverage_log (      -- 모든 판정 기록 (보관 1년, 제안 근거는 유지)
    judgment_id  TEXT PRIMARY KEY,             -- 날짜-기사번호-판정 (예: 20261119-1-a)
    item_id      TEXT REFERENCES items(id),
    target       TEXT, verdict TEXT,
    evidence_ids TEXT,                         -- JSON 목록
    reason       TEXT,
    misjudged    INTEGER NOT NULL DEFAULT 0,   -- 오판 여부 (2차)
    created      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proposals (         -- 체크리스트 반영 제안 (영구)
    id         TEXT PRIMARY KEY,               -- P-YYYYMMDD-NN
    status     TEXT NOT NULL DEFAULT '검토대기', -- 검토대기 / 반영완료
    type       TEXT NOT NULL,                  -- 수정 / 신규
    target_id  TEXT, area TEXT,
    title      TEXT, item TEXT, content TEXT,
    first_date TEXT NOT NULL, last_date TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proposal_evidence ( -- 제안 ↔ 근거 기사 (같은 제안이 다시 나오면 행만 추가)
    proposal_id TEXT NOT NULL REFERENCES proposals(id),
    item_id     TEXT NOT NULL REFERENCES items(id),
    added_date  TEXT NOT NULL,
    PRIMARY KEY (proposal_id, item_id)
);
CREATE TABLE IF NOT EXISTS rejected (          -- 삭제 요청된 제안 (2차. 지금은 비워 둔다, 영구)
    proposal_id TEXT PRIMARY KEY,
    type TEXT, target_id TEXT, item TEXT, content TEXT,
    reason TEXT, rejected_date TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checklist_snapshot (  -- 지난 실행의 체크리스트 (변경 감지용, 영구)
    id TEXT PRIMARY KEY, area TEXT, item TEXT, content TEXT, checks TEXT,
    snapshot_date TEXT NOT NULL
);
"""
TABLES = ["seen", "items", "coverage_log", "proposals", "proposal_evidence", "rejected", "checklist_snapshot"]


def connect_db(path=None) -> sqlite3.Connection:
    """DB에 연결하고 표가 없으면 만든다. 행은 사전처럼 읽을 수 있게 한다."""
    path = path or config.DB_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def load_records(conn: sqlite3.Connection) -> dict:
    """이번 실행에 필요한 누적 기록을 읽는다 (읽기 전용).

    - seen_urls: 이미 처리한 기사 URL (지역별) → 중복 제거(4-3)
    - proposals: 검토대기·반영완료 제안과 근거 기사 수 → 제안 병합(4-6)
    - rejected: 삭제 요청된 제안 → 같은 제안 다시 올리지 않기 (2차)
    - recent_coverage: 최근 30일 판정 → 점검 때 참고 (장기 메모리)
    """
    seen: dict[str, set[str]] = {}
    for r in conn.execute("SELECT url, region FROM seen WHERE kind = 'article'"):
        seen.setdefault(r["region"] or "", set()).add(r["url"])
    proposals = [dict(r) for r in conn.execute(
        """SELECT p.*, COUNT(e.item_id) AS evidence_count FROM proposals p
           LEFT JOIN proposal_evidence e ON e.proposal_id = p.id GROUP BY p.id ORDER BY p.id""")]
    rejected = [dict(r) for r in conn.execute("SELECT * FROM rejected")]
    since = (datetime.now(KST) - timedelta(days=30)).strftime("%Y-%m-%d")
    recent = [{**dict(r), "evidence_ids": json.loads(r["evidence_ids"] or "[]")}
              for r in conn.execute("SELECT * FROM coverage_log WHERE created >= ? ORDER BY created DESC", (since,))]
    return {"seen_urls": seen, "proposals": proposals, "rejected": rejected, "recent_coverage": recent}


def load_checklist_with_changes(conn: sqlite3.Connection) -> tuple[dict, list[str]]:
    """checklist.csv를 읽고, 지난 실행의 사본(checklist_snapshot)과 비교해 바뀐 항목 ID를 찾는다 (읽기 전용).

    바뀐 항목은 P2 반영완료 감지(recheck_proposals)에 쓴다. 첫 실행(사본 없음)이면 바뀐 항목 없음으로 본다.
    """
    from src.retriever import load_checklist  # 순환 import 방지

    checklist = load_checklist()
    snapshot = {r["id"]: r for r in conn.execute("SELECT * FROM checklist_snapshot")}
    if not snapshot:
        return checklist, []
    changed = [i for i, row in checklist.items()
               if i not in snapshot or (row["점검내용"], row["확인사항"]) != (snapshot[i]["content"], snapshot[i]["checks"])]
    changed += [i for i in snapshot if i not in checklist]  # 삭제된 항목
    return checklist, sorted(changed)


def read_recent_coverage(path=None) -> list[dict]:
    """질의 모드용: 최근 30일 판정(장기 메모리)을 **읽기 전용**으로 읽는다. DB가 없으면 빈 목록.

    질의 모드는 DB에 쓰지 않으므로(CLAUDE.md) connect_db(표 생성) 대신 읽기 전용 모드로 연다.
    """
    path = path or config.DB_PATH
    if not path.exists():
        return []
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return load_records(conn)["recent_coverage"]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}



# ── DB 쓰기 (4-9): commit_records 노드만 부른다 ─────────────


KEY_INFO_FIELDS = ("core_issue", "causes", "attack_path", "countermeasures", "changed_clauses", "effective_date", "stage", "targets")


def _unique_judgment_id(conn: sqlite3.Connection, base: str) -> str:
    """판정 ID는 날짜-기사번호-영문자. 같은 날 두 번 실행하면 번호가 겹치므로 뒤에 -2, -3을 붙인다."""
    jid, k = base, 1
    while conn.execute("SELECT 1 FROM coverage_log WHERE judgment_id = ?", (jid,)).fetchone():
        k += 1
        jid = f"{base}-{k}"
    return jid


def save_run(conn: sqlite3.Connection, *, today: str, seen_items: list[dict], articles: list[dict],
             numbers: dict[str, str], plan: list[dict], checklist: dict[str, dict]) -> dict:
    """한 번의 실행 결과를 저장한다. 커밋은 하지 않는다 (호출한 쪽이 트랜잭션 하나로 묶는다).

    today: YYYY-MM-DD (KST)
    seen_items: 처리 완료로 표시할 기사 (중복 제거·선별을 거친 후보 전체. 처리 실패 기사는 빼고 넘긴다)
    articles: 메일에 실린 기사별 결과 (articles_merged)
    numbers: 기사 ID → 메일의 기사 번호 (판정 ID에 쓴다)
    plan: merge_proposals의 결정 (new / attach / rejected)
    checklist: 지금 체크리스트 (변경 감지용 사본으로 저장)
    """
    stamp = today.replace("-", "")
    n = {"seen": 0, "items": 0, "coverage_log": 0, "proposals_new": 0, "evidence": 0}

    # 1) 처리 완료 URL. 같은 사건으로 묶인 다른 출처 기사(related)도 함께 표시해 다시 들어오지 않게 한다
    for it in seen_items:
        for url in [it["url"]] + [r["url"] for r in it.get("related", [])]:
            n["seen"] += conn.execute("INSERT OR IGNORE INTO seen (url, kind, region, first_seen) VALUES (?, 'article', ?, ?)",
                                      (url, it.get("region"), today)).rowcount

    # 2) 메일로 보낸 기사와 3) 판정 기록 (처리 실패 기사는 요약·판정이 없어 저장하지 않는다)
    for a in articles:
        if a.get("error") or not a.get("summary"):
            continue
        it, sm = a["item"], a["summary"]
        key_info = {k: sm.get(k) for k in KEY_INFO_FIELDS if sm.get(k)}
        conn.execute(
            """INSERT INTO items (id, url, source, region, title, category, event_key, summary, key_info, published, sent_date)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET summary = excluded.summary, key_info = excluded.key_info,
                                             category = excluded.category, sent_date = excluded.sent_date""",
            (it["id"], it["url"], it.get("source"), it.get("region"), it.get("title"), it.get("category") or sm.get("category"),
             it.get("event_key"), sm.get("summary"), json.dumps(key_info, ensure_ascii=False), it.get("published"), today))
        n["items"] += 1
        for i, c in enumerate(a.get("coverage", [])):
            jid = _unique_judgment_id(conn, f"{stamp}-{numbers.get(it['id'], '0')}-{'abcdefghijklmnopqrstuvwxyz'[i]}")
            conn.execute(
                "INSERT INTO coverage_log (judgment_id, item_id, target, verdict, evidence_ids, reason, created) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (jid, it["id"], c.get("target"), c.get("verdict"), json.dumps(c.get("evidence_ids", [])), c.get("reason"), today))
            n["coverage_log"] += 1

    # 4) 제안: 새 제안은 행 추가, 같은 제안이면 근거 기사만 추가하고 최근 근거 추가일을 바꾼다. 반려 목록과 같으면(2차) 저장 안 함
    for x in plan:
        p, ev = x["proposal"], x["evidence"]
        if x["action"] == "new":
            conn.execute(
                """INSERT INTO proposals (id, status, type, target_id, area, title, item, content, first_date, last_date)
                   VALUES (?, '검토대기', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (x["proposal_id"], p["type"], p.get("target_id", ""), p.get("area", ""), p.get("title"), p.get("item"),
                 p.get("content"), today, today))
            n["proposals_new"] += 1
        elif x["action"] == "attach":
            conn.execute("UPDATE proposals SET last_date = ? WHERE id = ?", (today, x["proposal_id"]))
        else:
            continue
        n["evidence"] += conn.execute("INSERT OR IGNORE INTO proposal_evidence (proposal_id, item_id, added_date) VALUES (?, ?, ?)",
                                      (x["proposal_id"], ev["item_id"], today)).rowcount

    # 5) 체크리스트 사본: 다음 실행의 변경 감지(P2 반영완료 감지)용으로 지금 내용으로 바꿔 둔다
    conn.execute("DELETE FROM checklist_snapshot")
    conn.executemany(
        "INSERT INTO checklist_snapshot (id, area, item, content, checks, snapshot_date) VALUES (?, ?, ?, ?, ?, ?)",
        [(i, r["분야"], r["점검항목"], r["점검내용"], r["확인사항"], today) for i, r in checklist.items()])
    return n


def prune_old_rows(conn: sqlite3.Connection, today: str) -> dict:
    """보관 기간이 지난 행을 지운다 (SERVICE.md 14장). 커밋은 호출한 쪽이 한다.

    seen 90일, items·coverage_log 1년. 제안의 근거로 연결된 기사와 그 판정은 지우지 않는다.
    proposals, rejected, checklist_snapshot은 영구 보관 (설정값 None).
    """
    base = datetime.strptime(today, "%Y-%m-%d")
    cut = {t: (base - timedelta(days=d)).strftime("%Y-%m-%d") for t, d in config.RETENTION_DAYS.items() if d}
    keep = "SELECT item_id FROM proposal_evidence"
    n = {}
    n["seen"] = conn.execute("DELETE FROM seen WHERE first_seen < ?", (cut["seen"],)).rowcount
    n["coverage_log"] = conn.execute(f"DELETE FROM coverage_log WHERE created < ? AND item_id NOT IN ({keep})",
                                     (cut["coverage_log"],)).rowcount
    n["items"] = conn.execute(f"""DELETE FROM items WHERE sent_date < ? AND id NOT IN ({keep})
                                  AND id NOT IN (SELECT item_id FROM coverage_log WHERE item_id IS NOT NULL)""",
                              (cut["items"],)).rowcount
    return n


def read_proposals_with_evidence(conn: sqlite3.Connection) -> list[dict]:
    """보기용 md 생성: 제안과 근거 기사 목록 (근거는 추가된 순서)."""
    out = []
    for p in conn.execute("SELECT * FROM proposals ORDER BY id"):
        ev = [dict(r) for r in conn.execute(
            """SELECT i.title, i.url, e.added_date FROM proposal_evidence e JOIN items i ON i.id = e.item_id
               WHERE e.proposal_id = ? ORDER BY e.added_date, i.title""", (p["id"],))]
        out.append({**dict(p), "evidence": ev})
    return out


def read_coverage_log(conn: sqlite3.Connection) -> list[dict]:
    """보기용 md 생성: 판정 기록 (최근 것부터)."""
    return [{**dict(r), "evidence_ids": json.loads(r["evidence_ids"] or "[]")} for r in conn.execute(
        """SELECT c.*, i.title, i.url FROM coverage_log c LEFT JOIN items i ON i.id = c.item_id
           ORDER BY c.created DESC, c.judgment_id""")]

# ── 메일 발송 (4-8) ───────────────────────────────────


def build_message(subject: str, html: str, text: str, sender: str, recipients: list[str]) -> EmailMessage:
    """HTML과 텍스트 본문을 함께 담은 multipart/alternative 메일을 만든다.

    텍스트 본문을 먼저, HTML을 나중에 넣는다. 메일 프로그램은 표시할 수 있는 마지막 형식(HTML)을 보여 주고,
    HTML을 못 여는 환경에서는 텍스트 본문이 보인다.
    """
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1] if "@" in sender else None)
    msg.set_content(text, charset="utf-8")
    msg.add_alternative(html, subtype="html", charset="utf-8")
    return msg


def send_mail(subject: str, html: str, text: str, recipients: list[str] | None = None) -> list[str]:
    """Gmail SMTP(SSL 465)로 메일 1통을 보낸다. 받는 사람 목록을 돌려준다.

    실패하면 예외를 그대로 올린다 (send_email 노드가 받아 "발송 실패"로 기록하고 저장을 건너뛴다).
    계정 정보는 .env에서 읽고, 오류 메시지·로그에 비밀번호를 남기지 않는다.
    """
    recipients = recipients or config.MAIL_TO
    if not (config.GMAIL_ADDRESS and config.GMAIL_APP_PASSWORD):
        raise RuntimeError(".env에 GMAIL_ADDRESS, GMAIL_APP_PASSWORD가 없습니다")
    if not recipients:
        raise RuntimeError(".env에 MAIL_TO(받는 사람)가 없습니다")
    msg = build_message(subject, html, text, config.GMAIL_ADDRESS, recipients)
    with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=config.SMTP_TIMEOUT) as smtp:
        smtp.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
        refused = smtp.send_message(msg)
    if refused:  # 일부 수신자만 거부된 경우
        raise RuntimeError(f"수신 거부된 주소 {len(refused)}개")
    return recipients


# ── 점검 에이전트 도구 (질의 모드 ReAct 점검, SERVICE.md 10.5) ─────────────
# LLM이 스스로 골라 부르는 읽기 전용 도구 2개. 실습 day4_practice/tools_basic.py 3단계(Pydantic 인자 스키마) 방식.
# - 결과는 <checklist_result> 구분 표시로 감싼 텍스트(content)와, 이번 호출이 돌려준 항목 ID 목록(artifact)을 함께 돌려준다.
#   artifact는 모델에는 보내지 않고, 점검 노드가 "도구로 실제 조회된 항목"을 모아 인용 ID를 검증하는 데 쓴다.
# - 검색 결과는 요약형(점검항목·점검내용)만 보여 주고, 확인사항까지 담긴 원문은 get_checklist_item으로 본다.
#   그래야 "애매할 때만 원문을 연다"는 선택이 의미가 있다.
# - 오류를 예외로 올리지 않고 안내 문구로 돌려준다. 에이전트가 다음 차례에 고칠 수 있게 하기 위해서다 (실습 day4 tool_error.py).


def format_checklist_brief(row: dict) -> str:
    """검색 결과 한 줄: [ID] 점검항목 (분야): 점검내용."""
    return f"[{row['id']}] {row['점검항목']} ({row['분야']}): {row['점검내용']}"


def format_checklist_full(row: dict) -> str:
    """항목 원문: 점검내용과 확인사항 전체 (검색 인덱스의 청크와 같은 형식)."""
    return to_document(row).page_content


class SearchChecklistInput(BaseModel):
    query: str = Field(description="찾을 통제 내용을 체크리스트 표현에 가깝게 쓴 짧은 검색어. "
                                   "예: '로그인 실패 횟수 제한', '백업 매체 소산 보관', '수탁자 교육 및 점검'",
                       min_length=2, max_length=100)


class GetChecklistItemInput(BaseModel):
    item_id: str = Field(description="체크리스트 항목 ID. 예: '2.9.2'", min_length=1, max_length=20)


@tool(args_schema=SearchChecklistInput, response_format="content_and_artifact")
def search_checklist(query: str) -> tuple[str, list[str]]:
    """ISMS-P 체크리스트에서 검색어와 관련된 항목 5개를 찾는다 (키워드 + 의미 검색).

    이미 받은 검색 결과에 점검 대상을 다루는 항목이 없어 보일 때, 기사 용어 대신 체크리스트에서 쓸 법한
    다른 표현으로 다시 찾는 데 쓴다. 같은 검색어를 되풀이하지 않는다.
    결과는 항목 ID, 점검항목, 점검내용만 보여 준다. 확인사항까지 보려면 get_checklist_item을 쓴다.
    """
    checklist = load_checklist()
    ids = [d.metadata["id"] for d in search(query, k=config.RETRIEVE_K) if d.metadata["id"] in checklist]
    if not ids:
        return f"<checklist_result>\n'{query}'와 관련된 항목을 찾지 못했습니다.\n</checklist_result>", []
    body = "\n".join(format_checklist_brief(checklist[i]) for i in ids)
    return f"<checklist_result>\n검색어: {query}\n{body}\n</checklist_result>", ids


@tool(args_schema=GetChecklistItemInput, response_format="content_and_artifact")
def get_checklist_item(item_id: str) -> tuple[str, list[str]]:
    """체크리스트 항목 1개의 원문(점검내용과 확인사항 전체)을 돌려준다.

    검색 결과의 한 줄 설명만으로는 그 항목이 점검 대상을 실제로 요구하는지 판단하기 어려울 때 쓴다.
    판정 근거로 인용하려는 항목이 애매하면 먼저 원문을 확인한다. 없는 ID면 "그런 항목 없음"을 돌려준다.
    """
    checklist = load_checklist()
    item_id = normalize_id(item_id)
    row = checklist.get(item_id)
    if row is None:
        return (f"<checklist_result>\n그런 항목 없음: '{item_id}'. 검색 결과에 나온 ID(예: 2.9.2)를 쓰세요.\n"
                f"</checklist_result>"), []
    return f"<checklist_result>\n{format_checklist_full(row)}\n</checklist_result>", [item_id]


CHECKLIST_TOOLS = [search_checklist, get_checklist_item]
