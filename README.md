# RSS Generator

RSS フィードを提供していない Web サイトをスクレイピングし、LLM で記事情報を構造化抽出して Atom フィードとして配信する個人用システム。

## 前提条件

以下のツール・アカウントが必要です。

| 項目 | 用途 |
|------|------|
| [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) | AWS リソースの操作（初期セットアップ時） |
| [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html) | ローカル開発時のビルド・検証（デプロイは GitHub Actions で実行） |
| Python 3.12 | Lambda ランタイム |
| AWS アカウント | 各種 AWS サービスの利用 |
| Discord アカウント | Bot の作成・運用 |
| GitHub アカウント | CI/CD (GitHub Actions) の利用 |

## セットアップ

### 1. Discord Bot の作成

1. [Discord Developer Portal](https://discord.com/developers/applications) で新しいアプリケーションを作成
2. Bot の **Application ID**、**Public Key**、**Bot Token** を控える
3. OAuth2 > URL Generator で `applications.commands` スコープを選択し、生成された URL でサーバーに Bot を招待

### 2. AWS Systems Manager パラメータストアの設定

以下のパラメータを **SecureString** で登録してください。

```bash
# Discord Bot Token
aws ssm put-parameter \
  --name "/rss-generator/discord-bot-token" \
  --type SecureString \
  --value "<YOUR_DISCORD_BOT_TOKEN>"

# Discord Public Key
aws ssm put-parameter \
  --name "/rss-generator/discord-public-key" \
  --type SecureString \
  --value "<YOUR_DISCORD_PUBLIC_KEY>"

# Jina Reader API Key (https://jina.ai/ で取得)
aws ssm put-parameter \
  --name "/rss-generator/jina-api-key" \
  --type SecureString \
  --value "<YOUR_JINA_API_KEY>"
```

> パラメータ名は `template.yaml` 内の参照と一致させてください。

### 3. Amazon Bedrock モデルアクセスの有効化

1. AWS コンソール > Amazon Bedrock > Model access を開く
2. **Amazon Nova 2 Lite** (amazon.nova-lite-v2:0) のアクセスをリクエストし、有効化する

### 4. GitHub Actions の設定（CI/CD）

OIDC 連携により長期 IAM アクセスキーは不要です。デプロイ（初回含む）は GitHub Actions で実行します。

#### 4a. IAM OIDC プロバイダーの作成

```bash
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1
```

#### 4b. GitHub Actions 用 IAM ロールの作成

以下の信頼ポリシーで IAM ロールを作成し、SAM デプロイに必要な権限を付与してください。

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::<AWS_ACCOUNT_ID>:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
        },
        "StringLike": {
          "token.actions.githubusercontent.com:sub": "repo:<GITHUB_OWNER>/<GITHUB_REPO>:ref:refs/heads/main"
        }
      }
    }
  ]
}
```

#### 4c. GitHub リポジトリの Secrets 設定

| Secret 名 | 値 |
|------------|-----|
| `AWS_ROLE_ARN` | 上記で作成した IAM ロールの ARN |

### 5. `samconfig.toml` の作成

GitHub Actions でのデプロイに必要な設定ファイルをリポジトリに含めます。以下を参考に作成してください。

```toml
version = 0.1

[default.deploy.parameters]
stack_name = "rss-generator"
region = "us-east-1"               # Bedrock が利用可能なリージョンを指定
capabilities = "CAPABILITY_IAM"
resolve_s3 = true
```

### 6. 初回デプロイ

`samconfig.toml` を含めた状態で `main` ブランチに push すると、GitHub Actions が `sam build && sam deploy` を自動実行します。以降も `main` への push で自動デプロイが行われます。

### 7. Discord Interactions Endpoint の設定

1. 初回デプロイ後、CloudFormation スタックの Outputs から **API Gateway のエンドポイント URL** を取得
2. Discord Developer Portal > アプリケーション > General Information > **Interactions Endpoint URL** に貼り付けて **Save Changes** をクリック
3. Discord が自動的に PING を送信し、Lambda が正しく応答すれば保存される（失敗時はエラーが表示される）

### 8. Slash Commands の登録

ギルドコマンド（即時反映）として登録します。**サーバー ID** は Discord でサーバーを右クリック > 「サーバー ID をコピー」で取得できます。

```bash
APP_ID="<YOUR_APPLICATION_ID>"
BOT_TOKEN="<YOUR_BOT_TOKEN>"
GUILD_ID="<YOUR_GUILD_ID>"

curl -X PUT "https://discord.com/api/v10/applications/${APP_ID}/guilds/${GUILD_ID}/commands" \
  -H "Authorization: Bot ${BOT_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '[
    {
      "name": "add",
      "description": "Register a URL for RSS feed generation",
      "options": [
        {"name": "url", "description": "Target URL", "type": 3, "required": true},
        {"name": "name", "description": "Site name", "type": 3, "required": false}
      ]
    },
    {
      "name": "list",
      "description": "List registered URLs"
    },
    {
      "name": "delete",
      "description": "Delete a registered URL",
      "options": [
        {"name": "site_id", "description": "Site ID to delete", "type": 3, "required": true}
      ]
    },
    {
      "name": "feeds",
      "description": "List feed URLs"
    }
  ]'
```

## 使い方

### Discord Slash Commands

| コマンド | 説明 |
|----------|------|
| `/add <url> [name]` | RSS 化したいサイトの URL を登録（name 省略時は URL から自動生成） |
| `/list` | 登録済み URL の一覧を表示 |
| `/delete <site_id>` | 登録済み URL を削除（S3 上の XML も削除） |
| `/feeds` | 配信中のフィード URL 一覧を表示 |

### RSS フィードの購読

`/feeds` コマンドで表示される CloudFront URL を RSS リーダーに登録してください。フィードは Atom 形式で配信されます。

## アーキテクチャ

```
[URL管理 - Discord Bot]
  ユーザー (Discord)
    → API Gateway → Lambda (discord-handler) → Lambda (manage) → DynamoDB

[RSS生成 - 定期実行 (1日1回)]
  EventBridge → Step Functions → Map (並列)
    → Lambda (generate-feed)
      → Jina Reader API (Markdown化)
      → Amazon Bedrock (記事の構造化抽出)
      → S3 (Atom XML 保存)

[RSS配信]
  S3 → CloudFront (TTL: 6時間) → 外部RSSリーダー
```

## ディレクトリ構成

```
.
├── CLAUDE.md
├── README.md
├── template.yaml               # SAM テンプレート
├── samconfig.toml               # SAM デプロイ設定
├── statemachine/
│   └── definition.asl.json      # Step Functions 定義
├── functions/
│   ├── discord_handler/         # Discord Interactions Endpoint
│   ├── manage/                  # URL管理 CRUD
│   ├── get_sites/               # サイト一覧取得
│   └── generate_feed/           # RSS生成
└── .github/
    └── workflows/
        └── deploy.yml           # GitHub Actions デプロイ
```

## 利用する外部サービス

| サービス | 用途 | 備考 |
|----------|------|------|
| [Jina Reader API](https://jina.ai/reader/) | URL を Markdown に変換 | 無料枠: 1,000 万トークン、API キーなし 20 RPM |
| Amazon Bedrock (Amazon Nova 2 Lite) | Markdown から記事情報を構造化抽出 | AWS アカウントでモデルアクセスの有効化が必要 |

## コスト目安

主な課金対象:

- **Lambda**: 実行回数・時間に応じた従量課金（無料枠あり）
- **DynamoDB**: 読み書きキャパシティ（オンデマンドの場合は従量課金）
- **S3**: ストレージ + リクエスト数
- **CloudFront**: データ転送量（個人利用なら無料枠内に収まる可能性あり）
- **Step Functions**: 状態遷移回数（Standard ワークフロー）
- **Bedrock**: 入出力トークン数に応じた従量課金

個人利用（数サイト〜数十サイト規模）であれば、ほとんどのサービスが無料枠内または月数ドル程度に収まる見込みです。

## ローカル開発

```bash
# テンプレートの検証
sam validate

# ローカルビルド
sam build

# デプロイ（samconfig.toml 設定済みの場合）
sam build && sam deploy
```

## ライセンス

[MIT License](LICENSE)
