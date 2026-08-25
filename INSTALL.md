# 安装与使用指南(新手向)

这份文档假设你**从来没有装过 Claude Code 的 skill**。照着一步一步做就行,大约 5 分钟。

---

## ⚠️ 先看这个:两种"输入框"不要搞混

这份文档里的命令分两类,**输入的地方完全不同**,弄错了会报错:

| 标记 | 输入在哪 | 长什么样 |
|---|---|---|
| 🟦 **【Claude Code 里输入】** | Claude Code 的对话框,就是你平时跟 Claude 打字聊天的地方 | 以 `/` 开头,例如 `/plugin` |
| ⬛ **【终端里输入】** | Mac 的「终端」App(Terminal)/ Windows 的 PowerShell | 例如 `pip3 install ...` |

下面每条命令我都会标清楚是哪一种。

---

## 第 0 步:确认你有 Claude Code

打开 Claude Code(桌面 App、或者在终端里输入 `claude`)。

能看到可以打字的对话框 = 没问题,继续下一步。
完全没装过 Claude Code = 先去装 Claude Code,再回来。

---

## 第 1 步:装 Python 依赖(⬛ 终端)

这个工具需要 Python 来读 Excel 文件。

**1a. 先检查有没有 Python**

打开终端,输入:

```bash
python3 --version
```

- 看到类似 `Python 3.9.6`(数字可能不同)→ ✅ 有,继续 1b
- 看到 `command not found` → 需要先装 Python 3,去 https://www.python.org/downloads/ 下载安装,装完重开终端再试

**1b. 检查需要的两个包**

终端里输入:

```bash
python3 -c "import pandas, openpyxl; print('OK')"
```

- 看到 `OK` → ✅ 已经装好了,跳到第 2 步
- 看到 `ModuleNotFoundError: No module named 'pandas'`(或 openpyxl)→ 继续 1c

**1c. 装这两个包**

终端里输入:

```bash
pip3 install pandas openpyxl
```

等它跑完(会刷一堆字,正常)。跑完再执行一次 1b 的检查命令,看到 `OK` 就成功了。

> 如果 `pip3 install` 报权限错误,试试:`pip3 install --user pandas openpyxl`

---

## 第 2 步:添加工具来源(🟦 Claude Code)

回到 **Claude Code 的对话框**,输入:

```
/plugin marketplace add alexli-fortrail/rtb-health-plugin
```

按 Enter。等几秒,应该会看到类似:

```
✔ Successfully added marketplace: appier-rtb-tools
```

看到 `✔ Successfully added` = 成功。

> 这一步是告诉 Claude Code「去哪里找这个工具」,只需要做一次。

---

## 第 3 步:安装(🟦 Claude Code)

同样在 Claude Code 对话框输入:

```
/plugin install rtb-health@appier-rtb-tools
```

按 Enter,应该会看到:

```
✔ Successfully installed plugin: rtb-health@appier-rtb-tools
```

---

## 第 4 步:重启 Claude Code

**这一步不能跳过。** 装完必须完全关闭 Claude Code 再重新打开,新工具才会生效。

---

## 第 5 步:打开自动更新(🟦 Claude Code,强烈建议)

这个工具还在持续改进。**不做这一步的话,你会永远停留在今天装的这个版本**,以后的修复和新功能都收不到。

1. 在 Claude Code 输入 `/plugin` 按 Enter
2. 用方向键切到 **Marketplaces** 那一栏
3. 选中 `appier-rtb-tools`
4. 找到 **Enable auto-update** 并打开它

> 界面细节可能随 Claude Code 版本略有不同,只要找到 `appier-rtb-tools` 这一项、把自动更新打开就行。

---

## 第 6 步:确认装好了(🟦 Claude Code)

输入:

```
/rtb-health
```

如果它开始问你「上传文件 + 说出目标」→ 🎉 装好了,可以用了。

如果提示找不到这个命令 → 看下面的「常见问题」。

---

# 怎么用

## 基本用法

1. 在 Claude Code 输入 `/rtb-health`
2. 它会让你提供两样东西,**一次给齐**:
   - **文件**:这周的 RTB 导出(xlsx 或 csv),直接把文件拖进对话框
   - **目标**:你这次想看什么,用大白话说就行

目标的写法举例:

```
Raw CPA < 10
```
```
ROAS 7D > 40%
```
```
Valid Action >= 5
```

最多可以设 3 条,它会链式筛选(第二条只在第一条命中的组里继续筛)。

3. 等它跑完,会给你一个**网页版报告链接**,点开就能看,也能直接分享给别人。

## 报告里有什么

- **健康检查** — 整体转化量、CPA、花钱最多/转化最多的 SSP、creative 拆解
- **多个 ROAS 窗口对比** — D0/D7 等所有窗口并排看,避免只看一个数字被误导
- **目标分析** — 先看哪些 SSP 整体达标,再拆到具体广告组,带多周走势图
- **诊断结论** — Claude 自己写的判断,比如「这个 SSP 看起来达标,但其实只有 1 笔转化撑着,不建议现在加量」

## 关于 token 消耗

- 工具本身:常驻 ~128 tokens,每次调用 ~4.7k tokens
- **文件大的时候它会先问你**:告诉你预计数据量,让你选「详细版」还是「精简版」,不会闷头烧额度
- 用免费额度的话建议选**精简版**:数据量小很多,但达标判定、CPA、异常检测这些**结论完全一样**

---

# 常见问题

### 输入 `/rtb-health` 说找不到命令

按顺序检查:
1. 第 4 步的**重启**做了吗?装完必须重启才生效
2. 第 3 步有看到 `✔ Successfully installed` 吗?没有的话重跑第 2、3 步
3. 还是不行 → 在 Claude Code 输入 `/plugin` 看看列表里有没有 `rtb-health`

### 报错 `ModuleNotFoundError: No module named 'pandas'`

Python 包没装好。回到**第 1 步的 1c**,在**终端**(不是 Claude Code)里跑:

```bash
pip3 install pandas openpyxl
```

### 报错 `command not found: python3`

Python 没装。去 https://www.python.org/downloads/ 装 Python 3,装完**重开终端**再试。

### 我改了/更新了,但没变化

需要手动更新一次(🟦 Claude Code):

```
/plugin update rtb-health@appier-rtb-tools
```

然后**重启 Claude Code**。

如果提示已是最新但你确定有更新,联系维护者——可能是版本号没更新导致的(见 README 的说明)。

### 报告链接打不开 / 想重新生成

直接跟 Claude 说「重新生成报告」或者换个目标重跑一次就行。

### 我的文件格式跟别人不一样,能用吗

可以试。这个工具会自动识别你文件里有哪些指标列,不要求固定格式。如果某个指标它没找到,它会明确说「这个文件里没有 XX 列」,而不是编一个数字给你。

---

# 需要帮忙?

- 完整功能说明:[README.md](./README.md)
- 装不上、报错看不懂:把**报错原文**截图或复制给维护者,比描述「装不上」有用得多
