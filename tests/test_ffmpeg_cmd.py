"""
録画用ffmpegコマンド組み立て(recorder.py::_build_ffmpeg_cmd)のユニットテスト。

実機のffmpeg実行やカメラ・マイクは一切不要な純粋なテストなので、
このコンテナ環境でも(実機の音声デバイスが無くても)実行できる。

  1. audio_device_name=None のとき、音声を追加する前と1バイトも
     違わないコマンド列になっているか(回帰防止)
  2. audio_device_name指定時、dshow入力・マッピング・音声コーデックが
     正しく追加されているか
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from recorder import _build_ffmpeg_cmd
from config import (
    FRAME_WIDTH, FRAME_HEIGHT, FPS, SEGMENT_SECONDS,
    FFMPEG_PRESET, FFMPEG_CRF, AUDIO_BITRATE,
)

SEGMENT_PATTERN = "/tmp/dummy/seg_%Y%m%d_%H%M%S.ts"


def main():
    print("--- audio_device_name=None: 音声追加前と完全一致するコマンドになっているか ---")
    cmd_no_audio = _build_ffmpeg_cmd(SEGMENT_PATTERN, audio_device_name=None)
    expected_no_audio = [
        "ffmpeg", "-y",
        "-f", "rawvideo",
        "-pix_fmt", "bgr24",
        "-s", f"{FRAME_WIDTH}x{FRAME_HEIGHT}",
        "-r", str(FPS),
        "-i", "-",
        "-c:v", "libx264",
        "-preset", FFMPEG_PRESET,
        "-crf", FFMPEG_CRF,
        "-pix_fmt", "yuv420p",
        "-g", str(FPS),
        "-f", "segment",
        "-segment_time", str(SEGMENT_SECONDS),
        "-segment_format", "mpegts",
        "-reset_timestamps", "1",
        "-strftime", "1",
        SEGMENT_PATTERN,
    ]
    print(cmd_no_audio)
    assert cmd_no_audio == expected_no_audio, "音声無効時のコマンドが従来と異なっている(回帰の可能性)"
    print("OK: 完全一致")

    print("\n--- audio_device_name指定時: dshow入力・マッピング・音声コーデックが含まれるか ---")
    cmd_with_audio = _build_ffmpeg_cmd(SEGMENT_PATTERN, audio_device_name="テストマイク")
    print(cmd_with_audio)

    assert "-f" in cmd_with_audio and "dshow" in cmd_with_audio, "dshow入力が無い"
    assert "audio=テストマイク" in cmd_with_audio, "マイクデバイス名の指定が無い"
    assert cmd_with_audio.count("-use_wallclock_as_timestamps") == 2, (
        "映像・音声の両方にuse_wallclock_as_timestampsが付いているべき")
    assert "-map" in cmd_with_audio, "-mapによる明示的なストリーム指定が無い"
    idx_map = cmd_with_audio.index("-map")
    assert cmd_with_audio[idx_map:idx_map + 4] == ["-map", "0:v", "-map", "1:a"], (
        "映像=入力0、音声=入力1のマッピングになっていない")
    assert "-c:a" in cmd_with_audio and "aac" in cmd_with_audio, "音声コーデック(aac)の指定が無い"
    assert AUDIO_BITRATE in cmd_with_audio, "音声ビットレートの指定が無い"
    # 映像側の設定(解像度・fps・プリセット等)は音声追加時も変わらないはず
    assert f"{FRAME_WIDTH}x{FRAME_HEIGHT}" in cmd_with_audio
    assert FFMPEG_PRESET in cmd_with_audio
    print("OK: 音声関連の引数が正しく含まれている")

    print("\n完了")


if __name__ == "__main__":
    main()
