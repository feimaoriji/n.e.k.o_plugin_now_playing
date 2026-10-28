# 正在播放 · now_playing

读取 Windows 系统媒体会话（SMTC），把「此刻电脑在放什么」送进 N.E.K.O：
她主动搭话时知道现在是哪首歌，普通文本聊天那一轮也能拿到同一份快照
（默认随用户的发言顺带刷新）。

Reads the Windows system media session (SMTC) and hands "what is playing right now"
to N.E.K.O: she knows the current track when she starts a conversation, and normal
chat turns get the same snapshot as well (refreshed along with the user's message
by default).

Windows のシステムメディアセッション（SMTC）を読み取り、「いま何が再生されているか」を
N.E.K.O に渡します。彼女が自分から話しかけるときも、通常のテキスト会話のターンでも
同じスナップショットを参照できます（既定ではユーザーの発話に合わせて更新）。

## 特性 · Features · 特徴

- 只读系统媒体会话：不改播放器、不注入、不依赖任何平台的私有 API
- 双通道喂上下文：主动搭话轮（心跳）与文本聊天轮各一条，各自去重只留最新
- 30s 新鲜度：暂停 / 关掉播放器超过 30s 就当「没在放」，不会瞎编
- 可选歌词（默认关）：打开后附带一行歌词，带缓存与超时保护
- 全部行为都可被 `NOW_PLAYING_*` 环境变量覆盖，默认开箱即用

## 配置 · Configuration · 設定

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `NOW_PLAYING_AUTOSTART` | `true` | 插件启动后自动开始后台轮询 |
| `NOW_PLAYING_INTERVAL_S` | `5.0` | 轮询间隔（秒），必须 < 30（新鲜度阈值），下限 1 |
| `NOW_PLAYING_CHAT_CONTEXT` | `true` | 文本聊天轮也喂快照（passive 队列 + TTL） |
| `NOW_PLAYING_CHAT_TTL_S` | 自动 | 文本通道快照有效期；默认 `2×间隔+2s`，夹在 8–60s |
| `NOW_PLAYING_INCLUDE_LYRIC` | `false` | 是否附带一行歌词（联网取，按曲缓存） |

## Development

The plugin source and its Git repository live at:

```text
N.E.K.O/plugin/plugins/now_playing
```

插件源码及其 Git 仓库直接位于：

```text
N.E.K.O/plugin/plugins/now_playing
```

プラグインのソースと Git リポジトリは次の場所にあります：

```text
N.E.K.O/plugin/plugins/now_playing
```

When publishing to the plugin market, use this GitHub repository name:

发布到插件市场时，请使用以下 GitHub 仓库名：

プラグインマーケットへ公開する際は、次の GitHub リポジトリ名を使用してください：

```text
n.e.k.o_plugin_now_playing
```

From this plugin repository root:

```bash
uvx ruff==0.12.4 check --ignore-noqa --config ruff.toml .
```

From the N.E.K.O repository root / 在 N.E.K.O 仓库根目录中 / N.E.K.O リポジトリのルートで：

```bash
uv run neko-plugin sync now_playing --clean
uv run neko-plugin check now_playing
uv run neko-plugin check -r now_playing
```

Python runtime dependencies are declared in `pyproject.toml` and synced into
`vendor/` for packaging. The generated `vendor/` directory is not committed;
local builds and CI recreate it before release checks.

Python 运行时依赖声明在 `pyproject.toml` 中，并在打包时同步到 `vendor/`。
生成的 `vendor/` 不提交；本地构建和 CI 会在发布检查前重新生成它。

Python ランタイム依存関係は `pyproject.toml` に宣言し、パッケージ化時に
`vendor/` へ同期します。生成された `vendor/` はコミットせず、ローカルビルドと
CI が公開前チェックで再生成します。

## Market release / Market 发布 / Market 公開

Publish the version declared in `plugin.toml`. By default this pushes the Git
tag, waits for the standard GitHub Release, and notifies the plugin market.

发布 `plugin.toml` 中声明的版本。默认会推送 Git tag、等待标准 GitHub
Release，然后通知插件市场。

`plugin.toml` で宣言されたバージョンを公開します。既定では Git tag を
push し、標準 GitHub Release を待ってからプラグインマーケットへ通知します。

```bash
uv run neko-plugin publish now_playing
```

To run only one half explicitly / 如需仅执行一部分 / 一方のみを実行する場合:

```bash
uv run neko-plugin publish github now_playing
uv run neko-plugin publish market https://github.com/owner/repo/releases/tag/v0.1.0
```

The generated `.github/workflows/release.yml` builds and uploads
`now_playing.neko-plugin`. The market independently verifies that Release
before publishing it.

生成的 `.github/workflows/release.yml` 会构建并上传插件包；Market 会独立验证
该 Release 后再发布。

生成された `.github/workflows/release.yml` がプラグインパッケージをビルドして
アップロードし、Market はその Release を独立検証してから公開します。

## Entry

```toml
entry = "plugin.plugins.now_playing:NowPlayingPlugin"
```
