"""
現地診断ツール。業者のUSBボタンが届いたら、まずこれを実行してください。

ボタンはキーボード入力として割り当てられているとのことなので、
このスクリプトはPC上で押されたキーをすべて表示します。
実際にボタンを押してみて、「何というキー名で表示されるか」を確認し、
そのキー名を config.py の BUTTON_KEY_NAME に設定してください。

使い方:
  1. USBボタンをPCに接続する
  2. python3 tools/detect_keyboard_key.py を実行する
  3. ボタンを押してみる → 押されたキーの名前が表示される
  4. その名前(例: 'space', 'f1', 'enter' など)をメモする
  5. Ctrl+Cで終了する

注意: このツールは押されたキーをすべて表示するため、通常のキーボード操作も
表示されます。ボタンを押した瞬間に表示される行だけを確認してください。
"""

import sys

try:
    from pynput import keyboard
except ImportError:
    print("エラー: pynputがインストールされていません。")
    print("先に以下を実行してください:")
    print("  pip install pynput --break-system-packages")
    sys.exit(1)


def key_name(key):
    """pynputのキーオブジェクトから、config.pyに書ける形の名前を作る"""
    if isinstance(key, keyboard.KeyCode):
        return key.char if key.char is not None else str(key)
    # keyboard.Key.space, keyboard.Key.f1 のような特殊キーの場合
    return key.name


def on_press(key):
    name = key_name(key)
    print(f"★キー押下検出★ キー名 = '{name}'   (config.pyに設定する場合はこの文字列)")


def on_release(key):
    if key == keyboard.Key.esc:
        print("\nEscキーで終了します。")
        return False


def main():
    print("キー入力の監視を開始します。ボタンを押してみてください。")
    print("終了するには Escキーを押すか、Ctrl+Cを押してください。\n")
    try:
        with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
            listener.join()
    except KeyboardInterrupt:
        print("\n終了します。")


if __name__ == "__main__":
    main()
