# v2: ヘッドレスブラウザによる記事抽出への移行計画

## 背景・課題

- `generate-feed` は現在、[Jina Reader API](https://jina.ai/)(`r.jina.ai`)でページをMarkdown化してから、Bedrockで記事一覧を構造化抽出している。
- Jina Reader の無料枠(APIキー登録時の1,000万トークン)を使い切った。無料枠は**リセットされない一回限りの付与**であり、登録から比較的早く枯渇した。
- APIキーなしの匿名アクセス(20 RPM)への切り戻しも検討したが、そもそも現状すでに無料枠(キーなし相当)を使っている状態であり、これ以上の逃げ道がない。
- 対象サイトの多くはJavaScriptレンダリングが必要(マンガ/小説の話数一覧サイト等)なため、「Jina失敗時に生HTMLを直接取得するフォールバック」は実用性が低いと判断し、不採用とした(単純なHTML取得ではJS必須サイトのコンテンツが取得できないため)。
- 個人プロダクトであり、追加コストは極力かけたくない。外部有料APIへの移行は避けたい。

## 方針

**外部の無料枠(他人の財布)に依存するのをやめ、Lambda上でヘッドレスブラウザ(Playwright + Chromium)を自前で動かしてレンダリングする方式に置き換える。**

- コストはAWS Lambdaの実行時間分のみ。1日1回・数十サイト程度の規模であれば、月数十〜百円程度に収まる想定。
- 第三者のレート制限・トークン枯渇から完全に独立できる。

### 影響範囲

- 変更するのは `functions/generate_feed/` の実装のみ。
- `discord_handler` / `manage` は**無変更**。DiscordのSlashコマンド(`/add` `/list` `/delete` `/feeds` `/generate`)はv2移行後も見た目上何も変わらない。
- Step Functions のステートマシン定義(`statemachine/definition.asl.json`)も基本的に無変更(`GenerateFeedFunctionArn` の参照先が新実装に変わるだけ)。

### 技術的な変更点

- Chromiumバイナリが重く(展開後250MB超)、現在のzipデプロイのLambdaサイズ上限に収まらないため、`generate-feed` 関数を **zipベースからコンテナイメージベース(ECR経由、`PackageType: Image`)に変更**する必要がある。
- 必要な変更:
  - `functions/generate_feed/Dockerfile` の追加(Playwright + Chromium同梱)
  - `template.yaml` の `GenerateFeedFunction` を `PackageType: Image` に変更、`Metadata` でDockerfileの場所を指定
  - GitHub Actions のデプロイフロー(`.github/workflows/deploy.yml`)に Docker build & ECR push のステップを追加
  - コールドスタートが数秒伸びる見込み(現状 `Timeout: 120秒` には収まる範囲)

## 開発・検証の進め方

### ブランチ運用

- 作業ブランチ: `feature/v2-headless-render`(mainから分岐)
- **本番の `main` ブランチにはマージするまで一切影響しない**(`main` への push でのみ GitHub Actions が本番デプロイを実行する運用のため)。

### 検証環境の分離

本番のDynamoDB(サイト情報)・S3(配信中フィード)・CloudFrontを一切汚さずに検証するため、**同じ `template.yaml` を使い、スタック名だけ変えた別スタック**にデプロイする。

```bash
sam deploy --stack-name rss-generator-v2-dev \
  --parameter-overrides FeedCustomDomainName=none FeedAcmCertificateArn=none \
  --guided
```

> **注意**: `FeedCustomDomainName` / `FeedAcmCertificateArn` パラメータのデフォルト値は `/rss-generator/...` という固定パスで、スタック名に連動しない。上書きしないと本番と同じカスタムドメインを奪い合って衝突するため、`none` を明示的に指定すること。

### 実データでの比較検証

本番の `sites` テーブルの中身(URL一覧)をdev環境にコピーし、同じ`site_id`で新旧の出力を見比べられるようにする。

```bash
python3 <<'EOF'
import boto3

dynamodb = boto3.resource("dynamodb")
src = dynamodb.Table("rss-generator-sites")
dst = dynamodb.Table("rss-generator-v2-dev-sites")

response = src.scan()
items = response.get("Items", [])

for item in items:
    item = dict(item)
    item["last_hash"] = ""  # devで必ず再生成させる
    dst.put_item(Item=item)

print(f"Copied {len(items)} sites into dev table.")
EOF
```

devのStep Functionsを手動実行:

```bash
aws stepfunctions start-execution --state-machine-arn <dev-StateMachineArn>
```

`site_id` はコピーで維持されるため、同じIDのフィードを両方のCloudFrontドメインで直接比較できる。

```
本番: https://<prod-domain>/feeds/<site_id>.xml
dev : https://<dev-domain>/feeds/<site_id>.xml
```

JSレンダリングが必要なサイト(マンガ話数一覧など)で、記事の件数・タイトル・日付が正しく抽出できているかを重点的に確認する。

### Discordとの関係

- 検証フェーズでは**Discordを一切介さない**。`generate-feed` を直接 `aws lambda invoke` してテストすれば十分(Discord Bot登録やSlashコマンド登録は不要)。

  ```bash
  aws lambda invoke \
    --function-name rss-generator-v2-dev-generate-feed \
    --payload '{"site_id":"test","url":"https://example.com/","name":"test","feed_path":"feeds/test.xml","last_hash":""}' \
    --cli-binary-format raw-in-base64-out \
    response.json
  ```

- 本番マージ後も、Discordコマンドのディスパッチ層は無変更のまま新しい `generate-feed` を呼び出すだけになる。

### 検証環境の後片付け

検証が済んだら丸ごと削除できる。

```bash
aws cloudformation delete-stack --stack-name rss-generator-v2-dev
```

## 本番への移行

1. `feature/v2-headless-render` の実装が完了し、dev環境での比較検証で問題ないことを確認する。
2. PRを作成し、mainにマージ(レビュー後、GitHub Actionsが自動で本番デプロイを実行)。
3. 問題が発生した場合は `git revert` で即座に旧実装(Jina Reader方式)へ戻せる。
4. 必要に応じて、環境変数等で新旧レンダリング方式を切り替えられるようにし、一部サイトだけ先行して新方式に切り替える段階移行も検討可能(必須ではない)。

## 未着手のタスク(次のステップ)

- [ ] `functions/generate_feed/Dockerfile` の作成(Playwright + Chromiumベースイメージ)
- [ ] `functions/generate_feed/app.py` のレンダリング部分をPlaywrightベースに書き換え
- [ ] `template.yaml` の `GenerateFeedFunction` を `PackageType: Image` に変更
- [ ] `.github/workflows/deploy.yml` にDocker build & ECR pushステップを追加
- [ ] devスタックへのデプロイと、本番サイト一覧を使った比較検証
- [ ] 問題なければmainへのPRマージ、本番切り替え
