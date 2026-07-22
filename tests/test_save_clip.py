"""
永久保存機能(data/saved/)のヘッドレステスト。MockCameraSourceで確認する。

  1. 「やめ」でクリップを作り、/api/clips/<filename>/save で保存できるか
  2. data/saved/ に実体ファイルが残るか、/api/saved で一覧取得できるか
  3. /saved/<filename> で動画本体が配信されるか
  4. 元の data/clips/ 側でFIFOにより削除されても、保存済みコピーは残るか
     (CLIP_SLOTS=2を超える回数「やめ」を発火して確認)
"""

import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from camera_source import MockCameraSource
from recorder import SegmentRingBufferRecorder
from clip_extractor import ClipExtractor
import web_server

PORT = 5061


def main():
    source = MockCameraSource(event_interval_sec=4.0)
    extractor = ClipExtractor()
    web_server.register(extractor)

    recorder = SegmentRingBufferRecorder(source, on_warning=lambda m: web_server.set_warning(m))
    recorder.start()
    web_server.clear_warning()

    import threading
    t = threading.Thread(target=web_server.run_server, kwargs={"port": PORT}, daemon=True)
    t.start()
    time.sleep(1)

    print("録画開始... 15秒間バッファを溜めます")
    time.sleep(15)

    print("\n--- 1回目の『やめ』 ---")
    clip1 = extractor.extract_on_yame()
    clip1_name = os.path.basename(clip1)
    print("clip1 =", clip1_name)

    print("\n--- clip1 を保存 ---")
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/api/clips/{clip1_name}/save", method="POST")
    with urllib.request.urlopen(req) as r:
        save_result = json.load(r)
    print("保存API結果:", save_result)
    assert save_result["ok"], "保存APIがok:falseを返した"
    saved_name = save_result["filename"]

    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/saved") as r:
        saved_list = json.load(r)
    print("保存済み一覧:", saved_list)
    assert any(s["filename"] == saved_name for s in saved_list), "保存済み一覧に出てこない"

    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/saved/{saved_name}") as r:
        data = r.read()
    print(f"保存済みクリップ配信確認: {len(data)} bytes")
    assert len(data) > 0, "保存済みクリップの配信が空"

    print("\n--- FIFOでclip1が消えるまで『やめ』を追加発火(CLIP_SLOTS=2超え) ---")
    time.sleep(3)
    extractor.extract_on_yame()
    time.sleep(3)
    extractor.extract_on_yame()
    time.sleep(1)

    print("元クリップ(data/clips/)は削除されているべき:",
          "OK" if not os.path.exists(clip1) else "NG(まだ存在)")
    assert not os.path.exists(clip1), "FIFOでclip1が削除されていない(テスト前提が崩れている)"

    saved_path = os.path.join(os.path.dirname(clip1), "..", "saved", saved_name)
    saved_path = os.path.normpath(saved_path)
    print("保存済みコピーは残っているべき:",
          "OK" if os.path.exists(saved_path) else "NG(消えている)")
    assert os.path.exists(saved_path), "永久保存したはずのコピーが消えている"

    recorder.stop()
    print("\n完了: 元クリップはFIFOで消えたが、保存済みコピーは残ることを確認した")


if __name__ == "__main__":
    main()
