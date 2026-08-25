# rtb-health

一个 Claude Code skill:上传一份 RTB 周报导出(xlsx/csv)+ 说出这次的优化目标,产出一份可分享的 HTML campaign health 报告。

报告内容包括:
- **健康检查** — All Time / Latest Week 两段扫描:总转化、按量加权 CPA、花费与转化占比 Top SSP、creative 拆解、MMP CVR
- **多指标视角** — 所有 ROAS 窗口并排对比、CTCV/VTCV rate、CPI、每个 SSP 的 Win Rate/CPM(用来区分「没量」vs「被压价」vs「价格结构本来就高」)
- **目标分析** — 先看哪些 SSP 整体达标,再拆到具体广告组(带多周走势图)
- **诊断** — 由 Claude 自己写的判断性结论,不是模板套话
- **辅助功能** — List/SK 标签拆解、周与周之间的结构变动检测、导出 CSV、目标预算分配 vs 实际对比

---

## 安装

> 🔰 **没装过 Claude Code skill?** 看这份一步步的图文指南:**[新手安装与使用指南 →](./INSTALL.md)**
> 包含环境检查、常见报错怎么办。下面是给熟手的简版。

在 Claude Code 里执行这两条,**一次性设置**:

```
/plugin marketplace add alexli-fortrail/rtb-health-plugin
```

```
/plugin install rtb-health@appier-rtb-tools
```

装完重启 Claude Code,输入 `/rtb-health` 就能用。

### ⚠️ 装完请顺手打开自动更新

这个 skill 还在持续迭代。**第三方 marketplace 默认不会自动更新**,不开的话你会一直停留在安装时的那个版本。

1. 输入 `/plugin`
2. 切到 **Marketplaces** 标签
3. 选中 `appier-rtb-tools`
4. 点 **Enable auto-update**

开启后 Claude Code 会在启动 session 时后台检查更新;更新在**下次启动**(或执行 `/reload-plugins`)后生效,不会打断当前对话。

想手动更新:

```
/plugin update rtb-health
```

---

## 使用方式

输入:

```
/rtb-health
```

然后按提示**一次性**给两样东西:

1. 这周的 RTB 导出文件(xlsx 或 csv,拖进对话框即可)
2. 这次想看的目标,例如:
   - `Raw CPA < 10`
   - `ROAS 7D > 40%`
   - `Valid Action >= 5`

最多可以设 3 条链式条件(第二条只在第一条命中的组里继续筛)。剩下的它会自己跑完,最后给你一个报告链接。

### 可选:CID 名称对照

如果你手上有一份 CID Overview(任何含「Ad Group ID」和「Ad Group Name」两列的表),可以一起上传,报告里就会显示规范化的广告组名称。不传也完全能用,广告组名称直接取导出文件里自带的。

---

## 环境要求

Python 3,以及两个包(读 xlsx 用):

```bash
pip3 install pandas openpyxl
```

---

## Token 消耗

用 `claude plugin details rtb-health` 可以随时查。当前版本:

| | 消耗 |
|---|---|
| 常驻(每个 session 都会加上) | ~128 tokens |
| 每次调用 `/rtb-health` | ~4.7k tokens |

这只是 skill 本身的开销,不含分析过程中实际处理数据的部分——文件越大、广告组越多,单次总消耗越高。如果你在用免费额度,建议先拿小一点的文件试。

---

## 已知的注意事项

- **List/SK 标签拆解是启发式的** — 靠从广告组名字里认 `SK 10`、`fix pm list`、`idfv` 这类字样。每个人命名习惯不同,认不出来的会归到 "No list tag detected",所以这块只能当粗略参考,不是权威口径。
- **部分比例指标是逐行平均,不是按量加权** — Win Rate、CPM、CPA(含目标判定里的 CPA/CPI 类指标)都做了按量加权;但 ROAS、CTCV/VTCV rate 这类在导出文件里没有独立的分子分母列可以还原,只能逐行平均。如果各广告组量级差异很大,读这些数字时要留意。
- **不覆盖更细的颗粒度** — OSV、bundle/domain、ISP 这些维度不在周报导出里,这个 skill 看不到。
- **异常检测需要 ≥3 周历史**(按广告组×SSP 分组),不满 3 周的组不会被标记。

---

## 目录结构

```
.
├── .claude-plugin/
│   ├── plugin.json          # 插件清单
│   └── marketplace.json     # marketplace 清单(安装时 add 的就是这个)
└── skills/
    └── rtb-health/
        ├── SKILL.md         # skill 本体:流程与判断规则
        └── scripts/
            ├── analyze.py       # 解析 + 计算(discover/health/full/analyze/export/allocation)
            └── render_report.py # 把 JSON 渲染成 HTML 报告
```

## 改动之后

### ⚠️ 必须 bump 版本号,否则更新不会生效

这是实测踩过的坑:**只改内容、不改 `.claude-plugin/plugin.json` 里的 `version`,使用者那边不会拿到更新**——即使他们开了自动更新、即使手动跑 `/plugin update`,装的还是旧版本(缓存目录是按版本号命名的)。

所以每次改动都要:

1. 改 `plugin.json` 的 `version`(改了功能 → 次版本号 +1,例如 `1.1.0` → `1.2.0`;只修 bug → `1.1.1`)
2. 本地验证清单没写坏:
   ```bash
   claude plugin validate ./ --strict
   ```
3. commit + push

开了自动更新的使用者下次启动 Claude Code 就会拿到;想立刻更新的可以手动跑:

```
/plugin update rtb-health@appier-rtb-tools
```

> 注意:这个 repo 里**不要提交任何真实的 campaign 数据**(导出文件、生成的报告、CID 对照表)。`.gitignore` 已经挡掉了常见的几种,但提交前还是看一眼 `git status`。
