@echo off
cd /d "C:\Users\user\test_project\biospectator"
if not exist logs mkdir logs
python biospectator_crawler.py >> logs\crawler.log 2>&1
git add docs/index.html docs/sent_urls.json >> logs\crawler.log 2>&1
git diff --cached --quiet || git commit -m "chore: 리포트 업데이트 %date% %time%" >> logs\crawler.log 2>&1
git pull --rebase origin main >> logs\crawler.log 2>&1
git push origin main >> logs\crawler.log 2>&1
