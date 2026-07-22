"""
クリップ破損検出→再作成のフォールバック機構(ラウンド5)のテスト。

実際のファイル破損(書き込み中セグメントとの読み取り競合)は狙って
再現するのが難しいため、_has_decode_errors をモック化して制御フロー
(1回目が壊れていたら安全な方式で作り直す/2回目も壊れていたら
警告だけ出して諦める)を検証する。MockCameraSourceで確認する。
"""

import os
import sys
import time
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from camera_source import MockCameraSource
from recorder import SegmentRingBufferRecorder
from clip_extractor import ClipExtractor
import audit_log


def main():
    source = MockCameraSource(event_interval_sec=4.0)
    recorder = SegmentRingBufferRecorder(source, on_warning=lambda m: print("[WARN]", m))
    extractor = ClipExtractor()

    print("録画開始... 15秒間バッファを溜めます")
    recorder.start()
    time.sleep(15)

    print("\n--- ケース1: 1回目は破損、2回目(作り直し後)は正常 ---")
    call_log = []

    def fake_has_decode_errors(path):
        call_log.append(path)
        return len(call_log) == 1  # 最初の呼び出しだけ「壊れている」ことにする

    logged_events = []
    original_log_event = audit_log.log_event

    def spy_log_event(event_type, **fields):
        logged_events.append((event_type, fields))
        return original_log_event(event_type, **fields)

    with mock.patch.object(ClipExtractor, "_has_decode_errors", staticmethod(fake_has_decode_errors)), \
         mock.patch("clip_extractor.audit_log.log_event", side_effect=spy_log_event):
        clip_path = extractor.extract_on_yame()

    print(f"最終的なクリップ: {clip_path}")
    print(f"_has_decode_errorsが呼ばれた回数: {len(call_log)} (期待値: 2=1回目と作り直し後の検証)")
    assert len(call_log) == 2, "作り直し後にもう一度検証されていない"
    assert os.path.exists(clip_path), "最終的なクリップファイルが存在しない"

    first_path = call_log[0]
    print(f"1回目に壊れていた方のファイルは削除されているべき: "
          f"{'OK' if not os.path.exists(first_path) else 'NG(まだ存在)'}")
    assert not os.path.exists(first_path), "破損した1回目のファイルが削除されずに残っている"
    assert first_path != clip_path, "1回目と最終ファイルが同じパス(作り直しになっていない)"

    print(f"監査ログに clip_corruption_fallback が記録されているか: "
          f"{[e for e in logged_events if e[0] == 'clip_corruption_fallback']}")
    fallback_events = [e for e in logged_events if e[0] == "clip_corruption_fallback"]
    assert len(fallback_events) == 1, "監査ログにフォールバック発生が記録されていない"
    assert fallback_events[0][1]["still_broken"] is False, "作り直し後は正常なはずなのにstill_broken=True"

    print("\n--- ケース2: 1回目・作り直し後の2回目とも破損(諦めて警告のみ) ---")
    time.sleep(3)

    def always_broken(path):
        return True

    logged_events.clear()
    with mock.patch.object(ClipExtractor, "_has_decode_errors", staticmethod(always_broken)), \
         mock.patch("clip_extractor.audit_log.log_event", side_effect=spy_log_event):
        clip_path2 = extractor.extract_on_yame()

    print(f"それでも最終的にクリップは返る(無限ループしない): {clip_path2}")
    assert os.path.exists(clip_path2), "2回とも壊れていてもファイル自体は残るべき"
    fallback_events2 = [e for e in logged_events if e[0] == "clip_corruption_fallback"]
    assert len(fallback_events2) == 1
    assert fallback_events2[0][1]["still_broken"] is True, "2回とも壊れているのでstill_broken=Trueのはず"

    recorder.stop()
    print("\n完了: 破損検出→作り直し→それでも壊れていた場合の3パターンとも期待通り")


if __name__ == "__main__":
    main()
