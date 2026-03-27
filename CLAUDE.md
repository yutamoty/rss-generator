# RSS Generator

RSSフィードを提供していないWebサイトをスクレイピングし、LLMで記事情報を構造化抽出してRSS化する個人用システム。

## アーキテクチャ

```
[URL管理 - Discord Bot (Slash Commands)]
  ユーザー (Discord)
    → Discord Interactions Endpoint
      → Lambda (discord-handler): コマンド解析・レスポンス
        → Lambda (manage): DynamoDB CRUD
          → DynamoDB (sites テーブル)

  Slash Commands:
    /add <url> [name]  - URL登録
    /list              - 登録URL一覧
    /delete <site_id>  - URL削除
    /feeds             - フィードURL一覧

[RSS生成 - 定期実行]
  EventBridge (cron: 1日1回)
    → Step Functions (Standard)
      → Lambda (get-sites): DynamoDB からサイト一覧取得
      → Map (並列): サイトごとに
        → Lambda (generate-feed):
          → Jina Reader API (r.jina.ai) でMarkdown化
          → コンテンツハッシュで差分検知（変化なし→スキップ）
          → Bedrock (Amazon Nova 2 Lite) で記事一覧を構造化抽出
          → Atom XML 生成 → S3 に PUT

[RSS配信]
  S3 → CloudFront (TTL: 6時間) → 外部RSSリーダー
```

## ディレクトリ構成

```
.
├── CLAUDE.md
├── template.yaml              # SAM テンプレート
├── samconfig.toml              # SAM デプロイ設定
├── statemachine/
│   └── definition.asl.json     # Step Functions ステートマシン定義
├── functions/
│   ├── discord_handler/        # Discord Interactions Endpoint Lambda
│   │   ├── app.py
│   │   └── requirements.txt
│   ├── manage/                 # URL管理 Lambda (CRUD)
│   │   ├── app.py
│   │   └── requirements.txt
│   ├── get_sites/              # サイト一覧取得 Lambda
│   │   ├── app.py
│   │   └── requirements.txt
│   └── generate_feed/          # RSS生成 Lambda
│       ├── app.py
│       └── requirements.txt
└── .github/
    └── workflows/
        └── deploy.yml          # GitHub Actions デプロイ
```

## 技術スタック

- IaC: SAM (AWS Serverless Application Model)
- Lambda ランタイム: Python 3.12
- フィード形式: Atom
- オーケストレーション: Step Functions (Standard)
- シークレット管理: Systems Manager パラメータストア
- デプロイ: GitHub Actions + OIDC連携（長期IAMキー不使用）

## AWSサービス

| サービス | 用途 |
|---------|------|
| API Gateway (REST) | Discord Interactions Endpoint |
| Lambda (discord-handler) | Discord Slash Commands の受信・レスポンス |
| Lambda (manage) | URL の CRUD |
| Lambda (get-sites) | DynamoDB からサイト一覧取得 |
| Lambda (generate-feed) | Jina Reader → Bedrock → Atom XML → S3 |
| Step Functions (Standard) | RSS生成オーケストレーション。Map で並列実行 |
| DynamoDB | サイト情報の保存 |
| EventBridge | Step Functions の定期トリガー（1日1回） |
| Bedrock (Amazon Nova 2 Lite) | Markdown→記事一覧の構造化抽出 |
| S3 | Atom XML の配置 |
| CloudFront | Atom フィードの外部配信（キャッシュTTL: 6時間） |

## 外部サービス

| サービス | 用途 |
|---------|------|
| Jina Reader API (r.jina.ai) | URLをMarkdown化。JSレンダリング対応。無料枠: 1,000万トークン、APIキーなし20 RPM |

## DynamoDB テーブル設計

```
sites テーブル
  PK: site_id (ULID)
  url: string          - 対象URL
  name: string         - サイト名
  feed_path: string    - S3上のフィードパス (feeds/xxx.xml)
  last_hash: string    - 差分検知用コンテンツハッシュ
  created_at: string   - ISO8601
  updated_at: string   - ISO8601
```

## Discord Bot (Slash Commands)

```
/add <url> [name]   - URL登録（name省略時はURLから自動生成）
/list               - 登録URL一覧表示
/delete <site_id>   - URL削除（S3のXMLも削除）
/feeds              - 配信中のフィードURL一覧
```

Discord Interactions Endpoint を API Gateway + Lambda (discord-handler) で受ける。
discord-handler は署名検証後、Lambda (manage) を invoke して CRUD を実行。

## Step Functions 処理フロー

1. **Lambda (get-sites)**: DynamoDB から全サイト取得
2. **Map (並列)**: サイトごとに Lambda (generate-feed) を実行
   - Jina Reader API で Markdown 取得 (`GET https://r.jina.ai/{url}`, `Accept: application/json`)
   - コンテンツハッシュと `last_hash` を比較 → 変化なし → スキップ
   - Bedrock (Amazon Nova 2 Lite) で記事一覧を構造化抽出 (title, link, date, summary)
   - Atom XML 生成 → S3 PUT
   - DynamoDB の `last_hash` を更新
   - **エラー時: 無視して次のサイトへ（次回実行で再取得）**

## セキュリティ

- ローカルに長期IAMアクセスキーを持たない
- Discord Bot の署名検証でリクエストの正当性を確認
- Lambda の IAM ロールで最小権限（DynamoDB, S3, Bedrock のみ）
- Discord Bot Token・Public Key は Systems Manager パラメータストアに保存
- GitHub Actions は OIDC 連携で一時クレデンシャルを使用
- CloudFront 経由の配信は公開（RSSリーダーからの購読用）

## デプロイ

```bash
# ローカルからのデプロイ（初回セットアップ時）
sam build && sam deploy --guided

# 通常は GitHub Actions による自動デプロイ
# main ブランチへの push で sam build && sam deploy が実行される
```

## 開発ルール

- コードの変更は PR 経由で main にマージする
- main への push で GitHub Actions が自動デプロイを実行する
- Lambda のコードは各 `functions/` サブディレクトリに配置する
- SAM テンプレートの変更時は `sam validate` で検証してからコミットする
- シークレット（APIキー等）は Systems Manager パラメータストアに保存し、コードにハードコードしない
