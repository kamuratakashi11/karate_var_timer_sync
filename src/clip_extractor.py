"""
「やめ」(記録員によるタイマー停止)操作を受けて、リングバッファ上の
セグメント群から直近 CLIP_DURATION_SECONDS 秒を切り出し、
確定クリップとして保持するモジュール。

保持ルール: 通常はCLIP_SLOTS枠のFIFO(古いものから上書き)。ただし、
作成されたクリップは、タイマーが実際に「動作中」だった累積時間が
CLIP_PROTECTION_RUNNING_SECONDS秒に達するまでは上書き削除の対象にしない
(オフィシャルミス等でストップボタンが連打されても、保護期間中の
クリップは消えず、一時的にCLIP_SLOTSを超えて保持される。タイマーが
十分な時間動いたら、保護が外れて通常のFIFOに戻る)。

タイマー(F2キー)との同期精度を上げるため、「今まさに書き込み中の
最新セグメント」も切り出し対象に含める(TS形式は書き込み中でも
安全に読めるため)。これにより、F2キー押下の瞬間と切り出される
映像の終端のズレは、最大でも1フレーム分程度(60fpsならおよそ16.7ms)
まで縮小される。

ただし、書き込みの途中で読みに行くと、書きかけの不完全なNALユニットを
読んでしまいH.264として破損した映像になることがある(実機検証で、
高確率で発生することを確認)。そのため、「やめ」の瞬間に書き込み中
だったセグメントを特定し、それが完全に書き終わる(=次のセグメントに
切り替わる)のを待ってから読みに行く。待ち時間は最大でも
SEGMENT_CLOSE_WAIT_TIMEOUT_SEC秒程度で済み、同期精度は落とさずに
読み取り競合を回避できる。待ってもなお破損していた場合の保険として、
生成直後にデコード検証も行い、それでも壊れていたら最新セグメントを
除外した安全な方式で作り直す。

この待ち時間があるため、extract_on_yame() は呼び出し元をブロックする
(最大でSEGMENT_CLOSE_WAIT_TIMEOUT_SEC秒程度)。物理ボタン
(--input-mode button)経由の場合、pynputのキーボードフックのコールバック
内でこの待ちをそのまま行うと、Windowsがフックを応答なしとみなして
無効化してしまう危険があるため、key_listener.py側で「やめ」処理を
別スレッドに逃がして呼び出している。
"""

import glob
import itertools
import os
import subprocess
import time

from config import (
    BUFFER_DIR, CLIPS_DIR, CLIP_DURATION_SECONDS, CLIP_SLOTS,
    SEGMENT_SECONDS, CLIP_PROTECTION_RUNNING_SECONDS,
    SEGMENT_CLOSE_WAIT_TIMEOUT_SEC,
)
from shared_lock import buffer_lock
import audit_log

# 短時間に連続して「やめ」が発火した場合でも、clip_idが重複してファイルが
# 上書きされることがないよう、時刻(ミリ秒まで)に加えて連番も付与する。
_clip_id_counter = itertools.count()


class ClipExtractor:
    def __init__(self, running_counter_fn=None):
        """
        running_counter_fn: タイマーの累積動作秒数を返す関数
                             (TimerSyncedKeyListener.get_running_accumulator)。
                             Noneの場合は保護機能を使わず、常に単純なFIFOで動作する
                             (--input-mode enter 使用時などタイマー追跡が無い場合)。
        """
        self.running_counter_fn = running_counter_fn
        # 古い順に並んだリスト。通常はCLIP_SLOTS件だが、保護中のクリップが
        # あると一時的にそれを超えることがある。
        # 各要素: {"path": str, "created_counter": float or None}
        self._slots = []

    def extract_on_yame(self):
        """
        「やめ」操作が呼ばれた瞬間に実行する。
        直近 CLIP_DURATION_SECONDS 秒をカバーするのに十分なセグメントを集め、
        結合してからトリミングし、クリップを作る。

        「やめ」の瞬間に書き込み中だったセグメントが書き終わるのを待ってから
        (最大SEGMENT_CLOSE_WAIT_TIMEOUT_SEC秒)読みに行くことで、同期精度を
        落とさずに読み取り競合(=映像破損)を避ける。それでも壊れていた場合の
        保険として、デコード検証→最新セグメント除外での作り直しにフォールバックする。
        """
        anchor_segment = self._wait_for_segment_to_close()
        final_path = self._build_clip(anchor_segment, exclude_anchor=False)

        if self._has_decode_errors(final_path):
            print(f"[ClipExtractor] 警告: {os.path.basename(final_path)} に"
                  "デコードエラーを検出しました(セグメント書き終わり待ちをしても"
                  "なお読み取り競合が起きた可能性)。最新セグメントを除外して"
                  "作り直します。")
            os.remove(final_path)
            final_path = self._build_clip(anchor_segment, exclude_anchor=True)
            still_broken = self._has_decode_errors(final_path)
            audit_log.log_event(
                "clip_corruption_fallback", clip=final_path, still_broken=still_broken
            )
            if still_broken:
                print(f"[ClipExtractor] 警告: 作り直し後も"
                      f"{os.path.basename(final_path)} にデコードエラーが残っています。"
                      "原因調査が必要です。")

        self._register_clip(final_path)
        return final_path

    def _wait_for_segment_to_close(self):
        """
        「やめ」の瞬間に一番新しかった(=書き込み中だった)セグメントを特定し、
        それより新しいセグメントが現れる(=書き終わって切り替わった)まで待つ。
        タイムアウトした場合は、待つのを諦めてその時点の最新セグメントを
        そのまま返す(呼び出し側のデコード検証・フォールバックに委ねる)。

        戻り値: 「やめ」の瞬間に書き込み中だったセグメントファイルのパス
        """
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
                break  # 新しいセグメントが現れた = anchor_segmentは書き終わった
            time.sleep(0.05)
        else:
            print(f"[ClipExtractor] 警告: {os.path.basename(anchor_segment)} の"
                  f"書き終わりを{SEGMENT_CLOSE_WAIT_TIMEOUT_SEC}秒待っても確認できません"
                  "でした。そのまま進みます。")

        return anchor_segment

    def _build_clip(self, anchor_segment, exclude_anchor):
        """
        セグメント群からクリップを1本組み立てる(結合→末尾トリム→MP4化)。
        anchor_segmentより新しいセグメント(「やめ」の後に始まったもの)は
        使わない(クリップの終端が「やめ」の瞬間より後にずれてしまうため)。

        anchor_segment: 「やめ」の瞬間に書き込み中だったセグメント。
        exclude_anchor: Trueの場合、anchor_segment自体も除外する
                        (書き終わり待ちがタイムアウトした等、まだ書き込み中の
                        懸念が残る場合の安全策。同期精度は最大SEGMENT_SECONDS秒
                        ほど落ちる)。
        """
        # 秒単位のタイムスタンプだけだと、短時間に連続して「やめ」が発火した
        # 場合(オフィシャルミスの連打など、まさに保護機能が必要な場面)に
        # clip_idが重複し、後のクリップが前のクリップのファイルを上書きして
        # しまう。ミリ秒+単調増加の連番を付けて確実に一意にする。
        clip_id = f"{time.strftime('%Y%m%d_%H%M%S')}_{int(time.time() * 1000) % 1000:03d}_{next(_clip_id_counter)}"
        concat_path = os.path.join(CLIPS_DIR, f"_concat_{clip_id}.txt")
        combined_path = os.path.join(CLIPS_DIR, f"_combined_{clip_id}.ts")
        final_path = os.path.join(CLIPS_DIR, f"bar_clip_{clip_id}.mp4")

        # ここから「セグメント一覧の取得→結合」までは、recorder.py側の
        # クリーンアップ処理(古いセグメント削除)と同時に走ると、
        # 使うはずだったファイルが削除された直後で読めない、という
        # 競合が起き得る。ロックで確実に排他する。
        with buffer_lock:
            segments = sorted(glob.glob(os.path.join(BUFFER_DIR, "seg_*.ts")))
            if not segments:
                raise RuntimeError("バッファにセグメントがありません(録画が開始されていない可能性)")

            if anchor_segment in segments:
                idx = segments.index(anchor_segment)
                candidates = segments[:idx] if exclude_anchor else segments[:idx + 1]
            else:
                # 待っている間にクリーンアップで消えた等の稀なケース。
                # 手に入る中で一番新しいものまでを使う。
                candidates = segments[:-1] if exclude_anchor else segments

            # 直近何秒分をカバーするのに必要な個数か(安全マージンとして+2)。
            needed = int(CLIP_DURATION_SECONDS // SEGMENT_SECONDS) + 2
            recent = candidates[-needed:]

            if not recent:
                raise RuntimeError("直近のセグメントが見つかりません")

            # concat用リストファイルを作成
            with open(concat_path, "w") as f:
                for seg in recent:
                    f.write(f"file '{os.path.abspath(seg)}'\n")

            # 再エンコードなしで結合(高速・劣化なし)。
            subprocess.run(
                ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_path,
                 "-c", "copy", combined_path],
                check=True, stderr=subprocess.DEVNULL,
            )
            # ここから先はcombined_pathという独立したファイルだけを使うので、
            # 元のセグメントファイル群には依存しない。ロックはここで解放してよい。

        # 結合後の全体長を取得し、末尾から切り出す。
        # 同時にTS→MP4へのコンテナ変換も行う(再生互換性のため、
        # 映像・音声ストリーム自体は再エンコードしない)。
        duration = self._probe_duration(combined_path)
        start = max(0.0, duration - CLIP_DURATION_SECONDS)

        subprocess.run(
            ["ffmpeg", "-y", "-ss", f"{start:.3f}", "-i", combined_path,
             "-t", f"{CLIP_DURATION_SECONDS:.3f}", "-c", "copy",
             "-movflags", "+faststart", final_path],
            check=True, stderr=subprocess.DEVNULL,
        )

        os.remove(concat_path)
        os.remove(combined_path)
        return final_path

    @staticmethod
    def _has_decode_errors(path):
        """
        生成直後のクリップを実際にデコードしてみて、壊れていないか検証する。
        書き込み中セグメントとの読み取り競合による破損(不完全なNALユニット等)
        を検出するための、クリップ生成のたびに行う軽量なチェック。
        """
        result = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", path, "-f", "null", "-"],
            capture_output=True, text=True,
        )
        return bool(result.stderr.strip())

    def _register_clip(self, path):
        """
        新しいクリップを登録し、保護期間を過ぎた古いクリップだけを
        CLIP_SLOTS件に収まるまで削除する(保護中のものは残す=一時的に
        CLIP_SLOTSを超えることを許容する)。
        """
        created_counter = self.running_counter_fn() if self.running_counter_fn else None
        self._slots.append({"path": path, "created_counter": created_counter})
        self._trim()

    def _trim(self):
        while len(self._slots) > CLIP_SLOTS:
            oldest = self._slots[0]
            if not self._is_evictable(oldest):
                # 一番古いものがまだ保護期間中 = これ以上削れない。
                # (一時的にCLIP_SLOTSを超えて保持することを許容する)
                break
            self._slots.pop(0)
            if os.path.exists(oldest["path"]):
                os.remove(oldest["path"])

    def _is_evictable(self, slot):
        """保護期間(CLIP_PROTECTION_RUNNING_SECONDS)を過ぎているかどうか"""
        if self.running_counter_fn is None or slot["created_counter"] is None:
            return True  # タイマー追跡がない場合は保護せず、常に単純なFIFOとして扱う
        elapsed_running = self.running_counter_fn() - slot["created_counter"]
        return elapsed_running >= CLIP_PROTECTION_RUNNING_SECONDS

    def list_current_clips(self):
        """監査画面に表示する現在保持中のクリップ一覧(古い順)"""
        return [s["path"] for s in self._slots]

    @staticmethod
    def _probe_duration(path):
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, check=True,
        )
        return float(result.stdout.strip())
