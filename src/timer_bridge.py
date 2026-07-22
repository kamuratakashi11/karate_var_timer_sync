"""
自作の空手タイマーソフト(karate-timer-system)からのHTTP通知を受けて、
録画トリガーを呼び出すモジュール。

key_listener.TimerSyncedKeyListener はキーボード入力(トグル式ボタン)しか
観測できないため、内部でスタート/ストップの状態を推測し、取りこぼし対策の
緊急再同期ボタンまで用意する必要があった。こちらはタイマー側のソースを
直接編集できる前提で、「今スタートした」「今ストップした」という事実を
明示的に区別して通知してもらう方式にしたため、その種の推測ロジックは
一切不要になっている。

呼び出され方: web_server.py の POST /api/timer/event が、この
TimerHttpBridge の on_start()/on_stop() を直接呼ぶ。
"""

import threading
import time


class TimerHttpBridge:
    def __init__(self, on_yame, on_event=None):
        """
        on_yame: 「やめ」判定された瞬間(=stopイベント受信時)に呼ばれるコールバック(引数なし)
        on_event: 監査ログ用。状態遷移が起きるたびに
                  on_event(event_type: str, state: str) の形式で呼ばれる
                  (event_type: "timer_start_detected" / "timer_stop_detected")。
                  Noneなら通知しない。
        """
        self.on_yame = on_yame
        self.on_event = on_event
        self._state = "stopped"
        self._running_accumulator = 0.0
        self._running_since = None
        self._lock = threading.Lock()

    def get_state(self):
        """現在の状態("running"/"stopped")。監査画面等での表示用"""
        return self._state

    def get_running_accumulator(self):
        """
        タイマーが『動作中』だった累積秒数(単調増加)。
        クリップの保護判定(clip_extractor.py)に使う。
        """
        with self._lock:
            if self._running_since is not None:
                return self._running_accumulator + (time.time() - self._running_since)
            return self._running_accumulator

    def on_start(self):
        """
        タイマー側から"start"イベントを受けた時に呼ぶ。
        HTTP経由のため通知の重複がありうる(再送等)ので、既にrunning中の
        場合は無視して冪等にする。戻り値: 実際に状態が変化したか
        """
        with self._lock:
            if self._state == "running":
                return False
            self._state = "running"
            self._running_since = time.time()
        print("[TimerHttpBridge] タイマー開始を検知")
        self._notify("timer_start_detected")
        return True

    def on_stop(self):
        """
        タイマー側から"stop"イベントを受けた時に呼ぶ。
        既にstopped中の重複通知は無視して冪等にする(二重にクリップが
        作られてしまうのを防ぐ)。戻り値: 実際に「やめ」を発火したか
        """
        with self._lock:
            if self._state == "stopped":
                return False
            self._state = "stopped"
            now = time.time()
            if self._running_since is not None:
                self._running_accumulator += now - self._running_since
                self._running_since = None
        print("[TimerHttpBridge] タイマー停止を検知 -> やめ処理を発火")
        self._notify("timer_stop_detected")
        self._fire()
        return True

    def _notify(self, event_type):
        if self.on_event:
            try:
                self.on_event(event_type, self._state)
            except Exception as e:
                print(f"[TimerHttpBridge] on_eventコールバックでエラー: {e}")

    def _fire(self):
        # extract_on_yame()/take_recorder.stop_take()は、セグメントの書き終わりを
        # 待つため最大数秒ブロックしうる(映像破損対策)。Flaskのリクエストハンドラ
        # スレッドでここを直接ブロックすると、タイマー側からのHTTPリクエストが
        # 数秒待たされてしまう(key_listener.pyの_fire()と同じ理由で別スレッドに逃がす)。
        def run():
            try:
                self.on_yame()
            except Exception as e:
                print(f"[TimerHttpBridge] コールバック実行中にエラー: {e}")

        threading.Thread(target=run, daemon=True).start()
