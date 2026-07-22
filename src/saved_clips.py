"""
監査担当が「良かった」と判断したクリップを、FIFO削除の対象外として
永久に残しておくためのモジュール。

clip_extractor.py のFIFO管理には一切触れず、data/clips/ 配下の
確定クリップを data/saved/ へ "コピー" するだけの独立した処理にする
(既存のクリップ生成・削除ロジックへの影響をゼロにするため)。

将来、複数コートの保存クリップを校内LANの集約サーバーに送る運用へ
拡張する際にそのまま使えるよう、保存のたびに「いつ・どのコートで・
元はどのクリップか」を data/saved/index.jsonl に1行ずつ記録しておく。
"""

import hashlib
import json
import os
import shutil
import threading
import time

from config import CLIPS_DIR, SAVED_DIR, SAVED_INDEX_PATH, COURT_NAME

_index_lock = threading.Lock()


def _ascii_court_slug(text):
    """
    ファイル名(=URLパスの一部になる)は日本語を含む COURT_NAME(既定
    "コート1")をそのまま使うとASCII前提の処理(urllib等)で壊れるため、
    ハッシュ値でASCII専用の短い識別子に変換する。人が読める表記は
    entry["court"] の方に別途そのまま保持する。
    """
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def save_clip(source_filename):
    """
    data/clips/<source_filename> を data/saved/ へコピーし、永久保存する。

    source_filename: クリップのファイル名のみ(パスは含まない想定)。
                      ディレクトリトラバーサル対策として basename 化した上で
                      CLIPS_DIR 直下に実在するファイルであることを確認する。

    戻り値: 保存したクリップのメタ情報(dict)
    例外: FileNotFoundError (元クリップが既に存在しない場合。
          保存ボタンを押した直後にFIFOで消えた等のレアケース)
    """
    source_filename = os.path.basename(source_filename)
    source_path = os.path.join(CLIPS_DIR, source_filename)
    if not os.path.exists(source_path):
        raise FileNotFoundError(f"元クリップが見つかりません: {source_filename}")

    saved_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    stamp = time.strftime("%Y%m%d_%H%M%S")
    court_part = _ascii_court_slug(COURT_NAME)
    orig_stem = os.path.splitext(source_filename)[0]
    saved_filename = f"saved_{court_part}_{stamp}_{orig_stem}.mp4"

    shutil.copyfile(source_path, os.path.join(SAVED_DIR, saved_filename))

    entry = {
        "saved_at": saved_at,
        "saved_at_epoch": time.time(),
        "court": COURT_NAME,
        "filename": saved_filename,
        "original_clip": source_filename,
    }
    with _index_lock:
        with open(SAVED_INDEX_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return entry


def list_saved_clips():
    """保存済みクリップの一覧を新しい順で返す"""
    if not os.path.exists(SAVED_INDEX_PATH):
        return []
    with open(SAVED_INDEX_PATH, encoding="utf-8") as f:
        lines = f.readlines()
    entries = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    # index.jsonl に記録があってもファイル実体が無い(手動削除等)場合は除外する
    entries = [e for e in entries if os.path.exists(os.path.join(SAVED_DIR, e["filename"]))]
    entries.reverse()
    return entries


def delete_saved_clips(filenames):
    """
    指定した保存済みクリップをdata/saved/とindex.jsonlの両方から削除する。
    filenames: ファイル名のリスト(パスは含まない想定。basename化してから扱う)
    """
    targets = {os.path.basename(f) for f in filenames}

    with _index_lock:
        for name in targets:
            path = os.path.join(SAVED_DIR, name)
            if os.path.exists(path):
                os.remove(path)

        if not os.path.exists(SAVED_INDEX_PATH):
            return
        with open(SAVED_INDEX_PATH, encoding="utf-8") as f:
            lines = f.readlines()
        remaining = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                entry = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if entry.get("filename") not in targets:
                remaining.append(json.dumps(entry, ensure_ascii=False))
        with open(SAVED_INDEX_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(remaining) + ("\n" if remaining else ""))
