# relay-core/importer/packer.py
"""歷史格式切分（第 4.2 節，v0.3 規則）。純函式，可單元測試。

- 同一天的文字盡量合併成一則，換日就送出。
- 加入下一則會超過 2000 字 → 先送出，新的一則不再重複標日期。
- 附件單獨成一則（caption＋附件）。
- 日期標頭只出現在「當天第一則」。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Iterator

import formatting
from importer.reader import HistMsg


@dataclass
class Packet:
    date: str
    content: str
    msgs: list[HistMsg] = field(default_factory=list)
    attachment: HistMsg | None = None  # 附件封包才有（且尚未判斷大小）
    has_header: bool = False


class Packer:
    def __init__(self, tz: str, style: str = "large", last_header_date: str | None = None) -> None:
        self.tz = tz
        self.style = style
        self.last_header = last_header_date
        self._date: str | None = None
        self._lines: list[str] = []
        self._msgs: list[HistMsg] = []
        self._head = False

    # ---- 內部 ----
    def _need_header(self, date: str) -> bool:
        return date != self.last_header

    def _room(self, with_header: bool) -> int:
        return formatting.DISCORD_LIMIT - (len(formatting.header("0000-00-00", self.style)) + 1 if with_header else 0)

    def _flush(self) -> Iterator[Packet]:
        if self._msgs:
            body = "\n".join(self._lines)
            content = formatting.build_mirror_content(self._date or "", body, None, None, self.style, self._head)
            yield Packet(self._date or "", content, list(self._msgs), None, self._head)
            if self._head:
                self.last_header = self._date
        self._lines, self._msgs, self._head = [], [], False

    def _start(self, date: str) -> None:
        self._date = date
        self._head = self._need_header(date)

    # ---- 對外 ----
    def feed(self, m: HistMsg) -> Iterator[Packet]:
        date = formatting.local_date(m.dt, self.tz)
        if self._msgs and date != self._date:  # 規則 1：換日
            yield from self._flush()

        if m.type in ("image", "file", "video", "voice") and (m.rel_path or m.missing_reason):
            # 規則 3：附件單獨成一則
            yield from self._flush()
            head = self._need_header(date)
            yield Packet(date, "", [m], m, head)  # content 由送出端依附件大小組合
            if head:
                self.last_header = date
            return

        line = m.text
        if not self._msgs:
            self._start(date)
        room = self._room(self._head)
        used = sum(len(x) for x in self._lines) + max(len(self._lines) - 1, 0)
        if self._msgs and used + 1 + len(line) > room:  # 規則 2：會超過 2000 字
            yield from self._flush()
            self._start(date)
            room = self._room(self._head)
        if len(line) > room:  # 規則 4：單則超長
            self._lines.append(formatting.truncate(line, room))
            self._msgs.append(m)
            yield from self._flush()
            return
        self._lines.append(line)
        self._msgs.append(m)

    def finish(self) -> Iterator[Packet]:
        yield from self._flush()


def pack_all(msgs: Iterable[HistMsg], tz: str, style: str = "large",
             last_header_date: str | None = None) -> Iterator[Packet]:
    p = Packer(tz, style, last_header_date)
    for m in msgs:
        yield from p.feed(m)
    yield from p.finish()


def attachment_content(pkt: Packet, note: str | None, style: str = "large") -> str:
    """附件封包的文字：（標頭）＋caption＋（附件沒搬時的說明行，例如「📎 a.pdf（過大未搬移）」）。"""
    m = pkt.attachment
    assert m is not None
    return formatting.build_mirror_content(
        pkt.date, m.text or "", None, None, style, pkt.has_header, [note] if note else None)
