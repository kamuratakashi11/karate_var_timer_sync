"""
中央死活監視ダッシュボード。

各コートのPC(karate_bar/src/main.pyを起動しているPC)が持つ
/api/health エンドポイントを定期的に問い合わせ、
「録画が正常に動いているか」だけを一覧表示する。

重要: 映像データ・クリップ本体は一切扱わない。
     ここで扱うのは軽量なJSON(稼働時間・最終フレーム受信からの経過秒数)のみ。
     抗議が来た際の実際のクリップ確認は、引き続き各コートの監査担当が
     自分のiPad/PCから該当コートのPCに直接アクセスして行う(このダッシュボードは無関係)。
"""

import json
import logging
import os
import time
import concurrent.futures

import requests
from flask import Flask, jsonify, render_template

app = Flask(__name__, static_folder="static", template_folder="static")

# src/web_server.pyと同様、Werkzeugの1リクエストごとのアクセスログが
# 長時間のポーリングで蓄積し、Windows上でハングの原因になり得るため抑制する。
logging.getLogger("werkzeug").setLevel(logging.WARNING)

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "courts_config.json")
REQUEST_TIMEOUT_SEC = 1.5


def load_courts():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)["courts"]


def fetch_court_health(court):
    url = f"http://{court['host']}:{court['port']}/api/health"
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT_SEC)
        resp.raise_for_status()
        data = resp.json()
        data["online"] = True
        data["court_config_name"] = court["name"]
        return data
    except requests.RequestException:
        return {
            "online": False,
            "court_config_name": court["name"],
            "court": court["name"],
            "recording_ok": False,
            "last_frame_age_sec": None,
            "uptime_sec": None,
            "message": "PCに接続できません(電源/ネットワークを確認してください)",
        }


@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/all_status")
def api_all_status():
    courts = load_courts()
    # 8コート分を並行して問い合わせる(直列だとタイムアウト時に最大12秒待つことになるため)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(courts) or 1) as executor:
        results = list(executor.map(fetch_court_health, courts))
    return jsonify(results)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=6000, threaded=True)
