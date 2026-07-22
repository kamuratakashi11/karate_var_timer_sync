"""
カメラが無い状態でパイプライン全体を検証するためのヘッドレステスト。
MockCameraSourceで疑似映像を流しながら、
  1. リングバッファ録画が正常に走るか
  2. 「やめ」操作(extract_on_yame)で本当に約6秒のクリップが切り出されるか
  3. 2枠FIFOで正しく上書きされるか
を確認する。
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from camera_source import MockCameraSource
from recorder import SegmentRingBufferRecorder
from clip_extractor import ClipExtractor
import subprocess


def probe_duration(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def main():
    def on_warning(msg):
        print(f"[WARNING] {msg}")

    source = MockCameraSource(event_interval_sec=4.0)
    recorder = SegmentRingBufferRecorder(source, on_warning=on_warning)
    extractor = ClipExtractor()

    print("録画開始(ダミー映像)... 15秒間バッファを溜めます")
    recorder.start()
    time.sleep(15)

    print("\n--- 1回目の『やめ』操作 ---")
    clip1 = extractor.extract_on_yame()
    dur1 = probe_duration(clip1)
    print(f"クリップ1: {clip1}  長さ={dur1:.2f}秒")

    time.sleep(3)

    print("\n--- 2回目の『やめ』操作(オフィシャルミスを模擬: 直後にもう一度) ---")
    clip2 = extractor.extract_on_yame()
    dur2 = probe_duration(clip2)
    print(f"クリップ2: {clip2}  長さ={dur2:.2f}秒")

    time.sleep(3)

    print("\n--- 3回目の『やめ』操作(3回目で1回目のクリップは上書き消去されるはず) ---")
    clip3 = extractor.extract_on_yame()
    dur3 = probe_duration(clip3)
    print(f"クリップ3: {clip3}  長さ={dur3:.2f}秒")

    print("\n--- 現在保持中のクリップ一覧(2枠のはず) ---")
    current = extractor.list_current_clips()
    for c in current:
        exists = os.path.exists(c)
        print(f"  {c}  (存在: {exists})")

    print(f"\n保持枠数: {len(current)} (期待値: 2)")
    print(f"クリップ1は削除されているべき: {'OK' if not os.path.exists(clip1) else 'NG(まだ存在)'}")

    recorder.stop()
    print("\n録画停止。テスト完了。")


if __name__ == "__main__":
    main()
