# forumweave — 抓主贴与楼中楼、折叠回复、多次编辑版本，重建线程树并增量补楼。

运行栈：Python 3.14 + 标准库 `sqlite3` + `httpx` + `pytest`（离线，无需联网）

## 运行方式
读 `fixtures/input.json` → `loader` → `normalizer` → 其余处理阶段 → `keyer` → 幂等落 `sqlite`。
```text
make run     # 运行离线流程，输出摄取数量和落库行数
make test    # 运行项目测试，包括流水线和幂等性检查
```

## 数据流与模块边界
每个业务阶段保持单一职责；`pipeline.py` 只负责按顺序调用阶段，不在编排层加入业务判断。
业务模块：`nesting`, `version`, `thread_parser`, `editor`, `author_id`。
通用模块：`loader / normalizer / keyer / store / pipeline / cli`。

## 模块说明
业务模块按独立阶段组织，通用模块负责加载、规范化、主键、存储和编排。
流水线按配置顺序调用处理阶段，输入输出均遵循项目定义的记录结构。

### editor：编辑事件的身份与顺序
`editor.py` 把摄入记录整理为带稳定身份的编辑事件，再做批量稳定化（纯函数）：

- **身份**：`event_id` 缺失时由主体/时间/操作者/正文派生指纹；重复消息（同 id 或同指纹）
  只保留首次到达者，去重台账随稳定事件交给 version。
- **顺序**：排序键为 `(时间, 版本号 rev, 到达序号 seq, event_id)`。时间相同且 rev 缺失或
  冲突、无法判定先后时打 `order_deferred=True`（暂缓判断），由确定性兜底键维持总顺序；
  迟到事件打 `late=True` 并按逻辑时间归位。`seq` 是事件固有属性，重放不依赖列表排列。
- **归并**：同主体、间隔不超过 `COALESCE_WINDOW` 的相邻编辑归并为一次编辑会话（连续快速
  编辑 = 一次正文变化）；与当前正文相同的提交记为 `no_op`；撤销（`kind="undo"`）不参与
  归并，按版本栈解释，支持指定目标版本。
- **证据**：每个稳定事件携带正文行级差异 `diff`、操作者 `actor` 与 `actor_evidence`
  （匿名编辑 `anonymous=True` 但保留原始证据）。
- **兼容**：不含编辑事件专属信号的旧格式记录走 naive 透传，不进入事件归并。

### version：差异与操作者证据的消费方
`version.py` 不重新判断身份/顺序，只消费稳定事件重放出确定性版本链：`no_op` 与重复事件
不产生版本；撤销按重放结果建版本并保留 `undo_of` 证据；同一组事件任意次、任意顺序重放，
版本链与最终正文一致。持久化采用两段式 ledger：`ingest_events` 幂等入库（pending）→
`confirm_versions` 单事务重放并确认；两步之间中断时，`recover_pending` 重放仍为 pending
的事件，版本号与最终正文和一次成功完全相同——恢复不产生第二次编辑。

## 持久化与测试
数据通过项目内 SQLite 存储，使用稳定键执行幂等写入；测试夹具位于项目测试目录，运行方式以 `Makefile` 为准。
