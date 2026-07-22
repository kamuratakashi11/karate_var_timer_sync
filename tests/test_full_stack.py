import sys, os, time, threading
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from camera_source import MockCameraSource
from recorder import SegmentRingBufferRecorder
from clip_extractor import ClipExtractor
import web_server

source = MockCameraSource()
extractor = ClipExtractor()
web_server.register(extractor)

recorder = SegmentRingBufferRecorder(source, on_warning=lambda m: web_server.set_warning(m))
recorder.start()
web_server.clear_warning()

t = threading.Thread(target=web_server.run_server, kwargs={"port": 5060}, daemon=True)
t.start()

time.sleep(9)
print("1回目のやめ ->", extractor.extract_on_yame())
time.sleep(3)
print("2回目のやめ ->", extractor.extract_on_yame())
time.sleep(2)

import urllib.request, json
with urllib.request.urlopen("http://127.0.0.1:5060/api/clips") as r:
    clips = json.load(r)
    print("API /api/clips ->", clips)

with urllib.request.urlopen("http://127.0.0.1:5060/api/status") as r:
    print("API /api/status ->", json.load(r))

# 実際に動画ファイル本体が正しく配信されるか
first_file = clips[0]["filename"]
with urllib.request.urlopen(f"http://127.0.0.1:5060/clips/{first_file}") as r:
    data = r.read()
    print(f"動画配信確認: {first_file} -> {len(data)} bytes, content-type={r.headers.get('Content-Type')}")

with urllib.request.urlopen("http://127.0.0.1:5060/") as r:
    html = r.read().decode()
    print("index.html配信確認:", "スワイプ" in html, len(html), "bytes")

recorder.stop()
print("完了")
