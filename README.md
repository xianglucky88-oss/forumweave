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

## thread_parser 解析约定
`thread_parser` 把单页 HTML 拆成结构化帖子，结果随记录沿流水线落库：

- `posts[].body`：正文，已剔除引用块；`posts[].quotes`：引用块列表——
  引用只作为线索（含 `ref` 跨页引用目标）保留，**绝不升格为帖子**，
  否则线程重建会平白多出帖子。
- `posts[].author` / `posts[].parent_hint`：作者线索与父回复提示
  （`回复 #2` 解析成本页 `post_id`，跨页的保留 `{page, floor}`）。
- `posts[].pos`：源页面位置 `{page, block, offset}`，offset 为起始标签
  在页面文本中的字符偏移。
- `page_meta`：分页尾部（当前页/总页数/下一页），分页尾部不算帖子。
- `parse_errors`：区块级诊断 `{block, offset, kind, detail, excerpt}`，
  指回失败区块；局部失败不拖垮整页。
- 同一页面重复抓取（含内容重复拼接的抓取故障）按 `post_key` 去重，
  不制造重复帖子；页面改版后按同义词类名提取同一批逻辑帖子。

### 折叠未加载 ≠ 正文为空
`empty_reason == "collapsed_not_loaded"`：正文没随页面下发，应局部重试：

```html
<div class="post collapsed" data-post-id="t1-p4">
  <span class="author">赵六</span>
  <div class="body collapsed" data-collapsed="1" data-lazy-url="/api/posts/t1-p4"></div>
  <div class="collapse-tip">该回复被折叠，加载后可展开</div>
</div>
```

判定：帖子或正文容器带 `collapsed` 类 / `data-collapsed` / `data-lazy-*`，
或存在折叠提示占位元素，且正文为空。

`empty_reason == "empty"`：容器完整渲染但内容确实为空：

```html
<div class="post" data-post-id="t1-p5">
  <span class="author">孙七</span>
  <div class="body">   </div>
</div>
```

判定：正文容器存在、无任何折叠/懒加载标记、文本为空白。

### 局部重试（每个失败区块是单独的恢复范围）
给记录挂 `retry={"blocks": {"<block 序号>": "<该区块 HTML 片段>"}}` 再交给
`thread_parser.process`：只重解析指定区块并按 `pos.block` 归并（同 `post_key`
替换、否则按序插入），其余区块旧输出原样保留，对应诊断被清除；折叠区块
展开补抓也走同一路径。

离线页面夹具在 `fixtures/pages/`：跨页引用（`thread_p2.html`）、嵌套标签
损坏（`thread_broken.html`）、编码异常（`thread_gbk.html`，GBK 字节声明成
UTF-8）、重复抓取拼接（`thread_p1_dup.html`）、页面改版（`thread_p1_v2.html`）。
测试对比重复抓取、局部重试及改版前后的节点数量与旧输出。

## 持久化与测试
数据通过项目内 SQLite 存储，使用稳定键执行幂等写入；测试夹具位于项目测试目录，运行方式以 `Makefile` 为准。
