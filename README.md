# WeightedSelection Tool

VRC イベント向けの抽選管理ツールです。  
CSV / Excel ファイルを読み込んで応募者情報を自動認識し、抽選前に各レコードを編集してから、選択した確率モードで抽選できます。

アプリは Tkinter 製のデスクトップアプリで、抽選セッション、応募レコード、当選結果、参加者履歴はローカル SQLite データベース `vrc_raffle.db` に保存されます。

## 主な機能

- CSV / Excel ファイルの読み込み
- 応募レコードの自動列認識
- 読み込み後のレコード個別編集、削除
- 均等抽選、線形加重抽選、指数加重抽選
- 抽選結果のセッション保存
- セッションごとの結果表示、編集、削除
- 参加者データベース管理
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
2. 「抽選」タブで CSV / Excel ファイルを選択します。
3. 必要に応じて「CSVプレビュー」で列の認識状況を確認します。
4. 「CSVを読込・自動認識」を押します。
5. 右側の「抽選記録」に読み込まれた応募者一覧が表示されます。
6. 必要に応じて、対象行を選択して「記録編集」または「記録削除」を行います。
7. 抽選人数と確率モードを選びます。
8. 「抽選開始」を押すと、すぐに抽選結果が表示されます。
9. 抽選内容は新しい Session として保存されます。
10. 保存後は「記録」タブから該当 Session を選び、Session 情報や抽選結果を編集できます。

抽選完了後、画面下部に `Session #ID` が表示されます。  
この ID を使うと、「記録」タブで該当 Session を探しやすくなります。

## 抽選モード

### 均等抽選

全員を同じ確率で抽選します。

### 線形加重

参加回数に応じて、ゆるやかに確率を上げます。

```text
weight = 1 + 参加回数 / 総人数
```

### 指数加重

参加回数が増えるほど、確率を強く上げます。

```text
weight = 2 ^ 参加回数
```

右側の「現在確率」は、読み込み後またはモード変更時に自動計算されます。

## セッション保存

「抽選開始」を押すと、以下の内容が保存されます。

- `raffle_sessions`
  - Session 名
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

## Session の編集

「記録」タブでは、保存済み Session を操作できます。

- 「編集」
  - Session 名と備考を編集できます。
- 「結果を表示」
  - その Session の当選結果を表示します。
  - 当選結果の各行を編集、削除できます。
- 「削除」
  - Session と関連する抽選結果、応募記録を削除します。

## プロジェクト構成

```text
WeightedSelection_Tool/
├─ main.py              # メインアプリ
├─ requirements.txt     # Python 依存パッケージ
├─ README.md            # このファイル
├─ .gitignore           # ローカル環境、DB、ビルド成果物の除外設定
└─ vrc_raffle.db        # ローカルDB。実行時に自動作成
```

`vrc_raffle.db`、`.venv`、`dist/`、`build/` などは Git 管理対象外です。

## 必要環境

- Windows
- Python 3.9 以上
- Tkinter が使える Python

Windows 版の公式 Python には通常 Tkinter が含まれています。

## セットアップ

プロジェクトフォルダで以下を実行します。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

PowerShell で仮想環境の有効化がブロックされる場合は、次を実行してください。

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

## 起動方法

```powershell
.\.venv\Scripts\python.exe main.py
```

または仮想環境を有効化してから起動します。

```powershell
.\.venv\Scripts\Activate.ps1
python main.py
```

## Free / Pro 設定

`main.py` は `_build_config.py` が存在する場合、その中の `FREE_BUILD` を読み込みます。

Free 版として起動する場合:

```powershell
'FREE_BUILD = True' | Set-Content _build_config.py
```

Pro 版として起動する場合:

```powershell
'FREE_BUILD = False' | Set-Content _build_config.py
```

`_build_config.py` がない場合は、開発用のデフォルトとして Pro 版で起動します。

## よくあるエラー

### `ModuleNotFoundError: No module named 'thefuzz'`

依存パッケージがインストールされていません。

```powershell
pip install -r requirements.txt
```

### `.venv\Scripts\python.exe` 実行時に `No Python at ...` と表示される

既存の `.venv` が、存在しない Python に紐付いています。仮想環境を作り直してください。

```powershell
Remove-Item -Recurse -Force .venv
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

### `python` コマンドが見つからない

Python がインストールされていない、または PATH に追加されていません。  
Python をインストールする際は `Add python.exe to PATH` を有効にしてください。

## exe 化

PyInstaller を使って exe 化できます。

```powershell
pip install pyinstaller
pyinstaller --onefile --windowed --name "VRC抽選ツール" main.py
```

生成された exe は `dist/` に出力されます。
