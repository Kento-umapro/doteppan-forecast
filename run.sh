#!/bin/zsh
# 毎朝9:40に実行。ダイニーから全店の日別実績を読み直して、この先60日の売上予報を作り、
# GitHub Pages に push する。
#
#   手動実行: ~/doteppan-forecast/run.sh
#   取得だけ: node ~/doteppan-forecast/scripts/fetch_kpi.mjs
#   作り直し: python3 ~/doteppan-forecast/scripts/build.py
#
# ※ ログインセッションは ~/doteppan-shussu/.profile を借りている。
#    切れたら `node ~/doteppan-shussu/scripts/browser.mjs` で開いてログインし直す。
set -u
cd "$HOME/doteppan-forecast" || exit 1
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

mkdir -p logs
LOG="logs/$(date +%Y-%m-%d).log"

{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') 開始 ====="
  node scripts/fetch_kpi.mjs || { echo "!! ダイニーからの取得に失敗しました"; exit 1; }
  python3 scripts/build.py   || { echo "!! 予測の作成に失敗しました"; exit 1; }

  if [[ -n "$(git status --porcelain docs)" ]]; then
    git add docs
    git commit -q -m "予報を更新（実績 $(date -v-1d +%Y-%m-%d) まで）"
    git push -q origin main && echo "push しました" || echo "!! push に失敗しました"
  else
    echo "変化なし（push なし）"
  fi
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') 完了 ====="
} >> "$LOG" 2>&1

STATUS=$?
[ $STATUS -ne 0 ] && osascript -e 'display notification "売上予報の更新に失敗しました。ログを確認してください" with title "どてっぱん 売上予報"' 2>/dev/null
# 古いログは30日で捨てる
find logs -name '20*.log' -mtime +30 -delete 2>/dev/null
exit $STATUS
