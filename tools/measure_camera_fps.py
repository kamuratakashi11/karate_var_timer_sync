"""
実際にカメラから何fps配信されているかを実測する診断スクリプト。

録画用のRealCameraSource(camera_source.py)と全く同じ初期化コード
(CAP_MSMF・FOURCC設定順序)を使って、実際にcap.read()を一定時間
繰り返し呼び、実測fpsを1秒ごとに表示する。

背景: 2026-07-23の実機検証(練習試合)で、録画された動画が実時間より
早送りのように見える不具合が見つかった。フレーム複製による時間ズレの
補正(recorder.pyの修正)は効いたが、代わりに動画がカクつくように
なった。これは実際のカメラ配信fpsが設定値(60fps)に大きく届いて
いないことを示唆している。ラウンド1の教訓通り、cv2.VideoCapture.get()
の返り値は「設定値」であり実配信レートを保証しないため、read()の
実測でしか確認できない。

照明条件によってfpsが変化する(暗いと自動露出でシャッター速度が
遅くなりfpsが落ちる)可能性があるため、照明を変えながら複数回
実行して比較するとよい。

使い方:
  python tools/measure_camera_fps.py --camera 0 --seconds 20
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from camera_source import RealCameraSource  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--camera", type=int, default=0, help="実カメラのデバイス番号")
    parser.add_argument("--seconds", type=float, default=20.0, help="計測する秒数")
    args = parser.parse_args()

    print(f"[measure_camera_fps] カメラ(index={args.camera})を開いています...")
    source = RealCameraSource(args.camera)
    source.start()

    print(f"[measure_camera_fps] {args.seconds}秒間、実測fpsを1秒ごとに表示します。"
          "録画時と同じ照明・向きにして計測してください。")

    start = time.monotonic()
    total_frames = 0
    fail_count = 0
    second_bucket = 0
    frames_in_bucket = 0

    while True:
        elapsed = time.monotonic() - start
        if elapsed >= args.seconds:
            break

        ok, frame = source.read()
        if not ok or frame is None:
            fail_count += 1
            continue

        total_frames += 1
        frames_in_bucket += 1

        bucket = int(elapsed)
        if bucket != second_bucket:
            print(f"  {second_bucket}〜{second_bucket + 1}秒目: {frames_in_bucket} fps")
            second_bucket = bucket
            frames_in_bucket = 0

    real_elapsed = time.monotonic() - start
    source.release()

    print("\n=== 結果 ===")
    print(f"実測平均fps: {total_frames / real_elapsed:.2f} "
          f"({total_frames}フレーム / {real_elapsed:.2f}秒)")
    if fail_count:
        print(f"読み取り失敗: {fail_count}回(カメラ接続を確認してください)")
    print("\n設定値(60fps)と比べて実測平均fpsが大きく下回っている場合、"
          "録画時のカクつきの原因は実配信レート不足です。"
          "上記の1秒ごとの内訳で、特定の区間だけfpsが落ちていないかも確認してください"
          "(暗い場所を向けた瞬間だけ落ちる、等があれば自動露出が疑わしいです)。")


if __name__ == "__main__":
    main()
