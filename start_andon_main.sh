#!/bin/bash
# デスクトップ描画およびネットワーク初期化待ち
sleep 5

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# 起動ログを保存しつつ実行
exec python3 andon_main.py >> andon_main_boot.log 2>&1
