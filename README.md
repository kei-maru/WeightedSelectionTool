# Google form抽選ツール

Google Form の応募CSV向けの抽選管理ツールです。  
CSV / Excel ファイルを読み込んで応募者情報を自動認識し、抽選前に各レコードを編集してから、選択した確率モードで抽選できます。

アプリはローカル Web アプリです。抽選セッション、応募レコード、当選結果、参加者履歴はローカル SQLite データベース `vrc_raffle.db` に保存されます。

## 起動方法

```bash
python3 web_app.py
```

起動するとブラウザで `http://127.0.0.1:8765/` が開きます。画面右上の「終了」ボタンでローカルサーバーを終了できます。

`main.py` も同じ Web アプリを起動します。

```bash
python3 main.py
```

## 主な機能

- CSV / Excel ファイルの読み込み
- 抽選ID列、結果表示列のクリック指定
- 行クリックによる応募者除外
- 特別条件による倍率加算
- 均等抽選、線形加重抽選、指数加重抽選
- Eventごとの抽選セッション管理
- 抽選結果のセッション保存
- セッションごとの結果表示、削除
- グローバルユーザー一覧
- VRC / X 情報による既存参加者のファジーマッチング

## 認識できる主な列

CSV / Excel 内の列名は、自動的に内部フィールドへ変換されます。

| 内部フィールド | 説明 | 認識される列名の例 |
| --- | --- | --- |
| `display_name` | 表示名 | `name`, `名前`, `display name`, `nickname`, `ニックネーム` |
| `vrc_id` | VRC ID | `vrc id`, `vrchat id`, `vrchat_id`, `vrcid`, `vrc名前`, `vrc name` |
| `vrc_url` | VRC URL | `vrc url`, `vrchat url`, `vrchat_url`, `vrcurl`, `vrchatリンク` |
| `x_id` | X ID | `x id`, `twitter id`, `x name`, `twitter name`, `x_id`, `twitterid` |
| `x_url` | X URL | `x url`, `twitter url`, `xリンク`, `twitterリンク`, `x_url` |
| `join_count` | 参加回数 | `join count`, `join_count`, `joins`, `参加回数`, `参加次数`, `応募回数` |
| `win_count` | 当選回数 | `win count`, `win_count`, `wins`, `当選回数`, `当选次数`, `中奖次数` |
| `current_probability` | 現在確率 | `current probability`, `probability`, `現在確率`, `现在概率`, `当前概率`, `抽签概率` |

CSV に `参加回数` や `当選回数` がない場合は、VRC ID / VRC URL / X ID / X URL を使って参加者データベースを検索し、既存履歴があれば自動で反映します。

## 使用手順

1. アプリを起動します。
2. CSV / Excel ファイルを読み込みます。
3. 列設定で、抽選ID列を左クリックします。
4. 結果に表示したい列を左クリックします。
5. 必要に応じて、未選択列を右クリックして特別条件と倍率を設定します。
6. 必要に応じて、行をクリックして応募者を抽選から除外します。
7. Event、抽選人数、確率モードを選びます。
8. 「抽選開始」を押すと、抽選結果タブへ移動して当選者を表示します。
9. 抽選内容は新しい Session として保存されます。
10. 抽選結果タブの以前の抽選結果から、Session の表示、削除ができます。

抽選完了後、画面下部に `Session #ID` が表示されます。  
この ID を使うと、抽選結果タブで該当 Session を探しやすくなります。

## 抽選モード

### 均等抽選

全員を同じ確率で抽選します。

### 線形加重

前回当選からの未当選累計を `n` として、少しずつ優先します。当選すると `n` は 0 に戻ります。

```text
weight = max(n^2, 1)
```

### 指数加重

前回当選からの未当選累計 `n` を強く反映します。当選すると `n` は 0 に戻ります。

```text
weight = 2^n
```

### 特別条件

列設定で未選択列を右クリックすると、回答値ごとの倍率を設定できます。

```text
最終weight = 基本weight * 特別条件倍率
```

右側の「現在確率」は、読み込み後またはモード変更時に自動計算されます。

## セッション保存

「抽選開始」を押すと、以下の内容が保存されます。

- `raffle_sessions`
  - CSV ファイル名
  - 抽選モード
  - 抽選人数
  - 作成日時
  - 備考
- `submission_records`
  - その Session に読み込まれた応募レコード
  - 既存参加者との紐付け情報
- `raffle_results`
  - 当選者情報
- `participants`
  - 参加者履歴
  - 本セッション参加分として参加回数を +1
  - 当選者は当選回数を +1

## Session / Event の編集

抽選結果タブでは、保存済み Session を操作できます。

- 「表示」
  - その Session の当選結果を表示します。
- 「削除」
  - Session と関連する抽選結果、応募記録を削除します。

Event編集タブでは、Event の作成、名前変更、削除ができます。主画面の Event select では、今回の抽選をどの Event に保存するかだけを選びます。

## プロジェクト構成

```text
WeightedSelection_Tool/
├─ main.py              # Web アプリ起動用エントリ
├─ web_app.py           # ローカル HTTP サーバー / 静的ファイル配信
├─ api.py               # API、抽選状態、Session / Event 操作
├─ core.py              # CSV 読み込み、DB、重み計算などの共通ロジック
├─ static/
│  ├─ index.html        # 画面レイアウト
│  ├─ styles.css        # UI スタイル
│  └─ app.js            # フロントエンド操作
├─ requirements.txt     # Python 依存パッケージ
├─ README.md            # このファイル
└─ vrc_raffle.db        # ローカルDB。実行時に自動作成
```

`vrc_raffle.db`、`.venv`、`dist/`、`build/` などは Git 管理対象外です。

## 必要環境

- Python 3.9 以上

## セットアップ

プロジェクトフォルダで以下を実行します。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Windows PowerShell の場合:

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

起動後、ブラウザで `http://127.0.0.1:8765/` 付近のローカル URL が開きます。ポートが使用中の場合は自動で次の空きポートを使います。

## 画面の使い方

- 「CSV / Excel を選択して読み込み」を押すとファイル選択画面が開き、選択後すぐに読み込みます。
- 列設定タブで、抽選ID列を左クリックします。
- その後、結果に表示したい列を左クリックします。
- 未選択列を右クリックすると、特別条件の値と倍率を設定できます。
- 応募者の行をクリックすると、その行を抽選対象から除外できます。除外行は赤色になります。
- 抽選後は抽選結果タブへ移動し、今回の当選者を上部に表示します。
- 保存済みセッションは抽選結果タブで表示、削除できます。
- ユーザー一覧タブでは、Event select で対象 Event を切り替え、参加回数、当選回数、現在の重みと確率を確認できます。

## よくあるエラー

### `ModuleNotFoundError: No module named 'pandas'`

依存パッケージがインストールされていません。

```bash
pip install -r requirements.txt
```

### `ModuleNotFoundError: No module named 'thefuzz'`

同じく依存パッケージ不足です。

```bash
pip install -r requirements.txt
```

### 仮想環境の Python が壊れている

既存の `.venv` が存在しない Python に紐付いている場合は、仮想環境を作り直してください。

```bash
rm -rf .venv
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 main.py
```

Windows の場合は `rm -rf .venv` の代わりに `Remove-Item -Recurse -Force .venv` を使ってください。
