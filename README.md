# Google form抽選ツール

Google Form などから出力した CSV / Excel ファイルを読み込み、応募者の列設定、除外、特別条件、履歴にもとづく重み付けを行って抽選するローカル Web アプリです。

データは外部サーバーへ送信されません。抽選履歴、応募記録、当選結果、参加者履歴はローカル SQLite データベース `vrc_raffle.db` に保存されます。

## 主な機能

- CSV / Excel ファイルの読み込み
- 抽選ID列と結果表示列のクリック指定
- 行クリックによる応募者除外
- 回答値に応じた特別条件倍率
- 均等抽選、ゆるやか加重、二乗加重
- 当選後の重みリセット
- Event ごとの抽選履歴管理
- 過去 Session の結果表示と削除
- Event 別ユーザー一覧
- 以前の参加・当選履歴の CSV / Excel 同期
- 同期データの追加、上書き、取り消し
- VRC / X 情報による既存参加者の照合
- ローカル Web UI
- Docker / Docker Compose 配備
- X OAuth 2.0 ログイン（PKCE）
- 保存しないゲスト抽選（均等確率 + 特別条件）
- X アカウントごとの Event・ユーザー履歴・抽選結果

## 必要環境

- Python 3.9 以上
- pip

依存パッケージは `requirements.txt` で管理しています。

## セットアップ

macOS / Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## 起動方法

```bash
python3 main.py
```

または:

```bash
python3 web_app.py
```

起動するとブラウザで `http://127.0.0.1:8765/` 付近の URL が開きます。ポートが使用中の場合は、自動で次の空きポートを使います。

Web サーバーと API は FastAPI / Uvicorn で動作します。開発時は `/docs` で API 定義を確認できます。

終了するときは画面右上の「終了」または左側の「アプリを終了」を押してください。

## 基本操作

1. 「CSV / Excel を選択して読み込み」を押します。
2. ファイルを選択すると自動で読み込まれます。
3. 列設定で、抽選に使う ID 列を左クリックします。
4. 結果に表示したい列を左クリックします。
5. 未選択列を右クリックすると、特別条件を設定できます。
6. 応募者の左端にある「除外」をクリックすると、その応募者を抽選対象から除外できます。
7. Event、抽選人数、確率モードを選びます。
8. 「抽選開始」を押します。
9. 抽選結果タブに移動し、今回の当選者が表示されます。
10. 画面下部の「以前の抽選結果」から過去 Session を表示できます。

## 列設定

列設定タブでは、読み込んだ CSV / Excel の列を以下の用途に指定します。

| 操作 | 意味 | 表示 |
| --- | --- | --- |
| 未選択列を左クリック | ID列または表示列に指定 | ID列は青、表示列は緑 |
| 選択済み列を左クリック | 指定を解除 | 通常表示に戻る |
| 未選択列を右クリック | 特別条件を設定 | 表頭はオレンジ |
| 行をクリック | 応募者を除外 / 除外解除 | 除外行は赤 |

ID列が未指定の状態で未選択列をクリックすると、その列が ID列になります。  
ID列が指定済みの状態で未選択列をクリックすると、結果表示列になります。

## 特別条件

未選択列を右クリックすると、列内の回答値から条件を選び、その条件に一致する応募者の重みを倍率で増やせます。

例:

```text
特別条件 = 【しました】 倍率 ×2
```

特別条件列は、表頭だけがオレンジになります。条件に一致した応募者のセルだけがオレンジ表示になります。

## 抽選モード

### 均等抽選

履歴を使わず、全員を同じ確率で抽選します。

```text
weight = 1
```

### ゆるやか加重 - n + 1

`n` は「前回当選後から現在までの参加累計」です。  
当選すると `n` は 0 に戻り、次回は最低重みから再スタートします。

```text
weight = n + 1
```

### 二乗加重 - (n + 1)^2

`n` が増えるほど、ゆるやか加重より強く優先します。  
当選すると `n` は 0 に戻ります。

```text
weight = (n + 1)^2
```

### 特別条件込みの最終重み

```text
final_weight = base_weight * special_multiplier
probability = final_weight / total_final_weight
```

## 当選後の重みリセット

当選者は、次回以降の重み計算に使う累計 `n` がリセットされます。  
ただし、ユーザー一覧に表示される参加回数と当選回数は履歴として保存され続けます。

## Event

Event は抽選を活動や企画ごとに分けるための分類です。

- 主画面の Event select では、今回の抽選を保存する Event を選びます。
- Event を選ばない場合は `default` に保存されます。
- Event編集タブでは、Event の作成、名前変更、削除ができます。
- 最初からある Event も名前を変更できます。他の Event が1つ以上ある場合だけ削除できます。
- 最初からある Event を削除すると、ユーザー履歴と Session は確認画面に表示された移動先 Event へ移されます。

## 抽選結果

抽選結果タブでは、以下を確認できます。

- 今回または選択中 Session の当選者
- 計算方法の簡単な説明
- 以前の抽選結果一覧

以前の抽選結果一覧では、Session の表示と削除ができます。Session 名変更機能はありません。
Session を削除すると、その Session の参加回数と当選回数も Event のユーザー一覧から戻され、重みと確率が再計算されます。
画面の Session 番号は X アカウントごとに `#1` から始まります。削除で空いた番号は、次の抽選で再利用されます。

## ユーザー一覧

ユーザー一覧タブでは、保存済み DB の参加者履歴を確認できます。

表示する Event を select box で切り替えられます。

- `すべて`: 全 Event の履歴
- `default`: Event 未指定の履歴
- 任意の Event: その Event の履歴

表示項目:

- 抽選ID
- 参加回数
- 当選回数
- 重み
- 現在確率

「Excel出力」を押すと、現在選択中の Event を `.xlsx` で保存します。

- `Sheet1`: 現在のユーザー確率表
- `Sheet2`: 保存済みの全抽選結果

CSV は複数 Sheet を持てないため、出力形式は Excel です。

## 以前の履歴を同期

ユーザー一覧の「データ同期」から、以前の参加回数・当選回数を取り込めます。

1. ユーザー一覧で対象 Event を選びます。
2. 「データ同期」を押します。
3. CSV / Excel を選択します。
4. 表頭を左クリックし、順番に ID列、参加回数列、当選回数列を指定します。
5. 同期先 Event を確認します。
6. 「追加」または「上書き」を選びます。初期値は「追加」です。
7. 「同期を実行」を押します。

列の色:

- 青: ID列
- 紫: 参加回数列
- 緑: 当選回数列

同期方法:

- `追加`（初期値）: 現在の Event の参加回数・当選回数へ、ファイルの値を加算します。
- `上書き`: 現在の Event のユーザー一覧を削除し、ファイルに含まれるユーザーと回数へ完全に入れ替えます。

同期ファイルには当選日時がないため、同期直後の連続未当選回数は `n = max(参加回数 - 当選回数, 0)` と推定します。線形は `n + 1`、二乗は `(n + 1)^2` を重みに使います。以後は落選参加ごとに `n + 1`、当選時に `n = 0` へ更新されます。

ユーザー一覧と重み計算は Event ごとのユーザー履歴だけを使います。抽選を行うと、この履歴の参加回数・当選回数が更新されます。Session は抽選結果の確認用で、ユーザー一覧へ重ねて加算しません。

### 同期を元に戻す

同期ポップアップ下部の「同期履歴」から「元に戻す」を押します。

- 取り消せるのは、その Event で最新の未取消 Batch だけです。
- 同期前に Event のユーザー表全体を独立したスナップショットとして保存します。
- 取り消すと、行単位の計算ではなく、この完全なスナップショットへ戻ります。
- 実際の抽選 Session は削除・変更されません。

## 保存されるデータ

SQLite データベース `vrc_raffle.db` に保存されます。

主なテーブル:

- `events`: Event 情報
- `participants`: 参加者履歴
- `raffle_sessions`: 抽選 Session
- `submission_records`: Session ごとの応募記録
- `raffle_results`: 当選結果
- `event_participant_history`: Event ごとのユーザー履歴
- `history_sync_batches`: 同期 Batch
- `history_sync_snapshots`: 同期直前の Event ユーザー表の完全バックアップ
- `history_sync_changes`: 同期取り消し用 before / after
- `auth_users`: ログインした X ユーザーの最小プロフィール

## Docker 配備

Docker Compose で起動できます。

```bash
docker compose up -d --build
```

ブラウザで次を開きます。

```text
http://SERVER_IP:8765/
```

ログ:

```bash
docker compose logs -f
```

停止:

```bash
docker compose down
```

DB はホスト側の `./data/vrc_raffle.db` に保存されます。コンテナを作り直しても履歴は残ります。

サーバー配備時は以下の設定になります。

- `APP_HOST=0.0.0.0`
- `APP_PORT=8765`
- `OPEN_BROWSER=0`
- `ALLOW_SHUTDOWN=0`
- `DB_PATH=/app/data/vrc_raffle.db`

公開サーバーでは画面の「終了」ボタンを非表示にし、HTTP 経由でサーバーを停止できないようにします。インターネットへ公開する場合は、リバースプロキシ、HTTPS、X ログイン、ファイアウォールを設定してください。

### X ログインを有効にする

1. X Developer Console で OAuth 2.0 を有効にします。
2. Callback URI に公開 URL の `/auth/callback` を正確に登録します。
3. `.env.example` を参考に `.env` を作成します。
4. `docker compose up -d --build` で起動します。

```dotenv
AUTH_REQUIRED=1
X_CLIENT_ID=your_client_id
X_CLIENT_SECRET=your_client_secret
X_REDIRECT_URI=https://raffle.example.com/auth/callback
SESSION_SECRET=十分に長いランダム文字列
COOKIE_SECURE=1
```

必要な X OAuth scope は `tweet.read users.read` です。`X_CLIENT_SECRET` は confidential client の場合だけ設定します。利用者を限定する場合は、X の数値 ID または username をカンマ区切りで指定します。

```dotenv
ALLOWED_X_USER_IDS=123456789,987654321
ALLOWED_X_USERNAMES=example_user,staff_account
```

access token は DB と Cookie に保存しません。`auth_users` には X ユーザー ID、username、表示名、画像 URL、ログイン日時だけを保存します。`AUTH_REQUIRED=0` のローカル起動ではログインなしで利用できます。

トップページはログインなしで直接開きます。

- 未ログイン: `単発抽選モード` になります。均等確率と特別条件を利用でき、抽選結果や参加回数は DB に保存されません。
- X ログイン後: `履歴保存モード` になります。Event、ユーザー履歴、抽選結果、同期履歴は X アカウントごとに保存されます。

右上の `Xでログイン` から認証します。ログイン後は同じ主画面へ戻り、右上に X のアイコンと名前が表示されます。

Docker を再起動しただけでは、X の Client ID がないため X ログインは有効になりません。`.env` に `AUTH_REQUIRED=1`、`X_CLIENT_ID`、`X_REDIRECT_URI`、`SESSION_SECRET` を設定してから再構築してください。

## プロジェクト構成

```text
WeightedSelectionTool/
├─ main.py              # 起動エントリ
├─ web_app.py           # FastAPI アプリ、Uvicorn 起動、静的ファイル配信
├─ fastapi_routes.py    # FastAPI の API ルート
├─ api.py               # 旧呼び出し向けの薄い互換 API
├─ core.py              # DB 初期化、CSV 読み込み、重み計算
├─ services/
│  ├─ api_services.py   # 機能別 class と API ディスパッチ
│  ├─ application.py    # 抽選ドメイン処理
│  └─ auth_service.py   # X OAuth 2.0 + PKCE
├─ static/
│  ├─ styles.css        # メイン画面 CSS
│  ├─ login.css         # ログイン画面 CSS
│  └─ app.js            # フロントエンド JS
├─ templates/
│  ├─ index.html        # メイン画面 HTML
│  └─ login.html        # X ログイン画面 HTML
├─ requirements.txt
├─ Dockerfile
├─ docker-compose.yml
├─ .dockerignore
├─ README.md
├─ 部署文档_OracleCloud_Nginx.md
├─ ユーザーマニュアル.md
└─ vrc_raffle.db        # 実行時に作成されるローカル DB
```

## よくあるエラー

### `ModuleNotFoundError: No module named 'pandas'`

依存パッケージが入っていません。

```bash
pip install -r requirements.txt
```

### `ModuleNotFoundError: No module named 'thefuzz'`

依存パッケージが入っていません。

```bash
pip install -r requirements.txt
```

### ブラウザを更新したらアクセスできない

現在の実装では、更新だけでサーバーを停止しません。  
アクセスできない場合は、ターミナルでアプリが起動中か確認し、再度 `python3 main.py` を実行してください。

### ポートが 8765 ではない

8765 が使用中の場合、自動で 8766、8767 のような次の空きポートを使います。ターミナルに表示された URL を開いてください。

## 注意

- `vrc_raffle.db` はローカル DB です。削除すると履歴も消えます。
- 抽選前に ID列を必ず指定してください。
- 除外した応募者は抽選対象に入りません。
- CSV の内容はブラウザ外のサーバーへ送信されません。
