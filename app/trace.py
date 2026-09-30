"""召回实物保管地点追溯:重放交接 / 装拆事件,追溯命中召回实物经过的保管地点。

库存实物以 GTIN + AI 21 序列号唯一标识,其批号(AI 10)、有效期(AI 17)以
库存扫描为基准;事件按 occurred_at 转 UTC 升序重放,同一时刻依输入顺序。

事件语义:

- 交接(transfer):仅作用于**无父件**实物;首次成功交接 from_location 须为 null,
  此后须等于当前持有人(无首次事件时为 null)。交接一个容器时,其所有后代
  随之换持有人,并同样按到达顺序记录保管地点。
- 装入(pack):子件须无父件、双方均为库存实物、批号 / 有效期与库存一致,
  双方当前持有人相同且非空(装入不改变持有人),且不能自装 / 成环。
- 取出(unpack):须为当前直接父子关系;取出后子件保留当时持有人(不重复记地点)。

扫描非法、缺序列号、引用库存不存在、批号 / 有效期(含缺失)与库存不一致的
事件按原下标报错且不生效;失败事件不影响后续事件。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date as dt_date
from datetime import timezone

from fastapi import HTTPException

from .gs1 import ParsedScan, ScanParseError, parse_scan
from .matcher import match_rules
from .models import (
    LocationTraceEventError,
    LocationTraceItem,
    LocationTraceRequest,
    LocationTraceResponse,
    TracePackingEvent,
    TraceTransferEvent,
)
from .packing import ItemKey

# 直接父子边:parent_of[child] = parent;持有人与包装树共享同一棵树
# (无父件实物方可交接;装入要求双方持有人相同,因此父子持有人始终一致)。


def _unprocessable(reason: str) -> HTTPException:
    return HTTPException(status_code=422, detail=reason)


def _item_label(key: ItemKey) -> str:
    return f"实物 (GTIN {key[0]}, 序列号 '{key[1]}')"


def _fmt_value(value: object) -> str:
    return "(缺失)" if value is None else f"'{value}'"


@dataclass
class _Node:
    """库存实物的重放状态。"""

    batch: str | None  # 批号基准(来自库存扫描)
    expiry: dt_date | None  # 有效期基准(来自库存扫描)
    holder: str | None = None  # 当前持有人;尚未成功交接时为 None
    locations: list[str] = field(default_factory=list)  # 按首次到达顺序去重的地点

    def arrive(self, location: str) -> None:
        """到达一个保管地点(去重,保留首次到达顺序)。"""
        if location not in self.locations:
            self.locations.append(location)


def parse_inventory(raw_scans: list[str]) -> dict[ItemKey, ParsedScan]:
    """解析库存扫描:须全部合法、含 AI 21 且 GTIN+序列号唯一,否则抛 422。"""
    inventory: dict[ItemKey, ParsedScan] = {}
    for index, raw in enumerate(raw_scans):
        try:
            parsed = parse_scan(raw)
        except ScanParseError as exc:
            raise _unprocessable(f"库存扫描 {index} 非法: {exc.reason}") from exc
        if parsed.serial is None:
            raise _unprocessable(f"库存扫描 {index} 缺少 AI 21 序列号,无法识别实物")
        key = (parsed.gtin, parsed.serial)
        if key in inventory:
            raise _unprocessable(
                f"库存扫描 {index} 与更早的库存扫描重复: 同一{_item_label(key)}"
            )
        inventory[key] = parsed
    return inventory


def trace_locations(request: LocationTraceRequest) -> LocationTraceResponse:
    """重放交接 / 装拆事件,返回命中召回实物经过的地点与失败事件的原因。"""
    inventory = parse_inventory(request.scans)
    nodes: dict[ItemKey, _Node] = {
        key: _Node(batch=parsed.batch, expiry=parsed.expiry)
        for key, parsed in inventory.items()
    }
    parent_of: dict[ItemKey, ItemKey] = {}
    errors: list[LocationTraceEventError] = []

    order = sorted(
        range(len(request.events)),
        key=lambda i: (request.events[i].occurred_at.astimezone(timezone.utc), i),
    )

    def parse_party(
        raw_scan: str, role: str, index: int
    ) -> tuple[ItemKey | None, str | None]:
        """解析事件一端扫描串,并校验库存引用与批号 / 有效期一致性。

        顺序:扫描合法 → 含序列号 → 引用库存实物 → 批号一致 → 有效期一致。
        """
        try:
            parsed = parse_scan(raw_scan)
        except ScanParseError as exc:
            return None, f"事件 {index} 的{role}扫描非法: {exc.reason}"
        if parsed.serial is None:
            return None, f"事件 {index} 的{role}扫描串缺少 AI 21 序列号,无法识别实物"
        key = (parsed.gtin, parsed.serial)
        node = nodes.get(key)
        if node is None:
            return None, f"事件 {index} 的{role}{_item_label(key)}不在库存扫描中"
        if parsed.batch != node.batch:
            return None, (
                f"事件 {index} 的{role}批号与库存不一致: "
                f"库存为 {_fmt_value(node.batch)},本次为 {_fmt_value(parsed.batch)}"
            )
        if parsed.expiry != node.expiry:
            return None, (
                f"事件 {index} 的{role}有效期与库存不一致: "
                f"库存为 {_fmt_value(node.expiry)},本次为 {_fmt_value(parsed.expiry)}"
            )
        return key, None

    def descendants_of(root: ItemKey) -> list[ItemKey]:
        """返回包装树中 root 的全部后代(不含自身),BFS 按树的当前结构展开。"""
        children_of: dict[ItemKey, list[ItemKey]] = {}
        for child, parent in parent_of.items():
            children_of.setdefault(parent, []).append(child)
        result: list[ItemKey] = []
        queue: deque[ItemKey] = deque(children_of.get(root, []))
        while queue:
            node = queue.popleft()
            result.append(node)
            queue.extend(children_of.get(node, []))
        return result

    for index in order:
        event = request.events[index]

        if isinstance(event, TraceTransferEvent):
            key, err = parse_party(event.scan, "扫描", index)
            if err is not None:
                errors.append(LocationTraceEventError(index=index, reason=err))
                continue
            assert key is not None
            if key in parent_of:
                errors.append(
                    LocationTraceEventError(
                        index=index,
                        reason=(
                            f"交接失败:{_item_label(key)}当前有直接父件"
                            f"{_item_label(parent_of[key])},仅无父件实物可直接交接"
                        ),
                    )
                )
                continue
            node = nodes[key]
            if event.from_location != node.holder:
                actual = "null" if event.from_location is None else f"'{event.from_location}'"
                expected = "null(实物尚无成功交接)" if node.holder is None else f"'{node.holder}'"
                errors.append(
                    LocationTraceEventError(
                        index=index,
                        reason=(
                            f"交接失败:from_location 须等于当前持有人 {expected},"
                            f"实际为 {actual}"
                        ),
                    )
                )
                continue
            # 成功交接:根实物及其全部后代随之换持有人并记录新地点
            for affected in (key, *descendants_of(key)):
                nodes[affected].holder = event.to_location
                nodes[affected].arrive(event.to_location)
            continue

        assert isinstance(event, TracePackingEvent)
        parent_key, err = parse_party(event.parent_scan, "父件", index)
        if err is not None:
            errors.append(LocationTraceEventError(index=index, reason=err))
            continue
        child_key, err = parse_party(event.child_scan, "子件", index)
        if err is not None:
            errors.append(LocationTraceEventError(index=index, reason=err))
            continue
        assert parent_key is not None and child_key is not None

        if event.action == "pack":
            if child_key in parent_of:
                errors.append(
                    LocationTraceEventError(
                        index=index,
                        reason=(
                            f"装入失败:子件{_item_label(child_key)}已有直接父件"
                            f"{_item_label(parent_of[child_key])},不能重复装入"
                        ),
                    )
                )
                continue
            if child_key == parent_key:
                errors.append(
                    LocationTraceEventError(
                        index=index, reason="装入失败:子件不能与父件为同一实物"
                    )
                )
                continue
            parent_holder = nodes[parent_key].holder
            child_holder = nodes[child_key].holder
            if parent_holder is None or child_holder is None:
                who = "父件" if parent_holder is None else "子件"
                errors.append(
                    LocationTraceEventError(
                        index=index,
                        reason=f"装入失败:{who}{_item_label(parent_key if parent_holder is None else child_key)}尚无成功交接,当前持有人缺失,不能装入",
                    )
                )
                continue
            if parent_holder != child_holder:
                errors.append(
                    LocationTraceEventError(
                        index=index,
                        reason=(
                            "装入失败:双方当前持有人须相同,"
                            f"父件持有人为 '{parent_holder}',子件持有人为 '{child_holder}'"
                        ),
                    )
                )
                continue
            # 沿父链向上检查 child 是否为 parent 的祖先(防止形成循环包装)
            ancestor: ItemKey | None = parent_of.get(parent_key)
            cyclic = False
            seen: set[ItemKey] = set()
            while ancestor is not None:
                if ancestor == child_key:
                    cyclic = True
                    break
                if ancestor in seen:  # 防御性:正常维护下不会出现环
                    break
                seen.add(ancestor)
                ancestor = parent_of.get(ancestor)
            if cyclic:
                errors.append(
                    LocationTraceEventError(
                        index=index,
                        reason=(
                            f"装入失败:子件{_item_label(child_key)}是父件{_item_label(parent_key)}"
                            "的祖先,装入将形成循环包装"
                        ),
                    )
                )
                continue
            # 装入不改变持有人:父子持有人相同,后代位置轨迹保持不变
            parent_of[child_key] = parent_key
        else:  # unpack
            if parent_of.get(child_key) != parent_key:
                current = parent_of.get(child_key)
                if current is None:
                    actual = "子件当前无直接父件"
                else:
                    actual = f"子件的当前直接父件为{_item_label(current)}"
                errors.append(
                    LocationTraceEventError(
                        index=index,
                        reason=f"取出失败:{actual},并非指定的父件{_item_label(parent_key)}",
                    )
                )
                continue
            del parent_of[child_key]
            # 取出后子件保留当时持有人,不重复记录地点

    # 仅对命中召回的库存实物输出
    items: list[LocationTraceItem] = []
    for key, parsed in inventory.items():
        hits = match_rules(parsed, request.rules)
        if not hits:
            continue
        items.append(
            LocationTraceItem(
                gtin=key[0],
                serial=key[1],
                locations=list(nodes[key].locations),
                rule_ids=hits,  # match_rules 已按规则输入顺序返回
            )
        )
    items.sort(key=lambda item: (item.gtin, item.serial))
    errors.sort(key=lambda error: error.index)
    return LocationTraceResponse(items=items, errors=errors)
