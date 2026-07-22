"""
リングバッファ録画モジュール。

VideoSource(実カメラ or Mock)からフレームを取得し、FFmpegの
segmentマルチプレクサに生フレームをパイプで流し込むことで、
SEGMENT_SECONDS 秒ごとの小さい映像断片を連続生成する。
BUFFER_SEGMENTS 個を超えた古いセグメントは自動的に削除し、
常に直近 SEGMENT_SECONDS * BUFFER_SEGMENTS 秒分だけを
ディスク上に保持する(=容量が増え続けない設計)。

断片形式はMPEG-TS(.ts)を採用している(mp4ではない)。
理由: mp4は「書き込みが完了してファイルの末尾にインデックス情報(moov atom)
が書かれるまで正しく読めない」形式のため、業者のタイマーとの同期精度を
上げるには「今まさに書き込み中の断片」を除外せざるを得ず、
最大でSEGMENT_SECONDS秒分のズレが発生してしまう(F2キー押下時のタイマー
停止と、映像の切り出し内容がズレる)。TS形式は書き込み中でも安全に
読めるため、この断片も含めて切り出せる。=タイマーとの同期精度が
大幅に向上する(ズレはほぼ1フレーム=約16.7ms程度まで縮小する)。

「やめ」操作時は clip_extractor.py がこのセグメント群から
直近6秒を切り出し、最終的にmp4へ変換して保存する。

config.pyのAUDIO_DEVICE_NAMEが設定されている場合、dshow(Windows専用)
経由でマイク音声も同じセグメントに含める。clip_extractor.py・
take_recorder.pyはどちらも-c copyで無劣化concat/trimしているだけなので、
音声ストリームが増えてもそちら側の変更は不要。
"""

import glob
import os
import platform
import subprocess
import threading
import time

from config import (
    FRAME_WIDTH, FRAME_HEIGHT, FPS,
    SEGMENT_SECONDS, BUFFER_SEGMENTS, BUFFER_DIR,
    FFMPEG_PRESET, FFMPEG_CRF, HEALTH_STALE_THRESHOLD_SEC,
    AUDIO_DEVICE_NAME, AUDIO_BITRATE,
)
from shared_lock import buffer_lock


def _build_ffmpeg_cmd(segment_pattern, audio_device_name=None):
    """
    録画用ffmpegコマンドを組み立てる。

    audio_device_name が None(既定)の場合は、映像のみを扱う従来通りの
    コマンドを返す(音声設定を追加する前と1バイトも変わらない)。
    設定されている場合のみ、dshow(Windows)経由のマイク入力を2つ目の
    入力として追加し、映像と一緒にMPEG-TSセグメントへ書き出す。

    rawvideo(bgr24)を標準入力から受け取り、H.264 + MPEG-TSでセグメント
    分割保存する。TS形式は書き込み中のファイルでも安全に読めるため、
    「今書いている最中の断片」も切り出し対象に含められる(同期精度向上のため)。
    """
    video_input = [
        "-f", "rawvideo",
        "-pix_fmt", "bgr24",
        "-s", f"{FRAME_WIDTH}x{FRAME_HEIGHT}",
        "-r", str(FPS),
    ]
    if audio_device_name:
        # 音声(dshowの実時間タイムスタンプ)と時間軸を揃えるため、映像側も
        # 「-r FPSで仮定した一定間隔」ではなく実際の到着時刻ベースにする。
        # 音声を使わない場合はこれまで通りの挙動(何も追加しない)。
        video_input += ["-use_wallclock_as_timestamps", "1"]
    video_input += ["-i", "-"]

    cmd = ["ffmpeg", "-y"] + video_input

    if audio_device_name:
        cmd += [
            "-f", "dshow",
            "-use_wallclock_as_timestamps", "1",
            "-i", f"audio={audio_device_name}",
            "-map", "0:v", "-map", "1:a",
        ]

    cmd += [
        "-c:v", "libx264",
        "-preset", FFMPEG_PRESET,
        "-crf", FFMPEG_CRF,
        "-pix_fmt", "yuv420p",
        "-g", str(FPS),  # 1秒に1回キーフレーム→セグメント境界を綺麗にする
    ]

    if audio_device_name:
        cmd += ["-c:a", "aac", "-b:a", AUDIO_BITRATE]

    cmd += [
        "-f", "segment",
        "-segment_time", str(SEGMENT_SECONDS),
        "-segment_format", "mpegts",
        "-reset_timestamps", "1",
        "-strftime", "1",
        segment_pattern,
    ]
    return cmd


class SegmentRingBufferRecorder:
    def __init__(self, source, on_warning=None):
        """
        source: VideoSource (RealCameraSource または MockCameraSource)
        on_warning: 映像取得に失敗した際に呼ばれるコールバック(GUI警告表示用)
        """
        self.source = source
        self.on_warning = on_warning
        self._ffmpeg_proc = None
        self._capture_thread = None
        self._cleanup_thread = None
        self._running = False
        self._start_time = None
        self._last_frame_time = None
        self._frame_count = 0
        # テイクモード録画中は、開始〜終了の区間を丸ごと切り出す必要があるため、
        # 通常のBUFFER_SEGMENTS超過削除を一時的に止めておく必要がある。
        self._cleanup_paused = False

    def get_health(self):
        """中央監視ダッシュボード向けの死活情報を返す"""
        now = time.time()
        last_frame_age = (now - self._last_frame_time) if self._last_frame_time else None
        uptime = (now - self._start_time) if self._start_time else 0
        recording_ok = last_frame_age is not None and last_frame_age < HEALTH_STALE_THRESHOLD_SEC
        return {
            "recording_ok": recording_ok,
            "last_frame_age_sec": round(last_frame_age, 2) if last_frame_age is not None else None,
            "uptime_sec": round(uptime, 1),
            "frame_count": self._frame_count,
        }

    def start(self):
        self.source.start()

        segment_pattern = os.path.join(BUFFER_DIR, "seg_%Y%m%d_%H%M%S.ts")

        # dshow(マイク入力)はWindows専用のため、Windows以外では
        # AUDIO_DEVICE_NAMEが設定されていても無視し、映像のみで動かす
        # (Mock開発環境等での動作を妨げないため)。
        audio_device_name = AUDIO_DEVICE_NAME if platform.system() == "Windows" else None
        cmd = _build_ffmpeg_cmd(segment_pattern, audio_device_name)

        self._ffmpeg_proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL
        )

        # 起動直後にffmpegがすぐ終了していないか確認する(音声デバイス名の
        # 指定ミスや、他アプリにデバイスを掴まれている場合に起動自体が
        # 失敗することがあるが、stderrを読んでいないため原因が分かりにくい。
        # _wait_for_web_server (main.py) と同じ考え方で、早期の失敗だけでも
        # 検知して警告を出す)。
        time.sleep(1.0)
        if self._ffmpeg_proc.poll() is not None:
            if self.on_warning:
                hint = f"(音声デバイス '{audio_device_name}' の可能性があります)" if audio_device_name else ""
                self.on_warning(f"録画プロセス(ffmpeg)の起動に失敗しました。{hint}")

        self._running = True
        self._start_time = time.time()
        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._capture_thread.start()

        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self._cleanup_thread.start()

    def _capture_loop(self):
        fail_count = 0
        while self._running:
            ok, frame = self.source.read()
            if not ok or frame is None:
                fail_count += 1
                if fail_count >= 5 and self.on_warning:
                    self.on_warning("カメラ映像を取得できません。接続を確認してください。")
                time.sleep(0.1)
                continue
            fail_count = 0
            self._last_frame_time = time.time()
            self._frame_count += 1
            try:
                self._ffmpeg_proc.stdin.write(frame.tobytes())
            except (BrokenPipeError, ValueError):
                if self.on_warning:
                    self.on_warning("録画プロセスが停止しました。再起動してください。")
                self._running = False
                break

    def _cleanup_loop(self):
        """BUFFER_SEGMENTS個を超えた古いセグメントファイルを削除し続ける"""
        while self._running:
            if self._cleanup_paused:
                time.sleep(SEGMENT_SECONDS)
                continue
            # 切り出し処理(clip_extractor.py)が同時に読み込み中でないことを保証してから削除する
            with buffer_lock:
                segments = sorted(glob.glob(os.path.join(BUFFER_DIR, "seg_*.ts")))
                # 末尾(最新)は書き込み中の可能性があるので削除対象から除外する
                deletable = segments[:-1]
                excess = len(deletable) - BUFFER_SEGMENTS
                if excess > 0:
                    for path in deletable[:excess]:
                        try:
                            os.remove(path)
                        except OSError:
                            pass
            time.sleep(SEGMENT_SECONDS)

    def pause_cleanup(self):
        """
        テイクモードの録画開始時に呼ぶ。開始〜終了の区間全体を後で切り出せるよう、
        通常のBUFFER_SEGMENTS超過削除を一時停止する(TAKE_MAX_DURATION_SECONDSの
        安全キャップにより、際限なく溜まり続けることはない)。
        """
        self._cleanup_paused = True

    def resume_cleanup(self):
        """テイクの切り出し完了後に呼ぶ。通常のFIFO削除を再開する"""
        self._cleanup_paused = False

    def stop(self):
        # 先にcapture_loopを止めてから(=書き込みをやめてから)stdinを閉じる。
        # 順序を逆にすると、閉じた直後にcapture_loopが書き込みを試みて
        # 誤ってon_warningが発火することがあるため。
        self._running = False
        if self._capture_thread:
            self._capture_thread.join(timeout=2)
        if self._ffmpeg_proc:
            try:
                self._ffmpeg_proc.stdin.close()
            except Exception:
                pass
            self._ffmpeg_proc.wait(timeout=5)
        self.source.release()
