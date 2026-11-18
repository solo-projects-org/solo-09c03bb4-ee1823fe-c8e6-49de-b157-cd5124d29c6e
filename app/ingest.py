"""大图层流式受理：逐块读取、逐要素解析，解析期内存不随要素总数增长。

设计要点：
- ``StreamingFeatureCollection`` 是一个手写的增量 JSON 识别器，输入为
  ``Iterable[bytes]``（HTTP 请求体或 multipart 文件块），每次只在内部
  缓冲区里保留“当前 token / 当前要素”所需的字节，处理完即丢弃；
- 要素逐个以完整 dict 的形式产出，由调用方立即做几何校验并落盘暂存，
  不在内存中累积整版要素；
- multipart/form-data 通过 python-multipart 的事件式解析器逐块转译，
  上传文件不经过全量缓冲；
- 解析器只负责“语法完整性”。文件尾部被截断、括号/引号不闭合都会在
  读完整个输入后或 EOF 处抛出 :class:`StreamingJSONError`，调用方据此
  拒收，且整版不会发布。
"""
from __future__ import annotations

import codecs
import json
from typing import Any, Callable, Iterator

# 防御性上限：GeoJSON 嵌套深度与单 token 大小。正常乡镇边界数据远不会
# 触及；仅用于避免恶意/损坏输入在流式识别器里制造无界缓冲。
MAX_NESTING_DEPTH = 1000
MAX_TOKEN_BYTES = 256 * 1024 * 1024  # 单个坐标串/字符串不超过 256MiB


class StreamingJSONError(ValueError):
    """流式 GeoJSON 输入在语法上不完整或不合法（HTTP 400）。"""


# token 回放槽的“空”标记（不能用 None，因为 JSON null 是合法 token）
_NO_PUSH = object()

# 词法器 EOF 标记（同样不能用 None，None 是合法的 JSON null）
_EOF = object()


# ----------------------------------------------------------------------
# 增量 JSON 词法识别器
# ----------------------------------------------------------------------
class _Tokenizer:
    """对字节块迭代器做增量 JSON 分词。

    内部缓冲区始终是“尚未产出的剩余字节”，产出一个 token 后立即裁剪，
    因此稳态内存约等于当前 token（当前要素）大小，而不是整个请求体。
    """

    _WS = (0x20, 0x09, 0x0D, 0x0A)  # 空格、制表、回车、换行
    _STRUCT = b"{}[]:,"
    _LITERALS = ("true", "false", "null")

    def __init__(self, chunks: Iterator[bytes]) -> None:
        self._chunks = chunks
        self._buf = bytearray()
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self._pushed: Any = _NO_PUSH

    # -- 输入维护 --------------------------------------------------------
    def _extend(self) -> bool:
        """把下一个网络块追加到缓冲区；没有更多块时返回 False。

        注意调用方通常在“缓冲区已耗尽”时拉取，因此空块不能当作 EOF，
        必须继续取下一块。
        """
        while True:
            try:
                chunk = next(self._chunks)
            except StopIteration:
                return False
            self._buf.extend(chunk)
            if self._buf:
                return True

    def _drain_decoder(self) -> None:
        try:
            self._decoder.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            raise StreamingJSONError(f"GeoJSON 解析失败：{exc}") from exc

    def push(self, token: Any) -> None:
        """回放一个 token（实现单值 lookahead 用）。"""
        if self._pushed is not _NO_PUSH:
            raise StreamingJSONError("GeoJSON 解析失败：词法器内部状态错误")
        self._pushed = token

    # -- 主入口 ----------------------------------------------------------
    def next_token(self) -> Any:
        if self._pushed is not _NO_PUSH:
            token = self._pushed
            self._pushed = _NO_PUSH
            return token

        # 跳过空白（跨块）：只删除空白字节，非空白字节必须留给后续识别。
        # 只有“缓冲区已空且再无新块”才是真正 EOF。
        while not self._buf:
            if not self._extend():
                self._drain_decoder()
                return _EOF
        while self._buf[0] in self._WS:
            del self._buf[0]
            while not self._buf:
                if not self._extend():
                    self._drain_decoder()
                    return _EOF

        first = self._buf[0]
        if first in self._STRUCT:
            ch = self._buf[0:1].decode("ascii")
            del self._buf[0]
            return ch
        if first == 0x22:  # "
            return self._read_string()
        if first == 0x2D or 0x30 <= first <= 0x39:  # - 或 0-9
            return self._read_number()
        if 0x61 <= first <= 0x7A:  # a-z，true/false/null
            return self._read_literal()
        raise StreamingJSONError(
            f"GeoJSON 解析失败：非法字符 {bytes([first])!r}"
        )

    # -- 字符串 ----------------------------------------------------------
    def _decode_str(self, raw: bytes) -> str:
        try:
            text = self._decoder.decode(raw)
            value = json.loads(text)
        except UnicodeDecodeError as exc:
            raise StreamingJSONError(f"GeoJSON 解析失败：{exc}") from exc
        except json.JSONDecodeError as exc:
            raise StreamingJSONError(f"GeoJSON 解析失败：{exc}") from exc
        if not isinstance(value, str):
            raise StreamingJSONError("GeoJSON 解析失败：期望字符串")
        return value

    def _read_string(self) -> str:
        # 定位闭合引号，兼顾转义；raw 含两端引号，保证 json.loads 可解
        start = 0
        i = 1
        while True:
            if i >= len(self._buf):
                if len(self._buf) > MAX_TOKEN_BYTES:
                    raise StreamingJSONError("GeoJSON 解析失败：字符串超出大小上限")
                if not self._extend():
                    self._drain_decoder()
                    raise StreamingJSONError(
                        "GeoJSON 解析失败：字符串未闭合（文件可能被截断）"
                    )
                continue
            c = self._buf[i]
            if c == 0x5C:  # 反斜杠转义：跳过下一个字节（含 \uXXXX 的 u）
                i += 2
                continue
            if c == 0x22:  # 闭合引号
                raw = bytes(self._buf[start : i + 1])
                del self._buf[: i + 1]
                return self._decode_str(raw)
            if c < 0x20:
                raise StreamingJSONError(
                    "GeoJSON 解析失败：字符串内存在未转义控制字符"
                )
            i += 1

    # -- 数字与字面量 -----------------------------------------------------
    def _scan_token(self, is_token_char: Callable[[int], bool]) -> bytes:
        """通用 token 扫描，返回不含分隔符的原始字节（分隔符留在缓冲区）。

        每拉取一个新块都重新从当前位置判定：一旦下一字节不是 token
        字符就立即结束；只有确认输入 EOF 时，token 才允许在缓冲区
        末尾结束，从而保证跨块 token 不会被截断。
        """
        i = 0
        while True:
            if len(self._buf) > MAX_TOKEN_BYTES:
                raise StreamingJSONError("GeoJSON 解析失败：token 超出大小上限")
            if i < len(self._buf):
                if not is_token_char(self._buf[i]):
                    break
                i += 1
                continue
            # 扫描到缓冲区末尾：拉块后重新判定，拉不到才是真正 EOF
            if not self._extend():
                break
        raw = bytes(self._buf[:i])
        del self._buf[:i]
        return raw

    def _read_number(self) -> int | float:
        raw = self._scan_token(
            lambda c: not (c in self._STRUCT or c in self._WS)
        )
        try:
            return json.loads(raw.decode("ascii"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise StreamingJSONError(f"GeoJSON 解析失败：{exc}") from exc

    def _read_literal(self) -> Any:
        raw = self._scan_token(lambda c: 0x61 <= c <= 0x7A).decode("ascii")
        if raw not in self._LITERALS:
            raise StreamingJSONError(f"GeoJSON 解析失败：非法字面量 {raw!r}")
        # 注意必须显式映射：json.loads("null") 是 None，与 next_token
        # 用 None 表示 EOF 冲突，直接返回会被上层误判为输入结束。
        if raw == "true":
            return True
        if raw == "false":
            return False
        return None  # JSON null（合法 token，非 EOF）

    # -- 递归下降读取完整 JSON 值（用于 features 之外的头部字段） --------
    def read_value(self, depth: int = 0) -> Any:
        if depth > MAX_NESTING_DEPTH:
            raise StreamingJSONError("GeoJSON 解析失败：嵌套深度超出上限")
        token = self.next_token()
        if token is _EOF:
            raise StreamingJSONError(
                "GeoJSON 解析失败：输入不完整（文件可能被截断）"
            )
        if token in ("{", "["):
            close = "}" if token == "{" else "]"
            result: Any = {} if token == "{" else []
            first = self.next_token()
            if first == close:
                return result
            self.push(first)
            if token == "{":
                while True:
                    key = self.next_token()
                    if key is _EOF or not isinstance(key, str):
                        raise StreamingJSONError("GeoJSON 解析失败：对象键必须是字符串")
                    if self.next_token() != ":":
                        raise StreamingJSONError("GeoJSON 解析失败：对象缺少 ':'")
                    result[key] = self.read_value(depth + 1)
                    nxt = self.next_token()
                    if nxt == "}":
                        return result
                    if nxt != ",":
                        raise StreamingJSONError("GeoJSON 解析失败：对象缺少 ',' 或 '}'")
            else:
                while True:
                    result.append(self.read_value(depth + 1))
                    nxt = self.next_token()
                    if nxt == "]":
                        return result
                    if nxt != ",":
                        raise StreamingJSONError("GeoJSON 解析失败：数组缺少 ',' 或 ']'")
        if token in ("}", "]", ":", ","):
            raise StreamingJSONError("GeoJSON 解析失败：出现意外的结构符号")
        return token  # 字符串 / 数字 / true / false / null


# ----------------------------------------------------------------------
# FeatureCollection 流式结构
# ----------------------------------------------------------------------
class StreamingFeatureCollection:
    """逐要素产出 GeoJSON FeatureCollection。

    用法::

        fc = StreamingFeatureCollection(byte_chunks)
        header = fc.start()              # 读到 features 数组起点
        for feature in fc.features():    # 每次产出一个完整 Feature dict
            ...
        fc.finish()                      # 读完尾部，校验无多余数据
    """

    def __init__(self, chunks: Iterator[bytes]) -> None:
        self._tk = _Tokenizer(chunks)
        self._header: dict[str, Any] = {}
        self._features_seen = 0

    @property
    def header(self) -> dict[str, Any]:
        """features 同级的其他字段（如 crs）。"""
        return self._header

    def start(self) -> dict[str, Any]:
        """读取顶层对象头部，直到定位 features 数组；返回头部字段。"""
        if self._tk.next_token() != "{":
            raise StreamingJSONError("请求体必须是 GeoJSON 对象")
        first = self._tk.next_token()
        if first == "}":
            raise StreamingJSONError("仅支持 GeoJSON FeatureCollection")
        self._tk.push(first)
        while True:
            key = self._tk.next_token()
            if key is _EOF or not isinstance(key, str):
                raise StreamingJSONError("GeoJSON 解析失败：对象键必须是字符串")
            if self._tk.next_token() != ":":
                raise StreamingJSONError("GeoJSON 解析失败：对象缺少 ':'")
            if key == "features":
                if self._tk.next_token() != "[":
                    raise StreamingJSONError("GeoJSON 解析失败：features 必须是数组")
                return self._header
            self._header[key] = self._tk.read_value()
            nxt = self._tk.next_token()
            if nxt == "}":
                raise StreamingJSONError("仅支持 GeoJSON FeatureCollection")
            if nxt != ",":
                raise StreamingJSONError("GeoJSON 解析失败：对象缺少 ',' 或 '}'")

    def features(self) -> Iterator[dict]:
        """逐个产出 Feature；产出后调用方应立即校验/暂存并释放引用。"""
        first = self._tk.next_token()
        if first == "]":
            # 空数组：仍需读完尾部
            return
        self._tk.push(first)
        while True:
            feature = self._tk.read_value(depth=1)
            yield feature
            self._features_seen += 1
            nxt = self._tk.next_token()
            if nxt == "]":
                return
            if nxt != ",":
                raise StreamingJSONError(
                    "GeoJSON 解析失败：features 数组缺少 ',' 或 ']'"
                )

    def finish(self) -> None:
        """读完 features 之后的剩余顶层字段并确认输入恰好结束。"""
        first = self._tk.next_token()
        if first == "}":
            if self._tk.next_token() is not _EOF:
                raise StreamingJSONError(
                    "GeoJSON 解析失败：顶层对象之后存在多余数据"
                )
            return
        if first != ",":
            raise StreamingJSONError("GeoJSON 解析失败：对象缺少 ',' 或 '}'")
        while True:
            key = self._tk.next_token()
            if key is _EOF or not isinstance(key, str):
                raise StreamingJSONError("GeoJSON 解析失败：对象键必须是字符串")
            if self._tk.next_token() != ":":
                raise StreamingJSONError("GeoJSON 解析失败：对象缺少 ':'")
            self._header[key] = self._tk.read_value()
            nxt = self._tk.next_token()
            if nxt == "}":
                break
            if nxt != ",":
                raise StreamingJSONError("GeoJSON 解析失败：对象缺少 ',' 或 '}'")
        if self._tk.next_token() is not _EOF:
            raise StreamingJSONError(
                "GeoJSON 解析失败：顶层对象之后存在多余数据"
            )

    @property
    def features_seen(self) -> int:
        return self._features_seen


# ----------------------------------------------------------------------
# multipart/form-data 事件式转译（不缓冲整个文件）
# ----------------------------------------------------------------------
def multipart_file_chunks(
    content_type: str,
    chunks: Iterator[bytes],
    *,
    file_field: str = "file",
) -> Iterator[bytes]:
    """把 multipart/form-data 流逐块转译为 ``file`` 字段的文件字节流。

    - 仅转发名为 ``file`` 且带 filename 的部分；其他表单字段不缓存；
    - 任意时刻内存中只保留一个网络块；
    - 尾部缺少结束 boundary（截断）时抛 :class:`StreamingJSONError`。
    """
    try:
        from python_multipart.multipart import (
            MultipartParseError,
            MultipartParser,
            parse_options_header,
        )
    except ImportError:  # 旧版包名 multipart
        from multipart.multipart import (  # type: ignore[no-redef]
            MultipartParseError,
            MultipartParser,
            parse_options_header,
        )

    _, options = parse_options_header(content_type)
    boundary = options.get(b"boundary")
    if not boundary:
        raise StreamingJSONError("multipart 请求缺少 boundary，无法解析上传文件")
    # 规范的 multipart 收尾：CRLF + "--" + boundary + "--" + CRLF。
    # 底层解析器对少写结尾 CRLF/短横的情况过于宽容，这里额外保留尾部
    # 窗口做严格校验，确保“尾部损坏/截断”一定被拒收。
    closing = b"\r\n--" + bytes(boundary) + b"--\r\n"
    tail = bytearray()

    state = {
        "in_file": False,
        "have_file": False,
        "ended": False,
        "pending": bytearray(),
        "header_field": b"",
        "header_value": b"",
        "disposition": b"",
    }

    def on_header_field(data: bytes, start: int, end: int) -> None:
        state["header_field"] = state.get("header_field", b"") + data[start:end]

    def on_header_value(data: bytes, start: int, end: int) -> None:
        state["header_value"] = state.get("header_value", b"") + data[start:end]

    def on_header_end() -> None:
        # 一个头部行结束：若是 Content-Disposition 则记录原始值，
        # 待所有头部收齐后统一判定本 part 是否为目标文件字段。
        if state.get("header_field", b"").lower() == b"content-disposition":
            state["disposition"] = bytes(state.get("header_value", b""))
        state["header_field"] = b""
        state["header_value"] = b""

    def on_headers_finished() -> None:
        state["in_file"] = False
        _, disp = parse_options_header(bytes(state.get("disposition", b"")))
        if (
            disp.get(b"name") == file_field.encode("ascii")
            and disp.get(b"filename") is not None
        ):
            state["in_file"] = True
            state["have_file"] = True
        state["header_field"] = b""
        state["header_value"] = b""
        state["disposition"] = b""

    def on_part_data(data: bytes, start: int, end: int) -> None:
        if state["in_file"] and end > start:
            state["pending"].extend(data[start:end])

    def on_part_end() -> None:
        state["in_file"] = False

    def on_end() -> None:
        # 仅当完整的结束 boundary（含 --）被消费时才触发；
        # 尾部截断的请求不会收到该事件。
        state["ended"] = True

    parser = MultipartParser(
        boundary,
        callbacks={
            "on_header_field": on_header_field,
            "on_header_value": on_header_value,
            "on_header_end": on_header_end,
            "on_headers_finished": on_headers_finished,
            "on_part_data": on_part_data,
            "on_part_end": on_part_end,
            "on_end": on_end,
        },
    )

    def drain() -> Iterator[bytes]:
        if state["pending"]:
            payload = bytes(state["pending"])
            state["pending"].clear()
            yield payload

    try:
        for chunk in chunks:
            # 仅保留与收尾标记等长的尾部窗口（定长，不随文件增长）
            tail.extend(chunk)
            if len(tail) > len(closing):
                del tail[: len(tail) - len(closing)]
            parser.write(chunk)
            yield from drain()
        parser.finalize()
        yield from drain()
    except MultipartParseError as exc:
        raise StreamingJSONError(f"multipart 文件解析失败：{exc}") from exc

    if not state["ended"] or bytes(tail) != closing:
        raise StreamingJSONError(
            "multipart 请求缺少完整结束 boundary（文件可能被截断）"
        )
    if not state["have_file"]:
        raise StreamingJSONError(
            "multipart 上传需提供名为 file 的 GeoJSON 文件字段"
        )


# ----------------------------------------------------------------------
# 异步请求体 -> 同步字节迭代器（在工作线程中驱动，不阻塞事件循环）
# ----------------------------------------------------------------------
def async_stream_to_sync_iter(make_async_iter: Callable[[], Any]) -> Iterator[bytes]:
    """在工作线程内同步消费一个异步字节流。

    ``make_async_iter`` 返回异步迭代器（在事件循环线程中创建），
    之后每个 chunk 通过 anyio 跨线程拉取，事件循环保持可服务其他请求。
    """
    from anyio import from_thread

    async_iter = from_thread.run_sync(make_async_iter)
    while True:
        try:
            chunk = from_thread.run(async_iter.__anext__)
        except StopAsyncIteration:
            return
        yield chunk
