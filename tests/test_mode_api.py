"""
テイクモード関連のWeb APIのヘッドレステスト。

  1. /api/mode でreplay⇄takeの切り替えができるか
  2. 録画中(running)はモード切替が拒否されるか
  3. /api/takes・/takes/<filename>・/api/takes/delete の疎通確認
  4. /api/saved/delete の疎通確認(既存の保存済みクリップ機能に対する追加分)
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
import recording_mode
import saved_clips
import web_server

PORT = 5061
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

    print("--- /api/mode 初期値 ---")
    status = get_json("/api/mode")
    print(status)
    assert status["mode"] == "replay"

    print("\n--- takeへ切り替え ---")
    code, result = post_json("/api/mode", {"mode": "take"})
    print(code, result)
    assert code == 200 and result["ok"] and result["mode"] == "take"
    assert recording_mode.get_mode() == "take"

    print("\n--- 録画中はモード切替を拒否することを確認(running=Trueを模擬) ---")
    web_server.register_timer_state_source(lambda: "running")
    code, result = post_json("/api/mode", {"mode": "replay"})
    print(code, result)
    assert code == 400 and not result["ok"]
    assert recording_mode.get_mode() == "take", "録画中なのにモードが変わってしまった"
    web_server.register_timer_state_source(lambda: "stopped")

    print("\n--- replayへ戻す ---")
    code, result = post_json("/api/mode", {"mode": "replay"})
    print(code, result)
    assert code == 200 and result["mode"] == "replay"

    print("\n--- テイクを1本作ってAPI経由で確認 ---")
    take_recorder.start_take()
    recorder.pause_cleanup()
    time.sleep(4)
    entry = take_recorder.stop_take()
    recorder.resume_cleanup()
    print("作成:", entry)

    takes = get_json("/api/takes")
    print("/api/takes ->", takes)
    assert any(x["filename"] == entry["filename"] for x in takes)

    with urllib.request.urlopen(f"{BASE}/takes/{entry['filename']}") as r:
        data = r.read()
    print(f"/takes/<filename> 配信確認: {len(data)} bytes")
    assert len(data) > 0

    print("\n--- /api/takes/delete ---")
    code, result = post_json("/api/takes/delete", {"filenames": [entry["filename"]]})
    print(code, result)
    assert code == 200 and result["ok"]
    takes_after = get_json("/api/takes")
    assert not any(x["filename"] == entry["filename"] for x in takes_after)

    print("\n--- /api/saved/delete(保存済みクリップの一括削除)---")
    clip_path = extractor.extract_on_yame()
    saved_entry = saved_clips.save_clip(os.path.basename(clip_path))
    print("保存:", saved_entry)
    code, result = post_json("/api/saved/delete", {"filenames": [saved_entry["filename"]]})
    print(code, result)
    assert code == 200 and result["ok"]
    saved_after = get_json("/api/saved")
    assert not any(x["filename"] == saved_entry["filename"] for x in saved_after)

    recorder.stop()
    print("\n完了")


if __name__ == "__main__":
    main()
