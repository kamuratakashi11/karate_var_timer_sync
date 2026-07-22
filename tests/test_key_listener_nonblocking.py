"""
ラウンド6で追加した「_fireは別スレッドで実行し、呼び出し元(pynputの
コールバックスレッド)をブロックしない」ことのテスト。
pynput自体は使わず、handle_key_eventを直接呼んでロジックだけ確認する。
"""

import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from key_listener import TimerSyncedKeyListener


def main():
    on_yame_started = threading.Event()
    on_yame_may_finish = threading.Event()
    call_count = []

    def slow_on_yame():
        call_count.append(1)
        on_yame_started.set()
        on_yame_may_finish.wait(timeout=5)  # 実際のextract_on_yame()の「待ち」を模擬

    listener = TimerSyncedKeyListener(on_yame=slow_on_yame)

    print("--- F2相当のキーでトグル(スタート→ストップ)し、ストップでon_yameが発火 ---")
    listener.handle_key_event("f2")  # スタート
    time.sleep(0.6)  # BUTTON_DEBOUNCE_SEC(既定0.5秒)を超えて待ってから次を押す
    t0 = time.monotonic()
    fired = listener.handle_key_event("f2")  # ストップ -> on_yame発火
    elapsed_to_return = time.monotonic() - t0

    print(f"handle_key_event の戻り時間: {elapsed_to_return:.3f}秒 "
          f"(on_yame内で5秒待つ設定だが、即座に返るべき)")
    assert fired is True
    assert elapsed_to_return < 1.0, "handle_key_eventがon_yameの完了を待ってブロックしてしまっている"

    print("on_yameが(別スレッドで)実際に呼ばれるまで待つ...")
    started = on_yame_started.wait(timeout=2)
    assert started, "on_yameが別スレッドで呼ばれていない"
    print(f"on_yame呼び出し回数: {len(call_count)} (期待値: 1)")
    assert len(call_count) == 1

    on_yame_may_finish.set()  # on_yame側の待ちを解除してスレッドを終わらせる
    time.sleep(0.2)

    print("\n完了: pynputコールバックはon_yameの完了を待たずに即座に返ることを確認した")


if __name__ == "__main__":
    main()
