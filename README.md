# Minecraft 区域清理工具

中文桌面 GUI，按 `InhabitedTime` 筛选整个 Minecraft Java 版 `.mca` 区域文件。支持 macOS 开发和 Windows 10/11 x64 运行。只读扫描后，必须在确认窗口明确授权，才会永久删除所选文件。

## Windows 使用

### 从源码运行

安装 [Python 3.12 x64](https://www.python.org/downloads/windows/)，保留 Tcl/Tk 和 Python Launcher 安装选项。在项目目录用 PowerShell 执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_windows.ps1
```

此脚本创建项目专用的 `.venv-win` 环境并安装依赖；ExecutionPolicy 仅对这次 PowerShell 进程生效。

### 打包成无需 Python 的 exe

在 Windows 上执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_windows.ps1
```

脚本先运行测试（GUI 测试会打开窗口，数据均为自动生成的临时存档），然后生成：

```text
dist/MinecraftRegionCleaner/MinecraftRegionCleaner.exe
dist/MinecraftRegionCleaner-windows-x64.zip
```

解压后双击 exe。**整个文件夹都要保留，包括 `_internal`，不能单独复制 exe。**

PyInstaller 不能直接在 macOS 上产出 Windows 程序；请在 Windows 本机或虚拟机打包。仓库也提供 `.github/workflows/windows.yml`，推送到自己的 GitHub 仓库后可手动运行并下载 Windows artifact；本项目未自动创建远程仓库或触发远程构建。

## macOS 开发运行

使用带 Tk 的 Python 3.12 或更新版本（例如 python.org 安装包）：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python main.py
```

如果提示缺少 tkinter，使用包含 Tcl/Tk 的 Python 安装包；`python3 -m tkinter` 可验证环境。

## 操作流程

界面左侧设置存档目录和清理规则，右侧显示扫描统计与区域列表。列表可按候选、豁免、异常筛选，并按路径搜索；点击行可展开文件详情。底部始终显示已勾选数量、预计释放空间和删除入口。

“勾选当前候选”只勾选当前筛选结果中的候选文件；切换筛选不会清除已有勾选，底部会提示筛选外仍选中的数量。“清除勾选”会清除所有选择。修改扫描设置会使清单失效。

1. 停止 Minecraft 服务器和其他写入存档的工具，备份完整存档。
2. 启动程序，选择包含 `world`、`world_nether`、`world_the_end` 的服务器根目录。程序默认使用启动时的工作目录；Windows 快捷方式可能改变此目录，务必查看界面显示的绝对路径。也可点击“程序所在目录”。
3. 输入计时阈值 n，单位可选 tick、秒、分钟。秒按 20 tick、分钟按 1200 tick 换算；界面显示最终整数 tick。默认 n=600。
4. 如需保护中心区域，启用“豁免中心区域”，填写 R。
5. 点击“开始扫描”。点击行查看原因和关联文件，点击勾选列或按空格排除不想删的候选。可导出 JSON 或 CSV 清单。
6. 点击“审阅并删除所选…”。核对完整文件列表，勾选停服和备份确认，再点击“永久删除以上文件”。返回检查或关闭确认窗口不会删除。
7. 查看结果与日志。取消会停止后续处理，但不能恢复已经删除的文件；再次操作需重新扫描。

## 筛选规则

- 处理范围限定在选定目录下三个指定世界内的 `region` 文件夹，支持常见 `DIM-1`、`DIM1` 和嵌套维度布局。缺失的世界目录会提示并跳过。
- 文件内**所有已存在区块**都必须成功读取，且 `InhabitedTime < n`。等于 n 时保留；空槽位不是计时为零的区块；完全空的区域文件保留。
- 读取新版根节点的 `InhabitedTime` 或旧版 `Level.InhabitedTime`，要求为非负 TAG_Long；不接受缺失字段、重复字段或模糊的回退值。
- 支持 gzip、zlib、未压缩数据、Minecraft 的 lz4-java block stream，以及外置 `.mcc` 区块。压缩数据和解压输出限制为每区块 64 MiB；未知压缩、损坏、超限或读取失败均保留整组文件。
- 不支持基岩版 LevelDB、旧 `.mcr`、Linear 等其他区域格式。

### 坐标豁免

对于 `r.x.z.mca`，当 `abs(x) < R AND abs(z) < R` 时整组豁免。两个条件必须同时满足；采用区域坐标，不是方块坐标（一个区域覆盖 32×32 区块，即 512×512 方块）。

例如 R=3：x 和 z 都在 -2、-1、0、1、2 中的文件受保护。

| 文件 | 坐标豁免 |
| --- | --- |
| `r.-2.1.mca` | 是 |
| `r.0.0.mca` | 是 |
| `r.3.0.mca` | 否，等于边界 |
| `r.1.-3.mca` | 否，只有一个轴满足 |

默认关闭坐标豁免。R=0 不豁免任何文件。各维度分别按自己的坐标计算，不对下界乘以 8。坐标豁免具有优先级，低计时也不能覆盖它。

### 关联文件

删除一个地形区域时，同时列出并清理同维度、同区域坐标的 `entities/r.x.z.mca`、`poi/r.x.z.mca`，以及三个存储目录中属于此区域的 `c.chunkX.chunkZ.mcc` 文件（包括同区域残留外置文件）。这些文件在确认列表中逐一显示，不会独立按计时筛选。其他区域的文件保持不动。

## 保护措施与实际限制

- 扫描阶段不修改存档。修改根目录、阈值或豁免设置会使已有清单失效。
- 不跟随世界目录内的符号链接、Windows junction/reparse point；有硬链接的候选文件也会保留。
- 记录文件身份、大小、mtime、ctime 和 SHA-256；候选扫描结束与删除前均复核。新增、丢失或变化的关联文件使整组跳过，删除前还会重新读取地形区块计时。
- **必须停服。** 复核无法消除服务器并发写入造成的竞争；程序不强行终止服务器，也不能保证跨文件原子删除。
- 使用永久删除，不进入回收站。地形文件先删，若其删除失败，关联文件不会继续删；中途失败会显示“部分删除”并逐文件记账。恢复须使用自己的完整备份。
- `InhabitedTime` 低不证明没有建筑。字段可能受服务端配置、插件或历史缺陷影响；请先抽查已知基地。工具不会自动发现领地或全部玩家建筑。
- 实际释放空间以删除文件的逻辑大小统计，磁盘压缩、快照、硬盘分配单位可能导致系统空闲空间变化不同。
- 重新探索已删除区域时，服务器会重新生成地形。

## 日志

每批操作先落盘保存删除计划，再记录每个文件的删除尝试、成功和各区域结果。日志写入失败会中止后续删除。日志不包含区块内容，只包含路径、元数据、指纹和执行结果。

- Windows：`%LOCALAPPDATA%\MinecraftRegionCleaner\logs`
- macOS：`~/Library/Application Support/MinecraftRegionCleaner/logs`
- Linux：`$XDG_STATE_HOME/MinecraftRegionCleaner/logs`，默认 `~/.local/state/...`

日志必须在扫描根目录之外。如果将整个用户目录选作扫描根目录，与日志路径冲突时会拒绝删除，请选择实际服务器目录。

## 测试

```bash
.venv/bin/python -m unittest discover -v
.venv/bin/python -m tests.gui_smoke
```

GUI 测试需要可用的桌面会话。所有测试只使用临时合成存档，涵盖压缩、NBT、阈值、坐标保护、关联文件、变更复核、取消、未确认拒绝删除及部分失败等情况。

当前开发验证环境为 macOS / Python 3.14。Windows 构建脚本以 Python 3.12 x64 为目标；Windows 实际运行和打包结果需在 Windows 环境验证，不能由 macOS 测试代替。

## 文件布局

```text
main.py                       GUI 启动入口
chunk_cleaner/region_reader.py MCA、压缩与 NBT 只读解析
chunk_cleaner/scanner.py       扫描、坐标豁免、关联文件与指纹
chunk_cleaner/deletion.py      确认后复核、删除及审计日志
chunk_cleaner/gui.py           中文 GUI
tests/                        临时合成存档测试
scripts/                      Windows 运行和打包脚本
PLAN.md                       实施计划
```
