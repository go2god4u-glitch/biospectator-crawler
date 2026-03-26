"""
BioSpectator 키워드 검색 크롤러
키워드로 검색 → 오늘 날짜 기사만 전문 크롤링 → HTML 리포트 저장
사용법: python biospectator_crawler.py
"""

import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import time
import re
import os
import smtplib
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
    "노보노디스크",
    "동아ST",
]

# 브라우저처럼 보이게 해서 차단 방지
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Referer": BASE_URL,
}


def get_target_dates() -> list[str]:
    """평일은 오늘만, 월요일은 토/일 포함 (주말 기사 누락 방지)"""
    today = datetime.now()
    dates = [today.strftime("%Y-%m-%d")]
    if today.weekday() == 0:  # 월요일
        dates.append((today - timedelta(days=1)).strftime("%Y-%m-%d"))  # 일
        dates.append((today - timedelta(days=2)).strftime("%Y-%m-%d"))  # 토
    return dates


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
            for tag in body_el.find_all(True):
                if tag.get("style"): del tag["style"]
                if tag.get("class"): del tag["class"]
            body = str(body_el)

        is_paid = "[유료]" if not body or len(body) < 100 else ""

        return {"키워드": info["키워드"], "제목": title, "날짜": date, "본문": body, "유료기사": is_paid, "URL": url}
    except Exception as e:
        return {"키워드": info["키워드"], "제목": "", "날짜": info.get("날짜", ""), "본문": f"[오류] {e}", "유료기사": "", "URL": url}


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

    # onclick 스크롤: 이메일 클라이언트가 #anchor 링크를 막는 경우 대비
    section_links = "\n".join(
        f'<li><a href="#" onclick="var el=document.getElementById(\'kw-{i}\');if(el){{el.scrollIntoView({{behavior:\'smooth\'}});}};return false;">{k.upper()} ({len(v)}건)</a></li>'
        for i, (k, v) in enumerate(by_keyword.items())
    )

    sections_html = ""
    for idx, (kw, arts) in enumerate(by_keyword.items()):
        cards = ""
        for a in arts:
            body_html  = a["본문"] if a["본문"] else "<span class='paid'>유료기사 - 전문 열람 불가</span>"
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

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BioSpectator 키워드 리포트 ({date_label})</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Malgun Gothic', sans-serif; background: #f4f6f9; color: #222; display: flex; }}
  nav {{ width: 220px; min-height: 100vh; background: #1a3a5c; color: #fff; padding: 24px 16px; position: sticky; top: 0; align-self: flex-start; }}
  nav .logo {{ font-size: 16px; font-weight: bold; color: #7ecfff; margin-bottom: 4px; }}
  nav .meta {{ font-size: 11px; color: #aac; margin-bottom: 20px; line-height: 1.6; }}
  nav ul {{ list-style: none; }}
  nav ul li {{ margin-bottom: 8px; }}
  nav ul li a {{ color: #cde; text-decoration: none; font-size: 13px; }}
  nav ul li a:hover {{ color: #fff; }}
  main {{ flex: 1; padding: 32px 40px; max-width: 900px; }}
  .section-title {{ font-size: 22px; color: #1a3a5c; border-left: 5px solid #0077cc; padding-left: 12px; margin: 40px 0 20px; }}
  .card {{ background: #fff; border-radius: 8px; box-shadow: 0 1px 4px rgba(0,0,0,.08); margin-bottom: 24px; }}
  .card-header {{ padding: 18px 20px 10px; border-bottom: 1px solid #eee; }}
  .card-header h2 {{ font-size: 17px; line-height: 1.5; }}
  .card-header h2 a {{ color: #1a3a5c; text-decoration: none; }}
  .card-header h2 a:hover {{ text-decoration: underline; }}
  .date {{ font-size: 12px; color: #888; margin-top: 4px; display: block; }}
  .card-body {{ padding: 16px 20px; font-size: 14px; line-height: 1.9; color: #333; }}
  .card-body h4 {{ font-size: 16px; font-weight: bold; color: #333; background: #f0f4f8; border-left: 4px solid #0077cc; padding: 12px 16px; margin: 12px 0 16px; line-height: 1.8; }}
  .card-body p {{ margin-bottom: 12px; white-space: pre-line; }}
  .card-body img {{ max-width: 100%; height: auto; margin: 8px 0; }}
  .card-footer {{ padding: 10px 20px; background: #f8f9fb; font-size: 13px; border-radius: 0 0 8px 8px; }}
  .card-footer a {{ color: #0077cc; text-decoration: none; }}
  .badge {{ font-size: 11px; padding: 2px 7px; border-radius: 10px; background: #fff0f0; color: #c00; border: 1px solid #fcc; margin-left: 8px; vertical-align: middle; }}
  .paid {{ color: #999; font-style: italic; }}
  .no-articles {{ color: #999; font-size: 14px; padding: 20px; }}
</style>
</head>
<body>
<nav>
  <div class="logo">BioSpectator</div>
  <div class="meta">
    수집일: {generated}<br>
    대상날짜: {date_label}<br>
    전체 {total}건
  </div>
  <ul>{section_links}</ul>
</nav>
<main>
{sections_html if sections_html else '<p class="no-articles">오늘 날짜 기사가 없습니다.</p>'}
</main>
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

    # HTML 파일을 이메일 본문으로 직접 삽입
    with open(html_path, "r", encoding="utf-8") as f:
        html_body = f.read()

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

    # 영어 키워드는 대/소문자 변형 추가 검색, 중복 URL 제거 후 원본 키워드로 그룹화
    all_infos = []
    seen_urls = set()
    for kw in KEYWORDS:
        search_variants = [kw]
        if re.search(r'[a-zA-Z]', kw):
            variants = {kw.lower(), kw.upper()}
            variants.discard(kw)
            search_variants += list(variants)
        for variant in search_variants:
            for info in search_articles(session, variant, target_dates):
                if info["URL"] not in seen_urls:
                    seen_urls.add(info["URL"])
                    info["키워드"] = kw
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
