---
paths:
  - "requirements.txt"
  - "pyproject.toml"
---

# Cow：依賴版本陷阱

> 自 `CLAUDE.md` 逐字搬出（2026-09-15，L1 瘦身）。編號沿用原編號，外部一律引標題；標題索引在 CLAUDE.md〈已知陷阱〉。

### 15. requirements 勿替 numpy/pandas 加上限

pandas-ta 只有 pre-release（0.4.x）且依賴 `pandas>=2.3.2`。曾誤鎖 `numpy<2`+`pandas<2.3`
→ 雲端 uv 報 `No solution`、pip fallback source-build pandas 卡死、**app 起不來**
（原本「全無 pin」反而正常）。
**正解**：`pandas-ta==0.4.71b0`（pin 確切 pre-release，uv 才願解析）、`pandas>=2.3.2`、
`numpy>=1.26`，**一律不加上限**。鎖版本前先看雲端 build log，不可憑記憶臆測。
