# Google form抽選ツール

Google Form などから出力した CSV / Excel ファイルを読み込み、応募者の列設定、除外、特別条件、履歴にもとづく重み付けを行って抽選するローカル Web アプリです。

データは外部サーバーへ送信されません。抽選履歴、応募記録、当選結果、参加者履歴はローカル SQLite データベース `vrc_raffle.db` に保存されます。

## 主な機能

- CSV / Excel ファイルの読み込み
- 抽選ID列と結果表示列のクリック指定
- 行クリックによる応募者除外
- 回答値に応じた特別条件倍率
- 均等抽選、線形加重、指数加重
- 当選後の重みリセット
- Event ごとの抽選履歴管理
- 過去 Session の結果表示と削除
- Event 別ユーザー一覧
- VRC / X 情報による既存参加者の照合
- ローカル Web UI

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

終了するときは画面右上の「終了」または左側の「アプリを終了」を押してください。

## 基本操作

1. 「CSV / Excel を選択して読み込み」を押します。
2. ファイルを選択すると自動で読み込まれます。
3. 列設定で、抽選に使う ID 列を左クリックします。
4. 結果に表示したい列を左クリックします。
5. 未選択列を右クリックすると、特別条件を設定できます。
6. 応募者の行をクリックすると、その応募者を抽選対象から除外できます。
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

### 線形加重 - (n + 1)^2

`n` は「前回当選後から現在までの参加累計」です。  
当選すると `n` は 0 に戻り、次回は最低重みから再スタートします。

```text
weight = (n + 1)^2
```

### 指数加重 - 2^n

`n` が増えるほど強く優先します。  
当選すると `n` は 0 に戻ります。

```text
weight = 2^n
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
- Event を削除すると、その Event に紐づく Session は `default` に戻ります。

## 抽選結果

抽選結果タブでは、以下を確認できます。

- 今回または選択中 Session の当選者
- 計算方法の簡単な説明
- 以前の抽選結果一覧

以前の抽選結果一覧では、Session の表示と削除ができます。Session 名変更機能はありません。

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

## 保存されるデータ

SQLite データベース `vrc_raffle.db` に保存されます。

主なテーブル:

- `events`: Event 情報
- `participants`: 参加者履歴
- `raffle_sessions`: 抽選 Session
- `submission_records`: Session ごとの応募記録
- `raffle_results`: 当選結果

## プロジェクト構成

```text
WeightedSelectionTool/
├─ main.py              # 起動エントリ
├─ web_app.py           # ローカル HTTP サーバー / 静的ファイル配信
├─ api.py               # API、抽選状態、Session / Event 操作
├─ core.py              # DB 初期化、CSV 読み込み、重み計算
├─ static/
│  ├─ index.html        # HTML
│  ├─ styles.css        # CSS
│  └─ app.js            # フロントエンド JS
├─ requirements.txt
├─ README.md
├─ 用户手册.md
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
