"""
録画モードの状態管理。

「リプレイモード」(既定): F2ストップの瞬間から遡ってCLIP_DURATION_SECONDS秒を
切り出す、従来通りのトリガー直前保存方式(clip_extractor.py)。
「テイクモード」: F2スタート〜ストップの区間をまるごと1本のクリップとして
保存する方式(take_recorder.py)。

web_server.py(iPad画面のトグルボタンからのAPI経由の読み書き)と
main.py(キーイベント処理での分岐)の両方から参照するため、
どちらにも依存しない独立した小さいモジュールとして切り出してある。
"""

import threading

REPLAY = "replay"
TAKE = "take"
_VALID_MODES = (REPLAY, TAKE)

_lock = threading.Lock()
_mode = REPLAY


def get_mode():
    with _lock:
        return _mode


def set_mode(mode, running=False):
    """
    モードを切り替える。

    running: 現在タイマーが「動作中」かどうか。Trueの間はモード切替を許可しない
             (テイクの録画中にモードが変わるとブックキーピングが壊れるため)。
             呼び出し側(main.py経由でTimerSyncedKeyListener.get_stateを参照)から
             渡してもらう想定。

    戻り値: 実際に切り替わったかどうか(bool)
    例外: ValueError (不正なmode名を渡した場合)
    """
    if mode not in _VALID_MODES:
        raise ValueError(f"不正なモードです: {mode}")
    global _mode
    with _lock:
        if running:
            return False
        _mode = mode
        return True
