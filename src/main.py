"""
起動エントリーポイント(PC側で実行)。

- 録画エンジン(リングバッファ)を起動
- Webサーバーを起動(iPad/PCブラウザから監査画面にアクセス可能にする)
- 記録員の「やめ」操作を受け付ける
  --input-mode enter  : キーボードのEnterキーで代用(動作確認・単体テスト用)
  --input-mode button : 業者のUSB早押しボタン(キーボード入力として認識)を監視
  --input-mode timer  : 自作の空手タイマーソフト(karate-timer-system)からの
                        HTTP通知(POST /api/timer/event)でスタート/ストップを検知

使い方:
  カメラ未着手時: python3 main.py --mock
  カメラ到着後  : python3 main.py --camera 0
  ボタン連動時  : python3 main.py --camera 0 --input-mode button
                 (事前に tools/detect_keyboard_key.py で認識確認・config.py設定が必要)
  タイマー連動時: python3 main.py --camera 0 --input-mode timer
                 (同一PC上でkarate-timer-system側にも通知コードの組み込みが必要)
"""

import argparse
import threading
import time
import urllib.request

from camera_source import MockCameraSource, RealCameraSource
from recorder import SegmentRingBufferRecorder
from clip_extractor import ClipExtractor
from take_recorder import TakeRecorder
import recording_mode
import web_server
import audit_log
from config import TAKE_MAX_DURATION_SECONDS


def _wait_for_web_server(port, timeout_sec=15):
    """監査Webサーバーが実際にHTTP応答を返すようになるまで待つ(起動失敗の検知用)"""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=1):
                return True
        except Exception:
            time.sleep(0.5)
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mock", action="store_true", help="カメラなしでダミー映像を使う")
    parser.add_argument("--camera", type=int, default=0, help="実カメラのデバイス番号")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--input-mode", choices=["enter", "button", "timer"], default="enter",
                         help="『やめ』操作の受付方法。enter=Enterキー(動作確認用)、"
                              "button=業者のUSBボタン連動、"
                              "timer=自作タイマーソフトからのHTTP通知連動")
    args = parser.parse_args()

    audit_log.log_event("system_start", input_mode=args.input_mode,
                         mock=args.mock, camera=args.camera)

    if args.mock:
        print("[main] ダミー映像モードで起動します")
        source = MockCameraSource()
    else:
        print(f"[main] 実カメラ(device={args.camera})で起動します")
        source = RealCameraSource(args.camera)

    extractor = ClipExtractor()
    take_recorder = TakeRecorder()
    take_cap_timer = None  # テイクの最大録画時間キャップ用(threading.Timer)

    def on_warning(msg):
        print(f"[警告] {msg}")
        web_server.set_warning(msg)
        audit_log.log_event("recording_warning", message=msg)

    recorder = SegmentRingBufferRecorder(source, on_warning=on_warning)
    web_server.register(extractor, recorder)

    recorder.start()
    web_server.clear_warning()

    # Webサーバーは別スレッドで起動(iPad/PCブラウザからアクセス可能にする)
    server_thread = threading.Thread(
        target=web_server.run_server, kwargs={"port": args.port}, daemon=True
    )
    server_thread.start()

    # daemon threadの中でapp.run()がポート競合・権限エラー等で起動失敗しても、
    # 例外は当該スレッド内で握りつぶされ、メインスレッド(録画処理)はそれと
    # 気づかず動き続けてしまう(実機検証で実際にこの現象を確認した:
    # WinError 10013でbindに失敗したが、録画自体は正常に継続していたため、
    # 誰も気づけないまま監査画面にアクセスできない状態が続いた)。
    # そのため実際にHTTP応答があるかを起動直後に確認し、確認できなければ
    # 大きな警告として出す。
    if _wait_for_web_server(args.port):
        print(f"[main] 監査画面: http://<このPCのIPアドレス>:{args.port}/ にiPadからアクセスしてください")
    else:
        message = (f"監査Webサーバー(ポート{args.port})の起動を確認できませんでした。"
                    "ポートの競合・ファイアウォール等が原因の可能性があります。"
                    "iPadから監査画面にアクセスできません。")
        print(f"[main] ★★★ 警告 ★★★ {message}")
        web_server.set_warning(message)
        audit_log.log_event("web_server_start_failed", port=args.port)

    def cancel_take_cap_timer():
        nonlocal take_cap_timer
        if take_cap_timer is not None:
            take_cap_timer.cancel()
            take_cap_timer = None

    def finish_take(auto_stopped=False):
        cancel_take_cap_timer()
        try:
            entry = take_recorder.stop_take()
            recorder.resume_cleanup()
            print(f"[テイク] クリップ確定: {entry['filename']} ({entry['duration_sec']}秒)")
            audit_log.log_event("take_created", auto_stopped=auto_stopped, **entry)
            if auto_stopped:
                msg = (f"テイクモードで最大録画時間({int(TAKE_MAX_DURATION_SECONDS)}秒)に達したため、"
                       "自動的に録画を終了しました。")
                print(f"[警告] {msg}")
                web_server.set_warning(msg)
                audit_log.log_event("take_auto_stopped_max_duration")
        except Exception as e:
            recorder.resume_cleanup()
            print(f"[エラー] テイク抽出に失敗しました: {e}")
            audit_log.log_event("take_error", error=str(e))

    def trigger_yame():
        if recording_mode.get_mode() == recording_mode.TAKE:
            if take_recorder.is_in_progress():
                finish_take()
            return
        try:
            clip_path = extractor.extract_on_yame()
            print(f"[やめ] クリップ確定: {clip_path}")
            audit_log.log_event("clip_created", clip=clip_path)
        except Exception as e:
            print(f"[エラー] クリップ抽出に失敗しました: {e}")
            audit_log.log_event("clip_error", error=str(e))

    def start_take():
        take_recorder.start_take()
        recorder.pause_cleanup()
        audit_log.log_event("take_started")
        nonlocal take_cap_timer
        take_cap_timer = threading.Timer(TAKE_MAX_DURATION_SECONDS, lambda: finish_take(auto_stopped=True))
        take_cap_timer.daemon = True
        take_cap_timer.start()

    def on_key_event(event_type, state):
        audit_log.log_event(event_type, timer_state=state)
        if event_type == "timer_start_detected" and recording_mode.get_mode() == recording_mode.TAKE:
            start_take()

    button_listener = None
    if args.input_mode == "button":
        from key_listener import TimerSyncedKeyListener
        button_listener = TimerSyncedKeyListener(on_yame=trigger_yame, on_event=on_key_event)
        web_server.register_timer_state_source(button_listener.get_state)
        # クリップ保護判定にタイマーの累積動作時間を使えるようにする
        extractor.running_counter_fn = button_listener.get_running_accumulator
        try:
            button_listener.start()
            print("[main] 物理ボタン(F2トグル+緊急ボタン)連動モードで待機中です")
            print(f"[main] 起動時のタイマー状態: {button_listener.get_state()} "
                  "(実際のタイマー表示と一致しているか必ず確認してください)")
        except RuntimeError as e:
            print(f"[エラー] {e}")
            print("[main] Enterキーモードにフォールバックします")
            args.input_mode = "enter"

    if args.input_mode == "timer":
        from timer_bridge import TimerHttpBridge
        timer_bridge = TimerHttpBridge(on_yame=trigger_yame, on_event=on_key_event)
        web_server.register_timer_bridge(timer_bridge)
        web_server.register_timer_state_source(timer_bridge.get_state)
        # クリップ保護判定にタイマーの累積動作時間を使えるようにする
        extractor.running_counter_fn = timer_bridge.get_running_accumulator
        print(f"[main] タイマー連動モードで待機中です(POST http://127.0.0.1:{args.port}/api/timer/event を待ちます)")
        print(f"[main] 起動時のタイマー状態: {timer_bridge.get_state()} "
              "(実際のタイマー表示と一致しているか必ず確認してください)")

    if args.input_mode == "enter":
        print("[main] 記録員操作: Enterキーで『やめ』(直近10秒を確定クリップとして保存)")
        print("[main] テイクモード中は、Enterキーがスタート/ストップのトグルとして動作します"
              "(1回目=スタート、2回目=ストップ。物理ボタン無しでもテイクモードを試せます)")

    print("[main] 終了: Ctrl+C")

    try:
        if args.input_mode == "enter":
            while True:
                input()
                if recording_mode.get_mode() == recording_mode.TAKE:
                    if take_recorder.is_in_progress():
                        trigger_yame()
                    else:
                        start_take()
                        print("[main] Enter検知: テイク開始(次のEnterでストップ)")
                else:
                    trigger_yame()
        else:
            # button: ボタン監視は別スレッドで動いている
            # timer: イベントはWebサーバースレッド経由で届く
            # いずれもメインスレッドは待機するだけでよい
            while True:
                threading.Event().wait(1)
    except KeyboardInterrupt:
        pass
    finally:
        print("\n[main] 終了処理中...")
        if button_listener:
            button_listener.stop()
        recorder.stop()
        audit_log.log_event("system_stop")
        print("[main] 終了しました")


if __name__ == "__main__":
    main()

