# 来たことある？（kitakoto）

Androidスマホで「今いる場所に過去来たことがあるか、いつ来たか」を、Googleマップのタイムライン履歴から調べるPWA。

## 方針（決定済み）
- **PWA＋静的ホスティング（GitHub Pages想定）**。サーバー処理なし・ビルド工程なし・外部ライブラリなし。
- **位置履歴は端末の外に出さない**。ユーザーが Timeline.json をアプリ内で選択 → ブラウザで解析 → IndexedDB に保存。公開リポジトリにはアプリ本体だけを置く。
- **オフラインで動くこと**（Service Workerでアプリ本体をキャッシュ）。海外旅行先での利用を想定。
- 対象はAndroid版Chrome。Termux案は検討の上で不採用。
- 操作は簡単に：アプリを開いたら即、現在地で検索して一覧を出す。

## ファイル構成
- `index.html` … UI・ロジックすべて（CSS/JSインライン）
- `sw.js` … キャッシュファースト。**アプリを変更したら `VERSION` を上げる**
- `manifest.webmanifest`, `icon-192.png`, `icon-512.png`

## データ仕様
入力として4形式に対応（`iterRecords()`）:
1. Android端末エクスポートの `Timeline.json`：`semanticSegments[]`
   - `visit.topCandidate.placeLocation.latLng`（"35.6812°, 139.7671°" 形式の文字列）、`semanticType`、`startTime`/`endTime` → kind=`visit`
   - `timelinePath[].point` / `.time` → kind=`path`（通過）
   - `rawSignals`、`userLocationProfile` は未使用
2. 旧Takeout Semantic Location History：`timelineObjects[].placeVisit`（`latitudeE7`、`location.name` をラベルに）
3. 旧Takeout `Records.json`：`locations[]` → path
4. このアプリ自身が書き出したバックアップJSON（`{app:"kitakoto", records:[...]}`、`backupPayload()`）：設定ダイアログの「バックアップを書き出す」で作れる。読み込み欄にそのまま渡せば戻せる

IndexedDB `wherewasi` / store `rec`
- keyPath `id` = `kind|t|lat(6桁)|lon(6桁)`（再読込しても重複しない）
- index `cell` = `floor(lat*100)_floor(lon*100)`（約1.1km格子）。検索時は半径のバウンディングボックスが掛かるセルを引いてから haversine で絞り込む

集計：日付ごと（`t` の先頭10文字＝現地日付。末尾Zのみ端末ローカルに変換）に、滞在（最長の滞在時間とラベル）か通過かを判定し、最寄り距離と座標を保持する。

## UI
- 藍色の背景に真鍮色のアクセント。前回の訪問日を回転した「スタンプ」で大きく表示するのが唯一の強い演出。初めての場所は浅葱色で「はじめて」。
- 半径 100m / 200m / 500m / 2km（localStorage に保存）
- 年ごとの一覧。行をタップすると展開し、その日の「タイムライン / Google フォト / Amazon フォト / 地点」を開ける。前回の日付分はスタンプの下にも常時表示（`dayLinks()`）
  - 日付指定の公式ディープリンクは無く、**実機で未検証**。Google フォトは日付文字列の検索、タイムラインは非公式の `maps/timeline?pb=!1m2!1m1!1s日付`、Amazon フォトは日付をコピーしてトップを開くだけ
- 設定ダイアログ：件数表示、永続保存の状態表示（`refreshPersist()`）、読み込み（複数ファイル可）、バックアップ書き出し（`exportBackup()`）、座標を手入力して検索、全削除
  - **端末のストレージが逼迫するとブラウザがIndexedDBを勝手に消すことがある**問題への対策として、`navigator.storage.persist()` を起動時と設定を開くたびにリクエストし、可否を表示する。Androidではホーム画面に追加すると通りやすい。これでも消える可能性はゼロではないので、バックアップ書き出しを別途用意した
  - 全削除は既存の意図的な操作（`wipeAll()`）。バックアップはその前に取っておくためのもの

## 状態
- ダミーデータで構文チェック・ロジック・表示は確認済み
- **実機の Timeline.json ではまだ未検証**。形式差異があれば `iterRecords()` を直す
- 経度±180度をまたぐ場合は未対応

## 作業時の注意
- ビルドツールやフレームワークを入れない（単一HTMLのまま保つ）
- ローカル確認は `python3 -m http.server` で。Geolocation は localhost か HTTPS でのみ動く
- 変更時は `sw.js` の VERSION を上げる
