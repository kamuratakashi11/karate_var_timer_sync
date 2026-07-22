"""
監査ログモジュール。

「いつF2/緊急ボタンが押されたか」「その結果クリップは生成できたか」
「タイマーの追跡状態はどう遷移したか」といった、抗議の正当性を巡って
後日確認が必要になった際の証跡を、1行1イベントのJSON Lines形式で
data/audit_log.jsonl に追記していく。

映像データそのものではなく、あくまで「何が起きたかの記録」のみを扱う。
複数スレッド(キー入力監視スレッド・メインスレッド等)から同時に
書き込まれる可能性があるため、ロックで保護する。
"""

import json
import os
import threading
import time

from config import AUDIT_LOG_PATH, COURT_NAME

_log_lock = threading.Lock()


def log_event(event_type, **fields):
    """
    監査ログに1件記録する。

    event_type: "system_start", "timer_start_detected", "timer_stop_detected",
                "emergency_resync", "clip_created", "clip_error",
                "recording_warning", "system_stop" など
    fields: イベントごとの追加情報(dict形式でそのまま記録される)
    """
    entry = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "timestamp_epoch": time.time(),
        "court": COURT_NAME,
        "event": event_type,
        **fields,
    }
    line = json.dumps(entry, ensure_ascii=False)

    with _log_lock:
        os.makedirs(os.path.dirname(AUDIT_LOG_PATH), exist_ok=True)
        with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    print(f"[監査ログ] {entry['timestamp']}  {event_type}  {fields}")


def read_recent_events(limit=50):
    """直近のログをlimit件だけ読み込む(監査画面等での表示用)"""
    if not os.path.exists(AUDIT_LOG_PATH):
        return []
    with open(AUDIT_LOG_PATH, encoding="utf-8") as f:
        lines = f.readlines()
    events = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events
