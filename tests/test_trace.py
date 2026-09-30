"""POST /v1/recall/custody-trace 的端到端测试。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

FNC1 = "\x1d"  # ASCII 29


def make_gtin(body13: str) -> str:
    """由 13 位本体计算模 10 校验位,生成合法 14 位 GTIN。"""
    assert len(body13) == 13 and body13.isdigit()
    total = sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(reversed(body13)))
    return body13 + str((10 - total % 10) % 10)


GTIN = make_gtin("0950600112345")
GTIN2 = make_gtin("0950600112346")
GTIN3 = make_gtin("0950600112347")


def scan(gtin=GTIN, serial="SN1", batch=None, expiry=None, prefix=False):
    """拼接含 AI 21 的扫描串(变长字段后视需要补 FNC1)。"""
    s = "]d2" if prefix else ""
    s += "01" + gtin
    if expiry is not None:
        s += "17" + expiry
    if batch is not None:
        s += "10" + batch
        if serial is not None:
            s += FNC1
    if serial is not None:
        s += "21" + serial
    return s


def rule(rid, gtin=GTIN, **kw):
    return {"id": rid, "gtin": gtin, **kw}


def handoff(item_scan, at, to_location, from_location=None):
    return {
        "action": "handoff",
        "scan": item_scan,
        "occurred_at": at,
        "from_location": from_location,
        "to_location": to_location,
    }


def pack(parent, child, at="2026-09-01T00:00:00Z"):
    return {
        "action": "pack",
        "parent_scan": parent,
        "child_scan": child,
        "occurred_at": at,
    }


def unpack(parent, child, at="2026-09-01T00:00:00Z"):
    return {
        "action": "unpack",
        "parent_scan": parent,
        "child_scan": child,
        "occurred_at": at,
    }


def post(scans, rules, events, expected=200):
    resp = client.post(
        "/v1/recall/custody-trace",
        json={"scans": scans, "rules": rules, "events": events},
    )
    assert resp.status_code == expected, resp.text
    return resp.json()


def expect_422(payload):
    resp = client.post("/v1/recall/custody-trace", json=payload)
    assert resp.status_code == 422, resp.text
    return resp


def item_map(body):
    return {(i["gtin"], i["serial"]): i for i in body["items"]}


# ---------- 基本交接与地点去重 ----------


def test_simple_handoff_chain_locations_first_arrival():
    s = scan(GTIN, "A")
    body = post(
        [s],
        [rule("R1")],
        [
            handoff(s, "2026-09-01T00:00:00Z", "WH-A"),
            handoff(s, "2026-09-01T01:00:00Z", "WH-B", from_location="WH-A"),
            handoff(s, "2026-09-01T02:00:00Z", "WH-A", from_location="WH-B"),  # 回访 WH-A 不重复
            handoff(s, "2026-09-01T03:00:00Z", "WH-C", from_location="WH-A"),
        ],
    )
    assert body["errors"] == []
    assert body["items"] == [
        {"gtin": GTIN, "serial": "A", "locations": ["WH-A", "WH-B", "WH-C"]}
    ]


def test_same_location_handoff_does_not_duplicate():
    s = scan(GTIN, "A")
    body = post(
        [s],
        [rule("R1")],
        [
            handoff(s, "2026-09-01T00:00:00Z", "WH-A"),
            handoff(s, "2026-09-01T01:00:00Z", "WH-A", from_location="WH-A"),
        ],
    )
    assert body["items"][0]["locations"] == ["WH-A"]


def test_hit_item_never_handoffed_has_empty_locations():
    # events 至少 1 条,用一件无关实物的交接占位
    other = scan(GTIN2, "B")
    body = post(
        [scan(GTIN, "A"), other],
        [rule("R1")],
        [handoff(other, "2026-09-01T00:00:00Z", "WH-A")],
    )
    assert body["items"] == [{"gtin": GTIN, "serial": "A", "locations": []}]


def test_only_recall_hits_returned_sorted_by_gtin_serial():
    body = post(
        [scan(GTIN2, "B"), scan(GTIN, "Z"), scan(GTIN, "A"), scan(GTIN3, "C")],
        [rule("R1"), rule("R2", gtin=GTIN2)],  # GTIN3 不命中
        [
            handoff(scan(GTIN2, "B"), "2026-09-01T00:00:00Z", "L1"),
            handoff(scan(GTIN, "Z"), "2026-09-01T00:00:00Z", "L2"),
            handoff(scan(GTIN, "A"), "2026-09-01T00:00:00Z", "L3"),
            handoff(scan(GTIN3, "C"), "2026-09-01T00:00:00Z", "L4"),
        ],
    )
    assert [(i["gtin"], i["serial"]) for i in body["items"]] == [
        (GTIN, "A"),
        (GTIN, "Z"),
        (GTIN2, "B"),
    ]


def test_rule_batch_and_expiry_conditions_apply_to_inventory():
    s = scan(GTIN, "A", batch="LOT1", expiry="251231")
    body = post(
        [s],
        [
            rule("R-no", batch="LOT2"),
            rule("R-date", date_from="2026-01-01"),
            rule("R-hit", batch="LOT1", date_to="2025-12-31"),
        ],
        [handoff(s, "2026-09-01T00:00:00Z", "WH-A")],
    )
    assert body["items"][0]["locations"] == ["WH-A"]


# ---------- 重放顺序:UTC 升序、同刻输入顺序 ----------


def test_events_replayed_in_utc_order_across_timezones():
    s = scan(GTIN, "A")
    body = post(
        [s],
        [rule("R1")],
        [
            # +08:00 的 09:00 即 UTC 01:00
            handoff(s, "2026-09-01T09:00:00+08:00", "WH-B", from_location="WH-A"),
            handoff(s, "2026-09-01T02:00:00Z", "WH-C", from_location="WH-B"),
            handoff(s, "2026-09-01T00:30:00Z", "WH-A"),
        ],
    )
    assert body["errors"] == []
    assert body["items"][0]["locations"] == ["WH-A", "WH-B", "WH-C"]


def test_same_instant_uses_input_order():
    s = scan(GTIN, "A")
    body = post(
        [s],
        [rule("R1")],
        [
            handoff(s, "2026-09-01T08:00:00+08:00", "WH-A"),
            handoff(s, "2026-09-01T00:00:00Z", "WH-B", from_location="WH-A"),
        ],
    )
    assert body["items"][0]["locations"] == ["WH-A", "WH-B"]
    # 顺序反过来:第一条要求 from=WH-A,但首次须 null → 失败;第二条成为首次交接
    body = post(
        [s],
        [rule("R1")],
        [
            handoff(s, "2026-09-01T00:00:00Z", "WH-B", from_location="WH-A"),
            handoff(s, "2026-09-01T08:00:00+08:00", "WH-A"),
        ],
    )
    assert body["items"][0]["locations"] == ["WH-A"]
    assert [e["index"] for e in body["errors"]] == [0]


# ---------- 交接链约束 ----------


def test_first_handoff_from_location_must_be_null():
    s = scan(GTIN, "A")
    body = post(
        [s],
        [rule("R1")],
        [
            handoff(s, "2026-09-01T00:00:00Z", "WH-A", from_location="WH-X"),
            handoff(s, "2026-09-01T01:00:00Z", "WH-B"),  # 前条失败,本条仍是首次
        ],
    )
    assert body["items"][0]["locations"] == ["WH-B"]
    assert [e["index"] for e in body["errors"]] == [0]
    assert "首次" in body["errors"][0]["reason"] and "null" in body["errors"][0]["reason"]


def test_followup_from_location_must_equal_current_holder():
    s = scan(GTIN, "A")
    body = post(
        [s],
        [rule("R1")],
        [
            handoff(s, "2026-09-01T00:00:00Z", "WH-A"),
            handoff(s, "2026-09-01T01:00:00Z", "WH-X", from_location="WH-Z"),  # 链断裂
            handoff(s, "2026-09-01T02:00:00Z", "WH-B", from_location="WH-A"),  # 接当前持有人
        ],
    )
    assert body["items"][0]["locations"] == ["WH-A", "WH-B"]
    assert [e["index"] for e in body["errors"]] == [1]
    assert "WH-A" in body["errors"][0]["reason"]


# ---------- 容器交接连带后代 ----------


def test_container_handoff_moves_all_descendants_locations():
    pallet, case, item = scan(GTIN, "P"), scan(GTIN2, "C"), scan(GTIN3, "I")
    body = post(
        [pallet, case, item],
        [rule("R1", gtin=GTIN3)],
        [
            handoff(pallet, "2026-09-01T00:00:00Z", "WH-A"),
            handoff(case, "2026-09-01T00:01:00Z", "WH-A"),
            handoff(item, "2026-09-01T00:02:00Z", "WH-A"),
            pack(pallet, case, "2026-09-01T01:00:00Z"),
            pack(case, item, "2026-09-01T01:01:00Z"),
            handoff(pallet, "2026-09-01T02:00:00Z", "WH-B", from_location="WH-A"),
        ],
    )
    assert body["errors"] == []
    # item 自身只交接去过 WH-A,随托盘容器交接到达 WH-B
    assert body["items"] == [
        {"gtin": GTIN3, "serial": "I", "locations": ["WH-A", "WH-B"]}
    ]


def test_handoff_of_parented_item_fails():
    pallet, item = scan(GTIN, "P"), scan(GTIN2, "I")
    body = post(
        [pallet, item],
        [rule("R1", gtin=GTIN2)],
        [
            handoff(pallet, "2026-09-01T00:00:00Z", "WH-A"),
            handoff(item, "2026-09-01T00:01:00Z", "WH-A"),
            pack(pallet, item, "2026-09-01T01:00:00Z"),
            handoff(item, "2026-09-01T02:00:00Z", "WH-B", from_location="WH-A"),
        ],
    )
    assert [e["index"] for e in body["errors"]] == [3]
    assert "无父件" in body["errors"][0]["reason"]
    # 失败交接不影响实物:item 仍只到过 WH-A
    assert item_map(body)[(GTIN2, "I")]["locations"] == ["WH-A"]


# ---------- 装入约束 ----------


def test_pack_requires_child_without_parent():
    p1, p2, child = scan(GTIN, "P1"), scan(GTIN2, "P2"), scan(GTIN3, "C")
    body = post(
        [p1, p2, child],
        [rule("R1", gtin=GTIN3)],
        [
            handoff(p1, "2026-09-01T00:00:00Z", "WH-A"),
            handoff(p2, "2026-09-01T00:00:00Z", "WH-A"),
            handoff(child, "2026-09-01T00:00:00Z", "WH-A"),
            pack(p1, child, "2026-09-01T01:00:00Z"),
            pack(p2, child, "2026-09-01T02:00:00Z"),  # child 已有父件
        ],
    )
    assert [e["index"] for e in body["errors"]] == [4]
    assert "已有直接父件" in body["errors"][0]["reason"]


def test_pack_requires_both_sides_same_holder():
    parent, child = scan(GTIN, "P"), scan(GTIN2, "C")
    body = post(
        [parent, child],
        [rule("R1", gtin=GTIN2)],
        [
            handoff(parent, "2026-09-01T00:00:00Z", "WH-A"),
            handoff(child, "2026-09-01T00:01:00Z", "WH-B"),
            pack(parent, child, "2026-09-01T01:00:00Z"),
        ],
    )
    assert [e["index"] for e in body["errors"]] == [2]
    assert "持有人不一致" in body["errors"][0]["reason"]


def test_pack_requires_both_sides_already_held():
    parent, child = scan(GTIN, "P"), scan(GTIN2, "C")
    body = post(
        [parent, child],
        [rule("R1", gtin=GTIN2)],
        [pack(parent, child, "2026-09-01T01:00:00Z")],
    )
    assert [e["index"] for e in body["errors"]] == [0]
    assert "首次交接" in body["errors"][0]["reason"]


def test_pack_self_and_cycle_rejected():
    a, b = scan(GTIN, "A"), scan(GTIN2, "B")
    body = post(
        [a, b],
        [rule("R1", gtin=GTIN), rule("R2", gtin=GTIN2)],
        [
            handoff(a, "2026-09-01T00:00:00Z", "WH-A"),
            handoff(b, "2026-09-01T00:00:00Z", "WH-A"),
            pack(a, a, "2026-09-01T01:00:00Z"),  # 自装
            pack(a, b, "2026-09-01T02:00:00Z"),
            pack(b, a, "2026-09-01T03:00:00Z"),  # a 是 b 的祖先,成环
        ],
    )
    assert [e["index"] for e in body["errors"]] == [2, 4]
    assert "同一实物" in body["errors"][0]["reason"]
    assert "循环包装" in body["errors"][1]["reason"]
    # 只有 a→b 装入成功
    assert item_map(body)[(GTIN2, "B")]["locations"] == ["WH-A"]


def test_pack_can_nest_after_alignment():
    pallet, case, item = scan(GTIN, "P"), scan(GTIN2, "C"), scan(GTIN3, "I")
    body = post(
        [pallet, case, item],
        [rule("R1", gtin=GTIN3)],
        [
            handoff(pallet, "2026-09-01T00:00:00Z", "WH-A"),
            handoff(case, "2026-09-01T00:00:00Z", "WH-A"),
            handoff(item, "2026-09-01T00:00:00Z", "WH-A"),
            pack(case, item, "2026-09-01T01:00:00Z"),
            pack(pallet, case, "2026-09-01T02:00:00Z"),  # case 虽有子件但自身无父件,允许装入
        ],
    )
    assert body["errors"] == []


# ---------- 取出:须当前直接父子关系,子件保留持有人 ----------


def test_unpack_requires_current_direct_parent():
    pallet, case, item = scan(GTIN, "P"), scan(GTIN2, "C"), scan(GTIN3, "I")
    body = post(
        [pallet, case, item],
        [rule("R1", gtin=GTIN3)],
        [
            handoff(pallet, "2026-09-01T00:00:00Z", "WH-A"),
            handoff(case, "2026-09-01T00:00:00Z", "WH-A"),
            handoff(item, "2026-09-01T00:00:00Z", "WH-A"),
            pack(case, item, "2026-09-01T01:00:00Z"),
            unpack(pallet, item, "2026-09-01T02:00:00Z"),  # pallet 是祖父,非直接父件
        ],
    )
    assert [e["index"] for e in body["errors"]] == [4]
    assert "取出失败" in body["errors"][0]["reason"]


def test_unpacked_child_keeps_holder_then_can_handoff_alone():
    pallet, item = scan(GTIN, "P"), scan(GTIN2, "I")
    body = post(
        [pallet, item],
        [rule("R1", gtin=GTIN2)],
        [
            handoff(pallet, "2026-09-01T00:00:00Z", "WH-A"),
            handoff(item, "2026-09-01T00:01:00Z", "WH-A"),
            pack(pallet, item, "2026-09-01T01:00:00Z"),
            handoff(pallet, "2026-09-01T02:00:00Z", "WH-B", from_location="WH-A"),
            unpack(pallet, item, "2026-09-01T03:00:00Z"),  # 取出时保留 WH-B
            # 容器随后去 WH-C,已取出的 item 不再随行
            handoff(pallet, "2026-09-01T04:00:00Z", "WH-C", from_location="WH-B"),
            handoff(item, "2026-09-01T05:00:00Z", "WH-D", from_location="WH-B"),
        ],
    )
    assert body["errors"] == []
    assert item_map(body)[(GTIN2, "I")]["locations"] == ["WH-A", "WH-B", "WH-D"]


def test_multi_level_descendants_follow_container_handoff():
    pallet, case, item = scan(GTIN, "P"), scan(GTIN2, "C"), scan(GTIN3, "I")
    body = post(
        [pallet, case, item],
        [rule("R1", gtin=GTIN3)],
        [
            handoff(pallet, "2026-09-01T00:00:00Z", "L0"),
            handoff(case, "2026-09-01T00:01:00Z", "L0"),
            handoff(item, "2026-09-01T00:02:00Z", "L0"),
            pack(case, item, "2026-09-01T01:00:00Z"),
            pack(pallet, case, "2026-09-01T01:01:00Z"),
            handoff(pallet, "2026-09-01T02:00:00Z", "L1", from_location="L0"),
            handoff(pallet, "2026-09-01T03:00:00Z", "L2", from_location="L1"),
            # 取出 case(连同其内部 item);case 与 item 保留 L2
            unpack(pallet, case, "2026-09-01T04:00:00Z"),
            handoff(pallet, "2026-09-01T05:00:00Z", "L3", from_location="L2"),
            # case 取出后为无父件,可自行交接;内层 item 仍随行到 L4
            handoff(case, "2026-09-01T06:00:00Z", "L4", from_location="L2"),
        ],
    )
    assert body["errors"] == []
    assert item_map(body)[(GTIN3, "I")]["locations"] == ["L0", "L1", "L2", "L4"]


def test_repack_after_unpack_then_follows_new_container():
    p1, p2, item = scan(GTIN, "P1"), scan(GTIN2, "P2"), scan(GTIN3, "I")
    body = post(
        [p1, p2, item],
        [rule("R1", gtin=GTIN3)],
        [
            handoff(p1, "2026-09-01T00:00:00Z", "WH-A"),
            handoff(p2, "2026-09-01T00:00:00Z", "WH-A"),
            handoff(item, "2026-09-01T00:01:00Z", "WH-A"),
            pack(p1, item, "2026-09-01T01:00:00Z"),
            handoff(p1, "2026-09-01T02:00:00Z", "WH-B", from_location="WH-A"),
            unpack(p1, item, "2026-09-01T03:00:00Z"),  # item 留在 WH-B
            # p2 仍在 WH-A,与 item(WH-B)持有人不一致 → 装入失败
            pack(p2, item, "2026-09-01T03:30:00Z"),
            handoff(p2, "2026-09-01T04:00:00Z", "WH-B", from_location="WH-A"),
            pack(p2, item, "2026-09-01T05:00:00Z"),  # 同持有人 WH-B,装入成功
            handoff(p2, "2026-09-01T06:00:00Z", "WH-C", from_location="WH-B"),
        ],
    )
    assert [e["index"] for e in body["errors"]] == [6]
    assert "持有人不一致" in body["errors"][0]["reason"]
    assert item_map(body)[(GTIN3, "I")]["locations"] == ["WH-A", "WH-B", "WH-C"]


# ---------- 事件静态校验:按原下标报错且不生效 ----------


def test_event_invalid_scan_reported_by_index():
    good = scan(GTIN, "A")
    body = post(
        [good, scan(GTIN2, "B")],
        [rule("R1")],
        [
            handoff(good, "2026-09-01T00:00:00Z", "WH-A"),
            handoff("01" + GTIN + "99", "2026-09-01T01:00:00Z", "WH-B"),
        ],
    )
    assert [e["index"] for e in body["errors"]] == [1]
    assert "扫描非法" in body["errors"][0]["reason"]


def test_event_scan_missing_serial_reported():
    body = post(
        [scan(GTIN, "A")],
        [rule("R1")],
        [handoff("01" + GTIN + "17251231", "2026-09-01T00:00:00Z", "WH-A")],
    )
    assert [e["index"] for e in body["errors"]] == [0]
    assert "缺少 AI 21" in body["errors"][0]["reason"]


def test_event_reference_not_in_inventory_reported():
    s = scan(GTIN, "A")
    ghost = scan(GTIN2, "GHOST")
    body = post(
        [s],
        [rule("R1"), rule("R2", gtin=GTIN2)],
        [
            handoff(ghost, "2026-09-01T00:00:00Z", "WH-A"),
            handoff(s, "2026-09-01T00:01:00Z", "WH-A"),
        ],
    )
    assert [e["index"] for e in body["errors"]] == [0]
    assert "不在库存扫描中" in body["errors"][0]["reason"]
    assert body["items"][0]["locations"] == ["WH-A"]


def test_pack_event_unknown_end_reported():
    s = scan(GTIN, "A")
    body = post(
        [s],
        [rule("R1")],
        [pack(s, scan(GTIN2, "C"), "2026-09-01T00:00:00Z")],
    )
    assert [e["index"] for e in body["errors"]] == [0]
    assert "子件" in body["errors"][0]["reason"] and "不在库存扫描中" in body["errors"][0]["reason"]


def test_event_batch_mismatch_with_inventory_reported():
    inv = scan(GTIN, "A", batch="LOT1", expiry="251231")
    body = post(
        [inv],
        [rule("R1", batch="LOT1")],
        [
            handoff(inv, "2026-09-01T00:00:00Z", "WH-A"),
            handoff(scan(GTIN, "A", batch="LOT2", expiry="251231"),
                    "2026-09-01T01:00:00Z", "WH-B", from_location="WH-A"),
        ],
    )
    # 批号不一致 → 事件不生效;库存实物仍命中规则但地点停在 WH-A
    assert [e["index"] for e in body["errors"]] == [1]
    assert "批号与库存不一致" in body["errors"][0]["reason"]
    assert body["items"][0]["locations"] == ["WH-A"]


def test_event_expiry_mismatch_including_missing_reported():
    inv = scan(GTIN, "A", expiry="251231")
    body = post(
        [inv],
        [rule("R1")],
        [
            handoff(inv, "2026-09-01T00:00:00Z", "WH-A"),
            # 库存有有效期,事件缺失 → 不一致
            handoff(scan(GTIN, "A"), "2026-09-01T01:00:00Z", "WH-B", from_location="WH-A"),
        ],
    )
    assert [e["index"] for e in body["errors"]] == [1]
    assert "有效期与库存不一致" in body["errors"][0]["reason"]


def test_event_batch_missing_on_both_sides_is_consistent():
    inv = scan(GTIN, "A")  # 库存无批号 / 有效期
    body = post(
        [inv],
        [rule("R1")],
        [
            handoff(inv, "2026-09-01T00:00:00Z", "WH-A"),
            handoff(scan(GTIN, "A"), "2026-09-01T01:00:00Z", "WH-B", from_location="WH-A"),
        ],
    )
    assert body["errors"] == []
    assert body["items"][0]["locations"] == ["WH-A", "WH-B"]


def test_pack_event_batch_mismatch_on_either_end_reported():
    p, c = scan(GTIN, "P"), scan(GTIN2, "C", batch="L1")
    body = post(
        [p, c],
        [rule("R1", gtin=GTIN2)],
        [
            handoff(p, "2026-09-01T00:00:00Z", "WH-A"),
            handoff(c, "2026-09-01T00:00:00Z", "WH-A"),
            pack(p, scan(GTIN2, "C", batch="L2"), "2026-09-01T01:00:00Z"),
        ],
    )
    assert [e["index"] for e in body["errors"]] == [2]
    assert "子件批号与库存不一致" in body["errors"][0]["reason"]


def test_failed_events_do_not_affect_later_events():
    s = scan(GTIN, "A")
    body = post(
        [s],
        [rule("R1")],
        [
            handoff(s, "2026-09-01T00:00:00Z", "WH-A", from_location="WH-X"),  # 首次非 null
            handoff("01" + GTIN + "99", "2026-09-01T01:00:00Z", "WH-X"),  # 扫描非法
            handoff(s, "2026-09-01T02:00:00Z", "WH-A"),  # 仍是首次,成功
            handoff(s, "2026-09-01T03:00:00Z", "WH-B", from_location="WH-A"),  # 成功
        ],
    )
    assert [e["index"] for e in body["errors"]] == [0, 1]
    assert body["items"][0]["locations"] == ["WH-A", "WH-B"]


def test_errors_sorted_by_original_index_not_replay_order():
    s = scan(GTIN, "A")
    body = post(
        [s],
        [rule("R1")],
        [
            handoff("01" + GTIN + "99", "2026-09-01T02:00:00Z", "WH-A"),  # 下标 0 最晚
            handoff(s, "2026-09-01T00:00:00Z", "WH-A"),
            handoff(s, "2026-09-01T01:00:00Z", "WH-B", from_location="WH-Z"),  # 链断裂
        ],
    )
    assert [e["index"] for e in body["errors"]] == [0, 2]


# ---------- 库存扫描整单 422 ----------


def test_invalid_inventory_scan_returns_422():
    resp = expect_422(
        {
            "scans": ["01" + GTIN + "99"],
            "rules": [rule("R1")],
            "events": [handoff(scan(GTIN2, "B"), "2026-09-01T00:00:00Z", "WH-A")],
        }
    )
    assert "库存扫描 0 非法" in resp.json()["detail"]


def test_inventory_scan_without_ai21_returns_422():
    resp = expect_422(
        {
            "scans": ["01" + GTIN + "17251231"],
            "rules": [rule("R1")],
            "events": [handoff(scan(GTIN2, "B"), "2026-09-01T00:00:00Z", "WH-A")],
        }
    )
    assert "缺少 AI 21" in resp.json()["detail"]


def test_duplicate_inventory_identity_returns_422():
    resp = expect_422(
        {
            "scans": [scan(GTIN, "A", batch="L1"), scan(GTIN, "A", batch="L2")],
            "rules": [rule("R1")],
            "events": [handoff(scan(GTIN2, "B"), "2026-09-01T00:00:00Z", "WH-A")],
        }
    )
    assert "重复" in resp.json()["detail"] and "库存扫描 1" in resp.json()["detail"]


# ---------- 请求体验证(422) ----------


def base_payload(**kw):
    payload = {
        "scans": [scan(GTIN, "A")],
        "rules": [rule("R1")],
        "events": [handoff(scan(GTIN, "A"), "2026-09-01T00:00:00Z", "WH-A")],
    }
    payload.update(kw)
    return payload


def test_validation_counts():
    expect_422(base_payload(scans=[]))
    expect_422(base_payload(scans=[scan(GTIN3, f"S{i}") for i in range(101)]))
    expect_422(base_payload(rules=[]))
    expect_422(base_payload(rules=[rule("R1")] * 101))
    h = handoff(scan(GTIN, "A"), "2026-09-01T00:00:00Z", "WH-A")
    expect_422(base_payload(events=[]))
    expect_422(base_payload(events=[h] * 201))
    # 边界:100 条扫描、200 条事件合法
    body = post(
        [scan(GTIN3, f"S{i}") for i in range(100)],
        [rule("R1", gtin=GTIN3)],
        [
            handoff(scan(GTIN3, f"S{i}"), "2026-09-01T00:00:00Z", "WH-A")
            for i in range(100)
        ],
    )
    assert len(body["items"]) == 100


def test_validation_rule_shape_and_unique_ids():
    expect_422(base_payload(rules=[rule("R1"), rule("R1")]))
    expect_422(base_payload(rules=[{"id": "R1", "gtin": "09506001123450"}]))


def test_validation_event_shape():
    # 缺时区
    expect_422(
        base_payload(
            events=[{"action": "handoff", "scan": scan(GTIN, "A"),
                     "occurred_at": "2026-09-01T00:00:00",
                     "from_location": None, "to_location": "WH-A"}]
        )
    )
    # 未知 action
    expect_422(
        base_payload(
            events=[{"action": "move", "scan": scan(GTIN, "A"),
                     "occurred_at": "2026-09-01T00:00:00Z",
                     "from_location": None, "to_location": "WH-A"}]
        )
    )
    # 交接缺字段
    expect_422(
        base_payload(
            events=[{"action": "handoff", "occurred_at": "2026-09-01T00:00:00Z",
                     "from_location": None, "to_location": "WH-A"}]
        )
    )
    # 装入缺字段
    expect_422(
        base_payload(
            events=[{"action": "pack", "parent_scan": scan(GTIN, "P"),
                     "occurred_at": "2026-09-01T00:00:00Z"}]
        )
    )
    # to_location 为空
    expect_422(
        base_payload(
            events=[{"action": "handoff", "scan": scan(GTIN, "A"),
                     "occurred_at": "2026-09-01T00:00:00Z",
                     "from_location": None, "to_location": ""}]
        )
    )
    # 多余字段
    expect_422(
        base_payload(
            events=[{"action": "handoff", "scan": scan(GTIN, "A"),
                     "occurred_at": "2026-09-01T00:00:00Z",
                     "from_location": None, "to_location": "WH-A", "extra": 1}]
        )
    )


def test_validation_top_level_required_and_no_extra():
    payload = base_payload()
    del payload["scans"]
    expect_422(payload)
    expect_422(base_payload(extra=1))


# ---------- 旧接口回归 ----------


def test_existing_endpoints_still_available():
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.post(
        "/v1/recall/check",
        json={"scans": [scan(GTIN, "A")], "rules": [rule("R1")]},
    ).status_code == 200
    assert client.post(
        "/v1/custody/audit",
        json={"events": [{
            "scan": scan(GTIN, "A"),
            "occurred_at": "2026-09-01T00:00:00Z",
            "from_location": None,
            "to_location": "WH-A",
        }]},
    ).status_code == 200
    assert client.post(
        "/v1/packing/audit",
        json={"operations": [
            {"action": "pack", "parent_scan": scan(GTIN, "P"),
             "child_scan": scan(GTIN2, "C")}
        ]},
    ).status_code == 200
