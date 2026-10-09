# aws-bedrock-info

Amazon Bedrockで利用可能なモデルの一覧と、各モデルのライフサイクル状態（ACTIVE / LEGACY）・EOL日付を
毎日チェックし、GitHub PagesにHTMLレポートとして公開するための小さなツール一式。

## 構成

- `scripts/list_bedrock_models.py` — 指定リージョンのBedrockモデル一覧を取得し、テーブル/CSV/HTMLで出力するスクリプト
- `.github/workflows/update-models.yml` — 毎日実行してGitHub Pagesにデプロイするworkflow
- `iam/` — GitHub Actions用IAMロールのポリシー雛形（OIDC信頼ポリシー＋最小権限ポリシー）

## ローカルでの実行

```bash
# AWS CLIの認証情報が設定済みであること (aws sts get-caller-identity で確認)
./scripts/list_bedrock_models.py --region ap-northeast-1 --region us-east-1
./scripts/list_bedrock_models.py --region ap-northeast-1 --html site/index.html
./scripts/list_bedrock_models.py --region ap-northeast-1 --csv models.csv
./scripts/list_bedrock_models.py --region ap-northeast-1 --no-eol   # EOL日付のスクレイピングをスキップ（高速）
```

Python標準ライブラリのみで動作します（追加の pip install は不要）。AWS CLIがインストール済みで
認証情報が設定されていることが前提です。

### 注意点

- `status`（ACTIVE/LEGACY）はBedrockの公式API（`list-foundation-models` / `get-foundation-model`）から
  取得しています。信頼できる値です。
- EOL日付はBedrockの公式APIでは返されません（APIスキーマ上は `endOfLifeTime` フィールドが定義されて
  いますが、実際には値が入っていません）。そのためこのスクリプトは、LEGACYになっているモデルについて
  [AWSドキュメントのモデルカードページ](https://docs.aws.amazon.com/bedrock/latest/userguide/model-cards.html)
  をスクレイピングしてEOL日付を補完しています。非公式な手段のため、AWSがページ構造を変更すると
  EOL日付の取得だけ失敗する可能性があります（その場合も `status` 列自体は影響を受けません）。

## GitHub Actions + GitHub Pagesのセットアップ手順

### 1. リポジトリを作成してpush

このディレクトリの内容をGitHubの新規リポジトリ（public — 無料のGitHub Pagesを使う場合はpublicが必要）
にpushしてください。

```bash
git push -u origin main
```

### 2. GitHub Pagesを有効化

リポジトリの Settings → Pages → Build and deployment → Source を **「GitHub Actions」** に設定してください。

### 3. AWS側にIAMロールを作成（OIDC、長期キー不要）

`iam/trust-policy.json` 内の `<AWS_ACCOUNT_ID>` を、GitHub Actions用のOIDCプロバイダー
（`arn:aws:iam::<AWS_ACCOUNT_ID>:oidc-provider/token.actions.githubusercontent.com`）が存在する
自分のAWSアカウントIDに書き換えてください。OIDCプロバイダーが未作成の場合は先に作成します。

```bash
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1
```

`iam/trust-policy.json` は `repository_id` / `repository_owner_id`（リポジトリ・所有者の数字ID）で
信頼先を縛っています。リポジトリ名やOrg名での一致（`sub: "repo:OWNER/REPO:*"`）と違い、リポジトリや
アカウントが将来リネーム・削除されて同じ名前を他人に使われても誤って一致しないという利点があります。
フォークして使う場合は、以下で自分のリポジトリの数字IDを確認し書き換えてください。

```bash
curl -s https://api.github.com/repos/<OWNER>/<REPO> | grep -E '"id"|"login"|"owner"'
```

アカウントIDやこれらの数字IDは機密情報ではありませんが、雛形のまま公開リポジトリに残すと誰の設定か
分かってしまうため、使う前に自分の値へ置き換えることを推奨します。

```bash
aws iam create-role \
  --role-name bedrock-model-status-reader \
  --assume-role-policy-document file://iam/trust-policy.json \
  --description "GitHub Actions (timerags/aws-bedrock-info) read-only access to list Bedrock foundation models"

aws iam put-role-policy \
  --role-name bedrock-model-status-reader \
  --policy-name BedrockListModelsReadOnly \
  --policy-document file://iam/permissions-policy.json
```

作成したロールのARN（`aws iam get-role --role-name bedrock-model-status-reader --query Role.Arn --output text`）
を控えておきます。

### 4. GitHubリポジトリにロールARNを登録

リポジトリの Settings → Secrets and variables → Actions → **Variables** タブで、
`AWS_ROLE_ARN` という名前の Repository variable を作成し、値に上記のロールARNを設定します。
（秘密情報ではない値なので Secrets ではなく Variables に置いています）

### 5. 動作確認

Actions タブから `Update Bedrock model status` workflowを手動実行（workflow_dispatch）し、成功すれば
`https://<OWNER>.github.io/<REPO>/` でレポートが閲覧できます。以降は毎日 00:00 UTC（09:00 JST）に自動実行されます。
