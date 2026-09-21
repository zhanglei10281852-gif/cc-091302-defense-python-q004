"""军械库出入核验服务测试。

运行：python -m unittest discover -s tests -v
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from src.service import (
    EquipmentStatus,
    OperationRejected,
    PersonRole,
    RejectReason,
    Service,
)

T0 = datetime(2026, 9, 21, 8, 0, 0, tzinfo=timezone.utc)


def t(seconds: float = 0) -> datetime:
    return T0 + timedelta(seconds=seconds)


class ArmoryTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = Service()

    def seed_person(
        self,
        pid="P001",
        name="张三",
        zones=("A区",),
        categories=("步枪",),
        qual_expiry=None,
        roles=(),
        id_number="110101199001011234",
    ):
        return self.svc.register_person(
            pid,
            name,
            id_number,
            qualifications=[("CAT:步枪", qual_expiry or t(3600))],
            authorized_zones=zones,
            authorized_categories=categories,
            roles=roles,
        )

    def seed_equipment(self, code="E001", zone="A区", dual=False, category="步枪"):
        return self.svc.register_equipment(
            code,
            category=category,
            zone=zone,
            required_qualification=f"CAT:{category}",
            dual_review_required=dual,
            device_time=t(-100),
        )


class TestIssueReturn(ArmoryTestBase):
    def test_issue_and_return_happy_path(self):
        self.seed_person()
        self.seed_equipment()
        self.svc.issue_equipment("E001", "P001", None, t(0), t(3600))
        self.assertEqual(self.svc.equipment["E001"].status, EquipmentStatus.ISSUED)
        self.assertEqual(self.svc.equipment["E001"].holder_id, "P001")

        self.svc.return_equipment("E001", "P001", None, t(100))
        self.assertEqual(self.svc.equipment["E001"].status, EquipmentStatus.IN_STOCK)
        self.assertIsNone(self.svc.equipment["E001"].holder_id)

    def test_double_issue_blocked(self):
        self.seed_person()
        self.seed_equipment()
        self.svc.issue_equipment("E001", "P001", None, t(0), t(3600))
        with self.assertRaises(OperationRejected) as ctx:
            self.svc.issue_equipment("E001", "P001", None, t(10), t(3600))
        self.assertEqual(ctx.exception.reason, RejectReason.ITEM_STATE_CONFLICT)

    def test_unknown_person_and_equipment(self):
        with self.assertRaises(OperationRejected) as ctx:
            self.svc.issue_equipment("NOPE", "GHOST", None, t(0), t(10))
        self.assertEqual(ctx.exception.reason, RejectReason.PERSON_UNKNOWN)
        self.seed_person()
        with self.assertRaises(OperationRejected) as ctx:
            self.svc.issue_equipment("NOPE", "P001", None, t(0), t(10))
        self.assertEqual(ctx.exception.reason, RejectReason.EQUIPMENT_UNKNOWN)

    def test_missing_qualification_blocks_issue(self):
        self.svc.register_person(
            "P002", "李四", "id-2",
            authorized_zones={"A区"}, authorized_categories={"步枪"},
            qualifications=[("CAT:手枪", t(3600))],
        )
        self.seed_equipment()
        with self.assertRaises(OperationRejected) as ctx:
            self.svc.issue_equipment("E001", "P002", None, t(0), t(3600))
        self.assertEqual(ctx.exception.reason, RejectReason.QUALIFICATION_MISSING)

    def test_expired_qualification_blocks_both_issue_and_return(self):
        self.seed_person(qual_expiry=t(100))
        self.seed_equipment()
        # 先在资质有效时领用
        self.svc.issue_equipment("E001", "P001", None, t(0), t(3600))
        # 资质失效后归还被阻止
        with self.assertRaises(OperationRejected) as ctx:
            self.svc.return_equipment("E001", "P001", None, t(200))
        self.assertEqual(ctx.exception.reason, RejectReason.QUALIFICATION_EXPIRED)
        self.assertIn("失效", ctx.exception.message)

    def test_zone_and_category_authorization(self):
        self.seed_person(zones=("B区",))
        self.seed_equipment()
        with self.assertRaises(OperationRejected) as ctx:
            self.svc.issue_equipment("E001", "P001", None, t(0), t(3600))
        self.assertEqual(ctx.exception.reason, RejectReason.ZONE_NOT_AUTHORIZED)

        self.svc.persons["P001"].authorized_zones.add("A区")
        self.svc.persons["P001"].authorized_categories.discard("步枪")
        with self.assertRaises(OperationRejected) as ctx:
            self.svc.issue_equipment("E001", "P001", None, t(0), t(3600))
        self.assertEqual(ctx.exception.reason, RejectReason.CATEGORY_NOT_AUTHORIZED)

    def test_holder_mismatch_return(self):
        self.seed_person()
        self.svc.register_person(
            "P009", "王五", "id-9",
            qualifications=[("CAT:步枪", t(3600))],
            authorized_zones={"A区"}, authorized_categories={"步枪"},
        )
        self.seed_equipment()
        self.svc.issue_equipment("E001", "P001", None, t(0), t(3600))
        with self.assertRaises(OperationRejected) as ctx:
            self.svc.return_equipment("E001", "P009", None, t(10))
        self.assertEqual(ctx.exception.reason, RejectReason.HOLDER_MISMATCH)


class TestDualReview(ArmoryTestBase):
    def _two_qualified_people(self):
        self.seed_person("P001", "张三")
        self.svc.register_person(
            "P002", "李四", "id-2",
            qualifications=[("CAT:步枪", t(3600))],
            authorized_zones={"A区"}, authorized_categories={"步枪"},
        )

    def test_missing_second_reviewer_blocks(self):
        self._two_qualified_people()
        self.seed_equipment(dual=True)
        with self.assertRaises(OperationRejected) as ctx:
            self.svc.issue_equipment("E001", "P001", None, t(0), t(3600))
        self.assertEqual(ctx.exception.reason, RejectReason.SECOND_REVIEWER_REQUIRED)
        self.assertIn("第二名复核人", ctx.exception.message)

    def test_same_person_reviewer_blocks(self):
        self._two_qualified_people()
        self.seed_equipment(dual=True)
        with self.assertRaises(OperationRejected) as ctx:
            self.svc.issue_equipment("E001", "P001", "P001", t(0), t(3600))
        self.assertEqual(ctx.exception.reason, RejectReason.REVIEWER_SAME_AS_ACTOR)

    def test_unqualified_witness_blocks(self):
        self.seed_person("P001", "张三")
        self.svc.register_person(
            "P002", "李四", "id-2",
            qualifications=[],
            authorized_zones={"A区"}, authorized_categories={"步枪"},
        )
        self.seed_equipment(dual=True)
        with self.assertRaises(OperationRejected) as ctx:
            self.svc.issue_equipment("E001", "P001", "P002", t(0), t(3600))
        self.assertEqual(ctx.exception.reason, RejectReason.QUALIFICATION_MISSING)

    def test_dual_review_issue_and_return_succeeds(self):
        self._two_qualified_people()
        self.seed_equipment(dual=True)
        self.svc.issue_equipment("E001", "P001", "P002", t(0), t(3600))
        self.svc.return_equipment("E001", "P001", "P002", t(100))
        self.assertEqual(self.svc.equipment["E001"].status, EquipmentStatus.IN_STOCK)


class TestOverdue(ArmoryTestBase):
    def test_overdue_return_without_supervisor_blocked(self):
        self.seed_person()
        self.seed_equipment()
        self.svc.issue_equipment("E001", "P001", None, t(0), t(60))
        with self.assertRaises(OperationRejected) as ctx:
            self.svc.return_equipment("E001", "P001", None, t(61))
        self.assertEqual(ctx.exception.reason, RejectReason.OVERDUE_RETURN)

    def test_overdue_exception_requires_real_supervisor_and_witness(self):
        self.seed_person()
        self.svc.register_person(
            "P002", "李四", "id-2",
            qualifications=[("CAT:步枪", t(3600))],
            authorized_zones={"A区"}, authorized_categories={"步枪"},
        )
        self.svc.register_person(
            "P003", "赵六", "id-3",
            qualifications=[("CAT:步枪", t(3600))],
            authorized_zones={"A区"}, authorized_categories={"步枪"},
            roles=[PersonRole.SUPERVISOR],
        )
        self.seed_equipment()
        self.svc.issue_equipment("E001", "P001", None, t(0), t(60))
        # 主管角色不对
        with self.assertRaises(OperationRejected) as ctx:
            self.svc.return_equipment(
                "E001", "P001", "P002", t(61), supervisor_id="P002"
            )
        self.assertEqual(ctx.exception.reason, RejectReason.SUPERVISOR_REQUIRED)
        # 三方齐全才放行
        entry = self.svc.return_equipment(
            "E001", "P001", "P002", t(61), supervisor_id="P003",
            note="演练延迟，主管授权例外",
        )
        self.assertTrue(entry["payload"]["overdue"])
        self.assertTrue(entry["payload"]["exception"])

    def test_outstanding_overdue_blocks_new_issue(self):
        self.seed_person()
        self.seed_equipment("E001")
        self.seed_equipment("E002")
        self.svc.issue_equipment("E001", "P001", None, t(0), t(60))
        with self.assertRaises(OperationRejected) as ctx:
            self.svc.issue_equipment("E002", "P001", None, t(61), t(3600))
        self.assertEqual(ctx.exception.reason, RejectReason.OUTSTANDING_OVERDUE_LOAN)


class TestTransfer(ArmoryTestBase):
    def test_transfer_requires_both_zones(self):
        self.seed_person(zones=("A区",))
        self.seed_equipment()
        self.svc.register_person(
            "P002", "李四", "id-2",
            qualifications=[("CAT:步枪", t(3600))],
            authorized_zones={"A区", "B区"}, authorized_categories={"步枪"},
        )
        with self.assertRaises(OperationRejected) as ctx:
            self.svc.transfer_equipment(
                "E001", "P001", None, "A区", "B区", t(0)
            )
        self.assertEqual(ctx.exception.reason, RejectReason.ZONE_NOT_AUTHORIZED)

        self.svc.transfer_equipment(
            "E001", "P002", None, "A区", "B区", t(0)
        )
        self.assertEqual(self.svc.equipment["E001"].zone, "B区")

    def test_transfer_issued_item_blocked(self):
        self.seed_person(zones=("A区", "B区"))
        self.seed_equipment()
        self.svc.issue_equipment("E001", "P001", None, t(0), t(3600))
        with self.assertRaises(OperationRejected) as ctx:
            self.svc.transfer_equipment("E001", "P001", None, "A区", "B区", t(10))
        self.assertEqual(ctx.exception.reason, RejectReason.ITEM_STATE_CONFLICT)


class TestSwipe(ArmoryTestBase):
    def test_swipe_grant_and_unknown_card(self):
        self.seed_person()
        ok = self.svc.record_swipe("DEV1", "P001", "A区", "IN", t(0), event_uid="u1")
        self.assertTrue(ok["payload"]["granted"])
        bad = self.svc.record_swipe("DEV1", "GHOST", "A区", "IN", t(1), event_uid="u2")
        self.assertTrue(bad["payload"]["unknown_card"])
        self.assertFalse(bad["payload"]["granted"])

    def test_duplicate_swipe_by_event_uid_links_original(self):
        self.seed_person()
        first = self.svc.record_swipe("DEV1", "P001", "A区", "IN", t(0), event_uid="u1")
        again = self.svc.record_swipe(
            "DEV1", "P001", "A区", "IN", t(900), event_uid="u1"
        )
        self.assertEqual(again["type"], "SWIPE_DUPLICATE")
        self.assertEqual(again["duplicate_of"], first["id"])
        # 无论隔多久，相同流水号只关联原事件
        swipes = [e for e in self.svc.timeline() if e["type"] == "SWIPE"]
        self.assertEqual(len(swipes), 1)

    def test_duplicate_swipe_within_window_without_uid(self):
        self.seed_person()
        first = self.svc.record_swipe("DEV1", "P001", "A区", "IN", t(0))
        second = self.svc.record_swipe("DEV1", "P001", "A区", "IN", t(30))
        self.assertEqual(second["duplicate_of"], first["id"])
        # 超出窗口视为新刷卡
        third = self.svc.record_swipe("DEV1", "P001", "A区", "IN", t(61))
        self.assertIsNone(third["duplicate_of"])

    def test_business_operation_validates_linked_swipe(self):
        self.seed_person()
        self.seed_equipment()
        with self.assertRaises(OperationRejected) as ctx:
            self.svc.issue_equipment(
                "E001", "P001", None, t(5), t(3600), swipe_event_uid="missing"
            )
        self.assertEqual(ctx.exception.reason, RejectReason.SWIPE_INVALID)

        self.svc.record_swipe("DEV1", "P001", "A区", "IN", t(0), event_uid="u1")
        entry = self.svc.issue_equipment(
            "E001", "P001", None, t(5), t(3600), swipe_event_uid="u1"
        )
        self.assertIsNotNone(entry["payload"]["swipe_event_id"])


class TestInventory(ArmoryTestBase):
    def _inventory_setup(self):
        self.seed_person("P001", "张三")
        self.svc.register_person(
            "P002", "李四", "id-2",
            qualifications=[("CAT:步枪", t(10000))],
            authorized_zones={"A区"}, authorized_categories={"步枪"},
        )
        self.seed_equipment("E001")
        self.seed_equipment("E002")

    def test_inventory_missing_discrepancy_and_resolution(self):
        self._inventory_setup()
        opened = self.svc.open_inventory("A区", "P001", t(100))
        sid = opened["payload"]["session_id"]
        # 盘点窗口内有门禁记录（纸面登记与门禁对不上一件）
        self.svc.record_swipe("GATE1", "P002", "A区", "IN", t(120), event_uid="g1")
        # 只点到 E002，E001 缺失
        confirmed = self.svc.confirm_inventory(
            sid, ["E002"], "P001", "P002", t(200)
        )
        kinds = {d["code"]: d["kind"] for d in confirmed["payload"]["discrepancies"]}
        self.assertEqual(kinds, {"E001": "MISSING"})
        missing = confirmed["payload"]["discrepancies"][0]
        self.assertTrue(
            any(ev["type"] == "SWIPE" for ev in missing["evidence"])
        )        # 差异定性：临时借用未归还，须双人
        res = self.svc.resolve_discrepancy(
            sid, "E001", "UNRETURNED_LOAN", "演习临时借用，门禁可印证",
            "P001", "P002", t(300),
        )
        self.assertEqual(res["payload"]["classification"], "UNRETURNED_LOAN")
        self.assertEqual(
            self.svc.sessions[sid].resolutions["E001"]["classification"],
            "UNRETURNED_LOAN",
        )

    def test_pre_open_swipe_still_collected_as_evidence(self):
        # 交接班时关键刷卡往往发生在开盘之前，证据窗口须回溯至上一盘点周期
        self._inventory_setup()
        self.svc.record_swipe("GATE1", "P002", "A区", "OUT", t(90), event_uid="g0")
        sid = self.svc.open_inventory("A区", "P001", t(100))["payload"]["session_id"]
        confirmed = self.svc.confirm_inventory(
            sid, ["E002"], "P001", "P002", t(200)
        )
        evidence = confirmed["payload"]["discrepancies"][0]["evidence"]
        self.assertTrue(any(ev["type"] == "SWIPE" for ev in evidence))

    def test_inventory_confirm_requires_two_people(self):
        self._inventory_setup()
        sid = self.svc.open_inventory("A区", "P001", t(100))["payload"]["session_id"]
        with self.assertRaises(OperationRejected) as ctx:
            self.svc.confirm_inventory(sid, ["E001", "E002"], "P001", "P001", t(200))
        self.assertEqual(ctx.exception.reason, RejectReason.REVIEWER_SAME_AS_ACTOR)

    def test_confirmed_inventory_cannot_be_reconfirmed(self):
        self._inventory_setup()
        sid = self.svc.open_inventory("A区", "P001", t(100))["payload"]["session_id"]
        self.svc.confirm_inventory(sid, ["E001", "E002"], "P001", "P002", t(200))
        with self.assertRaises(OperationRejected) as ctx:
            self.svc.confirm_inventory(sid, ["E002"], "P001", "P002", t(250))
        self.assertEqual(ctx.exception.reason, RejectReason.SESSION_ALREADY_CONFIRMED)

    def test_misplaced_and_unknown_item(self):
        self._inventory_setup()
        self.seed_equipment("E009", zone="B区")
        sid = self.svc.open_inventory("A区", "P001", t(100))["payload"]["session_id"]
        confirmed = self.svc.confirm_inventory(
            sid, ["E001", "E002", "E009", "X999"], "P001", "P002", t(200)
        )
        kinds = {d["code"]: d["kind"] for d in confirmed["payload"]["discrepancies"]}
        self.assertEqual(kinds["E009"], "MISPLACED")
        self.assertEqual(kinds["X999"], "UNKNOWN_ITEM")


class TestBackfill(ArmoryTestBase):
    def _setup(self):
        self.seed_person("P001", "张三")
        self.svc.register_person(
            "P002", "李四", "id-2",
            qualifications=[("CAT:步枪", t(10000))],
            authorized_zones={"A区"}, authorized_categories={"步枪"},
        )
        self.seed_equipment("E001")
        self.seed_equipment("E002")

    def test_backfill_sorted_and_duplicate_links_original(self):
        self._setup()
        # 在线先有一条刷卡
        online = self.svc.record_swipe(
            "DEV1", "P001", "A区", "IN", t(50), event_uid="u1"
        )
        reports = self.svc.backfill([
            {"type": "swipe", "device_id": "DEV1", "card_id": "P002",
             "zone": "A区", "direction": "IN", "device_time": t(200),
             "event_uid": "u9"},
            {"type": "swipe", "device_id": "DEV1", "card_id": "P001",
             "zone": "A区", "direction": "IN", "device_time": t(900),
             "event_uid": "u1"},  # 与在线记录重复
        ])
        self.assertTrue(all(r["accepted"] for r in reports))
        dup_link = [e for e in self.svc.timeline() if e["type"] == "SWIPE_DUPLICATE"][0]
        self.assertEqual(dup_link["duplicate_of"], online["id"])

    def test_backfill_never_overwrites_confirmed_inventory(self):
        self._setup()
        sid = self.svc.open_inventory("A区", "P001", t(100))["payload"]["session_id"]
        self.svc.confirm_inventory(
            sid, ["E001", "E002"], "P001", "P002", t(200)
        )
        snapshot_after_confirm = list(self.svc.sessions[sid].snapshot)

        # 迟到（设备时间早于盘点确认）的离线领用记录：隔离，不覆盖
        reports = self.svc.backfill([
            {"type": "issue", "code": "E001", "person_id": "P002",
             "witness_id": None, "device_time": t(150),
             "expected_return_at": t(3600)},
        ])
        self.assertFalse(reports[0]["accepted"])
        self.assertEqual(
            reports[0]["reason"], RejectReason.INVENTORY_CONFIRMED_PROTECTED
        )
        # 状态未变，盘点结果原封不动
        self.assertEqual(self.svc.equipment["E001"].status, EquipmentStatus.IN_STOCK)
        self.assertEqual(self.svc.sessions[sid].snapshot, snapshot_after_confirm)
        quarantined = [
            e for e in self.svc.timeline() if e["type"] == "BACKFILL_REJECTED"
        ]
        self.assertEqual(len(quarantined), 1)
        self.assertTrue(quarantined[0]["quarantined"])

        # 确认时点之后的补传可以正常并入
        ok = self.svc.backfill([
            {"type": "issue", "code": "E001", "person_id": "P002",
             "witness_id": None, "device_time": t(250),
             "expected_return_at": t(3600)},
        ])
        self.assertTrue(ok[0]["accepted"])
        self.assertEqual(self.svc.equipment["E001"].holder_id, "P002")

    def test_backfill_enforces_same_business_rules(self):
        self._setup()
        # 资质在 t(10000) 失效前有效；构造一条缺双人复核的双控装备领用
        self.seed_equipment("E003", dual=True)
        reports = self.svc.backfill([
            {"type": "issue", "code": "E003", "person_id": "P002",
             "witness_id": None, "device_time": t(150),
             "expected_return_at": t(3600)},
        ])
        self.assertFalse(reports[0]["accepted"])
        self.assertEqual(
            reports[0]["reason"], RejectReason.SECOND_REVIEWER_REQUIRED
        )
        self.assertEqual(self.svc.equipment["E003"].status, EquipmentStatus.IN_STOCK)


class TestTimelineAndAudit(ArmoryTestBase):
    def test_timeline_sorted_by_device_time_and_filtered(self):
        self.seed_person()
        self.seed_equipment()
        # 乱序到达
        self.svc.issue_equipment("E001", "P001", None, t(200), t(3000))
        self.svc.record_swipe("DEV1", "P001", "A区", "IN", t(50), event_uid="u1")
        self.svc.return_equipment("E001", "P001", None, t(400))

        line = self.svc.timeline(equipment_code="E001")
        times = [e["device_time"] for e in line]
        self.assertEqual(times, sorted(times))
        self.assertEqual(
            [e["type"] for e in line],
            ["EQUIPMENT_REGISTERED", "ISSUED", "RETURNED"],
        )
        # 人员过滤能看到其门禁与业务
        p_line = self.svc.timeline(person_id="P001")
        self.assertIn("SWIPE", [e["type"] for e in p_line])

    def test_hash_chain_detects_tampering(self):
        self.seed_person()
        self.seed_equipment()
        self.svc.issue_equipment("E001", "P001", None, t(0), t(3600))
        self.assertEqual(self.svc.verify_integrity(), [])
        # 篡改既有事件载荷
        self.svc._entries[-1]["payload"]["code"] = "E999"
        problems = self.svc.verify_integrity()
        self.assertTrue(problems)
        self.assertIn("哈希不匹配", problems[0])

    def test_export_hides_unrelated_identities(self):
        self.seed_person("P001", "张三", id_number="110101199001011234")
        self.svc.register_person(
            "P002", "李四", "id-2-secret",
            qualifications=[("CAT:步枪", t(3600))],
            authorized_zones={"A区"}, authorized_categories={"步枪"},
        )
        self.seed_equipment(dual=True)
        self.svc.issue_equipment("E001", "P001", "P002", t(0), t(3600))

        exported = self.svc.export_timeline("equipment", "E001")
        self.assertTrue(exported["integrity"]["ok"])
        text = json.dumps(exported, ensure_ascii=False)
        # 与本次核查无关的完整身份信息不出现
        self.assertNotIn("id-2-secret", text)
        # 默认连姓名都隐藏，只给脱敏编号
        self.assertNotIn("李四", text)
        self.assertIn("身份已隐藏", text)

        # 显式揭示相关复核人后：姓名可见，证件号仍只留末 4 位
        revealed = self.svc.export_timeline("equipment", "E001", reveal=["P002"])
        text2 = json.dumps(revealed, ensure_ascii=False)
        self.assertIn("李四", text2)
        self.assertNotIn("id-2-secret", text2)
        self.assertIn("****cret", text2)

        # 按人员导出时本人身份揭示
        person_export = self.svc.export_timeline("person", "P001")
        self.assertIn("张三", json.dumps(person_export, ensure_ascii=False))


class TestPersistence(unittest.TestCase):
    def test_replay_restores_state_and_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "audit.jsonl")
            svc = Service(store_path=path)
            svc.register_person(
                "P001", "张三", "id-1",
                qualifications=[("CAT:步枪", t(3600))],
                authorized_zones={"A区"}, authorized_categories={"步枪"},
            )
            svc.register_equipment("E001", "步枪", "A区", "CAT:步枪", False, t(-10))
            svc.issue_equipment("E001", "P001", None, t(0), t(3600))

            restored = Service(store_path=path)
            self.assertEqual(
                restored.equipment["E001"].status, EquipmentStatus.ISSUED
            )
            self.assertEqual(restored.equipment["E001"].holder_id, "P001")
            self.assertEqual(restored.verify_integrity(), [])

            # 日志仅追加
            with open(path, encoding="utf-8") as fh:
                lines = fh.readlines()
            restored.return_equipment("E001", "P001", None, t(100))
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(len(fh.readlines()), len(lines) + 1)

    def test_tampered_log_refuses_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "audit.jsonl")
            svc = Service(store_path=path)
            svc.register_person(
                "P001", "张三", "id-1",
                qualifications=[("CAT:步枪", t(3600))],
                authorized_zones={"A区"}, authorized_categories={"步枪"},
            )
            svc.register_equipment("E001", "步枪", "A区", "CAT:步枪", False, t(-10))
            with open(path, encoding="utf-8") as fh:
                rows = [json.loads(line) for line in fh]
            rows[0]["payload"]["name"] = "内鬼"
            with open(path, "w", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            with self.assertRaises(RuntimeError):
                Service(store_path=path)


if __name__ == "__main__":
    unittest.main()
