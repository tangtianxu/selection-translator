# Selection Translator / 划词翻译

一个面向 Windows 的轻量划词与剪贴板翻译工具，由 `ttx Vibe Coding` 制作。

在浏览器、PDF、Word 或其他应用中选中文字，按下全局快捷键即可复制、翻译并弹出结果。程序支持自动检测语言、中英互译、窗口置顶、系统托盘、翻译历史和本地 LaTeX 公式渲染。

## 功能

- 自定义全局快捷键，默认 `Ctrl+Shift+T`
- Google 翻译与自动语言检测
- 单请求互译、请求节流、本地缓存及 HTTP 429 自动退避
- 中文与英文自动互译
- 剪贴板变化后自动翻译（可选）
- 读取选中文字后恢复原剪贴板（关闭剪贴板自动翻译时）
- 置顶、仅译文、原文收起和系统托盘
- 8–18 号内容字号调整，正文、历史记录与公式同步缩放
- 保存并搜索最近 200 条历史记录
- 历史记录侧边面板，左侧原文、右侧译文
- 本地识别和渲染常见 LaTeX 公式，包括使用普通括号 `( ... )` 包围的公式
- 本地日志与异常保护

## 下载与使用

Windows 用户可在本仓库的 **Releases** 页面下载 `划词翻译.exe`，无需安装 Python。

1. 启动程序。
2. 在任意应用中选中文字。
3. 按 `Ctrl+Shift+T` 查看译文。

更完整的说明请参阅 [使用说明.md](使用说明.md)。

## 从源码运行

需要 Windows 和 Python 3.10 或更高版本：

```powershell
python -m pip install -r requirements.txt
python SelectionTranslator.py
```

构建单文件 EXE：

```powershell
python -m pip install -r requirements-dev.txt
python -m PyInstaller --noconfirm --clean --onefile --windowed --name "划词翻译" SelectionTranslator.py
```

## 公式支持

公式完全在本地渲染。程序会在显示前自动兼容 Markdown 转义的下划线，以及 `\mathbf c`、`\mathcal C`、`\mathsf T` 等未加大括号的常见 TeX 简写，但不会改动复制内容和历史记录。常见的上下标、分式、根式、求和、积分、极限、希腊字母和概率表达式可直接显示。矩阵、分段函数和多行对齐等完整 LaTeX 环境可能保留为红色源码。

## 隐私与限制

- 翻译文本会发送到 Google 翻译服务；请勿用于敏感、保密或受限制的内容。
- 本项目使用 Google 翻译的非官方免密钥网页接口，接口可能变化，也不适合批量或商业服务。
- 翻译历史与日志仅保存在当前 Windows 用户的本机应用数据目录。
- 某些管理员权限应用可能阻止普通权限程序模拟复制。

## 许可证

[MIT License](LICENSE)
