"""
自作タイマーソフト連携用の POST /api/timer/event のヘッドレステスト。

  1. --input-mode timer で登録していない状態では400を返すこと
  2. start→stopのPOSTで、リプレイモード時はクリップが1本生成されること
  3. 重複したstart/stop通知(HTTP再送等を想定)は無視され、二重にクリップが
     作られないこと(冪等性)
  4. テイクモード中はstart→stopでテイクが1本生成されること
"""

import json
import os
import sys
import time
import threading
import urllib.request
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from camera_source import MockCameraSource
from recorder import SegmentRingBufferRecorder
from clip_extractor import ClipExtractor
from take_recorder import TakeRecorder
from timer_bridge import TimerHttpBridge
import recording_mode
import web_server

PORT = 5062
BASE = f"http://127.0.0.1:{PORT}"


def get_json(path):
    with urllib.request.urlopen(f"{BASE}{path}") as r:
        return json.load(r)


def post_json(path, body):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}{path}", data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def main():
    source = MockCameraSource()
    extractor = ClipExtractor()
    take_recorder = TakeRecorder()
    web_server.register(extractor)

    recorder = SegmentRingBufferRecorder(source, on_warning=lambda m: web_server.set_warning(m))
    recorder.start()
    web_server.clear_warning()

    t = threading.Thread(target=web_server.run_server, kwargs={"port": PORT}, daemon=True)
    t.start()
    time.sleep(2)

    print("--- timer_bridge未登録時は400を返すこと ---")
    code, result = post_json("/api/timer/event", {"event": "start"})
    print(code, result)
    assert code == 400 and not result["ok"]

    take_cap = {}

    def on_yame():
        if recording_mode.get_mode() == recording_mode.TAKE:
            if take_recorder.is_in_progress():
                entry = take_recorder.stop_take()
                recorder.resume_cleanup()
                take_cap["entry"] = entry
            return
        take_cap["clip"] = extractor.extract_on_yame()

    def on_start_take_if_needed():
        if recording_mode.get_mode() == recording_mode.TAKE:
            take_recorder.start_take()
            recorder.pause_cleanup()

    def on_event(event_type, state):
        if event_type == "timer_start_detected":
            on_start_take_if_needed()

    bridge = TimerHttpBridge(on_yame=on_yame, on_event=on_event)
    web_server.register_timer_bridge(bridge)
    web_server.register_timer_state_source(bridge.get_state)

    print("\n--- 15秒バッファを溜める ---")
    time.sleep(15)

    print("\n--- リプレイモード: start -> stop でクリップが1本生成されること ---")
    code, result = post_json("/api/timer/event", {"event": "start"})
    print(code, result)
    assert code == 200 and result["ok"] and result["changed"] is True
    assert bridge.get_state() == "running"

    time.sleep(2)

    code, result = post_json("/api/timer/event", {"event": "stop"})
    print(code, result)
    assert code == 200 and result["ok"] and result["changed"] is True
    # _fire()は別スレッドで実行されるため、クリップ生成の完了を少し待つ
    deadline = time.time() + 5
    while "clip" not in take_cap and time.time() < deadline:
        time.sleep(0.1)
    assert "clip" in take_cap, "on_yame(クリップ抽出)が呼ばれなかった"
    assert os.path.exists(take_cap["clip"])
    print("生成されたクリップ:", take_cap["clip"])

    print("\n--- 重複stop通知は無視され、二重にクリップが作られないこと(冪等性) ---")
    take_cap.pop("clip")
    code, result = post_json("/api/timer/event", {"event": "stop"})
    print(code, result)
    assert code == 200 and result["ok"] and result["changed"] is False
    time.sleep(1)
    assert "clip" not in take_cap, "既にstopped中なのに再度on_yameが発火してしまった"

    print("\n--- 不正なevent値は400を返すこと ---")
    code, result = post_json("/api/timer/event", {"event": "pause"})
    print(code, result)
    assert code == 400 and not result["ok"]

    print("\n--- テイクモード: start -> stop でテイクが1本生成されること ---")
    recording_mode.set_mode(recording_mode.TAKE, running=False)
    code, result = post_json("/api/timer/event", {"event": "start"})
    print(code, result)
    assert code == 200 and result["ok"]
    time.sleep(3)
    code, result = post_json("/api/timer/event", {"event": "stop"})
    print(code, result)
    assert code == 200 and result["ok"]

    deadline = time.time() + 5
    while "entry" not in take_cap and time.time() < deadline:
        time.sleep(0.1)
    assert "entry" in take_cap, "テイクモードでon_yameが発火しなかった"
    print("生成されたテイク:", take_cap["entry"])
    assert os.path.exists(os.path.join(
        os.path.dirname(__file__), "..", "data", "takes", take_cap["entry"]["filename"]
    ))

    recorder.stop()
    print("\n完了")


if __name__ == "__main__":
    main()
