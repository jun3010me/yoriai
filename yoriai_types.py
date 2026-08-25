"""yoriai.py と tools.py・network.py の間で共有される定数・ツールスキーマ。

仮の判断(モジュール分割第一弾・第二弾への対応): tools.py・network.py への
切り出しにあたり、以下の定数は「tools.py/network.py 側の関数が使うが、
yoriai.py 側の関数(まだ移動していないコードや、将来 llm_stream.py に
移動する予定のコード)からも参照される」という双方向の依存関係にある。
tools.py/network.py に置いたまま yoriai.py からimportさせる、あるいは
その逆にすると、`yoriai.py`⇔`tools.py`/`network.py`の循環importが
発生してしまうため、依存の無いこのモジュールに切り出し、両側から参照する
形で解消する。値そのものは元のyoriai.py上の定義から変更していない。
"""

# 仮の判断: チャットの接続確立自体はカード取得と同程度の速さで判定してよい
# (詳細はyoriai.py側のCHAT_READ_TIMEOUT_SECのコメントを参照)。
CHAT_CONNECT_TIMEOUT_SEC = 5

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
CHAT_MAX_OUTPUT_TOKENS = 8192

# 仮の判断: 依頼文の「空きリソース上位2〜3台」という表現の範囲内で、
# 上限を3台に固定する(候補がそれより少ない場合は、いる分だけに送る)。
MULTI_QUERY_TARGET_COUNT = 3

# 仮の判断: PROGRESS.mdはYoriai自身が状態管理に使うファイルであり、
# モデルが自由に書き換えたり消したりできてしまうと、進行状況の
# 永続化・巡回モード・自動再開の前提が壊れる。
PROGRESS_FILENAME = "PROGRESS.md"

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
