"""召回实物追溯:重放交接 / 装拆事件,追溯命中召回的库存实物经过的保管地点。

库存实物由库存扫描串的 GTIN + AI 21 序列号唯一标识;批号(AI 10)、有效期(AI 17,
含字段缺失)以库存扫描为基准。库存扫描须全部合法、含 AI 21 且 GTIN+序列号唯一,
否则整个请求返回 422。

事件先做与重放状态无关的静态校验(扫描合法、含序列号、引用库存实物、批号 / 有效期
与库存一致),不通过者按原下标报错且不生效;通过者按 occurred_at 转 UTC 升序、
同一时刻依输入顺序重放:

- 交接(handoff)仅作用于无父件(最外层容器)实物:首次交接 from_location 为 null,
  此后 from_location 须等于当前持有人;成功后该容器及其全部后代一并改持新地点,
  各后代的「经过地点」均追加该新地点。
- 装入(pack):子件须无父件、双方持有人相同(均已完成首次交接),且不能自装 / 成环;
  装入不改变持有人,不产生新的经过地点。
- 取出(unpack):指定父件须是子件的当前直接父件;取出后子件保留当时持有人。
- 失败事件不改变任何状态,不影响后续事件。

只对命中召回的库存实物,按 GTIN、序列号排序返回其按成功交接首次到达顺序去重的
保管地点;事件错误按原下标返回。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as dt_date
from datetime import timezone

from fastapi import HTTPException

from .gs1 import ParsedScan, ScanParseError, parse_scan
from .matcher import match_rules
from .models import (
    CustodyEventError,
    RecallCustodyTraceItem,
    RecallCustodyTraceRequest,
    RecallCustodyTraceResponse,
    TraceEvent,
    TraceHandoffEvent,
    TraceLinkEvent,
)

# 实物标识:GTIN + AI 21 序列号
ItemKey = tuple[str, str]


@dataclass
class _ItemState:
    """单件库存实物的重放状态。"""

    batch: str | None
    expiry: dt_date | None
    holder: str | None = None  # 当前持有人;首次成功交接前为 None
    parent: ItemKey | None = None  # 当前直接父件
    children: set[ItemKey] = field(default_factory=set)  # 当前直接子件
    visits: list[str] = field(default_factory=list)  # 成功交接首次到达顺序去重的地点


def _unprocessable(reason: str) -> HTTPException:
    return HTTPException(status_code=422, detail=reason)


def _item_label(key: ItemKey) -> str:
    return f"实物 (GTIN {key[0]}, 序列号 '{key[1]}')"


def _fmt_value(value: object) -> str:
    return "(缺失)" if value is None else f"'{value}'"


def _parse_event_scan(
    raw_scan: str, role: str, index: int
) -> tuple[ItemKey | None, ParsedScan | None, str | None]:
    """解析事件一端的扫描串;失败时返回原因(含原下标与角色)。

    role 为 "父件" / "子件";交接事件传空串。
    """
    prefix = f"的{role}" if role else ""
    try:
        parsed = parse_scan(raw_scan)
    except ScanParseError as exc:
        return None, None, f"事件 {index}{prefix}扫描非法: {exc.reason}"
    if parsed.serial is None:
        return None, None, f"事件 {index}{prefix}扫描串缺少 AI 21 序列号,无法识别实物"
    return (parsed.gtin, parsed.serial), parsed, None


def _descendants(root: ItemKey, states: dict[ItemKey, _ItemState]) -> list[ItemKey]:
    """返回 root 的全部后代(不含 root 自身),按包装树前序遍历。"""
    out: list[ItemKey] = []
    stack: list[ItemKey] = [root]
    while stack:
        node = stack.pop()
        for child in sorted(states[node].children, reverse=True):
            out.append(child)
            stack.append(child)
    return out


def trace_custody(request: RecallCustodyTraceRequest) -> RecallCustodyTraceResponse:
    """解析库存、校验并重放事件,返回命中实物的经过地点与事件错误。"""

    # 1. 库存扫描:须全部合法、含 AI 21 且 GTIN+序列号唯一,否则整单 422
    inventory: dict[ItemKey, ParsedScan] = {}
    states: dict[ItemKey, _ItemState] = {}
    first_seen: dict[ItemKey, int] = {}
    for index, raw in enumerate(request.scans):
        try:
            parsed = parse_scan(raw)
        except ScanParseError as exc:
            raise _unprocessable(f"库存扫描 {index} 非法: {exc.reason}") from exc
        if parsed.serial is None:
            raise _unprocessable(f"库存扫描 {index} 缺少 AI 21 序列号,无法识别实物")
        key = (parsed.gtin, parsed.serial)
        if key in first_seen:
            raise _unprocessable(
                f"库存扫描 {index} 与库存扫描 {first_seen[key]} 重复: 同一{_item_label(key)}"
            )
        first_seen[key] = index
        inventory[key] = parsed
        states[key] = _ItemState(batch=parsed.batch, expiry=parsed.expiry)

    # 2. 事件静态校验(与重放状态无关):不通过者按原下标报错,不参与重放
    prepared: list[tuple[int, TraceEvent, tuple[ItemKey, ...]]] = []
    errors: list[CustodyEventError] = []

    def check_end(
        idx: int,
        raw_scan: str,
        role: str,
    ) -> tuple[ItemKey | None, str | None]:
        """校验事件一端的扫描串:合法、含序列号、引用库存实物、批号 / 有效期与库存一致。

        role 为 "父件" / "子件";交接事件传空串。
        """
        end_key, parsed, parse_err = _parse_event_scan(raw_scan, role, idx)
        if parse_err is not None:
            return None, parse_err
        assert end_key is not None and parsed is not None
        prefix = f"的{role}" if role else ""
        if end_key not in inventory:
            return None, f"事件 {idx}{prefix}引用的{_item_label(end_key)}不在库存扫描中"
        inv = inventory[end_key]
        if parsed.batch != inv.batch:
            return None, (
                f"事件 {idx}{prefix}批号与库存不一致: "
                f"库存为 {_fmt_value(inv.batch)},事件为 {_fmt_value(parsed.batch)}"
            )
        if parsed.expiry != inv.expiry:
            return None, (
                f"事件 {idx}{prefix}有效期与库存不一致: "
                f"库存为 {_fmt_value(inv.expiry)},事件为 {_fmt_value(parsed.expiry)}"
            )
        return end_key, None

    for index, event in enumerate(request.events):
        if isinstance(event, TraceHandoffEvent):
            ref_key, err = check_end(index, event.scan, "")
            if err is not None:
                errors.append(CustodyEventError(index=index, reason=err))
                continue
            assert ref_key is not None
            prepared.append((index, event, (ref_key,)))
        else:
            assert isinstance(event, TraceLinkEvent)
            parent_key, err = check_end(index, event.parent_scan, "父件")
            if err is not None:
                errors.append(CustodyEventError(index=index, reason=err))
                continue
            child_key, err = check_end(index, event.child_scan, "子件")
            if err is not None:
                errors.append(CustodyEventError(index=index, reason=err))
                continue
            assert parent_key is not None and child_key is not None
            prepared.append((index, event, (parent_key, child_key)))

    # 3. 重放顺序:UTC 升序,同刻输入顺序
    prepared.sort(
        key=lambda item: (
            item[1].occurred_at.astimezone(timezone.utc),
            item[0],
        )
    )

    # 4. 逐条重放;失败事件不改状态,按原下标报错
    for index, event, refs in prepared:
        if isinstance(event, TraceHandoffEvent):
            key = refs[0]
            state = states[key]
            if state.parent is not None:
                errors.append(
                    CustodyEventError(
                        index=index,
                        reason=(
                            f"交接失败:{_item_label(key)}当前有直接父件"
                            f"{_item_label(state.parent)},交接仅作用于无父件实物"
                        ),
                    )
                )
                continue
            if state.holder is None:
                if event.from_location is not None:
                    errors.append(
                        CustodyEventError(
                            index=index,
                            reason=(
                                "实物首次交接的 from_location 须为 null,"
                                f"实际为 '{event.from_location}'"
                            ),
                        )
                    )
                    continue
            elif event.from_location != state.holder:
                actual = (
                    "null" if event.from_location is None else f"'{event.from_location}'"
                )
                errors.append(
                    CustodyEventError(
                        index=index,
                        reason=(
                            f"from_location 须等于当前持有人 '{state.holder}',实际为 {actual}"
                        ),
                    )
                )
                continue

            # 容器交接:容器自身及全部后代随之换持有人
            affected = [key, *_descendants(key, states)]
            for affected_key in affected:
                affected_state = states[affected_key]
                affected_state.holder = event.to_location
                if event.to_location not in affected_state.visits:
                    affected_state.visits.append(event.to_location)
        else:
            assert isinstance(event, TraceLinkEvent)
            parent_key, child_key = refs
            parent_state = states[parent_key]
            child_state = states[child_key]

            if event.action == "pack":
                if child_state.parent is not None:
                    errors.append(
                        CustodyEventError(
                            index=index,
                            reason=(
                                f"装入失败:子件{_item_label(child_key)}已有直接父件"
                                f"{_item_label(child_state.parent)},不能重复装入"
                            ),
                        )
                    )
                    continue
                if child_key == parent_key:
                    errors.append(
                        CustodyEventError(
                            index=index,
                            reason="装入失败:子件不能与父件为同一实物",
                        )
                    )
                    continue
                if parent_state.holder is None or child_state.holder is None:
                    unheld = (
                        _item_label(parent_key)
                        if parent_state.holder is None
                        else _item_label(child_key)
                    )
                    errors.append(
                        CustodyEventError(
                            index=index,
                            reason=f"装入失败:{unheld}尚未完成首次交接,装入双方须已在同一持有人处",
                        )
                    )
                    continue
                if parent_state.holder != child_state.holder:
                    errors.append(
                        CustodyEventError(
                            index=index,
                            reason=(
                                "装入失败:双方持有人不一致,"
                                f"父件在 '{parent_state.holder}',子件在 '{child_state.holder}'"
                            ),
                        )
                    )
                    continue
                # 沿父链向上检查 child 是否为 parent 的祖先(含 parent 本身,已在上面排除)
                ancestor: ItemKey | None = parent_state.parent
                cyclic = False
                seen: set[ItemKey] = set()
                while ancestor is not None:
                    if ancestor == child_key:
                        cyclic = True
                        break
                    if ancestor in seen:  # 防御性:正常维护下不会出现环
                        break
                    seen.add(ancestor)
                    ancestor = states[ancestor].parent
                if cyclic:
                    errors.append(
                        CustodyEventError(
                            index=index,
                            reason=(
                                f"装入失败:子件{_item_label(child_key)}是父件{_item_label(parent_key)}"
                                "的祖先,装入将形成循环包装"
                            ),
                        )
                    )
                    continue
                child_state.parent = parent_key
                parent_state.children.add(child_key)
            else:  # unpack
                if child_state.parent != parent_key:
                    current = child_state.parent
                    if current is None:
                        actual = "子件当前无直接父件"
                    else:
                        actual = f"子件的当前直接父件为{_item_label(current)}"
                    errors.append(
                        CustodyEventError(
                            index=index,
                            reason=f"取出失败:{actual},并非指定的父件{_item_label(parent_key)}"
                        )
                    )
                    continue
                # 取出:子件保留当时持有人(即父件树当前持有人),仅断开父子边
                child_state.parent = None
                parent_state.children.discard(child_key)

    # 5. 仅对命中召回的库存实物输出经过地点
    items: list[RecallCustodyTraceItem] = []
    for key in sorted(inventory):
        parsed = inventory[key]
        if not match_rules(parsed, request.rules):
            continue
        items.append(
            RecallCustodyTraceItem(
                gtin=key[0],
                serial=key[1],
                locations=list(states[key].visits),
            )
        )
    errors.sort(key=lambda error: error.index)
    return RecallCustodyTraceResponse(items=items, errors=errors)
