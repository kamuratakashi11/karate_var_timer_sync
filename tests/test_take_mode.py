"""
テイクモード(型の演武など、開始〜終了を通しで録画するモード)の
ヘッドレステスト。MockCameraSourceで疑似映像を流しながら、

  1. pause_cleanup()中はBUFFER_SEGMENTSを超えてもセグメントが
     削除されないこと
  2. start_take()〜stop_take()の経過時間ぶんが正しく切り出されること
     (固定10秒ではなく可変長になっていること)
  3. resume_cleanup()後は通常のFIFO削除が再開すること
  4. 生成されたテイクがlist_takes()に載り、delete_takes()で
     ファイル・index.jsonlの両方から消えること

を確認する。
"""

import glob
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from camera_source import MockCameraSource
from recorder import SegmentRingBufferRecorder
from take_recorder import TakeRecorder, list_takes, delete_takes
import subprocess

from config import BUFFER_DIR, BUFFER_SEGMENTS, SEGMENT_SECONDS


def probe_duration(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def count_buffer_segments():
    return len(glob.glob(os.path.join(BUFFER_DIR, "seg_*.ts")))


def main():
    def on_warning(msg):
        print(f"[WARNING] {msg}")

    source = MockCameraSource(event_interval_sec=4.0)
    recorder = SegmentRingBufferRecorder(source, on_warning=on_warning)
    take_recorder = TakeRecorder()

    print("録画開始(ダミー映像)...")
    recorder.start()

    normal_retention_sec = BUFFER_SEGMENTS * SEGMENT_SECONDS
    print(f"通常時の保持想定: 約{normal_retention_sec}秒分のセグメント")

    print("\n--- テイク開始(pause_cleanupで削除を一時停止) ---")
    take_recorder.start_take()
    recorder.pause_cleanup()

    take_duration_sec = normal_retention_sec + 8
    print(f"{take_duration_sec}秒間録画を続ける(通常のFIFO保持秒数を超える長さ)...")
    time.sleep(take_duration_sec)

    segments_during_take = count_buffer_segments()
    print(f"pause_cleanup中のセグメント数: {segments_during_take} "
          f"(BUFFER_SEGMENTS={BUFFER_SEGMENTS}を超えているはず)")
    assert segments_during_take > BUFFER_SEGMENTS, (
        "pause_cleanup中にセグメントが削除されてしまっている(想定外)")

    print("\n--- テイク終了 ---")
    entry = take_recorder.stop_take()
    recorder.resume_cleanup()
    print(f"テイク確定: {entry}")

    take_path = os.path.join(os.path.dirname(BUFFER_DIR), "takes", entry["filename"])
    actual_duration = probe_duration(take_path)
    print(f"実測長さ: {actual_duration:.2f}秒 (要求: {take_duration_sec}秒)")
    assert abs(actual_duration - take_duration_sec) < 2.0, (
        f"テイクの長さが想定と大きくずれている: {actual_duration:.2f}秒")

    print("\n--- resume_cleanup後、通常のFIFO削除が再開することを確認 ---")
    time.sleep(normal_retention_sec + SEGMENT_SECONDS * 3)
    segments_after_resume = count_buffer_segments()
    print(f"resume_cleanup後のセグメント数: {segments_after_resume} "
          f"(BUFFER_SEGMENTS={BUFFER_SEGMENTS}前後に戻っているはず)")
    # cleanup_loopはSEGMENT_SECONDS間隔のポーリングなので、タイミング次第で
    # 定常状態(BUFFER_SEGMENTS+1程度)から数個ずれることがある。ここでは
    # 「pause中に増えた分(BUFFER_SEGMENTS超過分)がちゃんと減っていること」を
    # 確認できれば十分なので、余裕を持たせた閾値にする。
    assert segments_after_resume < segments_during_take, (
        "resume_cleanup後もセグメントが減っていない(想定外)")
    assert segments_after_resume <= BUFFER_SEGMENTS + 3, (
        "resume_cleanup後もセグメントが定常状態近くまで削除されていない(想定外)")

    print("\n--- list_takes() / delete_takes() の確認 ---")
    takes = list_takes()
    assert any(t["filename"] == entry["filename"] for t in takes), "list_takes()に反映されていない"
    print(f"list_takes() -> {takes}")

    delete_takes([entry["filename"]])
    assert not os.path.exists(take_path), "delete_takes()後もファイルが残っている"
    takes_after_delete = list_takes()
    assert not any(t["filename"] == entry["filename"] for t in takes_after_delete), (
        "delete_takes()後もindex.jsonlに残っている")
    print("削除確認OK")

    recorder.stop()
    print("\n録画停止。テスト完了。")


if __name__ == "__main__":
    main()
