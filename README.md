# forumweave — 抓主贴与楼中楼、折叠回复、多次编辑版本，重建线程树并增量补楼。

难度：**困难**　|　子方向：数据采集 / 爬虫与 RPA　|　运行栈：Python 3.14 + 标准库 `sqlite3` + `httpx` + `pytest`（离线，无需联网）

## 基础版代码（起点，能跑）
读 `fixtures/input.json` → `loader` → `normalizer` → 其余已实现变换 → `keyer` → 幂等落 `sqlite`。
```
make run     # 跑通 happy-path，打印 {fed, rows}
make test    # happy-path + 幂等 + 缺口桩断言
```

## 单一职责约定
每个模块 = 一个文件 = 一个聚焦函数；`pipeline.py` 只做顺序编排、不含业务判断。
业务模块：`thread_parser, nesting, author_id, editor, version` ＋ 通用 `loader / normalizer / keyer / store / pipeline / cli`。

## 故意留空（功能开发面）
- `nesting.py` → `process()` 抛 `NotImplementedError`（对记录做单一职责变换：nesting；功能开发目标，未接入 happy-path）
- `version.py` → `process()` 抛 `NotImplementedError`（对记录做单一职责变换：version；功能开发目标，未接入 happy-path）

## 朴素实现短板（功能优化面）
- `thread_parser.py` → 朴素实现 + docstring 标注短板（对记录做单一职责变换：thread_parser；功能优化目标）
- `editor.py` → 朴素实现 + docstring 标注短板（对记录做单一职责变换：editor；功能优化目标）

> 缺口模块刻意未接入 happy-path，以保证基线可运行；它们正是出题要补的 0→1 / 1→n 功能。
