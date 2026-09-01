"""yoriai.py と tools.py・network.py・llm_stream.py の間で共有される
定数・ツールスキーマ。

仮の判断(モジュール分割第一弾〜第三弾への対応): tools.py・network.py・
llm_stream.py への切り出しにあたり、以下の定数は「切り出し先の関数が
使うが、yoriai.py 側にまだ残っている関数からも参照される」という
双方向の依存関係にある。切り出し先に置いたまま yoriai.py から
importさせる、あるいはその逆にすると、`yoriai.py`⇔各モジュールの
循環importが発生してしまうため、依存の無いこのモジュールに切り出し、
両側から参照する形で解消する。値そのものは元のyoriai.py上の定義から
変更していない。
"""

import os

# 仮の判断: チャットの接続確立自体はカード取得と同程度の速さで判定してよいが、
# LLMの生成そのものは(モデルサイズや質問内容によっては)数十秒かかることが
# あるため、読み取りタイムアウトは長めに取る。tools.py・llm_stream.py側と
# yoriai.py側(_stream_chat_from_candidate等)の双方から参照される。
CHAT_CONNECT_TIMEOUT_SEC = 5

# 仮の判断: 上記CHAT_CONNECT_TIMEOUT_SECと対になる読み取りタイムアウト。
# トークンが実際に流れ続けている間は働かない(データが届く限り
# 「タイムアウト」にはならない)ため、応答が延々と続く問題への歯止めには
# ならない(そちらはCHAT_MAX_OUTPUT_TOKENSで別途対応する)。llm_stream.py側
# (_stream_ollama_turn等)とyoriai.py側(_stream_chat_from_candidate)の
# 双方から参照される。
#
# 仮の判断(実機報告への対応): 大型モデル(例: MacStudio上のqwen3-235b)は
# 最初の1トークンを出すまでの時間(プロンプト処理+thinking)がこの値を
# 超えることがあり、特に対話プロトコルはラウンドを重ねるほど議事録全文を
# プロンプトに累積するため起きやすい。既定値そのものを引き上げるのでは
# なく、KEEP_ALIVE(llm_stream.py・YORIAI_OLLAMA_KEEP_ALIVE環境変数)と
# 同じ流儀で環境変数による上書きのみを可能にし、実機の状況を見ながら
# 調整できるようにする。
CHAT_READ_TIMEOUT_SEC = int(os.environ.get("YORIAI_CHAT_READ_TIMEOUT_SEC", "120"))

# 仮の判断: 各バックエンドのベースURL。llm_stream.py側(_stream_ollama_turn等)
# と、yoriai.py側に残るシステム情報取得系(get_ollama_installed_models等)の
# 双方から参照される。
OLLAMA_BASE_URL = "http://localhost:11434"
LMSTUDIO_BASE_URL = "http://localhost:1234"
# 仮の判断: mlx_lm.serverの既定ポート(8080)を前提とする。
MLX_LM_BASE_URL = "http://localhost:8080"

# 仮の判断: mDNS/カード取得サーバーが名乗るサービス種別。network.py側の
# YoriaiListener・CardRequestHandlerと、yoriai.py側にまだ残っているmDNS
# 起動コード(run_agent)の双方から参照される。
SERVICE_TYPE = "_yoriai._tcp.local."

# 仮の判断: 自己紹介カード取得・組織フィンガープリント検証で使うHTTP
# ヘッダー名。network.py側(CardRequestHandler・YoriaiListener・
# discover_via_tailscale)と、yoriai.py側にまだ残っているチャット問い合わせ
# コード(_fetch_org_snapshot等)の双方から参照される。
ORG_FINGERPRINT_HEADER = "X-Yoriai-Org-Fingerprint"

# 仮の判断: カード取得サーバーへの問い合わせのタイムアウト秒数。
# network.py側と、yoriai.py側にまだ残っているコードの双方から参照される。
CARD_REQUEST_TIMEOUT_SEC = 5

# 仮の判断: バックエンド(Ollama/LM Studio/MLX-LM)への問い合わせに応答の
# 最大トークン数を指定していなかったため、生成が延々と続いてしまう不具合が
# 実機で報告された。詳細はyoriai.py側の元のコメントを参照。
#
# 仮の判断(実機報告への対応): コンテキスト長の大きいモデルを動かしている
# 機体では、思考モデルの長い思考にもこの上限が頭打ちになってしまう
# (yoriai.py側の_decide_max_output_tokensで、モデルのコンテキスト長に
# 応じて自動的に引き上げる)。それでもなお手動で明示的に上書きしたい
# 場合のため、CHAT_READ_TIMEOUT_SECと同じ流儀で環境変数
# YORIAI_CHAT_MAX_OUTPUT_TOKENSによる上書きを可能にする
# (_decide_max_output_tokensは、この値を自動計算結果の下限として
# 常に尊重する)。
CHAT_MAX_OUTPUT_TOKENS = int(os.environ.get("YORIAI_CHAT_MAX_OUTPUT_TOKENS", "8192"))

# 仮の判断: 依頼文の「空きリソース上位2〜3台」という表現の範囲内で、
# 上限を3台に固定する(候補がそれより少ない場合は、いる分だけに送る)。
MULTI_QUERY_TARGET_COUNT = 3

# 仮の判断: PROGRESS.mdはYoriai自身が状態管理に使うファイルであり、
# モデルが自由に書き換えたり消したりできてしまうと、進行状況の
# 永続化・巡回モード・自動再開の前提が壊れる。
PROGRESS_FILENAME = "PROGRESS.md"

# 仮の判断: 協業モードの生成物をYoriai本体(yoriai.py・config.py等)と
# 同じディレクトリに置くと、どれが本体でどれが生成物か見分けにくく、
# 生成物のファイル名がYoriai本体のファイル名(config.py等)と衝突する
# リスクもある。そのため、生成物は必ず「<--dirで指定したディレクトリ>/
# projects/<プロジェクト名>/」というサブディレクトリにまとめる。
# progress.py側(_find_incomplete_projects)とyoriai.py側に残る
# タスクキュー・resume-all・fix系コードの双方から参照される。
PROJECTS_SUBDIR_NAME = "projects"

# ---------------------------------------------------------------------------
# ファイル読み取りツール(協業モードのレビュー専用、修正依頼専用の両方で使用)
# ---------------------------------------------------------------------------
#
# 仮の判断(実機で発見された不具合を受けての設計変更): 当初は、レビュー
# 依頼のリクエストボディに実装依頼元が「その時点でプロジェクトに実際に
# 保存されているファイルの中身」をavailable_filesとして直接埋め込み、
# read_fileの実行はレビュー担当自身のプロセス内でその辞書を参照するだけ、
# という設計にしていた。しかしこの方式では、available_filesを埋め込む
# タイミングのスナップショットが固定されてしまい、LLMの応答生成中に他の
# ワーカースレッドが新しいファイルを完成させても、その更新がレビュー担当
# には一切反映されない不具合が実機で見つかった。
#
# 修正後は、read_fileを「レビュー担当のプロセス内では実行しない、
# 呼び出し元(実装依頼元)が引き取って実行するツール」として扱う。
READ_FILE_TOOL_NAME = "read_file"
READ_FILE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": READ_FILE_TOOL_NAME,
        "description": (
            "レビュー中のプロジェクトに含まれる、他のファイルの実際の中身を読み取る。"
            "自分が担当していないファイルとの連携(関数名・引数・データ形式が"
            "合っているか等)を、推測ではなく実際のコードを確認してから判断したい"
            "場合に使う。まだ実装されていないファイルを指定した場合は、その旨が返される。"
            "引数を省略するとファイル全体を返すが、大きなファイルを確認する際は、まず"
            "search_in_fileで関連箇所を探し、start_line/end_line(またはlineと"
            "context_lines)で必要な範囲だけを指定して読むことを検討すること。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "読みたいファイル名(例: storage.py、またはtemplates/base.htmlのようなサブディレクトリ内のパス)",
                },
                "start_line": {
                    "type": "integer",
                    "description": "読み始めたい行番号(1始まり)。省略時はファイル全体を返す。",
                },
                "end_line": {
                    "type": "integer",
                    "description": "読み終わりたい行番号。省略時はファイル末尾(または開始行から一定範囲)まで。",
                },
                "line": {
                    "type": "integer",
                    "description": (
                        "特定の行番号を中心に前後だけ読みたい場合に指定する"
                        "(context_linesと併用。start_line/end_lineの代わりに使える)。"
                    ),
                },
                "context_lines": {
                    "type": "integer",
                    "description": "lineを指定した場合に、その前後何行を含めるか(省略時は20行)。",
                },
            },
            "required": ["filename"],
        },
    },
}

SEARCH_IN_FILE_TOOL_NAME = "search_in_file"
SEARCH_IN_FILE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": SEARCH_IN_FILE_TOOL_NAME,
        "description": (
            "他のファイルの中身全体をread_fileで読む前に、まずキーワードや関数名で"
            "ファイル内を検索し、一致した行番号とその前後数行の抜粋だけを確認する。"
            "大きなファイルの該当箇所に見当をつけてから、read_fileでその範囲だけを"
            "読むと、無駄なやり取り・トークン消費を減らせる。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "検索したいファイル名(例: storage.py、またはtemplates/base.htmlのようなサブディレクトリ内のパス)",
                },
                "query": {"type": "string", "description": "検索したいキーワードまたは関数名"},
                "context_lines": {
                    "type": "integer",
                    "description": "一致箇所の前後何行を抜粋に含めるか(省略時は5行)。",
                },
            },
            "required": ["filename", "query"],
        },
    },
}
