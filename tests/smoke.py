"""端到端行为验证脚本：python -m tests.smoke（从仓库根目录运行）。"""

from datetime import datetime, timedelta, timezone

from src.service import (
    Equipment,
    Person,
    PolicyViolation,
    Service,
)

t0 = datetime(2026, 9, 21, 8, 0, 0, tzinfo=timezone.utc)
def T(h, m=0):  # noqa: N802
    return t0 + timedelta(hours=h, minutes=m)


def build_service(tmp=None):
    s = Service(tmp)
    # 装备：步枪（双人双控，需枪证，时限 4h）、头盔（单人，无资质要求，时限 1h）
    s.register_equipment(Equipment("R-001", "步枪", "A区", "GUN_LICENSE", True, 240))
    s.register_equipment(Equipment("H-001", "头盔", "A区", None, False, 60))
    s.register_equipment(Equipment("R-002", "备用步枪", "B区", "GUN_LICENSE", True, 240))

    s.register_person(Person(
        "P01", "张三", "11010119900101001X",
        {"GUN_LICENSE": T(30)}, {"A区", "B区"}, {"armory_supervisor"},
    ))
    s.register_person(Person(
        "P02", "李四", "31010119900202002Y",
        {"GUN_LICENSE": T(2)}, {"A区"}, set(),  # 资质在 10:00 失效
    ))
    s.register_person(Person(
        "P03", "王五", "44010119900303003Z", {}, {"A区"}, set(),  # 无枪证
    ))
    s.register_person(Person(
        "P04", "审计赵", "50010119900404004W", {}, set(), {"security_audit"},
    ))
    return s


def check(name, fn):
    try:
        fn()
    except AssertionError:
        print(f"FAIL  {name}")
        raise
    print(f"ok    {name}")


def test_happy_path_issue_and_return():
    s = build_service()
    swipe = s.record_swipe(event_id="W1", device_id="GATE-A", person_id="P01",
                           gate="东门", zone="A区", direction="IN", device_time=T(1))
    s.issue_equipment(event_id="E1", device_id="TERM-1", equipment_code="R-001",
                      person_id="P01", second_person_id="P02",
                      access_event_id="W1", device_time=T(1, 1))
    st = s.status("R-001")
    assert st["state"] == "ON_LOAN" and st["holder"] == "P01"

    # 归还前先归还不需要再刷卡？需要关联入场：还装备时通常再次入库，补一次刷卡
    s.record_swipe(event_id="W2", device_id="GATE-A", person_id="P01",
                   gate="东门", zone="A区", direction="IN", device_time=T(2))
    s.return_equipment(event_id="E2", device_id="TERM-1", equipment_code="R-001",
                       person_id="P01", access_event_id="W2", device_time=T(2, 1))
    assert s.status("R-001")["state"] == "IN_STORE"
    # 拒绝留证数量为 0
    assert all(e["kind"] != "POLICY_DENIAL" for e in s.store.sorted_events())


def test_duplicate_swipe_links_original():
    s = build_service()
    e1 = s.record_swipe(event_id="W1", device_id="GATE-A", person_id="P01",
                        gate="东门", zone="A区", direction="IN", device_time=T(1))
    # 相同 event_id
    e1b = s.record_swipe(event_id="W1", device_id="GATE-A", person_id="P01",
                         gate="东门", zone="A区", direction="IN", device_time=T(1))
    # 同窗口内重放（设备自分配了 id）
    e1c = s.record_swipe(event_id="W1-retry", device_id="GATE-A", person_id="P01",
                         gate="东门", zone="A区", direction="IN",
                         device_time=T(1) + timedelta(seconds=10))
    assert e1b["event_id"] == "W1"
    assert e1c["event_id"] == "W1"
    assert s.store.aliases["W1-retry"] == "W1"
    swipes = [e for e in s.store.sorted_events() if e["kind"] == "SWIPE"]
    assert len(swipes) == 1


def test_expired_qualification_blocked():
    s = build_service()
    s.record_swipe(event_id="W1", device_id="GATE-A", person_id="P02",
                   gate="东门", zone="A区", direction="IN", device_time=T(3))
    try:
        s.issue_equipment(event_id="E1", device_id="TERM-1", equipment_code="R-001",
                          person_id="P02", second_person_id="P01",
                          access_event_id="W1", device_time=T(3, 1))
        raise AssertionError("应阻止")
    except PolicyViolation as exc:
        codes = {c for c, _ in exc.reasons}
        assert "QUALIFICATION_EXPIRED" in codes
    denial = [e for e in s.store.sorted_events() if e["kind"] == "POLICY_DENIAL"]
    assert len(denial) == 1 and s.status("R-001")["state"] == "IN_STORE"


def test_missing_second_reviewer_blocked():
    s = build_service()
    s.record_swipe(event_id="W1", device_id="GATE-A", person_id="P01",
                   gate="东门", zone="A区", direction="IN", device_time=T(1))
    try:
        s.issue_equipment(event_id="E1", device_id="TERM-1", equipment_code="R-001",
                          person_id="P01", second_person_id=None,
                          access_event_id="W1", device_time=T(1, 1))
        raise AssertionError("应阻止")
    except PolicyViolation as exc:
        assert {c for c, _ in exc.reasons} == {"MISSING_SECOND_REVIEWER"}


def test_unqualified_second_reviewer_blocked():
    s = build_service()
    s.record_swipe(event_id="W1", device_id="GATE-A", person_id="P01",
                   gate="东门", zone="A区", direction="IN", device_time=T(1))
    try:
        s.issue_equipment(event_id="E1", device_id="TERM-1", equipment_code="R-001",
                          person_id="P01", second_person_id="P03",
                          access_event_id="W1", device_time=T(1, 1))
        raise AssertionError("应阻止")
    except PolicyViolation as exc:
        assert "SECOND_NOT_QUALIFIED" in {c for c, _ in exc.reasons}


def test_overdue_return_normal_blocked_then_exception_channel():
    s = build_service()
    s.record_swipe(event_id="W1", device_id="GATE-A", person_id="P01",
                   gate="东门", zone="A区", direction="IN", device_time=T(1))
    s.issue_equipment(event_id="E1", device_id="TERM-1", equipment_code="R-001",
                      person_id="P01", second_person_id="P02",
                      access_event_id="W1", device_time=T(1, 1))
    # 6 小时后普通归还 -> 超时
    s.record_swipe(event_id="W2", device_id="GATE-A", person_id="P01",
                   gate="东门", zone="A区", direction="IN", device_time=T(7))
    try:
        s.return_equipment(event_id="E2", device_id="TERM-1", equipment_code="R-001",
                           person_id="P01", access_event_id="W2", device_time=T(7, 1))
        raise AssertionError("应阻止普通超时归还")
    except PolicyViolation as exc:
        assert "OVERDUE_RETURN" in {c for c, _ in exc.reasons}
    assert s.status("R-001")["state"] == "ON_LOAN"
    # 异常通道：P01 交还，主官 P01? 不行——主官不能是领用人。构造：主官换另一个持主官角色的人。
    # P01 既是领用人又是主官不行；这里用 P02 当主官？P02 无 supervisor 角色 -> 被阻止。
    try:
        s.receive_overdue_return(event_id="E3", device_id="TERM-1",
                                 equipment_code="R-001", person_id="P01",
                                 supervisor_id="P02", second_person_id="P03",
                                 reason="演习延误", device_time=T(7, 2))
        raise AssertionError("应阻止：非主官、复核人无资质")
    except PolicyViolation as exc:
        codes = {c for c, _ in exc.reasons}
        assert "SUPERVISOR_REQUIRED" in codes and "SECOND_NOT_QUALIFIED" in codes

    # 增加一名合格主官 P05 与一名资质有效的复核人 P06（P02 枪证 10:00 已失效）
    s.register_person(Person("P05", "孙主官", "11010119910505005V",
                             {"GUN_LICENSE": T(30)}, {"A区"}, {"armory_supervisor"}))
    s.register_person(Person("P06", "周复核", "11010119910606006U",
                             {"GUN_LICENSE": T(30)}, {"A区"}, set()))
    s.receive_overdue_return(event_id="E4", device_id="TERM-1",
                             equipment_code="R-001", person_id="P01",
                             supervisor_id="P05", second_person_id="P06",
                             reason="演习延误", device_time=T(7, 3))
    assert s.status("R-001")["state"] == "IN_STORE"


def test_transfer_blocks_unauthorized_zone():
    s = build_service()
    s.record_swipe(event_id="W1", device_id="GATE-B", person_id="P01",
                   gate="西门", zone="B区", direction="IN", device_time=T(1))
    try:
        s.transfer_equipment(event_id="M1", device_id="TERM-2", equipment_code="R-002",
                             to_zone="A区", person_id="P01", second_person_id="P02",
                             device_time=T(1, 2))
        raise AssertionError("应阻止：复核人无 B区授权")
    except PolicyViolation as exc:
        codes = {c for c, _ in exc.reasons}
        assert "SECOND_ZONE_NOT_AUTHORIZED" in codes

    # P05 拥有两区授权
    s.register_person(Person("P05", "孙主官", "11010119910505005V",
                             {"GUN_LICENSE": T(30)}, {"A区", "B区"}, set()))
    s.transfer_equipment(event_id="M2", device_id="TERM-2", equipment_code="R-002",
                         to_zone="A区", person_id="P01", second_person_id="P05",
                         device_time=T(1, 3))
    assert s.status("R-002")["zone"] == "A区"


def test_inventory_full():
    import tempfile
    from pathlib import Path

    path = Path(tempfile.mkdtemp()) / "log.json"
    s = build_service(path)
    s.begin_inventory(batch_id="C1", event_id="C1B", device_id="PDA-1",
                      zone="A区", counter_id="P01", device_time=T(5))
    s.report_count(batch_id="C1", event_id="C1R1", device_id="PDA-1",
                   equipment_code="R-001", seen=True, seen_zone="A区",
                   counter_id="P01", device_time=T(5, 1))
    s.report_count(batch_id="C1", event_id="C1R2", device_id="PDA-1",
                   equipment_code="H-001", seen=False,
                   counter_id="P01", device_time=T(5, 2))
    # P03 仅有 A区授权，无枪证；差异含双控装备吗？R-001 PRESENT，H-001 MISSING（非双控），可以
    s.confirm_inventory(batch_id="C1", event_id="C1F", device_id="PDA-1",
                        confirmer_id="P01", second_person_id="P03",
                        device_time=T(5, 3))
    assert s.status("H-001")["state"] == "MISSING"
    confirm_event = [e for e in s.store.sorted_events() if e["kind"] == "INVENTORY_CONFIRM"][0]
    findings = {r["equipment_code"]: r["finding"] for r in confirm_event["payload"]["results"]}
    assert findings == {"R-001": "PRESENT", "H-001": "MISSING"}

    # 已确认后禁止再上报
    try:
        s.report_count(batch_id="C1", event_id="C1R3", device_id="PDA-1",
                       equipment_code="H-001", seen=True, seen_zone="A区",
                       counter_id="P01", device_time=T(5, 4))
        raise AssertionError("应阻止改动已确认盘点")
    except PolicyViolation as exc:
        assert "INVENTORY_ALREADY_CONFIRMED" in {c for c, _ in exc.reasons}

    # 离线补传一台门禁终端：里面有一条 T(3) 的 H-001 领用记录，声称缺失装备被借出
    # ——与已确认盘点冲突，必须隔离且不得改变 MISSING 结论
    outcomes = s.ingest_offline(device_id="TERM-9", records=[
        {"op": "swipe", "event_id": "OW1", "person_id": "P01", "gate": "东门",
         "zone": "A区", "direction": "IN", "device_time": T(2, 50).isoformat()},
        {"op": "issue", "event_id": "OE1", "equipment_code": "H-001",
         "person_id": "P01", "access_event_id": "OW1",
         "device_time": T(2, 55).isoformat()},
    ])
    issue_backfill = outcomes[1]
    assert issue_backfill["applied"] is False
    codes = {r["code"] for r in issue_backfill["payload"]["quarantine_reasons"]}
    assert "INVENTORY_ALREADY_CONFIRMED" in codes
    assert s.status("H-001")["state"] == "MISSING"
    assert len(s.quarantined) == 1

    # 落盘重载：完整性 + 状态一致
    s.save()
    s2 = build_service(path)
    assert s2.status("H-001")["state"] == "MISSING"
    assert s2.verify_timeline()["ok"]


def test_tamper_detection():
    s = build_service()
    s.record_swipe(event_id="W1", device_id="GATE-A", person_id="P01",
                   gate="东门", zone="A区", direction="IN", device_time=T(1))
    event = s.store.get("W1")
    event["payload"]["zone"] = "B区"  # 篡改
    problems = s.verify_timeline()["problems"]
    assert any("篡改" in p for p in problems)


def test_timeline_and_redaction():
    s = build_service()
    s.record_swipe(event_id="W1", device_id="GATE-A", person_id="P01",
                   gate="东门", zone="A区", direction="IN", device_time=T(1))
    s.issue_equipment(event_id="E1", device_id="TERM-1", equipment_code="R-001",
                      person_id="P01", second_person_id="P02",
                      access_event_id="W1", device_time=T(1, 1))
    tl = s.timeline_by_equipment("R-001")
    assert [e["event_id"] for e in tl] == ["W1", "E1"]

    # 装备焦点导出：P01/P02 是业务参与方，姓名保留；证件号对非审计掩码
    export = s.export_timeline(focus="equipment", focus_id="R-001", requester_id="P01")
    people = export["events"][1]["people"]
    assert people["actor"]["name"] == "张三"
    assert "*" in people["actor"]["id_number"]

    # 审计角色可见明文证件号
    export_audit = s.export_timeline(focus="equipment", focus_id="R-001", requester_id="P04")
    assert export_audit["events"][1]["people"]["actor"]["id_number"] == "11010119900101001X"

    # 人员焦点导出：复核人 P02 对 P01 核查无关 -> 隐去
    export_p = s.export_timeline(focus="person", focus_id="P01", requester_id="P04")
    second = next(
        p for e in export_p["events"] if e["event_id"] == "E1"
        for key, p in e["people"].items() if key == "second_person_id"
    )
    assert second["name"] == "（非核查对象，已隐去）" and "P02" not in second["person_id"]

    # 时间线按设备时间有序
    times = [e["device_time"] for e in export["events"]]
    assert times == sorted(times)


def test_offline_idempotent_reupload():
    s = build_service()
    s.record_swipe(event_id="W1", device_id="GATE-A", person_id="P01",
                   gate="东门", zone="A区", direction="IN", device_time=T(1))
    s.issue_equipment(event_id="E1", device_id="TERM-1", equipment_code="H-001",
                      person_id="P01", access_event_id="W1", device_time=T(1, 1))
    # 再次补传同一批：事件只关联原事件，不产生重复领用
    outcomes = s.ingest_offline(device_id="TERM-1", records=[
        {"op": "issue", "event_id": "E1", "equipment_code": "H-001",
         "person_id": "P01", "access_event_id": "W1", "device_time": T(1, 1).isoformat()},
    ])
    assert outcomes[0]["event_id"] == "E1"
    assert len([e for e in s.store.sorted_events() if e["kind"] == "ISSUE"]) == 1


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        check(fn.__name__, fn)
    print(f"\n{len(tests)} tests passed")
