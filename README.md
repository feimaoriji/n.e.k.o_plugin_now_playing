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

This plugin declares no runtime Python dependencies in `pyproject.toml`. The
WinRT bindings it needs (`winrt-runtime`, `winrt-Windows.Media.Control`,
`winrt-Windows.Foundation`, `winrt-Windows.Foundation.Collections`,
`typing_extensions`) are committed under `vendor/`, which the plugin loader
prepends to `sys.path` — the same convention the official plugins follow. So
`neko-plugin sync` has nothing to install and the package works offline with
no pip step.

If you would rather install those wheels from PyPI (Windows only — that is
where the wheels exist), they are still listed in the optional `win32` extra:

```bash
pip install -e ".[win32]"
```

本插件在 `pyproject.toml` 中不声明运行时 Python 依赖。它用到的 WinRT 绑定
（`winrt-runtime`、`winrt-Windows.Media.Control`、`winrt-Windows.Foundation`、
`winrt-Windows.Foundation.Collections`、`typing_extensions`）已提交在 `vendor/`
目录中，插件加载器会把它加到 `sys.path` 前面——与官方插件仓库的做法一致。
因此 `neko-plugin sync` 无需安装任何东西，离线宿主不用 pip 也能直接跑。

若想改从 PyPI 安装这些轮子（仅 Windows 有轮子），它们仍保留在可选的 `win32`
extra 中：

```bash
pip install -e ".[win32]"
```

本プラグインは `pyproject.toml` にランタイム Python 依存関係を宣言していません。
必要な WinRT バインディング（`winrt-runtime`、`winrt-Windows.Media.Control`、
`winrt-Windows.Foundation`、`winrt-Windows.Foundation.Collections`、
`typing_extensions`）は `vendor/` にコミット済みで、プラグインローダーが
`sys.path` の先頭に追加します（公式プラグインと同じ方式）。そのため
`neko-plugin sync` は何もインストールせず、オフラインのホストでも pip 無しで
動作します。

PyPI から入れたい場合（Windows のみ、wheel が存在する環境）は任意の `win32`
extra に残してあります。

```bash
pip install -e ".[win32]"
```

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
