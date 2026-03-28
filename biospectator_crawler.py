"""
================================================================================
바이오 키워드 뉴스 크롤러  |  KTH_bionews_morning
================================================================================

■ 수정하는 곳은 딱 두 군데
  1. 키워드  → 아래 KEYWORDS / KEYWORD_ALIASES
  2. 사이트  → 아래 SITES 목록 (함수 2개 작성 후 등록)

■ 전체 동작 흐름
  평일 오전 9:30 (GitHub Actions 자동 실행)
    → 각 사이트 로그인(필요한 경우)
    → 키워드별 검색 (대/소문자 + 별칭 모두) — 등록된 모든 사이트 수행
    → 오늘 + 어제 날짜 기사 수집 (월요일은 금/토/일 포함)
    → sent_urls.json 대조 → 이미 발송된 기사 제외 (날짜 중복 방지)
    → 새 기사만 전문 크롤링
    → docs/index.html 생성 → GitHub Pages 업로드
    → 이메일 발송 (상단 링크버튼 + email_body=True 사이트 기사 미리보기)
    → sent_urls.json 업데이트 (최대 500건 보관)

■ GitHub Secrets 목록 (Settings → Secrets → Actions)
  BIOS_ID            BioSpectator 로그인 아이디
  BIOS_PW            BioSpectator 비밀번호
  GMAIL_FROM         발신 Gmail 주소
  GMAIL_APP_PASSWORD Gmail 앱 비밀번호 (2단계 인증 후 발급)
  GMAIL_TO           수신 이메일 주소

■ 배포 구조
  저장소: https://github.com/go2god4u-glitch/KTH_bionews_morning
  Pages:  https://go2god4u-glitch.github.io/KTH_bionews_morning/

================================================================================

■ 수정 이력
  ─────────────────────────────────────────────────────────────────────────────
  2026-03-28  더바이오(thebionews.net) 크롤링 소스 추가 (집 개인 PC / Claude Code)
  ─────────────────────────────────────────────────────────────────────────────
  1. 더바이오 검색/크롤링 함수 추가 (POST 방식, 로그인 불필요, 사진 포함)
  2. SITES 레지스트리 도입 → 사이트 추가 시 함수 2개 + SITES 항목 1개만 추가
  3. main()이 SITES 자동 순회 → 사이트 추가해도 main() 수정 불필요
  4. 출처 뱃지 색상을 SITES에서 관리 → CSS 수정 불필요
  5. 이메일 본문 포함 여부를 email_body 플래그로 사이트별 설정
  ─────────────────────────────────────────────────────────────────────────────

================================================================================
"""

import requests
from bs4 import BeautifulSoup, NavigableString, Comment
from datetime import datetime, timedelta
import time
import re
import os
import json
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
}

PAGES_URL      = "https://go2god4u-glitch.github.io/KTH_bionews_morning/"
SENT_URLS_FILE = "docs/sent_urls.json"


# ══════════════════════════════════════════════════════════════════════════════
# ★ 1. 키워드 설정  ← 여기만 수정
# ══════════════════════════════════════════════════════════════════════════════

# 모니터링할 키워드 목록. 영어 포함 시 대/소문자 자동 검색
KEYWORDS = [
    "DA-1726",
    "Vanoglipel",
    "메타비아",
    "glp-1",
    "Amylin",
    "GPR119",
    "동아ST",
    "노보노디스크",
]

# 키워드 별칭: 하나의 키워드로 묶을 동의어 목록
# - 대표 키워드(KEYWORDS)로 그룹화되어 리포트에 표시됨
# - 별칭도 하이라이트 강조 대상에 포함됨
KEYWORD_ALIASES = {
    "동아ST":      ["동아에스티"],
    "Amylin":     ["아밀린"],
    "메타비아":    ["MetaVia"],
    "노보노디스크": ["Novo Nordisk"],
    "GPR119":     ["GPR-119"],
    "Vanoglipel": ["바노글리펠"],
    "DA-1726":    [],
}


# ══════════════════════════════════════════════════════════════════════════════
# 공통 유틸
# ══════════════════════════════════════════════════════════════════════════════

def get_target_dates() -> list[str]:
    today = datetime.now()
    dates = [today.strftime("%Y-%m-%d"),
             (today - timedelta(days=1)).strftime("%Y-%m-%d")]
    if today.weekday() == 0:  # 월요일: 금/토/일 추가
        dates.append((today - timedelta(days=2)).strftime("%Y-%m-%d"))
        dates.append((today - timedelta(days=3)).strftime("%Y-%m-%d"))
    return list(dict.fromkeys(dates))


def build_search_variants(kw: str) -> list[str]:
    """키워드 → 대/소문자 변형 + 별칭 목록"""
    variants = [kw]
    if re.search(r'[a-zA-Z]', kw):
        extras = {kw.lower(), kw.upper()}
        extras.discard(kw)
        variants += list(extras)
    for alias in KEYWORD_ALIASES.get(kw, []):
        variants.append(alias)
        if re.search(r'[a-zA-Z]', alias):
            variants += [alias.lower(), alias.upper()]
    return variants


def deduplicate_across_sites(articles: list[dict]) -> list[dict]:
    """
    사이트 간 동일 기사 그룹화
    - 제목 유사도 80% 이상이면 같은 기사로 판단
    - SITES 등록 순서 우선 기사를 대표로 유지
    - 중복 기사는 제거하되, 대표 기사의 'also_in' 필드에 출처·URL·제목 기록
      → 리포트 카드 하단에 "다른 사이트에서도 보도" 섹션으로 표시
    """
    import difflib

    def normalize(text: str) -> str:
        return re.sub(r'[^\w가-힣]', '', text.lower())

    site_order  = [s["name"] for s in SITES]
    sorted_arts = sorted(articles, key=lambda a: site_order.index(a["출처"]) if a["출처"] in site_order else 999)

    kept = []
    for article in sorted_arts:
        matched = None
        for kept_art in kept:
            if kept_art["출처"] == article["출처"]:
                continue
            sim = difflib.SequenceMatcher(None, normalize(article["제목"]), normalize(kept_art["제목"])).ratio()
            if sim >= 0.8:
                matched = kept_art
                break
        if matched:
            # 대표 기사의 also_in에 중복 출처 추가
            matched.setdefault("also_in", []).append({
                "출처": article["출처"],
                "URL":  article["URL"],
                "제목": article["제목"],
                "_badge_color":  article["_badge_color"],
                "_badge_bg":     article["_badge_bg"],
                "_badge_border": article["_badge_border"],
            })
            print(f"  [중복감지] '{article['제목'][:40]}' → {article['출처']}에서도 보도")
        else:
            article.setdefault("also_in", [])
            kept.append(article)

    return kept


def highlight_keywords(body_html: str) -> str:
    """본문 HTML에서 키워드(+별칭) 모두 형광 <mark>로 강조"""
    all_terms = set()
    for kw in KEYWORDS:
        all_terms.add(kw)
        if re.search(r'[a-zA-Z]', kw):
            all_terms |= {kw.lower(), kw.upper()}
        for alias in KEYWORD_ALIASES.get(kw, []):
            all_terms.add(alias)
            if re.search(r'[a-zA-Z]', alias):
                all_terms |= {alias.lower(), alias.upper()}
    for v in sorted(all_terms, key=len, reverse=True):
        pattern = f'({re.escape(v)})(?![^<]*>)'
        body_html = re.sub(pattern, r'<mark>\1</mark>', body_html, flags=re.IGNORECASE)
    return body_html


# ══════════════════════════════════════════════════════════════════════════════
# BioSpectator 크롤러
# ══════════════════════════════════════════════════════════════════════════════

BIOS_BASE_URL   = "https://www.biospectator.com"
BIOS_LOGIN_URL  = "https://member.biospectator.com/login_prc.php"
BIOS_SEARCH_URL = BIOS_BASE_URL + "/section/search_list?searchkey={keyword}&page={page}"


def bios_login(session: requests.Session) -> bool:
    user_id  = os.getenv("BIOS_ID")
    password = os.getenv("BIOS_PW")
    if not user_id or not password:
        user_id  = input("BioSpectator 아이디: ").strip()
        password = input("BioSpectator 비밀번호: ").strip()
    session.get("https://member.biospectator.com/login.php", headers=HEADERS)
    session.post(
        BIOS_LOGIN_URL,
        data={"MEMR_EID": user_id, "MEMR_PWD": password, "URL": BIOS_BASE_URL + "/"},
        headers={**HEADERS, "Referer": "https://member.biospectator.com/login.php"},
        allow_redirects=True,
    )
    ok = "LOGIN_IDX" in session.cookies
    print(f"[BioSpectator] {'로그인 성공' if ok else '로그인 실패'}")
    return ok


def bios_search(session: requests.Session, keyword: str, target_dates: list[str]) -> list[dict]:
    results, seen = [], set()
    for page in range(1, 10):
        url  = BIOS_SEARCH_URL.format(keyword=keyword, page=page)
        resp = session.get(url, headers={**HEADERS, "Referer": BIOS_BASE_URL}, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        found_any = stop = False
        for a_tag in soup.select("a[href*='/news/view/']"):
            if len(a_tag.get_text(strip=True)) < 5:
                continue
            href = a_tag.get("href", "")
            url2 = BIOS_BASE_URL + href if href.startswith("/") else href
            if url2 in seen:
                continue
            seen.add(url2)
            date = ""
            for parent in [a_tag.find_parent(),
                           a_tag.find_parent().find_parent() if a_tag.find_parent() else None]:
                if parent:
                    m = re.search(r"(\d{4}-\d{2}-\d{2})", parent.get_text())
                    if m:
                        date = m.group(1); break
            if not date:
                continue
            if date in target_dates:
                found_any = True
                results.append({"날짜": date, "URL": url2})
            elif date < min(target_dates):
                stop = True; break
        if stop or not found_any:
            break
        time.sleep(0.3)
    print(f"  [BioSpectator/{keyword}] {len(results)}건")
    return results


def bios_crawl_article(session: requests.Session, info: dict) -> dict:
    url = info["URL"]
    try:
        resp    = session.get(url, headers={**HEADERS, "Referer": BIOS_BASE_URL}, timeout=10)
        soup    = BeautifulSoup(resp.text, "html.parser")
        h3      = soup.select_one("h3")
        title   = h3.get_text(strip=True) if h3 else ""
        date    = info.get("날짜", "")
        date_el = soup.select_one(".datetime")
        if date_el:
            m = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", date_el.get_text())
            if m: date = m.group(1)
        body    = ""
        body_el = soup.select_one(".article_view")
        if body_el:
            for tag in body_el.select(".ad, .related, script, style, .viwe-pay-coment, .reporter"):
                tag.decompose()
            for c in body_el.find_all(string=lambda t: isinstance(t, Comment)):
                c.extract()
            for tag in body_el.find_all(['p', 'div']):
                if not tag.get_text(strip=True) and not tag.find('img'):
                    tag.decompose()
            for tag in body_el.find_all(True):
                if tag.get("style"): del tag["style"]
                if tag.get("class"): del tag["class"]
            for node in list(body_el.descendants):
                if isinstance(node, NavigableString) and node.parent.name not in ['script', 'style']:
                    text = str(node)
                    if '\n' in text and text.strip():
                        node.replace_with(BeautifulSoup(text.replace('\n', '<br>'), 'html.parser'))
            body = str(body_el)
        return {"제목": title, "날짜": date, "본문": body,
                "유료기사": "[유료]" if not body or len(body) < 100 else "", "URL": url}
    except Exception as e:
        return {"제목": "", "날짜": info.get("날짜", ""), "본문": f"[오류] {e}",
                "유료기사": "", "URL": url}


# ══════════════════════════════════════════════════════════════════════════════
# 더바이오 크롤러
# ══════════════════════════════════════════════════════════════════════════════

THEBIO_BASE_URL   = "https://www.thebionews.net"
THEBIO_SEARCH_URL = THEBIO_BASE_URL + "/news/articleList.html"


def thebio_search(session: requests.Session, keyword: str, target_dates: list[str]) -> list[dict]:
    results, seen = [], set()
    for page in range(1, 10):
        resp = session.post(
            THEBIO_SEARCH_URL,
            data={"sc_word": keyword, "page": page},
            headers={**HEADERS, "Referer": THEBIO_BASE_URL},
            timeout=10,
        )
        soup = BeautifulSoup(resp.text, "html.parser")
        found_any = stop = False
        for li in soup.select("li"):
            a_tag = li.select_one("H2.titles a[href*='articleView']") or \
                    li.select_one("h2.titles a[href*='articleView']")
            if not a_tag: continue
            href = a_tag.get("href", "")
            url  = THEBIO_BASE_URL + href if href.startswith("/") else href
            if url in seen: continue
            seen.add(url)
            date   = ""
            byline = li.select_one(".byline")
            if byline:
                for em in byline.select("em"):
                    m = re.search(r"(\d{4})\.(\d{2})\.(\d{2})", em.get_text())
                    if m:
                        date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"; break
            if not date: continue
            if date in target_dates:
                found_any = True
                results.append({"날짜": date, "URL": url})
            elif date < min(target_dates):
                stop = True; break
        if stop or not found_any: break
        time.sleep(0.3)
    print(f"  [더바이오/{keyword}] {len(results)}건")
    return results


def thebio_crawl_article(session: requests.Session, info: dict) -> dict:
    url = info["URL"]
    try:
        resp    = session.get(url, headers={**HEADERS, "Referer": THEBIO_BASE_URL}, timeout=10)
        soup    = BeautifulSoup(resp.text, "html.parser")
        h1      = soup.select_one("h1.heading") or soup.select_one(".heading")
        title   = h1.get_text(strip=True) if h1 else ""
        date    = info.get("날짜", "")
        info_ul = soup.select_one("ul.infomation")
        if info_ul:
            m = re.search(r"입력\s+(\d{4})\.(\d{2})\.(\d{2})\s+(\d{2}:\d{2})", info_ul.get_text())
            if m: date = f"{m.group(1)}-{m.group(2)}-{m.group(3)} {m.group(4)}"
        body    = ""
        body_el = soup.select_one("#article-view-content-div")
        if body_el:
            for tag in body_el.select("script, style, .ad, .related"):
                tag.decompose()
            for c in body_el.find_all(string=lambda t: isinstance(t, Comment)):
                c.extract()
            for tag in body_el.find_all(['p', 'div']):
                if not tag.get_text(strip=True) and not tag.find('img'):
                    tag.decompose()
            for tag in body_el.find_all(True):
                if tag.name == "img":
                    tag.attrs = {k: v for k, v in {"src": tag.get("src",""), "alt": tag.get("alt","")}.items() if v}
                elif tag.name == "figcaption":
                    tag.attrs = {}
                else:
                    for attr in ["style", "class", "id"]:
                        if tag.get(attr): del tag[attr]
            body = str(body_el)
        return {"제목": title, "날짜": date, "본문": body,
                "유료기사": "[유료]" if not body or len(body) < 100 else "", "URL": url}
    except Exception as e:
        return {"제목": "", "날짜": info.get("날짜", ""), "본문": f"[오류] {e}",
                "유료기사": "", "URL": url}


# ══════════════════════════════════════════════════════════════════════════════
# ★ 2. 사이트 설정  ← 새 사이트 추가 시 여기에 등록
# ══════════════════════════════════════════════════════════════════════════════
#
# 새 사이트 추가 방법:
#   1. 위에 search 함수, crawl_article 함수 2개 작성
#   2. 아래 SITES 목록에 항목 1개 추가
#   → main() / save_html() / send_email() 수정 불필요
#
# 각 항목 설명:
#   name           표시 이름 (뱃지, 로그 등에 사용)
#   badge_color    뱃지 글자색 (hex)
#   badge_bg       뱃지 배경색 (hex)
#   badge_border   뱃지 테두리색 (hex)
#   requires_login 로그인 필요 여부
#   login_fn       로그인 함수 (불필요하면 None)
#   search_fn      검색 함수 (keyword, target_dates → list[dict])
#   crawl_fn       기사 크롤링 함수 (info → dict)
#   email_body     True: 이메일 본문에 기사 포함 / False: 링크로만 제공

SITES = [
    {
        "name":           "BioSpectator",
        "badge_color":    "#1a5cb8",
        "badge_bg":       "#e8f0fe",
        "badge_border":   "#bad0f8",
        "requires_login": True,
        "login_fn":       bios_login,
        "search_fn":      bios_search,
        "crawl_fn":       bios_crawl_article,
        "email_body":     True,
    },
    {
        "name":           "더바이오",
        "badge_color":    "#1a7a3c",
        "badge_bg":       "#e6f4ea",
        "badge_border":   "#a8d5b5",
        "requires_login": False,
        "login_fn":       None,
        "search_fn":      thebio_search,
        "crawl_fn":       thebio_crawl_article,
        "email_body":     False,
    },
    # ── 새 사이트는 여기에 추가 ──────────────────────────────────────────────
    # {
    #     "name":           "사이트이름",
    #     "badge_color":    "#색상",
    #     "badge_bg":       "#배경색",
    #     "badge_border":   "#테두리색",
    #     "requires_login": False,
    #     "login_fn":       None,
    #     "search_fn":      새사이트_search,
    #     "crawl_fn":       새사이트_crawl_article,
    #     "email_body":     False,
    # },
]


# ══════════════════════════════════════════════════════════════════════════════
# HTML 리포트 생성
# ══════════════════════════════════════════════════════════════════════════════

def _render_also_in(also_in: list[dict]) -> str:
    """카드 하단 '다른 사이트에서도 보도' 섹션 HTML 생성"""
    if not also_in:
        return ""
    links = " &nbsp;·&nbsp; ".join(
        f'<a href="{d["URL"]}" target="_blank" '
        f'style="color:{d["_badge_color"]};text-decoration:none;font-weight:bold;">'
        f'[{d["출처"]}] {d["제목"][:35]}{"…" if len(d["제목"]) > 35 else ""}</a>'
        for d in also_in
    )
    return f'<div class="also-in">📌 다른 사이트에서도 보도: {links}</div>'


def save_html(articles: list[dict], target_dates: list[str]) -> str:
    os.makedirs("docs", exist_ok=True)

    by_keyword = defaultdict(list)
    for a in articles:
        if a["URL"] not in {x["URL"] for x in by_keyword[a["키워드"]]}:
            by_keyword[a["키워드"]].append(a)

    # 사이트별 건수 (등록된 사이트만)
    src_counts = " &nbsp;|&nbsp; ".join(
        f'{s["name"]} {sum(1 for a in articles if a["출처"] == s["name"])}건'
        for s in SITES if any(a["출처"] == s["name"] for a in articles)
    )

    sections_html = ""
    for idx, (kw, arts) in enumerate(by_keyword.items()):
        cards = ""
        for a in arts:
            body_html  = highlight_keywords(a["본문"]) if a["본문"] else "<span class='paid'>유료기사 - 전문 열람 불가</span>"
            paid_badge = '<span class="badge" style="background:#fff0f0;color:#c00;border:1px solid #fcc;">유료</span>' if a["유료기사"] else ""
            badge_style = f'background:{a["_badge_bg"]};color:{a["_badge_color"]};border:1px solid {a["_badge_border"]};'
            src_badge   = f'<span class="badge" style="{badge_style}">{a["출처"]}</span>'
            cards += f"""
            <article class="card">
                <div class="card-header">
                    <h2>{src_badge}<a href="{a['URL']}" target="_blank">{a['제목']}</a>{paid_badge}</h2>
                    <span class="date">{a['날짜']}</span>
                </div>
                <div class="card-body">{body_html}</div>
                <div class="card-footer">
                    <a href="{a['URL']}" target="_blank">원문 보기 &rarr;</a>
                    {_render_also_in(a.get("also_in", []))}
                </div>
            </article>"""
        sections_html += f"""
        <section id="kw-{idx}">
            <h1 class="section-title">#{kw.upper()}</h1>
            {cards}
        </section>"""

    date_label   = " / ".join(target_dates)
    generated    = datetime.now().strftime("%Y년 %m월 %d일 %H:%M")
    header_links = " &nbsp;|&nbsp; ".join(
        f'<a href="#" onclick="var el=document.getElementById(\'kw-{i}\');if(el){{el.scrollIntoView({{behavior:\'smooth\'}});}};return false;" style="color:#fff;text-decoration:none;font-size:13px;">{k.upper()} ({len(v)}건)</a>'
        for i, (k, v) in enumerate(by_keyword.items())
    )

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>바이오 키워드 리포트 ({date_label})</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Malgun Gothic', sans-serif; background: #f4f6f9; color: #222; }}
  .top-bar {{ background: #1a3a5c; color: #fff; padding: 10px 24px; font-size: 12px; display: flex; align-items: center; gap: 16px; flex-wrap: wrap; position: sticky; top: 0; z-index: 100; }}
  .top-bar .logo {{ font-size: 15px; font-weight: bold; color: #7ecfff; margin-right: 8px; }}
  .top-bar .meta {{ color: #aac; }}
  .top-bar .src-count {{ font-size: 11px; color: #cce; }}
  .top-bar .links {{ display: flex; gap: 8px; flex-wrap: wrap; }}
  .wrap {{ max-width: 900px; margin: 0 auto; padding: 24px 20px; }}
  .section-title {{ font-size: 20px; color: #1a3a5c; border-left: 5px solid #0077cc; padding-left: 12px; margin: 32px 0 16px; }}
  .card {{ background: #fff; border-radius: 8px; box-shadow: 0 1px 4px rgba(0,0,0,.08); margin-bottom: 20px; }}
  .card-header {{ padding: 12px 20px 8px; border-bottom: 1px solid #eee; }}
  .card-header h2 {{ font-size: 17px; line-height: 1.5; }}
  .card-header h2 a {{ color: #1a3a5c; text-decoration: none; }}
  .card-header h2 a:hover {{ text-decoration: underline; }}
  .date {{ font-size: 12px; color: #888; margin-top: 4px; display: block; }}
  .card-body {{ padding: 16px 20px; font-size: 14px; line-height: 1.9; color: #333; }}
  .card-body h4 {{ font-size: 16px; font-weight: bold; color: #333; background: #f0f4f8; border-left: 4px solid #0077cc; padding: 10px 16px; margin: 0 0 12px; line-height: 1.8; }}
  .card-body p {{ margin-bottom: 12px; white-space: pre-line; }}
  .card-body img {{ max-width: 100%; height: auto; margin: 8px 0; }}
  .card-footer {{ padding: 10px 20px; background: #f8f9fb; font-size: 13px; border-radius: 0 0 8px 8px; }}
  .card-footer a {{ color: #0077cc; text-decoration: none; }}
  .badge {{ font-size: 11px; padding: 2px 7px; border-radius: 10px; margin-right: 4px; vertical-align: middle; font-weight: bold; }}
  mark {{ background: #ffff00; padding: 0 2px; font-style: normal; }}
  .paid {{ color: #999; font-style: italic; }}
  .no-articles {{ color: #999; font-size: 14px; padding: 20px; }}
  .also-in {{ margin-top: 8px; padding-top: 8px; border-top: 1px dashed #ddd; font-size: 12px; color: #666; }}
</style>
</head>
<body>
<div id="top" class="top-bar">
  <span class="logo">바이오 뉴스</span>
  <span class="meta">{generated} &nbsp;|&nbsp; {date_label} &nbsp;|&nbsp; 전체 {len(articles)}건</span>
  <span class="src-count">{src_counts}</span>
  <div class="links">{header_links}</div>
</div>
<div class="wrap">
{sections_html if sections_html else '<p class="no-articles">오늘 날짜 기사가 없습니다.</p>'}
</div>
</body>
</html>"""

    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(html)
    return "docs/index.html"


# ══════════════════════════════════════════════════════════════════════════════
# 중복 방지
# ══════════════════════════════════════════════════════════════════════════════

def load_sent_urls() -> set:
    if os.path.exists(SENT_URLS_FILE):
        with open(SENT_URLS_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f).get("urls", []))
    return set()


def save_sent_urls(new_urls: list[str], existing: set):
    all_urls = list(existing | set(new_urls))[-500:]
    os.makedirs("docs", exist_ok=True)
    with open(SENT_URLS_FILE, "w", encoding="utf-8") as f:
        json.dump({"urls": all_urls, "updated": datetime.now().isoformat()}, f, ensure_ascii=False, indent=2)


# ══════════════════════════════════════════════════════════════════════════════
# 이메일 발송
# ══════════════════════════════════════════════════════════════════════════════

def send_email(target_dates: list[str], articles: list[dict]):
    gmail_from = os.getenv("GMAIL_FROM")
    app_pw     = os.getenv("GMAIL_APP_PASSWORD")
    gmail_to   = os.getenv("GMAIL_TO")
    if not app_pw:
        print("[SKIP] Gmail 앱 비밀번호 미설정 → 이메일 발송 건너뜀")
        return

    date_label = " / ".join(target_dates)
    subject    = f"[바이오뉴스] {date_label} 키워드 리포트 ({len(articles)}건)"

    # email_body=False 사이트 안내 배너
    notice_lines = ""
    for site in SITES:
        if not site["email_body"]:
            cnt = sum(1 for a in articles if a["출처"] == site["name"])
            if cnt > 0:
                notice_lines += f"""
        <div style="font-family:'Malgun Gothic',sans-serif;text-align:center;padding:8px 24px;
                    background:{site['badge_bg']};color:{site['badge_color']};font-size:13px;">
          {site['name']} 기사 <b>{cnt}건</b>은 위 링크(브라우저)에서 확인하세요.
        </div>"""

    header = f"""
    <div style="font-family:'Malgun Gothic',sans-serif;text-align:center;padding:24px;background:#1a3a5c;">
      <span style="font-size:18px;font-weight:bold;color:#7ecfff;">바이오 키워드 리포트</span>
      <span style="color:#aac;font-size:12px;margin-left:16px;">{date_label} &nbsp;|&nbsp; 총 {len(articles)}건</span><br><br>
      <a href="{PAGES_URL}" target="_blank"
         style="display:inline-block;padding:12px 28px;background:#0077cc;color:#fff;
                text-decoration:none;border-radius:6px;font-size:15px;font-weight:bold;">
        📰 브라우저에서 열기 (전체 기사)
      </a>
    </div>
    {notice_lines}
    <hr style="border:none;border-top:3px solid #1a3a5c;margin:0;">"""

    # email_body=True 사이트 기사를 이메일 본문에 포함
    body_html = ""
    for site in SITES:
        if not site["email_body"]:
            continue
        site_articles = [a for a in articles if a["출처"] == site["name"]]
        if not site_articles:
            continue
        by_kw = defaultdict(list)
        for a in site_articles:
            if a["URL"] not in {x["URL"] for x in by_kw[a["키워드"]]}:
                by_kw[a["키워드"]].append(a)
        sections = ""
        for kw, arts in by_kw.items():
            cards = ""
            for a in arts:
                bh        = highlight_keywords(a["본문"]) if a["본문"] else "<span style='color:#999;font-style:italic;'>유료기사 - 전문 열람 불가</span>"
                paid_b    = '<span style="font-size:11px;padding:2px 7px;border-radius:10px;background:#fff0f0;color:#c00;border:1px solid #fcc;margin-left:8px;">유료</span>' if a["유료기사"] else ""
                cards    += f"""
                <div style="background:#fff;border-radius:8px;box-shadow:0 1px 4px rgba(0,0,0,.08);margin-bottom:20px;">
                  <div style="padding:12px 20px 8px;border-bottom:1px solid #eee;">
                    <div style="font-size:17px;line-height:1.5;">
                      <a href="{a['URL']}" target="_blank" style="color:#1a3a5c;text-decoration:none;">{a['제목']}</a>{paid_b}
                    </div>
                    <span style="font-size:12px;color:#888;margin-top:4px;display:block;">{a['날짜']}</span>
                  </div>
                  <div style="padding:16px 20px;font-size:14px;line-height:1.9;color:#333;">{bh}</div>
                  <div style="padding:10px 20px;background:#f8f9fb;font-size:13px;border-radius:0 0 8px 8px;">
                    <a href="{a['URL']}" target="_blank" style="color:#0077cc;text-decoration:none;">원문 보기 &rarr;</a>
                  </div>
                </div>"""
            sections += f"""
            <div style="margin-top:32px;">
              <h1 style="font-size:20px;color:#1a3a5c;border-left:5px solid #0077cc;padding-left:12px;margin-bottom:16px;">#{kw.upper()}</h1>
              {cards}
            </div>"""
        body_html += f"""
        <div style="max-width:900px;margin:0 auto;padding:24px 20px;font-family:'Malgun Gothic',sans-serif;">
          <div style="font-size:13px;color:#888;margin-bottom:8px;">▼ {site['name']} 기사 미리보기</div>
          {sections}
        </div>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = gmail_from
    msg["To"]      = gmail_to
    msg.attach(MIMEText(header + body_html, "html", "utf-8"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
            server.starttls()
            server.login(gmail_from, app_pw)
            server.send_message(msg)
        print(f"[OK] 이메일 발송 완료 → {gmail_to}")
    except Exception as e:
        print(f"[FAIL] 이메일 발송 실패: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# 메인  (SITES를 자동 순회 — 수정 불필요)
# ══════════════════════════════════════════════════════════════════════════════

def main():
    session      = requests.Session()
    target_dates = get_target_dates()
    print(f"대상 날짜: {', '.join(target_dates)}")
    print(f"검색 키워드: {', '.join(KEYWORDS)}\n")

    # 사이트별 로그인
    site_ok = {}
    for site in SITES:
        if site["requires_login"]:
            site_ok[site["name"]] = site["login_fn"](session)
        else:
            site_ok[site["name"]] = True

    # 사이트별 검색
    all_infos, seen_urls = [], set()
    for site in SITES:
        name = site["name"]
        if not site_ok[name]:
            print(f"\n[{name}] 로그인 실패 → 건너뜀")
            continue
        print(f"\n[{name} 검색]")
        for kw in KEYWORDS:
            for variant in build_search_variants(kw):
                for info in site["search_fn"](session, variant, target_dates):
                    if info["URL"] not in seen_urls:
                        seen_urls.add(info["URL"])
                        info["키워드"] = kw
                        info["출처"]  = name
                        info["_site"] = site
                        all_infos.append(info)

    if not all_infos:
        print("\n오늘 날짜에 해당하는 기사가 없습니다.")
        return

    # 중복 발송 제외
    sent_urls = load_sent_urls()
    before    = len(all_infos)
    all_infos = [i for i in all_infos if i["URL"] not in sent_urls]
    if before - len(all_infos):
        print(f"\n  → 이미 발송된 기사 {before - len(all_infos)}건 제외")
    if not all_infos:
        print("새로운 기사가 없습니다 (모두 이미 발송됨).")
        return

    # 사이트별 건수 출력
    cnt_str = " / ".join(
        f'{s["name"]} {sum(1 for i in all_infos if i["출처"]==s["name"])}건'
        for s in SITES
    )
    print(f"\n총 {len(all_infos)}건 전문 크롤링 시작... ({cnt_str})")

    # 전문 크롤링
    articles = []
    for i, info in enumerate(all_infos, 1):
        site    = info["_site"]
        article = site["crawl_fn"](session, info)
        article["키워드"]       = info["키워드"]
        article["출처"]         = info["출처"]
        article["_badge_color"] = site["badge_color"]
        article["_badge_bg"]    = site["badge_bg"]
        article["_badge_border"]= site["badge_border"]
        articles.append(article)
        print(f"  ({i}/{len(all_infos)}) [{info['출처']}] {article['제목'][:50]}...")
        time.sleep(0.5)

    # 사이트 간 중복 기사 제거
    before   = len(articles)
    articles = deduplicate_across_sites(articles)
    if before - len(articles):
        print(f"  → 사이트 간 중복 {before - len(articles)}건 제거")

    # HTML 저장
    html_path = save_html(articles, target_dates)
    paid      = sum(1 for a in articles if a["유료기사"])
    print(f"\n[OK] 저장 완료: {html_path}")
    print(f"  전체: {len(articles)}건 (전문: {len(articles)-paid}건 / 유료: {paid}건)")

    save_sent_urls([a["URL"] for a in articles], sent_urls)
    send_email(target_dates, articles)


if __name__ == "__main__":
    main()
