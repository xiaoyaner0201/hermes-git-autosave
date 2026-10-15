[English](README.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md)

# git-autosave

Hermes のディレクトリ単位ファイルシステムチェックポイント（`checkpoints.enabled`）に代わる軽量なローカル保護です。
変更系ツール呼び出しの前に、専用 index を使い、対象 Git リポジトリの作業状態を
reflog 付きの `refs/hermes-autosave/<branch>` に保存します。同一ターンは保護に成功したパスで重複排除し、
先の保存範囲外の明示対象は後からも保存します。HEAD、ブランチ、実 index、作業ツリーは変更せず、
push も行いません。ツール呼び出しを阻止、書き換え、承認することもありません。

Git 外や拒否対象のトップレベルリポジトリでは、変更直前の既存ファイルだけをコピーします。新規ファイルには変更前
バージョンがなく、これらの場所でディレクトリ対象のコピーは作成しません。

## checkpoints や git-hook との違い

組み込み checkpoints は独立したファイルシステムスナップショットストアで、非 Git プロジェクトを含むディレクトリを
保護します。本プラグインは各プロジェクトのローカル Git オブジェクト DB を再利用し、単一ファイルコピーを補助として
提供します。非 Git ディレクトリ全体は復元できません。対象範囲を絞ることで走査負荷を減らします。

カタログの `git-hook` は fetch/pull を行い、実ブランチに commit/push します。本プラグインは一切行いません。
シャドウ ref はローカルの復旧履歴であり、プロジェクトのコミットや同期ではありません。用途は異なります。
autosave はバックアップサービスでもデータ損失を防ぐ保証でもありません。

## インストール

Hermes >= 0.22.0、Python 3.11+ とローカル Git が必要で、Linux と macOS に対応します。0.3.0 は Windows
非対応です（ターミナルコマンドのパス解析が POSIX シェル前提で、Windows パスを誤読します）。旧版 Hermes は変更前スナップショットの実行中に
並行ツール呼び出しを誤って阻止することがあり、v0.22.0 でこのガードの競合が修正されています
（[NousResearch/hermes-agent#105223](https://github.com/NousResearch/hermes-agent/pull/105223)）。

```sh
hermes plugins install xiaoyaner0201/hermes-git-autosave
hermes plugins enable git-autosave
```

実行中の CLI/gateway セッションを再起動してフックを読み込みます。隔離プラグインホスト内ではローカル CLI
サブコマンドの登録は意図的に省略されます。観察フックは CLI 登録に依存しません。

組み込み checkpoints の代わりに使う場合、アクティブ profile の config.yaml に設定します：

```yaml
checkpoints:
  enabled: false
```

狭い Git/単一ファイルの保護で十分な場合にのみ無効化してください。既存のチェックポイントデータの移行・削除は
行いません。両方の有効化も可能ですが、スナップショット処理が重複します。

## 設定

すべて任意で、アクティブ profile の `plugins.entries.git-autosave.settings` に置きます。登録時に ctx.get_config
から読み込みます。コア関数はプラグインコンテキストなしでも動作します。数値は有限の正数が必要で、真偽値や文字列
は拒否します。件数上限は整数です。不正な値は既定値に戻し、サービスインスタンスごとに一度だけ ledger.jsonl に記録します。

| 設定 | 既定値 | 意味 |
| --- | --- | --- |
| `storage_dir` | 未設定 | データルートを上書き。~ を展開し、相対パスは起動ディレクトリを基準にします。 |
| `keep_days` | `3` | prune、status、台帳検索の保存期間。prune は明示実行が必要です。 |
| `max_file_mb` | `10` | 新たにハッシュするファイルと補助コピーの上限（MiB）。既存の追跡 blob は残る場合があります。 |
| `hook_budget_seconds` | `8` | フック全体の協調的時間予算。上限は 12 秒で、ホストの 30 秒タイムアウトより短い値です。 |
| `big_repo_entries` | `20000` | index エントリ数が超えると、対象パスだけを更新します。 |
| `max_untracked` | `2000` | 保存対象の未追跡ファイル数が超えると、明示的な対象未追跡ファイルのみ追加します。 |
| `extra_skip_dirs` | `[]` | 追加で除外するパス構成要素名。追跡済みファイルも除外します。 |
| `deny_toplevels` | `[]` | 単一ファイルコピーのみを許可する追加のリポジトリトップレベルパス。 |

```yaml
plugins:
  entries:
    git-autosave:
      settings:
        keep_days: 3
        hook_budget_seconds: 8
        extra_skip_dirs: [generated-dependencies]
        deny_toplevels: []
```

既定ストレージは `plugins.plugin_storage.plugin_data_dir("git-autosave")` です。モジュールが使えない場合は
`<HERMES_HOME>/plugin-data/git-autosave` にフォールバックします。

```text
<plugin-data-root>/
  files/<YYYYMMDD>/objects/<hash-prefix>/v2-<path-sha256>.<HHMMSS>-<ns-sequence>-<sha8>
  files/<YYYYMMDD>/objects/<hash-prefix>/<copy-name>.json
  ledger.jsonl
  state/
```

新しい名前は正規化絶対パスの SHA-256 を使い、元のファイル名要素を含めません。元の名前がファイルシステム上限でも
コピーとメタデータ名は有限長です。元のパス、内容 ID、モード、観測キーは所有者限定の原子的な隣接 JSON に保存し、
ベストエフォート台帳に依存しません。メタデータのない新コピーは不完全で、列挙しません。プロセス間重複排除は最新の
内容・モード観測とのみ比較し、A → B → A やモード循環の全遷移を保持します。ロック付き永続シーケンスが時計の
停止・逆行時も Git とコピーに共通の増加キーを割り当てます。旧パス型配置は読み取り可能で、モードは旧台帳または
所有者限定にフォールバックし、最古の名前は mtime 順です。全配置でメタデータ/台帳の元パス不一致を拒否します。
由来不明の旧コピーは --to で確認し、元ファイルを直接上書きしないでください。
インストール先に状態を保存せず、Git オブジェクトは各リポジトリに残ります。

## 保護範囲と安全ルール

- write_file と patch のパス、V4A Update/Add/Delete/Move ヘッダーが変更前保護を起動します。
  ヘッダー空白と Move 矢印前後の任意空白はホスト構文に従います。~ 展開後の絶対
  パスはそのまま使います。相対パスは公開 resolve_agent_cwd() に従います（セッション cwd → TERMINAL_CWD →
  起動ディレクトリ）。ヘルパーがない場合は TERMINAL_CWD または os.getcwd() を使います。
- symlink 経由の内容書き込み（write_file、patch Update/Add、terminal リダイレクト/tee/インプレース編集）は
  解決後の参照先を独立したパスとして保護します。許可リポジトリでは通常保存、それ以外は単一ファイルコピーで、
  除外規則も適用します。patch Delete/Move と terminal unlink/move は項目だけを保護します。
  V4A のリンク経由の書き込みはホストと同じく扱います。Update ヘッダーは操作前に解決し、
  以降の各ホップは先行する Move/Delete/Add が残した項目をたどり、Add は新しい通常項目です。
  リンク項目の復元には Git 保存が必要です。Git 外・拒否リポジトリではリンク項目自体は保存しません。
  find <link> はリンク版と台帳由来の参照先検索ヒントだけを表示し、別名状態で復元先を変更しません。
- terminal の workdir は初期 cwd で、先頭の静的 cd をその後適用します。env -C/--chdir はそのコマンドだけの
  cwd を変更し、install -t/--target-directory は宛先ディレクトリも選びます。shell リダイレクトは静的 cd 後の
  セグメントの shell cwd で解決し、env/Git 子コマンドの cwd は使いません。Git -C と一般的な変更コマンドの
  明示パスで対象リポジトリを選び、cwd 外のファイルも保護します。上限付きトークン解析は改行境界、
  sudo/env/command/nohup/time/nice/xargs、rm/rmdir/unlink/mv、cp/rsync/install/dd/shred/chmod/touch、
  リダイレクト（>、>|、>>、&>>）、sed/perl インプレース、truncate、Git checkout/restore/reset/clean/stash/
  rebase/switch/rm/mv/apply/am/merge/pull/cherry-pick/revert、checkout-index、read-tree -u、内容変更 update-index、
  worktree remove、patch/unzip/tar 展開、find -delete/-exec、非追加 tee を検出します。
  tee -a/tee --append は意図的に対象外ですが、>> は保護します。完全な shell パーサーではなく、動的パスや
  スクリプト・インタープリター・shell 関数による間接変更は見逃す場合があります。承認判断は行いません。
  65,536 文字（ASCII で 64 KiB）を超えるコマンドは分割せず、保守的に破壊的とみなし、初期 workdir の
  リポジトリだけを保護します。サービスごとに警告を一度記録し、コマンド内容は保存しません。
- 依存ディレクトリは追跡済みでも常に除外します：node_modules、bower_components、jspm_packages、.pnpm-store、
  .yarn、vendor、.venv、venv、site-packages、dist-packages、__pypackages__、.tox、.nox、.eggs、Pods、
  Carthage、.gradle、.m2、.cargo、.bundle。
- ビルド・キャッシュは未追跡ファイルと補助コピーのみ除外し、追跡済みは保持します：__pycache__、.pytest_cache、
  .mypy_cache、.ruff_cache、.hypothesis、.cache、.next、.nuxt、.svelte-kit、.turbo、.parcel-cache、.vite、
  .angular、.expo、dist、build、out、target、coverage、.coverage、htmlcov、.terraform、DerivedData。
- 拒否対象のトップレベルはファイルシステム・ドライブルート、ホーム、HERMES_HOME と祖先、HERMES_HOME/profiles/*
  の直接ディレクトリ、macOS のボリュームマウント直下、Linux /mnt と /media 直下、および deny_toplevels。
  単一ファイルコピーのみで、リポジトリ全体は保存しません。
- 大規模リポジトリ、または過去一時間以内の最近の遅いスナップショットが三秒以上かかったリポジトリでは paths-only
  モードを使います。ディレクトリ対象には追跡済みファイルのみ追加し、無関係な未追跡項目は列挙しません。
  無関係なパスは実 index の版を維持し、
  無関係な作業変更が保存されない場合があります。Gitlink（サブモジュール）は再帰走査しません。
- リポジトリごとの非ブロッキングロックで hook、sweep、prune を直列化します。POSIX は flock、Windows は
  msvcrt バイト範囲ロックで、アドバイザリ動作はファイルシステム依存です。使用中ならスキップして記録します。
  ホストの turn ID がない場合、ターン境界は推測しません。変更のないツリーの重複排除は継続します。同一ターン
  重複排除はサイズ制限付きプロセスローカル状態で、agent プロセス間では共有しません。
  同一ターン・同一 HEAD oid/分岐で実際に raw-hash した同じパスだけが明示対象を抑制します。stat/index 基準だけでは
  抑制しません。状態変更 Git コマンドはそのリポジトリのキャッシュを無効化し、その変更前保存では再充填しません。
  後の ignored/sparse/未保護対象は再試行し、失敗・スキップ・過大ファイルは対象範囲に数えません。
- Git 子プロセスとリポジトリ探索は残り予算を共有します。POSIX タイムアウト時はプロセスグループを終了します。
  デーモンは作成しません。ファイル操作は協調的期限を確認しますが、OS レベルで停止したファイルシステムを Python
  が確実に中断することはできません。Windows 子プロセスの終了処理はベストエフォートです。応答の良いローカル
  ディスクを使用してください。
- Git 子プロセスでは fsmonitor、hooks、split-index、自動保守を無効化します。HEAD から新規 private index を
  作り、index-info のみで書きます。Python が読み取り専用 index stat と lstat（racily-clean を含む）を比較し、
  Git に作業ツリー/index の比較や更新をさせません。明示された既存ファイルは正規化、filter、assume-unchanged
  に関係なく生バイトを保存します。無関係なファイルは index 表現を残す場合があります。
  暗黙・ディレクトリ保存では skip-worktree の index/HEAD 基準を保持し、実バイトが異なる場合も同様です。
  sparse-checkout で不在のファイルは削除扱いにしません。明示された存在するファイルは実バイトを保存します。
  シャドウ symbolic ref は
  スキップし、ref 書き込み・削除は --no-deref。GIT_NO_LAZY_FETCH=1 と protocol.allow=never で暗黙 fetch を禁止し、
  必要なオブジェクトがなければログを残してツールはブロックしません。
- Git が mirror を真と解釈するリモート（値なしキー、非ゼロ整数、yes、on を含む）、または refs/* を含む push
  refspec があると保存をスキップします。不正な mirror 真偽値もエラーを記録してスキップし、安全と仮定しません。
  通常の git push、git push --all、git push --tags はシャドウ refs を公開しません。git push --mirror や
  refs/* refspec は公開してしまうため、非公開スナップショットを含むリポジトリで使わないでください。

## CLI と復元方法

すべてのサブコマンドは --json をサポートします：

```sh
hermes autosave status
hermes autosave find path/to/file
hermes autosave list [repo]
hermes autosave prune --dry-run
hermes autosave prune
hermes autosave sweep --since-minutes 360 --budget-seconds 150
```

status はルート、サイズ、keep_days 内に保存されたリポジトリ、最近の台帳エラーを表示します。list は reflog 版と
生存 tip を表示します。find は変更前の生バイトを実際に読んだパスだけを表示し、分岐や Git/コピーをまたいで
観測順に並べます。Git の Autosave-Observed trailer は観測パス・内容 ID・モード、コミットキーは at_ns を記録し、
コピー情報は隣接メタデータにあります。台帳損失でも index/HEAD のプレースホルダーを新しい版にしません。
paths-only は選択パスだけを観測します（ディレクトリは追跡済みへ展開）。分岐/バックエンド間の A→B→A も新しい A を
記録します。他の観測が介在しない連続同一内容のみ重複排除し、無関係な観測でも保守的に新イベントを作ります。
由来のない旧 Git 保存や通常 HEAD は find に出さず、Git で手動確認します。旧コピーはメタデータ/台帳にフォールバック。
復元は対象を置き換えるので版を確認してください。番号は 1 始まりです。find <link> はリンク項目版だけを表示し、
台帳ヒントで参照先の独立した履歴を案内します。ヒントや旧別名状態は復元に使いません。履歴変更後は find を再実行してください。

```sh
hermes autosave restore path/to/file --version 1
hermes autosave restore path/to/file --version 2 --to recovered/file --json
git -C <repo> reflog show refs/hermes-autosave/<branch>
git -C <repo> diff 'refs/hermes-autosave/<branch>@{1}' -- path/to/file
```

restore は親ディレクトリを作り、一時兄弟ファイルと原子的置換で通常ファイルと保存モードを復元します。
既存の対象リンクをたどらず symlink を再作成し、実 index は変更しません。restore は指定パス（または --to）
だけを書きます。参照先内容は hermes autosave restore <referent> で復元します。Git リンク項目版はリンクを再作成し、
Git 外・拒否リポジトリではリンク項目を保存しません。restored_to は変更したパスを示します。
restore --to は設計上、宛先の親ディレクトリの symlink をたどります。復元前に親が信頼できることを確認してください。
たどらず置換するのは最後の宛先リンクのみです。コピーには元のモードを記録し、
モード情報のない旧コピーは所有者限定ファイルとして復元します。git show > file はリンクをたどりモードを失うので
使わないでください。git checkout <snapshot> -- <file> はモードも戻しますが実 index も変更します。
reflog スナップショットはユーザーブランチの祖先ではありません。detached HEAD は安定した
 detached-<repository-hash> シャドウ ref を使います。

prune は tip が keep_days より古いシャドウ ref を削除し、古い reflog を失効させ、古いコピー日付ディレクトリを
削除し、台帳を整理します。日付ディレクトリは暦日単位です。git gc は一切実行せず、到達不能オブジェクトは通常の
Git GC まで残る場合があります。明示実行または別途スケジュールしてください。--dry-run はスナップショット・ref・
台帳を変更しません。storage/files/state/日付ディレクトリが symlink なら拒否し、no-follow ディレクトリ記述子と
リンク攻撃に強い rmtree で削除します。台帳・ロック I/O も固定した root/state 記述子を使い、識別情報が変わると
中止します。安全な原語のないプラットフォーム（Windows を含む）ではエラーを報告し、
危険な削除に代えず prune を拒否します。sweep 予算は台帳走査と各呼び出し処理にも適用し、1 MiB を超える
セッションメッセージは警告してスキップします。

## sweep の自己スケジュール

sweep は予算切れ、agent 子プロセス、未再起動セッションなどのフック見逃しを補います。mode=ro URI と短い SQLite
タイムアウトで <HERMES_HOME>/state.db を読み取り専用で開きます。COALESCE(last_activity_at, started_at) で最近の
セッションを見つけ、messages から write/patch のパスや terminal workdir/先頭 cd を得ます。同じ検索期間の台帳
リポジトリも対象にします（既定六時間）。schema は非公開でバージョン依存です。列を調査し、欠落は警告にして、
台帳は利用できます。ディスク全体は走査しません。sweep は変更後に実行するため、失われた変更前版は再現できません。

成功時は --json を除いて stdout なし、終了 0。エラーや schema 警告は stdout に短い要約を出し、終了は 0 のまま
です。これにより Hermes no-agent cron が配信できます。--json は常に errors、warnings、リポジトリ結果、コピー
件数・パスを含むオブジェクトを出します。終了コードでなくこれらのフィールドを監視してください。プラグインは
スケジュールもバックグラウンドプロセスも作成しません。

スケジューラーは一つ選び、同じ profile/HERMES_HOME と Hermes 実行ファイルの絶対パスを使ってください。

1. Hermes no-agent cron：<HERMES_HOME>/scripts/ に autosave-sweep.sh を自分で作成します：

   ```sh
   #!/bin/sh
   exec /absolute/path/to/hermes autosave sweep --since-minutes 360 --budget-seconds 150
   ```

   自分でスケジュールします：

   ```sh
   hermes cron create "every 10m" --no-agent --script autosave-sweep.sh --deliver telegram --name autosave-sweep
   ```

   接続済みメッセージング先を選ぶか、保存のみなら --deliver local。通常の端末にライブ配信はありません。
   使用中のバージョンの hermes cron create --help を確認してください。
2. crontab：`*/10 * * * * /absolute/path/to/hermes autosave sweep --since-minutes 360 --budget-seconds 150` を追加。
   必要なら HERMES_HOME と PATH を明示します。cron の環境は shell と異なります。
3. launchd（macOS）：ユーザー LaunchAgent の ProgramArguments を executable、autosave、sweep、--since-minutes、
   360、--budget-seconds、150 に設定。StartInterval=600、環境と StandardOutPath/StandardErrorPath を指定し、
   launchctl bootstrap で自分で読み込みます。
4. systemd（Linux）：ユーザー oneshot service に ExecStart=/absolute/path/to/hermes autosave sweep と明示環境、
   timer に OnUnitActiveSec=10min。systemctl --user enable --now で自分で有効化します。

保存期間を強制する場合は hermes autosave prune も別途スケジュールします。sweep は暗黙の整理をしません。

## Disclosure（開示）

sweep の探索のため Hermes セッション DB を読み取り専用で参照します。agent が触れるリポジトリでローカル Git
子プロセスを実行し、各 .git にシャドウ refs と reflog を書き、agent が変更しようとする既存ファイルをプラグイン
データディレクトリへコピーします。.gitignore で除外されていない未追跡ファイルはローカル .git オブジェクト DB に
保存され得ます。秘密・機密ファイルの ignore 規則を事前確認してください。コピーとオブジェクトはローカルで暗号化
されません。ネットワーク呼び出し、バックグラウンドプロセス、認証情報要求はなく、認証ストアやテレメトリサービスと
連携しません。認証ストアの作業ファイルはコピー・読み取りせず、未追跡として収集もしません：
HERMES_HOME/.env、HERMES_HOME/auth.json（保守的に任意 auth.json）、.env、.env.*、.netrc、.git-credentials、
.npmrc、.pypirc、*.pem、*.key、id_rsa*、id_ed25519*、id_ecdsa*、.claude/.credentials.json、および
.ssh、.aws、.docker、.azure、.kube、.config/gcloud、.config/gh 配下の全ファイルです。
.aws/credentials、.aws/config、.docker/config.json、.azure/accessTokens.json、.azure/msal_token_cache.*、
.kube/config、.config/gh/hosts.yml、gcloud の application_default_credentials.json、credentials.db、
access_tokens.db を含みます。大文字小文字を区別せず絶対パスの祖先も調べるため、認証ディレクトリ内に
ネストしたリポジトリにも適用します。字句パスと各 symlink 置換を解決前に確認し（最大 40 ホップ）、
循環・長すぎる内容チェーンはスキップしますが、最後のリンク項目は独立して保存できます。
認証/依存名の中間リンクやディレクトリ別名も内容除外の対象です。
ユーザーが既にコミットした項目は HEAD のまま残し、
作業・stage の認証ファイル変更は収集しません。
これは名前による除外で秘密スキャナーではないため、その他の機密ファイルには ignore 規則が必要です。

## 制限と開発

保護はベストエフォートです。リポジトリエラー、競合、大きすぎるファイル、未マージ index、間接 terminal 変更、
ホストローカルで利用不能なパス（リモート・コンテナツール）、予算切れで未保存になる場合があります。同時変更中の
作業ツリーの原子的状態は保証できません。Windows ロックはファイルシステムに依存し、ACL がプライバシーを決定
します。復元コマンドは PowerShell でなく cmd.exe 向けです。同一ターン重複排除はプロセスローカル、セッション
schema は調査しますが互換保証はありません。削除しても永続データや refs は消しません。

**既知の制限：シンボリックリンクを経由する複数操作の V4A パッチ。** 1 つのパッチでリンクを移動・削除してから移動先を
更新すると、プラグインは操作を再生して実際に書き込まれるファイルを求めます。この再生はパスを文字どおり比較し、ホストは
ファイルシステム経由で解決します。大文字小文字を区別しない、または Unicode を正規化するファイルシステム（macOS・Windows
の既定）では、リンク先の中間リンク名が大文字小文字や Unicode 形式だけ異なる場合（`MIDDLE` と `middle`）、ホストが
編集するファイルではなく古いリンク先を保護し、編集されたファイルに変更前の版が残らないことがあります。再生はホストの
パッチ検証もやり直さないため、ホストが拒否したパッチでも無関係なファイルのローカルコピーが残る場合があり、V4A 呼び出しの
`path` 引数がパッチの触れないリンク先をコピーすることもあります。通常の `write_file` と単一操作の `patch` によるリンク
経由の編集は影響を受けません。このようなパッチの復元に頼る場合は、先にコミットするか Move と Update を別の呼び出しに
分けてください。

```sh
python -m pip install -r requirements-dev.txt
python -m ruff check .
python -m pytest -q
```

隔離テストは一時リポジトリ/HERMES_HOME を使い、ネットワークを使いません。CI は Ubuntu・macOS と
Python 3.11/3.13 を対象にします。CONTRIBUTING.md と SECURITY.md を参照してください。
Apache-2.0 — Copyright 2026 The hermes-git-autosave Authors。LICENSE を参照。
