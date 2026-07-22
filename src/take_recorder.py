"""
テイクモード(型の演武など、開始〜終了を通しで録画するモード)の
クリップ抽出モジュール。

clip_extractor.py と同じ「バッファのTSセグメント群をconcatしてtrimする」
パターンを踏襲するが、固定のCLIP_DURATION_SECONDS秒ではなく、
start_take()〜stop_take()の実経過時間ぶんを可変長で切り出す点が異なる。

テイクはFIFOで自動削除される clips/ とは別枠の data/takes/ に置き、
保護期間の概念も持たない(削除はWeb画面の一括削除UIでのみ行う)。

clip_extractor.pyと同様、「やめ」相当(F2ストップ)の瞬間に書き込み中
だったセグメントをそのまま読むとH.264デコードエラー(映像破損)を
起こすことがあるため、書き終わるのを待ってから読みに行き、それでも
壊れていた場合は最新セグメントを除外して作り直すフォールバックを持つ
(詳細はclip_extractor.pyのモジュールdocstringを参照)。
"""

import glob
import hashlib
import itertools
import json
import os
import subprocess
import threading
import time

from config import (
    BUFFER_DIR, TAKE_DIR, TAKE_INDEX_PATH, SEGMENT_SECONDS, COURT_NAME,
    SEGMENT_CLOSE_WAIT_TIMEOUT_SEC,
)
from shared_lock import buffer_lock
import audit_log

_take_id_counter = itertools.count()
_index_lock = threading.Lock()


def _ascii_court_slug(text):
    """saved_clips.py と同じ考え方: ファイル名(=URLパスの一部)には
    日本語を含みうるCOURT_NAMEをそのまま使わず、ASCII専用のハッシュにする。
    人が読める表記はindex.jsonl側にそのまま保持する。"""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


class TakeRecorder:
    def __init__(self):
        self._start_time = None

    def start_take(self):
        """テイク開始(F2スタート、テイクモード時)に呼ぶ。開始時刻を記録するだけ。
        バッファのcleanup一時停止はmain.py側でrecorder.pause_cleanup()を呼んで行う。"""
        self._start_time = time.time()

    def is_in_progress(self):
        return self._start_time is not None

    def stop_take(self):
        """
        テイク終了(F2ストップ、または最大時間キャップ)に呼ぶ。
        start_take()からの経過時間ぶんをリングバッファから切り出し、
        data/takes/ にmp4として保存する。

        ストップの瞬間に書き込み中だったセグメントが書き終わるのを待ってから
        読みに行くことで映像破損を避け、それでも壊れていた場合は最新セグメントを
        除外して作り直す(clip_extractor.pyと同じ考え方)。

        戻り値: 保存したテイクのメタ情報(dict)
        """
        if self._start_time is None:
            raise RuntimeError("start_take()が呼ばれていません")

        elapsed = max(0.1, time.time() - self._start_time)
        self._start_time = None

        anchor_segment = self._wait_for_segment_to_close()
        final_path = self._build_take_clip(anchor_segment, elapsed, exclude_anchor=False)

        if self._has_decode_errors(final_path):
            print(f"[TakeRecorder] 警告: {os.path.basename(final_path)} に"
                  "デコードエラーを検出しました。最新セグメントを除外して作り直します。")
            os.remove(final_path)
            final_path = self._build_take_clip(anchor_segment, elapsed, exclude_anchor=True)
            still_broken = self._has_decode_errors(final_path)
            audit_log.log_event(
                "take_corruption_fallback", clip=final_path, still_broken=still_broken
            )
            if still_broken:
                print(f"[TakeRecorder] 警告: 作り直し後も"
                      f"{os.path.basename(final_path)} にデコードエラーが残っています。"
                      "原因調査が必要です。")

        entry = self._register_take(final_path, elapsed)
        return entry

    def _wait_for_segment_to_close(self):
        """clip_extractor.ClipExtractor._wait_for_segment_to_close()と同じロジック"""
        with buffer_lock:
            segments = sorted(glob.glob(os.path.join(BUFFER_DIR, "seg_*.ts")))
        if not segments:
            raise RuntimeError("バッファにセグメントがありません(録画が開始されていない可能性)")
        anchor_segment = segments[-1]

        deadline = time.monotonic() + SEGMENT_CLOSE_WAIT_TIMEOUT_SEC
        while time.monotonic() < deadline:
            with buffer_lock:
                current = sorted(glob.glob(os.path.join(BUFFER_DIR, "seg_*.ts")))
            if current and current[-1] != anchor_segment:
                break
            time.sleep(0.05)
        else:
            print(f"[TakeRecorder] 警告: {os.path.basename(anchor_segment)} の"
                  f"書き終わりを{SEGMENT_CLOSE_WAIT_TIMEOUT_SEC}秒待っても確認できません"
                  "でした。そのまま進みます。")

        return anchor_segment

    def _build_take_clip(self, anchor_segment, elapsed, exclude_anchor):
        """
        セグメント群からテイクを1本組み立てる(結合→末尾trim→MP4化)。
        anchor_segmentより新しいセグメントは使わない(clip_extractor.pyの
        _build_clip()と同じ考え方)。
        """
        take_id = f"{time.strftime('%Y%m%d_%H%M%S')}_{int(time.time() * 1000) % 1000:03d}_{next(_take_id_counter)}"
        concat_path = os.path.join(TAKE_DIR, f"_concat_{take_id}.txt")
        combined_path = os.path.join(TAKE_DIR, f"_combined_{take_id}.ts")
        court_part = _ascii_court_slug(COURT_NAME)
        final_path = os.path.join(TAKE_DIR, f"take_{court_part}_{take_id}.mp4")

        with buffer_lock:
            segments = sorted(glob.glob(os.path.join(BUFFER_DIR, "seg_*.ts")))
            if not segments:
                raise RuntimeError("バッファにセグメントがありません(録画が開始されていない可能性)")

            if anchor_segment in segments:
                idx = segments.index(anchor_segment)
                candidates = segments[:idx] if exclude_anchor else segments[:idx + 1]
            else:
                candidates = segments[:-1] if exclude_anchor else segments

            # 経過時間をカバーするのに必要な個数(安全マージンとして+2)。
            # cleanup_loopはstart_take()時点からpause_cleanup()で止まっている前提なので、
            # 区間全体のセグメントがまだ残っているはず。
            needed = int(elapsed // SEGMENT_SECONDS) + 2
            recent = candidates[-needed:]

            if not recent:
                raise RuntimeError("直近のセグメントが見つかりません")

            with open(concat_path, "w") as f:
                for seg in recent:
                    f.write(f"file '{os.path.abspath(seg)}'\n")

            subprocess.run(
                ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_path,
                 "-c", "copy", combined_path],
                check=True, stderr=subprocess.DEVNULL,
            )

        duration = self._probe_duration(combined_path)
        start = max(0.0, duration - elapsed)

        subprocess.run(
            ["ffmpeg", "-y", "-ss", f"{start:.3f}", "-i", combined_path,
             "-t", f"{elapsed:.3f}", "-c", "copy",
             "-movflags", "+faststart", final_path],
            check=True, stderr=subprocess.DEVNULL,
        )

        os.remove(concat_path)
        os.remove(combined_path)
        return final_path

    @staticmethod
    def _has_decode_errors(path):
        """clip_extractor.ClipExtractor._has_decode_errors()と同じロジック"""
        result = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", path, "-f", "null", "-"],
            capture_output=True, text=True,
        )
        return bool(result.stderr.strip())

    def _register_take(self, path, duration_sec):
        taken_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        entry = {
            "taken_at": taken_at,
            "taken_at_epoch": time.time(),
            "court": COURT_NAME,
            "filename": os.path.basename(path),
            "duration_sec": round(duration_sec, 2),
        }
        with _index_lock:
            with open(TAKE_INDEX_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    @staticmethod
    def _probe_duration(path):
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, check=True,
        )
        return float(result.stdout.strip())


def list_takes():
    """保存済みテイクの一覧を新しい順で返す"""
    if not os.path.exists(TAKE_INDEX_PATH):
        return []
    with open(TAKE_INDEX_PATH, encoding="utf-8") as f:
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
    entries = [e for e in entries if os.path.exists(os.path.join(TAKE_DIR, e["filename"]))]
    entries.reverse()
    return entries


def delete_takes(filenames):
    """
    指定したテイクをdata/takes/とindex.jsonlの両方から削除する。
    filenames: ファイル名のリスト(パスは含まない想定。basename化してから扱う)
    """
    targets = {os.path.basename(f) for f in filenames}

    with _index_lock:
        for name in targets:
            path = os.path.join(TAKE_DIR, name)
            if os.path.exists(path):
                os.remove(path)

        if not os.path.exists(TAKE_INDEX_PATH):
            return
        with open(TAKE_INDEX_PATH, encoding="utf-8") as f:
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
        with open(TAKE_INDEX_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(remaining) + ("\n" if remaining else ""))
