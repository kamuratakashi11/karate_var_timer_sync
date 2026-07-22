"""
空手BAR(Bunkai/Appeal Review)システム 設定ファイル
開発者がここの値を直接編集して運用パラメータを変更する(MVP段階の想定)
"""
import os

# --- コート識別 ---
# 8コート運用時、中央監視ダッシュボードが各PCを区別するための名前。
# 各コートのPCごとにここを書き換えるか、環境変数 COURT_NAME で上書きする
# (テストや複数プロセス同時起動時の利便性のため環境変数を優先)
COURT_NAME = os.environ.get("COURT_NAME", "コート1")

# --- カメラ映像設定 ---
FRAME_WIDTH = 1280
FRAME_HEIGHT = 720
FPS = 60

# --- リングバッファ設定 ---
# ffmpegの segment マルチプレクサで何秒ごとにファイルを区切るか
SEGMENT_SECONDS = 2
# 何個分のセグメントを保持し続けるか(古いものは自動削除)
# SEGMENT_SECONDS * BUFFER_SEGMENTS が実際の常時保持秒数の目安
# 例: 2秒 x 9個 = 18秒分バッファ(CLIP_DURATION_SECONDS=10秒に対して
# 余裕を持たせている。CLIP_DURATION_SECONDSを変更した場合、
# 少なくともその1.5〜2倍程度のバッファ秒数を確保すること)
BUFFER_SEGMENTS = 9

# --- クリップ抽出設定 ---
# 「やめ」操作の瞬間から遡って何秒分を確定クリップとして切り出すか。
# ルール上は6秒だが、念のため余裕を持たせて10秒にしている。
CLIP_DURATION_SECONDS = 10.0

# 「やめ」の瞬間に書き込み中だったセグメントは、切り出し前にこの秒数まで
# 書き終わりを待つ(実機検証で、書き込み中のまま読むと高確率でH.264の
# デコードエラー=映像破損を起こすことが判明したため)。待っても書き終わらない
# 場合はそのまま進み、デコード検証によるフォールバック(clip_extractor.py・
# take_recorder.py)に委ねる。
SEGMENT_CLOSE_WAIT_TIMEOUT_SEC = SEGMENT_SECONDS + 1.0

# 確定クリップを何世代分保持するか(FIFO、オフィシャルミス対応で2以上を推奨)
# 将来的に3,4と増やす場合はここを変更して再ビルドする
CLIP_SLOTS = 2

# クリップは作成された直後、タイマーが実際に「動作中」だった累積時間が
# ここで指定した秒数に達するまでは、FIFOによる上書き削除の対象にしない。
# (タイマー表示上の時刻ではなく、こちらのシステムが把握している
# スタート/ストップの実動作時間を基準にする。タイマー側の手動巻き戻し・
# 早送りの影響を受けないようにするため)
# これにより、ストップボタンを連打してもクリップが誤って消えることがなくなる
# (--input-mode button 使用時のみ有効。enterモードではタイマー状態を
# 追跡していないため、この保護は効かず常に通常のFIFOで動作する)
CLIP_PROTECTION_RUNNING_SECONDS = 2.0

# --- テイクモード設定(型など、開始〜終了を通しで録画するモード) ---
# 「リプレイモード」(既定): F2ストップの瞬間から遡ってCLIP_DURATION_SECONDS秒を
# 切り出す、従来通りのトリガー直前保存方式。
# 「テイクモード」: F2スタート〜ストップの区間をまるごと1本のクリップとして
# 保存する方式(型の演武など、数分間の通し録画が必要な場面向け)。
# ボタン押し忘れ等でストップが来ないまま放置された場合にリングバッファが
# 際限なく溜まり続けないよう、上限時間で自動的にストップ扱いにする。
TAKE_MAX_DURATION_SECONDS = 600.0  # 10分(想定5分程度に対して十分な安全マージン)

# --- 音声設定 ---
# 映像に加えてマイク音声も録りたい場合、Windowsが認識しているマイクの
# デバイス名をここに設定する(例: "マイク配列 (Realtek(R) Audio)")。
# 確認方法: PowerShellで `ffmpeg -list_devices true -f dshow -i dummy` を
# 実行すると、認識されているビデオ・オーディオデバイス名の一覧が表示される。
# カメラのデバイス番号やBUTTON_KEY_NAME同様、PCごとに実機で確認が必要な値。
# None のままだと音声は無効(従来通り映像のみを録画する)。
AUDIO_DEVICE_NAME = None
AUDIO_BITRATE = "128k"

# --- ディレクトリ設定 ---

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUFFER_DIR = os.path.join(BASE_DIR, "data", "buffer")     # ローリングセグメント置き場
CLIPS_DIR = os.path.join(BASE_DIR, "data", "clips")       # 確定済み6秒クリップ置き場(FIFOで自動削除される)
SAVED_DIR = os.path.join(BASE_DIR, "data", "saved")       # 監査担当が「保存」したクリップの永久保存置き場(FIFOの対象外)
SAVED_INDEX_PATH = os.path.join(SAVED_DIR, "index.jsonl")  # 保存クリップのメタ情報(いつ・どのコートか)
TAKE_DIR = os.path.join(BASE_DIR, "data", "takes")         # テイクモードの通し録画置き場(FIFOの対象外、一括削除UIで管理)
TAKE_INDEX_PATH = os.path.join(TAKE_DIR, "index.jsonl")    # テイクのメタ情報(いつ・どのコートか・長さ)

os.makedirs(BUFFER_DIR, exist_ok=True)
os.makedirs(CLIPS_DIR, exist_ok=True)
os.makedirs(SAVED_DIR, exist_ok=True)
os.makedirs(TAKE_DIR, exist_ok=True)

# --- エンコード設定 ---
# ultrafast推奨: リアルタイム性優先。画質より安定性が重要なため
FFMPEG_PRESET = "ultrafast"
FFMPEG_CRF = "23"  # 数字が小さいほど高画質・高負荷(18-28が実用域)

# --- 死活監視設定(中央ダッシュボード向け) ---
# 最後にフレームを受信してからこの秒数を超えたら「録画停止」とみなす
HEALTH_STALE_THRESHOLD_SEC = 3.0

# --- 物理ボタン連携設定(業者のタイマー用早押しボタンとの連動) ---
# ボタンはキーボード入力として割り当てられている(業者確認済み)。
# tools/detect_keyboard_key.py で実際に押して確認したキー名をここに設定する。
# 例: スペースキーなら "space"、F1キーなら "f1"、通常の文字キーならその文字そのもの。
# None のままだと、どのキーを押しても反応してしまい誤爆の危険があるため、
# 必ず現地確認の上で具体的なキー名を設定すること。
BUTTON_KEY_NAME = "f2"           # 業者確認済み: タイマー停止/開始はF2キー(トグル式)に割り当て
BUTTON_DEBOUNCE_SEC = 0.5        # 誤ってチャタリングで2回反応するのを防ぐ間隔

# F2はトグル式(スタート/ストップを交互に切り替える)なので、内部で押下回数を
# 数えて偶数回目(ストップ)だけ「やめ」処理を発火する。ただしこの方式は
# 押下の取りこぼし等で一度ズレると誤動作し続けるリスクがあるため、
# 緊急時に強制的に「やめ」を発火しつつ内部状態を再同期するための
# 専用ボタン(別キー)を用意する。tools/detect_keyboard_key.py で確認して設定すること。
BUTTON_EMERGENCY_KEY_NAME = None  # 例: "f3" (緊急やめ・再同期ボタン)

# システム起動時点でのタイマーの状態(通常は試合開始前=停止中のはず)。
# ここが実際の状態とズレていると、最初のF2押下から誤判定が始まってしまうため、
# 起動時に記録員/監査が「今タイマーが本当に止まっているか」を必ず確認すること。
TIMER_INITIAL_STATE = "stopped"   # "stopped" または "running"

# --- 監査ログ設定 ---
# F2/緊急ボタンの押下、クリップ生成の成否、タイマー追跡状態の遷移などを記録する。
# 抗議の正当性を巡って後日確認が必要になった際の証跡として使う。
AUDIT_LOG_PATH = os.path.join(BASE_DIR, "data", "audit_log.jsonl")

