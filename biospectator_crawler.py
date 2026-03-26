"""
================================================================================
BioSpectator 키워드 검색 크롤러
================================================================================

■ 제작 환경
  - Claude Code (claude.ai 앱 내 코드 탭, 터미널 포함)
  - Windows 10 로컬 PC에서 개발 및 테스트
  - 파일 위치: C:/Users/user/test_project/biospectator/

■ 동작 방식
  - KEYWORDS 목록의 키워드로 BioSpectator 검색
  - 오늘 + 어제 날짜 기사만 전문 크롤링 (월요일은 토/일 추가)
  - 결과를 HTML 리포트로 저장 후 이메일 발송

■ 배포 방식 (GitHub Actions)
  - GitHub 저장소: https://github.com/go2god4u-glitch/biospectator-crawler
  - 저장소는 Private으로 설정 (코드/크롤링 로직 보호)
  - .github/workflows/daily-crawler.yml 에 스케줄 정의
  - 평일 오전 9:30 KST (UTC 00:30) 자동 실행
  - 로그인 정보/이메일 계정은 GitHub Secrets에 암호화 저장
    (저장소 Settings → Secrets and variables → Actions 에서 확인/수정 가능)
  - 수동 실행: GitHub Actions 탭 → Run workflow 버튼

■ 나중에 코드 수정이 필요할 때
  ─────────────────────────────────────────────────────
  1. Claude 앱(claude.ai) 실행 → 이 대화 열기
     (또는 새 대화에서 "이 파일 열어줘" 후 파일 경로 전달)

  2. 파일 불러오기:
     Claude Code 탭(터미널)에서 자동으로 로컬 파일에 접근 가능
     별도로 불러올 필요 없이 바로 "~~ 수정해줘" 라고 요청하면 됨
     (파일 경로: C:/Users/user/test_project/biospectator/biospectator_crawler.py)

  3. 수정 요청 예시:
     - "키워드에 '삼성바이오로직스' 추가해줘"
     - "이메일 수신자를 xxx@donga.co.kr 로 바꿔줘"
     - "월~금 9:30 → 8:00으로 실행 시간 바꿔줘"

  4. 수정 후 GitHub 배포:
     Claude가 자동으로 git commit + push 처리
     push 완료 즉시 GitHub Actions에 반영됨 (별도 작업 불필요)

  5. 이메일 수신자/발신자 변경 시:
     GitHub Secrets에서 GMAIL_TO 값을 직접 수정
     → 저장소 Settings → Secrets → GMAIL_TO → Update
     (또는 Claude에게 "이메일 수신자 바꿔줘"라고 요청하면 API로 처리)

  6. 키워드만 추가할 때는 아래 KEYWORDS 리스트에만 추가하면 됨
  ─────────────────────────────────────────────────────

■ 주요 파일 구조
  biospectator/
  ├── biospectator_crawler.py   ← 메인 크롤러 (이 파일)
  ├── requirements.txt          ← Python 패키지 목록
  ├── .env                      ← 로그인/이메일 정보 (로컬 전용, GitHub에 올라가지 않음)
  ├── .gitignore                ← .env, *.html 제외 설정
  └── .github/
      └── workflows/
          └── daily-crawler.yml ← GitHub Actions 스케줄 정의

================================================================================
"""

import requests
from bs4 import BeautifulSoup, NavigableString
from datetime import datetime, timedelta
import time
import re
import os
import smtplib
import css_inline
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()  # .env 에서 BIOS_ID, BIOS_PW 로드

BASE_URL   = "https://www.biospectator.com"
LOGIN_URL  = "https://member.biospectator.com/login_prc.php"
SEARCH_URL = BASE_URL + "/section/search_list?searchkey={keyword}&page={page}"

# 키워드 추가는 여기에만 하면 됨. 영어 포함 시 대/소문자 자동 검색
KEYWORDS = [
    "glp-1",
    "Amylin",
    "동아ST",
    "노보노디스크",
    "메타비아",
]

# 키워드 별칭: 하나만 입력해도 묶인 모든 단어로 검색
# - 검색은 모든 별칭으로 수행, 결과는 대표 키워드(KEYWORDS에 있는 것)로 그룹화
# - 하이라이트도 별칭 포함 모두 강조
KEYWORD_ALIASES = {
    "동아ST":    ["동아에스티"],
    "Amylin":   ["아밀린"],
    "메타비아":  ["MetaVia"],
    "노보노디스크": ["Novo Nordisk"],
}

# 브라우저처럼 보이게 해서 차단 방지
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Referer": BASE_URL,
}


def get_target_dates() -> list[str]:
    """
    항상 오늘 + 어제 포함 (당일 크롤링 이후 올라온 기사 누락 방지)
    월요일은 토/일까지 추가 포함
    """
    today = datetime.now()
    dates = [today.strftime("%Y-%m-%d"),
             (today - timedelta(days=1)).strftime("%Y-%m-%d")]  # 항상 어제 포함
    if today.weekday() == 0:  # 월요일: 금/토/일 추가
        dates.append((today - timedelta(days=2)).strftime("%Y-%m-%d"))  # 토
        dates.append((today - timedelta(days=3)).strftime("%Y-%m-%d"))  # 금
    return list(dict.fromkeys(dates))  # 중복 제거 (순서 유지)


def login(session: requests.Session) -> bool:
    """.env 없으면 직접 입력. 로그인 성공 여부는 쿠키 LOGIN_IDX로 판단"""
    user_id  = os.getenv("BIOS_ID")
    password = os.getenv("BIOS_PW")
    if not user_id or not password:
        user_id  = input("아이디: ").strip()
        password = input("비밀번호: ").strip()

    session.get("https://member.biospectator.com/login.php", headers=HEADERS)  # 세션쿠키 획득
    session.post(
        LOGIN_URL,
        data={"MEMR_EID": user_id, "MEMR_PWD": password, "URL": BASE_URL + "/"},
        headers={**HEADERS, "Referer": "https://member.biospectator.com/login.php"},
        allow_redirects=True,
    )

    logged_in = "LOGIN_IDX" in session.cookies
    print("[OK] 로그인 성공" if logged_in else "[FAIL] 로그인 실패")
    return logged_in


def search_articles(session: requests.Session, keyword: str, target_dates: list[str]) -> list[dict]:
    """
    검색 결과에서 대상 날짜 기사 URL 수집 (최대 10페이지)
    결과는 최신순이므로 대상 날짜보다 오래된 기사가 나오면 즉시 중단
    """
    results = []
    seen = set()

    for page in range(1, 10):
        url  = SEARCH_URL.format(keyword=keyword, page=page)
        resp = session.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")

        found_any = False
        stop      = False

        for a_tag in soup.select("a[href*='/news/view/']"):
            title = a_tag.get_text(strip=True)
            if len(title) < 5:  # 아이콘 등 짧은 텍스트 제외
                continue

            href        = a_tag.get("href", "")
            article_url = BASE_URL + href if href.startswith("/") else href
            if article_url in seen:
                continue
            seen.add(article_url)

            # 날짜는 부모/조부모 텍스트에서 정규식으로 추출
            date = ""
            for parent in [a_tag.find_parent(), a_tag.find_parent().find_parent() if a_tag.find_parent() else None]:
                if parent:
                    m = re.search(r"(\d{4}-\d{2}-\d{2})", parent.get_text())
                    if m:
                        date = m.group(1)
                        break

            if not date:
                continue

            if date in target_dates:
                found_any = True
                results.append({"키워드": keyword, "날짜": date, "URL": article_url})
            elif date < min(target_dates):
                stop = True
                break

        if stop or not found_any:
            break
        time.sleep(0.3)

    print(f"  [{keyword}] {len(results)}건 발견")
    return results


def crawl_article(session: requests.Session, info: dict) -> dict:
    """
    기사 전문 크롤링
    - 제목: <h3>, 날짜: .datetime
    - 본문: .article_view HTML 유지 (광고/유료안내문/기자정보 제거)
    - <h4>(핵심요약)는 CSS로 강조 표시하기 위해 태그 유지
    - 인라인 style/class 제거 → 리포트 CSS가 일관 적용되도록
    - 본문 100자 미만이면 유료기사로 판단
    """
    url = info["URL"]
    try:
        resp = session.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")

        title = ""
        h3 = soup.select_one("h3")
        if h3:
            title = h3.get_text(strip=True)

        date    = info.get("날짜", "")
        date_el = soup.select_one(".datetime")
        if date_el:
            m = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", date_el.get_text())
            if m:
                date = m.group(1)

        body    = ""
        body_el = soup.select_one(".article_view")
        if body_el:
            # 불필요 요소 제거: 광고, 관련기사, 유료안내박스, 기자정보
            for tag in body_el.select(".ad, .related, script, style, .viwe-pay-coment, .reporter"):
                tag.decompose()
            # HTML 주석 제거 (제목~h4 사이 빈 공간 원인)
            from bs4 import Comment
            for comment in body_el.find_all(string=lambda t: isinstance(t, Comment)):
                comment.extract()
            # 비어있는 <p>, <div>, <br> 연속 제거
            for tag in body_el.find_all(['p', 'div']):
                if not tag.get_text(strip=True) and not tag.find('img'):
                    tag.decompose()
            for tag in body_el.find_all(True):
                if tag.get("style"): del tag["style"]
                if tag.get("class"): del tag["class"]
            # 텍스트 노드의 \n → <br> 변환 (내용 있는 노드만, 태그 사이 공백은 제외)
            for node in list(body_el.descendants):
                if isinstance(node, NavigableString) and node.parent.name not in ['script', 'style']:
                    text = str(node)
                    if '\n' in text and text.strip():  # 공백만 있는 노드는 건너뜀
                        node.replace_with(BeautifulSoup(text.replace('\n', '<br>'), 'html.parser'))
            body = str(body_el)

        is_paid = "[유료]" if not body or len(body) < 100 else ""

        return {"키워드": info["키워드"], "제목": title, "날짜": date, "본문": body, "유료기사": is_paid, "URL": url}
    except Exception as e:
        return {"키워드": info["키워드"], "제목": "", "날짜": info.get("날짜", ""), "본문": f"[오류] {e}", "유료기사": "", "URL": url}


def highlight_keywords(body_html: str, keywords: list[str]) -> str:
    """본문 HTML에서 키워드(+별칭) 모두 형광 <mark>로 강조. HTML 태그 내부는 건드리지 않음"""
    all_terms = set()
    for kw in keywords:
        all_terms.add(kw)
        if re.search(r'[a-zA-Z]', kw):
            all_terms |= {kw.lower(), kw.upper()}
        for alias in KEYWORD_ALIASES.get(kw, []):   # 별칭도 강조 대상에 포함
            all_terms.add(alias)
            if re.search(r'[a-zA-Z]', alias):
                all_terms |= {alias.lower(), alias.upper()}
    for v in sorted(all_terms, key=len, reverse=True):  # 긴 것 먼저 치환
        pattern = f'({re.escape(v)})(?![^<]*>)'
        body_html = re.sub(pattern, r'<mark>\1</mark>', body_html, flags=re.IGNORECASE)
    return body_html


def save_html(articles: list[dict], target_dates: list[str]) -> str:
    """
    HTML 리포트 저장 (biospectator_YYYYMMDD_HHMM.html)
    - 레이아웃: 고정 사이드바(키워드 링크) + 스크롤 본문
    - 키워드별 섹션 구분, 기사는 연속 배치 (박스 스크롤 없음)
    - 키워드/섹션 제목은 대문자 표시
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    html_path = f"biospectator_{timestamp}.html"

    by_keyword = defaultdict(list)
    for a in articles:
        if a["URL"] not in {x["URL"] for x in by_keyword[a["키워드"]]}:
            by_keyword[a["키워드"]].append(a)

    sections_html = ""
    for idx, (kw, arts) in enumerate(by_keyword.items()):
        cards = ""
        for a in arts:
            body_html  = highlight_keywords(a["본문"], KEYWORDS) if a["본문"] else "<span class='paid'>유료기사 - 전문 열람 불가</span>"
            paid_badge = '<span class="badge">유료</span>' if a["유료기사"] else ""
            cards += f"""
            <article class="card">
                <div class="card-header">
                    <h2><a href="{a['URL']}" target="_blank">{a['제목']}</a>{paid_badge}</h2>
                    <span class="date">{a['날짜']}</span>
                </div>
                <div class="card-body">{body_html}</div>
                <div class="card-footer"><a href="{a['URL']}" target="_blank">원문 보기 &rarr;</a></div>
            </article>"""
        sections_html += f"""
        <section id="kw-{idx}">
            <h1 class="section-title">#{kw.upper()}</h1>
            {cards}
        </section>"""

    date_label = " / ".join(target_dates)
    generated  = datetime.now().strftime("%Y년 %m월 %d일 %H:%M")
    total      = len(articles)

    # 상단 헤더: 이메일/브라우저 모두 바로 기사가 시작되도록 compact하게
    header_links = " &nbsp;|&nbsp; ".join(
        f'<a href="#" onclick="var el=document.getElementById(\'kw-{i}\');if(el){{el.scrollIntoView({{behavior:\'smooth\'}});}};return false;" style="color:#fff;text-decoration:none;font-size:13px;">{k.upper()} ({len(v)}건)</a>'
        for i, (k, v) in enumerate(by_keyword.items())
    )

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BioSpectator 키워드 리포트 ({date_label})</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Malgun Gothic', sans-serif; background: #f4f6f9; color: #222; }}
  .top-bar {{ background: #1a3a5c; color: #fff; padding: 10px 24px; font-size: 12px; display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }}
  .top-bar .logo {{ font-size: 15px; font-weight: bold; color: #7ecfff; margin-right: 8px; }}
  .top-bar .meta {{ color: #aac; }}
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
  .badge {{ font-size: 11px; padding: 2px 7px; border-radius: 10px; background: #fff0f0; color: #c00; border: 1px solid #fcc; margin-left: 8px; vertical-align: middle; }}
  mark {{ background: #ffff00; padding: 0 2px; font-style: normal; }}
  .paid {{ color: #999; font-style: italic; }}
  .no-articles {{ color: #999; font-size: 14px; padding: 20px; }}
</style>
</head>
<body>
<div id="top" class="top-bar">
  <span class="logo">BioSpectator</span>
  <span class="meta">{generated} &nbsp;|&nbsp; {date_label} &nbsp;|&nbsp; 전체 {total}건</span>
  <div class="links">{header_links}</div>
</div>
<div class="wrap">
{sections_html if sections_html else '<p class="no-articles">오늘 날짜 기사가 없습니다.</p>'}
</div>
</body>
</html>"""

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    return html_path


def send_email(html_path: str, target_dates: list[str], article_count: int):
    """
    완성된 HTML 리포트를 Gmail로 발송
    - Gmail SMTP (포트 587, TLS) 사용
    - .env의 GMAIL_FROM / GMAIL_APP_PASSWORD / GMAIL_TO 필요
    - 앱 비밀번호 미설정 시 발송 건너뜀
    """
    gmail_from = os.getenv("GMAIL_FROM")
    app_pw     = os.getenv("GMAIL_APP_PASSWORD")
    gmail_to   = os.getenv("GMAIL_TO")

    if not app_pw or app_pw == "여기에_앱비밀번호_입력":
        print("[SKIP] Gmail 앱 비밀번호 미설정 → 이메일 발송 건너뜀")
        return

    # HTML 파일 로드 후 CSS 인라인화 (Gmail은 <style> 블록을 제거하므로 필수)
    with open(html_path, "r", encoding="utf-8") as f:
        html_body = f.read()
    html_body = css_inline.inline(html_body)

    date_label = " / ".join(target_dates)
    subject    = f"[BioSpectator] {date_label} 키워드 리포트 ({article_count}건)"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = gmail_from
    msg["To"]      = gmail_to
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
            server.starttls()
            server.login(gmail_from, app_pw)
            server.send_message(msg)
        print(f"[OK] 이메일 발송 완료 → {gmail_to}")
    except Exception as e:
        print(f"[FAIL] 이메일 발송 실패: {e}")


def main():
    session = requests.Session()
    if not login(session):
        return

    target_dates = get_target_dates()
    print(f"\n대상 날짜: {', '.join(target_dates)}")
    print(f"검색 키워드: {', '.join(KEYWORDS)}\n")

    # 검색 변형 생성: 영어 대/소문자 + KEYWORD_ALIASES 별칭 모두 포함
    # 결과는 KEYWORDS 대표 키워드로 그룹화
    all_infos = []
    seen_urls = set()
    for kw in KEYWORDS:
        search_variants = [kw]
        if re.search(r'[a-zA-Z]', kw):          # 영어 포함 시 대/소문자 추가
            variants = {kw.lower(), kw.upper()}
            variants.discard(kw)
            search_variants += list(variants)
        for alias in KEYWORD_ALIASES.get(kw, []):  # 별칭도 검색 목록에 추가
            search_variants.append(alias)
            if re.search(r'[a-zA-Z]', alias):
                search_variants += [alias.lower(), alias.upper()]
        for variant in search_variants:
            for info in search_articles(session, variant, target_dates):
                if info["URL"] not in seen_urls:
                    seen_urls.add(info["URL"])
                    info["키워드"] = kw           # 대표 키워드로 그룹화
                    all_infos.append(info)

    if not all_infos:
        print("오늘 날짜에 해당하는 기사가 없습니다.")
        return

    print(f"\n총 {len(all_infos)}건 전문 크롤링 시작...")

    articles = []
    for i, info in enumerate(all_infos, 1):
        article = crawl_article(session, info)
        articles.append(article)
        print(f"  ({i}/{len(all_infos)}) {article['제목'][:50]}...")
        time.sleep(0.5)

    html_path = save_html(articles, target_dates)
    paid = sum(1 for a in articles if a["유료기사"])
    print(f"\n[OK] 저장 완료: {html_path}")
    print(f"  전체: {len(articles)}건 (전문: {len(articles)-paid}건 / 유료: {paid}건)")

    # 이메일 발송
    send_email(html_path, target_dates, len(articles))


if __name__ == "__main__":
    main()
