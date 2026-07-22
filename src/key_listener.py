"""
業者の物理ボタン(タイマーPCに接続されているUSB早押しボタン)を監視するモジュール。
ボタンはキーボード入力として割り当てられていることが確認されているため、
OSレベルのグローバルキーボードフック(pynput)で監視する。
タイマー側のソフトウェアには一切触れず、同じキー入力を横取りして拾うだけの方式。

F2キーはスタート/ストップを交互に切り替えるトグル式であることが確認されている。
そのため、単純に「押されるたびに毎回やめ処理を発火する」わけにはいかず、
内部でスタート/ストップの状態を数えて追跡し、「ストップ」に変わった瞬間だけ
発火させる。

ただしこの追跡方式は、押下の取りこぼしや二重検知が一度でも起きると、
以降ずっとスタート/ストップの判定が反転したままになるという構造的な弱さを
持つ。そのため、緊急時に強制的に「やめ」を発火しつつ内部状態を
「今は確実にストップである」という前提に再同期するための、
専用の緊急ボタン(別キー)を用意している。

事前準備: tools/detect_keyboard_key.py で実際のキー名を確認し、
config.py の BUTTON_KEY_NAME / BUTTON_EMERGENCY_KEY_NAME に設定しておくこと。
"""

import threading
import time

from config import (
    BUTTON_KEY_NAME, BUTTON_EMERGENCY_KEY_NAME,
    BUTTON_DEBOUNCE_SEC, TIMER_INITIAL_STATE,
)


class TimerSyncedKeyListener:
    def __init__(self, on_yame, on_event=None):
        """
        on_yame: 「やめ」判定された瞬間(=ストップに変わった瞬間、または
                 緊急ボタン押下時)に呼ばれるコールバック(引数なし)
        on_event: 監査ログ用。状態遷移が起きるたびに
                  on_event(event_type: str, state: str) の形式で呼ばれる
                  (event_type: "timer_start_detected" / "timer_stop_detected" /
                   "emergency_resync")。Noneなら通知しない。
        """
        self.on_yame = on_yame
        self.on_event = on_event
        self._listener = None
        self._last_toggle_time = 0.0
        self._last_emergency_time = 0.0
        self._state = TIMER_INITIAL_STATE  # "running" または "stopped"

        # クリップ保護用: タイマーが「動作中」だった累積秒数を追跡する。
        # タイマー表示上の時刻(手動で巻き戻し・早送りされ得る)ではなく、
        # こちらが把握しているスタート/ストップの実動作時間を基準にするため、
        # 独自にカウントする。
        self._running_accumulator = 0.0
        self._running_since = time.time() if self._state == "running" else None

        if not BUTTON_KEY_NAME:
            print("[TimerSyncedKeyListener] 警告: BUTTON_KEY_NAMEが未設定です。"
                  "tools/detect_keyboard_key.py で確認して config.py に設定してください。")
        if not BUTTON_EMERGENCY_KEY_NAME:
            print("[TimerSyncedKeyListener] 警告: BUTTON_EMERGENCY_KEY_NAMEが未設定です。"
                  "同期がズレた際に復帰する手段が無い状態なので、必ず設定してください。")

    def get_state(self):
        """現在追跡しているタイマー状態("running"/"stopped")。監査画面等での表示用"""
        return self._state

    def get_running_accumulator(self):
        """
        タイマーが『動作中』だった累積秒数(単調増加)。
        クリップの保護判定(clip_extractor.py)に使う。
        現在動作中であれば、進行中の分もリアルタイムで加算して返す。
        """
        if self._running_since is not None:
            return self._running_accumulator + (time.time() - self._running_since)
        return self._running_accumulator

    def handle_key_event(self, key_name):
        """
        実際のキーイベントを受けて、F2(トグル)か緊急ボタンかを判定し処理する。
        pynputに依存しないロジック部分だけを切り出してあるので、
        テスト時はpynput無しでもこのメソッドを直接呼んで検証できる。
        戻り値: 実際に「やめ」を発火したかどうか(bool)
        """
        if BUTTON_EMERGENCY_KEY_NAME and key_name == BUTTON_EMERGENCY_KEY_NAME:
            return self._handle_emergency()

        if BUTTON_KEY_NAME and key_name == BUTTON_KEY_NAME:
            return self._handle_toggle()

        return False  # 対象外のキーは無視

    def _handle_toggle(self):
        now = time.time()
        if now - self._last_toggle_time < BUTTON_DEBOUNCE_SEC:
            return False  # チャタリング防止

        # スタート/ストップを反転
        if self._state == "stopped":
            self._state = "running"
            self._running_since = now  # 累積動作時間のカウントを開始
            print("[TimerSyncedKeyListener] F2検知: スタートと判定(やめ処理は発火しません)")
            self._last_toggle_time = now
            self._notify("timer_start_detected")
            return False
        else:
            self._state = "stopped"
            if self._running_since is not None:
                self._running_accumulator += now - self._running_since
                self._running_since = None
            self._last_toggle_time = now
            print("[TimerSyncedKeyListener] F2検知: ストップと判定 -> やめ処理を発火")
            self._notify("timer_stop_detected")
            self._fire()
            return True

    def _handle_emergency(self):
        now = time.time()
        # 緊急ボタンはデバウンスだけ短めに設けるが、基本的には常に反応させる
        # (誤操作より取りこぼしの方が有害なため)。通常のF2用タイマーとは
        # 完全に別管理にしてあるので、直後にF2を押した/押されていても影響しない。
        if now - self._last_emergency_time < 0.2:
            return False
        self._last_emergency_time = now

        print("[TimerSyncedKeyListener] 緊急ボタン検知 -> 強制的にやめ処理を発火し、"
              "内部状態を『停止中』に再同期します")
        if self._state == "running" and self._running_since is not None:
            self._running_accumulator += now - self._running_since
            self._running_since = None
        self._state = "stopped"  # 次のF2押下は必ず「スタート」として扱われるようになる
        self._notify("emergency_resync")
        self._fire()
        return True

    def _notify(self, event_type):
        if self.on_event:
            try:
                self.on_event(event_type, self._state)
            except Exception as e:
                print(f"[TimerSyncedKeyListener] on_eventコールバックでエラー: {e}")

    def _fire(self):
        # extract_on_yame()/take_recorder.stop_take()は、セグメントの書き終わりを
        # 待つため最大数秒ブロックしうる(映像破損対策)。ここ(pynputのキーボード
        # フックのコールバックスレッド)で直接ブロックすると、Windowsがフックを
        # 応答なしとみなして無効化してしまう危険があるため、必ず別スレッドで
        # 実行し、このコールバック自体は即座に返すようにする。
        def run():
            try:
                self.on_yame()
            except Exception as e:
                print(f"[TimerSyncedKeyListener] コールバック実行中にエラー: {e}")

        threading.Thread(target=run, daemon=True).start()

    def start(self):
        try:
            from pynput import keyboard
        except ImportError:
            raise RuntimeError(
                "pynputがインストールされていません。"
                "pip install pynput --break-system-packages を実行してください。"
            )

        def _key_to_name(key):
            if isinstance(key, keyboard.KeyCode):
                return key.char
            return key.name

        def _on_press(key):
            key_name = _key_to_name(key)
            if key_name is not None:
                self.handle_key_event(key_name)

        self._listener = keyboard.Listener(on_press=_on_press)
        self._listener.start()

    def stop(self):
        if self._listener:
            self._listener.stop()
