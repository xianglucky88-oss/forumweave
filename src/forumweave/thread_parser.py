"""单一职责：把单个论坛页面解析成结构化帖子列表（thread_parser）。

输入：携带 ``html_bytes``（原始字节，推荐，可处理编码异常）或 ``html``
（已解码文本）的页面记录；其余记录原样透传，只打阶段标记。

输出：在同一记录上附加以下字段，沿现有流水线（author_id → editor → keyer →
store）继续传递——

- ``posts``       : 帖子节点列表。每个节点包含 ``body``（正文，已剔除引用块）、
  ``quotes``（引用块列表，引用只作为线索保留，绝不升格为帖子，否则线程重建会
  平白多出帖子）、``author``（作者线索 ``{"name", "uid"}``）、``parent_hint``
  （父回复线索）、``floor``、``post_id`` / ``post_key``（去重键）、
  ``empty_reason``（空正文原因）以及 ``pos``（源页面位置：
  ``{"page", "block", "offset"}``，offset 为起始标签在页面文本中的字符偏移）。
- ``page_meta``   : 分页尾部信息 ``{"page", "total_pages", "next"}``；
  分页尾部本身永远不会被当成帖子。
- ``parse_errors``: 区块级诊断列表。每个失败区块是独立的恢复范围，诊断
  ``{"block", "offset", "kind", "detail", "excerpt"}`` 指回失败区块。
- ``encoding``    : 编码判定 ``{"declared", "sniffed", "used", "anomaly",
  "replacements"}``。
- ``deduped``     : 本页内因重复抓取被去重的帖子数。

折叠未加载 vs 正文确实为空
--------------------------
``empty_reason == "collapsed_not_loaded"``：页面只给了占位，正文根本没随页面
下发，应当局部重试（展开/补抓），不能当作空帖。样例::

    <div class="post collapsed" data-post-id="t1-p4">
      <span class="author">赵六</span>
      <div class="body collapsed" data-collapsed="1" data-lazy-url="/api/posts/t1-p4"></div>
      <div class="collapse-tip">该回复被折叠，加载后可展开</div>
    </div>

判定依据：帖子或正文容器带 ``collapsed`` 类、``data-collapsed`` 或
``data-lazy-*`` 属性，或存在折叠提示占位元素，且正文文本为空。

``empty_reason == "empty"``：正文容器完整渲染，但内容确实为空（留白/被清空）。
样例::

    <div class="post" data-post-id="t1-p5">
      <span class="author">孙七</span>
      <div class="body">   </div>
    </div>

判定依据：正文容器存在、无任何折叠/懒加载标记、文本为空白。

局部重试（每个失败区块是单独的恢复范围）
----------------------------------------
记录上附带 ``retry = {"blocks": {"<block 序号>": "<该区块的 HTML 片段>"}}`` 时，
只重解析指定区块并按 ``pos.block`` 归并回 ``posts``（同 ``post_key`` 替换、
否则按区块序插入），其余区块的旧输出原样保留；对应诊断被清除，仍失败的区块
重新生成诊断。折叠区块展开后也走同一条路径（部分加载的恢复）。

同一页面重复抓取（含内容被重复拼接的抓取故障）按 ``post_key`` 去重，不制造
重复帖子；页面改版后通过同义词类名（post/message、body/msg-body、quote/cite、
pagination/page-nav 等）提取同一批逻辑帖子。
"""
import hashlib
import re
from html.parser import HTMLParser

PARSER_VERSION = "v2"

# ------------------------------------------------------------- 词表（含改版同义词）

POST_TOKENS = {"post", "message", "entry", "msg"}
CONTAINER_TOKENS = {"posts", "post-list", "post-container", "messages",
                    "message-list", "thread", "thread-list"}
BODY_TOKENS = {"body", "content", "text", "msg-body", "post-body",
               "post-content", "message-body", "entry-content"}
QUOTE_TOKENS = {"quote", "cite", "quotation", "reference", "quote-block"}
QUOTE_AUTHOR_TOKENS = {"quote-author", "cite-author", "quote-meta", "quote-by"}
AUTHOR_TOKENS = {"author", "username", "nickname", "poster", "user",
                 "user-name", "uname"}
FLOOR_TOKENS = {"floor", "post-floor", "floor-no", "floor-num"}
REPLY_TOKENS = {"reply-to", "in-reply-to", "replyto", "parent-hint"}
COLLAPSED_TOKENS = {"collapsed", "folded", "collapsed-post"}
TIP_TOKENS = {"collapse-tip", "collapsed-placeholder", "expand-tip",
              "fold-tip", "collapse-placeholder"}
PAGER_TOKENS = {"pagination", "pager", "page-nav", "pages", "page-links"}
CURRENT_TOKENS = {"current", "active", "on"}
META_TOKENS = {"meta", "post-meta", "post-head", "post-header"}

_POST_ID_RE = re.compile(r"^(?:post|message|entry|msg)[-_]\S*", re.I)
_FLOOR_RE = re.compile(r"^\s*#?\s*(\d+)\s*(?:楼|层)?\s*$")
_PAGE_FLOOR_RE = re.compile(r"第\s*(\d+)\s*页\D{0,8}?#?\s*(\d+)")
_HASH_FLOOR_RE = re.compile(r"#\s*(\d+)")
_LOU_RE = re.compile(r"(\d+)\s*楼")
_AT_RE = re.compile(r"@([^\s，,。]+)")
_WROTE_RE = re.compile(r"([^\s:：]{1,20})\s*(?:写道|说|wrote)\s*[:：]")
_META_CHARSET_RE = re.compile(
    br"<meta[^>]*?charset\s*=\s*[\"']?([A-Za-z0-9_\-]+)", re.I)
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
              "link", "meta", "param", "source", "track", "wbr"}


class BlockParseError(Exception):
    """单个帖子区块的解析失败——每个失败区块是独立的恢复范围。"""

    def __init__(self, kind, detail, offset=0):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail
        self.offset = offset

    def to_diag(self, block, source):
        """生成指回失败区块的诊断：区块序号 + 字符偏移 + 源页面摘录。"""
        excerpt = " ".join(source[self.offset:self.offset + 80].split())
        return {"block": block, "offset": self.offset, "kind": self.kind,
                "detail": self.detail, "excerpt": excerpt}


# ------------------------------------------------------------- 容错 DOM

class _El:
    __slots__ = ("tag", "attrs", "children", "offset")

    def __init__(self, tag, attrs, offset):
        self.tag = tag
        self.attrs = {k: (v if v is not None else "") for k, v in attrs}
        self.children = []
        self.offset = offset


class _Text:
    __slots__ = ("data",)

    def __init__(self, data):
        self.data = data


def _line_starts(text):
    starts = [0]
    for m in re.finditer("\n", text):
        starts.append(m.end())
    return starts


class _TreeBuilder(HTMLParser):
    """宽松构建 DOM：嵌套损坏时自动闭合到最近匹配标签，多余闭标签忽略。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _El("#root", [], 0)
        self.stack = [self.root]
        self._starts = [0]

    def feed(self, text):
        self._starts = _line_starts(text)
        super().feed(text)
        self.close()

    def _offset(self):
        line, col = self.getpos()
        return self._starts[line - 1] + col

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        el = _El(tag, attrs, self._offset())
        self.stack[-1].children.append(el)
        if tag not in _VOID_TAGS:
            self.stack.append(el)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(_El(tag.lower(), attrs, self._offset()))

    def handle_endtag(self, tag):
        tag = tag.lower()
        for j in range(len(self.stack) - 1, 0, -1):
            if self.stack[j].tag == tag:
                del self.stack[j:]
                return
        # 没有匹配的开放标签：多余闭标签，忽略

    def handle_data(self, data):
        if data:
            self.stack[-1].children.append(_Text(data))


# ------------------------------------------------------------- 树工具

def _classes(el):
    return set(el.attrs.get("class", "").split())


def _find_first(el, pred, skip=None):
    for c in el.children:
        if not isinstance(c, _El):
            continue
        if skip is not None and skip(c):
            continue
        if pred(c):
            return c
        r = _find_first(c, pred, skip)
        if r is not None:
            return r
    return None


def _find_all(el, pred, skip=None):
    out = []

    def rec(node):
        for c in node.children:
            if not isinstance(c, _El):
                continue
            if skip is not None and skip(c):
                continue
            if pred(c):
                out.append(c)
            rec(c)

    rec(el)
    return out


def _text_of(el, skip=None):
    parts = []

    def rec(node):
        for c in node.children:
            if isinstance(c, _Text):
                parts.append(c.data)
            elif skip is not None and skip(c):
                continue
            else:
                parts.append(" ")  # 元素边界分隔，归一化时折叠
                rec(c)
                parts.append(" ")

    rec(el)
    return " ".join("".join(parts).split())


# ------------------------------------------------------------- 结构判定

def _is_quote(el):
    return el.tag == "blockquote" or bool(_classes(el) & QUOTE_TOKENS)


def _is_post_block(el):
    if _is_quote(el) or _classes(el) & CONTAINER_TOKENS:
        return False
    if el.attrs.get("data-post-id"):
        return True
    if _POST_ID_RE.match(el.attrs.get("id", "")):
        return True
    return bool(_classes(el) & POST_TOKENS)


def _is_body_el(el):
    return bool(_classes(el) & BODY_TOKENS)


def _is_nonbody(el):
    return _is_quote(el) or bool(_classes(el) & (REPLY_TOKENS | TIP_TOKENS
                                                | PAGER_TOKENS))


def _is_nonbody_or_meta(el):
    return (_is_nonbody(el)
            or bool(_classes(el) & (AUTHOR_TOKENS | FLOOR_TOKENS | META_TOKENS))
            or el.tag in ("header", "nav"))


def _find_post_blocks(root):
    """帖子区块（文档序）。引用块/分页尾部不算帖子；嵌套在另一帖子
    内部的帖子区块视为嵌套损坏，仍恢复为独立区块。"""
    out = []

    def rec(el):
        for c in el.children:
            if not isinstance(c, _El):
                continue
            if _is_quote(c):
                continue
            if _is_post_block(c):
                out.append(c)
            rec(c)

    rec(root)
    return out


def _find_quotes(root):
    """最外层引用块（嵌套引用归入外层）。"""
    out = []

    def rec(el):
        for c in el.children:
            if not isinstance(c, _El):
                continue
            if _is_quote(c):
                out.append(c)
            else:
                rec(c)

    rec(root)
    return out


def _has_collapse_marker(post_el, body_el):
    for el in (post_el, body_el):
        if el is None:
            continue
        if _classes(el) & COLLAPSED_TOKENS:
            return True
        if str(el.attrs.get("data-collapsed", "")).lower() in ("1", "true", "yes"):
            return True
        if any(a.startswith("data-lazy") for a in el.attrs):
            return True
    return _find_first(post_el,
                       lambda e: bool(_classes(e) & TIP_TOKENS),
                       skip=_is_quote) is not None


# ------------------------------------------------------------- 区块内提取

def _find_author(post_el):
    uid = post_el.attrs.get("data-uid") or post_el.attrs.get("data-user-id")
    name = post_el.attrs.get("data-author") or post_el.attrs.get("data-user")
    el = _find_first(post_el, lambda e: bool(_classes(e) & AUTHOR_TOKENS),
                     skip=_is_quote)
    if el is not None:
        name = name or _text_of(el) or None
        uid = uid or el.attrs.get("data-uid") or None
    return {"name": name or None, "uid": uid or None}


def _find_floor(post_el):
    el = _find_first(post_el, lambda e: bool(_classes(e) & FLOOR_TOKENS),
                     skip=_is_quote)
    if el is None:
        return None
    m = _FLOOR_RE.match(_text_of(el))
    return int(m.group(1)) if m else None


def _find_parent_hint(post_el):
    for attr in ("data-parent", "data-parent-id", "data-reply-to"):
        v = post_el.attrs.get(attr)
        if v:
            return {"post": v}
    rt = _find_first(post_el, lambda e: bool(_classes(e) & REPLY_TOKENS),
                     skip=_is_quote)
    if rt is None:
        return None
    text = _text_of(rt)
    m = _PAGE_FLOOR_RE.search(text)
    if m:
        return {"page": int(m.group(1)), "floor": int(m.group(2))}
    m = _HASH_FLOOR_RE.search(text) or _LOU_RE.search(text)
    if m:
        return {"floor": int(m.group(1))}
    m = _AT_RE.search(text)
    if m:
        return {"author": m.group(1)}
    return {"raw": text} if text else None


def _parse_quote(q):
    text = _text_of(q)
    author = (q.attrs.get("data-author") or q.attrs.get("data-cite")
              or q.attrs.get("cite"))
    if not author:
        ae = _find_first(q, lambda e: bool(_classes(e) & QUOTE_AUTHOR_TOKENS))
        if ae is not None:
            author = _text_of(ae) or None
    if not author:
        m = _WROTE_RE.search(text)
        if m:
            author = m.group(1)
    ref = {}
    if q.attrs.get("data-post-id"):
        ref["post"] = q.attrs["data-post-id"]
    if q.attrs.get("data-page"):
        try:
            ref["page"] = int(q.attrs["data-page"])
        except ValueError:
            pass
    if "page" not in ref:
        m = re.search(r"第\s*(\d+)\s*页", text)
        if m:
            ref["page"] = int(m.group(1))
    if q.attrs.get("data-floor"):
        try:
            ref["floor"] = int(q.attrs["data-floor"])
        except ValueError:
            pass
    if "floor" not in ref:
        m = _HASH_FLOOR_RE.search(text)
        if m:
            ref["floor"] = int(m.group(1))
    quote = {"author": author or None, "text": text}
    if ref:
        quote["ref"] = ref
    return quote


def _content_key(author, body, quotes):
    raw = "|".join([author.get("name") or "", body]
                   + [q["text"] for q in quotes])
    return "h:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _parse_block(el, *, page, block):
    post_id = el.attrs.get("data-post-id") or None
    if post_id is None:
        idv = el.attrs.get("id", "")
        if _POST_ID_RE.match(idv):
            post_id = idv
    author = _find_author(el)
    body_el = _find_first(el, _is_body_el, skip=_is_quote)
    collapsed = _has_collapse_marker(el, body_el)
    quotes = [_parse_quote(q) for q in _find_quotes(body_el or el)]
    flags = []
    if body_el is not None:
        body = _text_of(body_el, skip=_is_nonbody)
    elif collapsed:
        body = ""
    else:
        body = _text_of(el, skip=_is_nonbody_or_meta)
        if not body:
            raise BlockParseError(
                "missing-body",
                "post block has no body container and no collapsed marker",
                el.offset)
        flags.append("loose-body")
    if "�" in body or any("�" in q["text"] for q in quotes):
        flags.append("suspect-encoding")
    empty_reason = None
    if not body:
        # 折叠未加载（内容未随页面下发）≠ 正文确实为空（容器完整但无内容）
        empty_reason = "collapsed_not_loaded" if collapsed else "empty"
    return {
        "post_id": post_id,
        "post_key": post_id or _content_key(author, body, quotes),
        "floor": _find_floor(el),
        "author": author,
        "body": body,
        "quotes": quotes,
        "parent_hint": _find_parent_hint(el),
        "empty_reason": empty_reason,
        "flags": flags,
        "pos": {"page": page, "block": block, "offset": el.offset},
    }


def _resolve_hints(posts):
    """楼层号 → 本页 post_id；无显式回复对象时回退到引用块线索。"""
    floor_map = {}
    for p in posts:
        if p.get("floor") is not None and p.get("post_id"):
            floor_map.setdefault(p["floor"], p["post_id"])
    for p in posts:
        ph = p.get("parent_hint")
        if ph and "post" not in ph and "floor" in ph and "page" not in ph:
            pid = floor_map.get(ph["floor"])
            if pid and pid != p.get("post_id"):
                ph["post"] = pid
        if not ph:
            for q in p.get("quotes") or []:
                ref = q.get("ref") or {}
                if ref.get("post"):
                    hint = {"post": ref["post"], "via": "quote"}
                    if ref.get("page") is not None:
                        hint["page"] = ref["page"]
                    p["parent_hint"] = hint
                    break
    return posts


def _parse_pagination(root):
    pager = _find_first(root, lambda e: bool(_classes(e) & PAGER_TOKENS))
    if pager is None:
        return {}
    current = None
    cur_el = _find_first(pager, lambda e: bool(_classes(e) & CURRENT_TOKENS)
                         or e.tag in ("strong", "b"))
    if cur_el is not None:
        m = re.search(r"\d+", _text_of(cur_el))
        if m:
            current = int(m.group())
    nums = []
    next_href = None
    for a in _find_all(pager, lambda e: e.tag == "a"):
        t = _text_of(a)
        if t.isdigit():
            nums.append(int(t))
        rel = a.attrs.get("rel", "")
        if next_href is None and ("next" in rel
                                  or re.search(r"下一页|下页|next|›|»", t, re.I)):
            next_href = a.attrs.get("href") or None
    total = max(nums + ([current] if current is not None else []),
                default=None)
    return {"page": current, "total_pages": total, "next": next_href}


# ------------------------------------------------------------- 编码

def _decode_page(raw, declared=None):
    """声明编码 → meta 嗅探 → BOM → utf-8 → gb18030 逐级回退，标记异常。"""
    info = {"declared": declared, "sniffed": None, "used": None,
            "anomaly": False, "replacements": 0}
    if isinstance(raw, str):
        info["used"] = "unicode"
        return raw, info
    m = _META_CHARSET_RE.search(raw[:4096])
    if m:
        info["sniffed"] = m.group(1).decode("ascii", "replace").lower()
    bom = None
    if raw.startswith(b"\xef\xbb\xbf"):
        bom = "utf-8-sig"
    elif raw.startswith(b"\xff\xfe"):
        bom = "utf-16-le"
    elif raw.startswith(b"\xfe\xff"):
        bom = "utf-16-be"
    candidates = []
    for enc in (declared, info["sniffed"], bom, "utf-8", "gb18030"):
        if enc and enc.lower() not in {c.lower() for c in candidates}:
            candidates.append(enc)
    text = None
    for enc in candidates:
        try:
            text = raw.decode(enc)
            info["used"] = enc
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if text is None:
        text = raw.decode("utf-8", "replace")
        info["used"] = "utf-8+replace"
    info["replacements"] = text.count("�")
    expected = (declared or info["sniffed"] or bom or "utf-8").lower()
    info["anomaly"] = info["used"].lower() != expected or info["replacements"] > 0
    return text, info


# ------------------------------------------------------------- 页面级解析

def parse_page(text, *, page=None):
    """解析整页：帖子（去重）、分页尾部、区块级诊断。"""
    builder = _TreeBuilder()
    builder.feed(text)
    blocks = _find_post_blocks(builder.root)
    posts = []
    errors = []
    for i, el in enumerate(blocks):
        try:
            posts.append(_parse_block(el, page=page, block=i))
        except BlockParseError as e:
            errors.append(e.to_diag(i, text))
        except Exception as e:  # 局部失败不拖垮整页
            errors.append(BlockParseError(
                "internal", f"{type(e).__name__}: {e}", el.offset
            ).to_diag(i, text))
    _resolve_hints(posts)
    seen = set()
    uniq = []
    deduped = 0
    for p in posts:
        if p["post_key"] in seen:
            deduped += 1  # 重复抓取/重复拼接：不制造重复帖子
            continue
        seen.add(p["post_key"])
        uniq.append(p)
    return {"posts": uniq, "page_meta": _parse_pagination(builder.root),
            "errors": errors, "deduped": deduped}


def parse_block_fragment(fragment, *, page=None, block=0):
    """重试路径：把单个区块的 HTML 片段解析成一个帖子节点。"""
    builder = _TreeBuilder()
    builder.feed(fragment)
    blocks = _find_post_blocks(builder.root)
    if len(blocks) != 1:
        raise BlockParseError(
            "fragment-count",
            f"retry fragment must contain exactly 1 post block, got {len(blocks)}",
            0)
    return _parse_block(blocks[0], page=page, block=block)


def _apply_retry(rec, retry):
    """只重解析 retry 指定的区块并归并；其余区块旧输出原样保留。"""
    blocks = (retry or {}).get("blocks") or {}
    if not blocks:
        return rec
    rec = dict(rec)
    posts = list(rec.get("posts") or [])
    errors = list(rec.get("parse_errors") or [])
    page = rec.get("page_file") or rec.get("id")
    retried = []
    for key in sorted(blocks, key=lambda k: int(k)):
        i = int(key)
        errors = [e for e in errors if e.get("block") != i]
        try:
            post = parse_block_fragment(blocks[key], page=page, block=i)
        except BlockParseError as e:
            errors.append(e.to_diag(i, blocks[key]))
            continue
        post["pos"]["via"] = "retry"
        for j, old in enumerate(posts):
            if old.get("post_key") == post["post_key"]:
                posts[j] = post  # 同键替换（如折叠区块展开）
                break
        else:
            j = 0
            while j < len(posts) and (posts[j].get("pos") or {}).get("block", 0) < i:
                j += 1
            posts.insert(j, post)
        retried.append(i)
    _resolve_hints(posts)
    rec["posts"] = posts
    rec["parse_errors"] = sorted(errors, key=lambda e: e.get("block", 0))
    rec["retried"] = retried
    return rec


# ------------------------------------------------------------- 流水线入口

def process(record):
    rec = dict(record)
    raw = rec.pop("html_bytes", None)
    retry = rec.pop("retry", None)
    html = rec.get("html")
    if raw is None and html is None:
        # 非页面记录：原样透传；若带 retry 则只合并重试区块
        rec.setdefault("_stage_thread_parser", PARSER_VERSION)
        return _apply_retry(rec, retry) if retry else rec
    if raw is not None:
        text, enc = _decode_page(raw, declared=rec.get("declared_charset"))
    else:
        text, enc = _decode_page(html, declared=rec.get("declared_charset"))
    rec["encoding"] = enc
    page = rec.get("page_file") or rec.get("id")
    parsed = parse_page(text, page=page)
    rec["html"] = text
    rec["posts"] = parsed["posts"]
    rec["page_meta"] = parsed["page_meta"]
    rec["parse_errors"] = parsed["errors"]
    rec["deduped"] = parsed["deduped"]
    rec["parser"] = {"name": "thread_parser", "version": PARSER_VERSION}
    rec["_stage_thread_parser"] = PARSER_VERSION
    if retry:
        rec = _apply_retry(rec, retry)
    return rec
