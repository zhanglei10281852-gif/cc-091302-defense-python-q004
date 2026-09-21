"""军械库出入核验服务。

领域职责
--------
* 主数据：装备编码与存放区、人员资质（含失效时间）与授权范围、双人复核要求；
* 业务操作：领用、归还、移库、盘点确认与差异定性；
* 门禁：刷卡按设备时间入时间线，重复刷卡只关联原事件；
* 离线补传：补传记录照常留痕，但不得覆盖已确认的盘点结果；
* 审计：仅追加（append-only）日志 + SHA-256 哈希链，支持按装备/人员检索
  不可篡改时间线，导出时按"当前核查"最小化暴露身份信息。

约束被违反时抛出 :class:`OperationRejected`，其中 ``reason`` 为机器可读的
原因码，``message`` 为面向值班员的中文说明。仅使用 Python 3.11 标准库。
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

__all__ = [
    "Service",
    "OperationRejected",
    "RejectReason",
    "EquipmentStatus",
    "PersonRole",
]

# ---------------------------------------------------------------------------
# 常量与异常
# ---------------------------------------------------------------------------


class RejectReason:
    """操作被阻止的原因码（机器可读）。"""

    PERSON_UNKNOWN = "PERSON_UNKNOWN"
    EQUIPMENT_UNKNOWN = "EQUIPMENT_UNKNOWN"
    QUALIFICATION_MISSING = "QUALIFICATION_MISSING"
    QUALIFICATION_EXPIRED = "QUALIFICATION_EXPIRED"
    ZONE_NOT_AUTHORIZED = "ZONE_NOT_AUTHORIZED"
    CATEGORY_NOT_AUTHORIZED = "CATEGORY_NOT_AUTHORIZED"
    SECOND_REVIEWER_REQUIRED = "SECOND_REVIEWER_REQUIRED"
    REVIEWER_SAME_AS_ACTOR = "REVIEWER_SAME_AS_ACTOR"
    SUPERVISOR_REQUIRED = "SUPERVISOR_REQUIRED"
    OVERDUE_RETURN = "OVERDUE_RETURN"
    OUTSTANDING_OVERDUE_LOAN = "OUTSTANDING_OVERDUE_LOAN"
    ITEM_STATE_CONFLICT = "ITEM_STATE_CONFLICT"
    HOLDER_MISMATCH = "HOLDER_MISMATCH"
    SWIPE_INVALID = "SWIPE_INVALID"
    SESSION_NOT_OPEN = "SESSION_NOT_OPEN"
    SESSION_ALREADY_CONFIRMED = "SESSION_ALREADY_CONFIRMED"
    INVENTORY_CONFIRMED_PROTECTED = "INVENTORY_CONFIRMED_PROTECTED"


class OperationRejected(Exception):
    """业务规则阻止了操作；原因见 ``reason``，说明见 ``message``。"""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


class EquipmentStatus:
    IN_STOCK = "IN_STOCK"
    ISSUED = "ISSUED"


class PersonRole:
    SUPERVISOR = "SUPERVISOR"  # 可授权逾期例外归还


# ---------------------------------------------------------------------------
# 主数据模型
# ---------------------------------------------------------------------------


@dataclass
class Qualification:
    code: str
    expires_at: datetime

    def valid_at(self, at: datetime) -> bool:
        return at < self.expires_at


@dataclass
class Person:
    person_id: str
    name: str
    id_number: str  # 敏感身份信息，默认不进任何导出
    qualifications: dict[str, Qualification] = field(default_factory=dict)
    authorized_zones: set[str] = field(default_factory=set)
    authorized_categories: set[str] = field(default_factory=set)
    roles: set[str] = field(default_factory=set)


@dataclass
class Equipment:
    code: str
    category: str
    zone: str
    required_qualification: str
    dual_review_required: bool = False
    status: str = EquipmentStatus.IN_STOCK
    holder_id: str | None = None
    expected_return_at: datetime | None = None


@dataclass
class InventorySession:
    session_id: str
    zone: str
    opened_at: datetime
    opened_by: str
    confirmed_at: datetime | None = None
    confirmed_by: str | None = None
    snapshot: list[str] = field(default_factory=list)
    observed: list[str] = field(default_factory=list)
    discrepancies: list[dict[str, Any]] = field(default_factory=list)
    resolutions: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def confirmed(self) -> bool:
        return self.confirmed_at is not None


# ---------------------------------------------------------------------------
# 时间与序列化工具
# ---------------------------------------------------------------------------


def _to_dt(value: datetime | str | int | float) -> datetime:
    """统一转为带时区的 UTC 时间（naive 视为 UTC）。"""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# 补传记录中需要规范化为 ISO 字符串的时间字段
_TIME_FIELDS = frozenset(
    {"device_time", "expected_return_at", "received_at", "opened_at", "confirmed_at"}
)


# ---------------------------------------------------------------------------
# 服务主体
# ---------------------------------------------------------------------------


class Service:
    """军械库出入核验领域服务。

    Parameters
    ----------
    store_path:
        可选的 JSONL 审计日志路径。给定后每个事件仅追加落盘，重启时回放重建
        全部状态；不传则纯内存运行。
    dedup_window:
        门禁设备未提供事件流水号时，同人/同设备/同闸口/同方向在该时间窗内的
        重复刷卡只关联原事件。
    """

    # 盘点确认始终需要双人复核
    INVENTORY_DUAL_REVIEW = True

    def __init__(
        self,
        store_path: str | None = None,
        dedup_window: timedelta = timedelta(seconds=60),
    ):
        self.store_path = store_path
        self.dedup_window = dedup_window

        self.persons: dict[str, Person] = {}
        self.equipment: dict[str, Equipment] = {}
        self.sessions: dict[str, InventorySession] = {}

        # 仅追加日志（到达顺序，即哈希链顺序）
        self._entries: list[dict[str, Any]] = []
        self._seq = 0
        # (device_id, event_uid) -> 首次刷卡事件 id
        self._swipe_index: dict[tuple[str, str], str] = {}

        if store_path and os.path.exists(store_path):
            self._replay()

    # ------------------------------------------------------------------
    # 主数据登记
    # ------------------------------------------------------------------

    def register_person(
        self,
        person_id: str,
        name: str,
        id_number: str,
        qualifications: Iterable[tuple[str, datetime | str]] = (),
        authorized_zones: Iterable[str] = (),
        authorized_categories: Iterable[str] = (),
        roles: Iterable[str] = (),
    ) -> dict[str, Any]:
        payload = {
            "person_id": person_id,
            "name": name,
            "qualifications": [
                {"code": code, "expires_at": _iso(_to_dt(exp))}
                for code, exp in qualifications
            ],
            "authorized_zones": sorted(authorized_zones),
            "authorized_categories": sorted(authorized_categories),
            "roles": sorted(roles),
        }
        # id_number 仅以脱敏形式留痕，完整值保存在进程主数据中
        entry = self._emit("PERSON_REGISTERED", datetime.now(timezone.utc), payload)
        person = Person(
            person_id=person_id,
            name=name,
            id_number=id_number,
            qualifications={
                code: Qualification(code, _to_dt(exp))
                for code, exp in qualifications
            },
            authorized_zones=set(authorized_zones),
            authorized_categories=set(authorized_categories),
            roles=set(roles),
        )
        self.persons[person_id] = person
        return entry

    def register_equipment(
        self,
        code: str,
        category: str,
        zone: str,
        required_qualification: str | None = None,
        dual_review_required: bool = False,
        device_time: datetime | str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "code": code,
            "category": category,
            "zone": zone,
            "required_qualification": required_qualification or f"CAT:{category}",
            "dual_review_required": dual_review_required,
        }
        at = _to_dt(device_time) if device_time is not None else datetime.now(timezone.utc)
        entry = self._emit("EQUIPMENT_REGISTERED", at, payload)
        self.equipment[code] = Equipment(
            code=code,
            category=category,
            zone=zone,
            required_qualification=payload["required_qualification"],
            dual_review_required=dual_review_required,
        )
        return entry

    # ------------------------------------------------------------------
    # 门禁刷卡
    # ------------------------------------------------------------------

    def record_swipe(
        self,
        device_id: str,
        card_id: str,
        zone: str,
        direction: str,
        device_time: datetime | str | int | float,
        event_uid: str | None = None,
        offline: bool = False,
    ) -> dict[str, Any]:
        """记录一次门禁刷卡。

        重复刷卡（相同 ``(device_id, event_uid)``，或无流水号时落在去重时间窗
        内的同卡同闸口同方向刷卡）不会生成新事件，只返回关联到的原事件，
        并在返回条目上以 ``duplicate_of`` 标明原事件 id。
        """
        at = _to_dt(device_time)
        person = self.persons.get(card_id)

        original = self._find_original_swipe(
            device_id, card_id, zone, direction, at, event_uid
        )
        if original is not None:
            # 关联关系本身也留痕，但不产生任何状态变化
            link = self._emit(
                "SWIPE_DUPLICATE",
                at,
                {
                    "device_id": device_id,
                    "card_id": card_id,
                    "zone": zone,
                    "direction": direction,
                    "event_uid": event_uid,
                },
                actor_id=card_id if person else None,
                offline=offline,
                duplicate_of=original,
            )
            link["original"] = original
            return {**link, "original": original}

        granted = person is not None and zone in person.authorized_zones
        payload = {
            "device_id": device_id,
            "card_id": card_id,
            "zone": zone,
            "direction": direction,
            "event_uid": event_uid,
            "granted": granted,
            "unknown_card": person is None,
        }
        entry = self._emit(
            "SWIPE", at, payload, actor_id=card_id if person else None, offline=offline
        )
        if event_uid is not None:
            self._swipe_index[(device_id, event_uid)] = entry["id"]
        return entry

    def _find_original_swipe(
        self,
        device_id: str,
        card_id: str,
        zone: str,
        direction: str,
        at: datetime,
        event_uid: str | None,
    ) -> str | None:
        if event_uid is not None:
            return self._swipe_index.get((device_id, event_uid))
        for e in self._entries:
            if e["type"] != "SWIPE":
                continue
            p = e["payload"]
            if (
                p["device_id"] == device_id
                and p["card_id"] == card_id
                and p["zone"] == zone
                and p["direction"] == direction
                and abs(_to_dt(e["device_time"]) - at) <= self.dedup_window
            ):
                return e["id"]
        return None

    # ------------------------------------------------------------------
    # 业务操作：领用
    # ------------------------------------------------------------------

    def issue_equipment(
        self,
        code: str,
        person_id: str,
        witness_id: str | None,
        device_time: datetime | str,
        expected_return_at: datetime | str,
        swipe_event_uid: str | None = None,
        offline: bool = False,
    ) -> dict[str, Any]:
        at = _to_dt(device_time)
        due = _to_dt(expected_return_at)
        person = self._require_person(person_id)
        eq = self._require_equipment(code)

        if eq.status != EquipmentStatus.IN_STOCK:
            raise OperationRejected(
                RejectReason.ITEM_STATE_CONFLICT,
                f"装备 {code} 当前状态为 {eq.status}，不可重复领用",
            )
        if due <= at:
            raise OperationRejected(
                RejectReason.OVERDUE_RETURN,
                "约定归还时间必须晚于领用时间，否则必然超时",
            )
        self._assert_authorized(person, eq, at, action="领用")
        self._assert_no_outstanding_overdue(person_id, at)
        witness = self._check_dual_review(eq, person_id, witness_id, at, action="领用")
        swipe = self._check_linked_swipe(swipe_event_uid, person_id, eq.zone)

        payload = {
            "code": code,
            "person_id": person_id,
            "witness_id": witness.person_id if witness else None,
            "expected_return_at": _iso(due),
            "swipe_event_id": swipe["id"] if swipe else None,
        }
        self._protect_with_confirmed_inventory(code, eq.zone, at)
        entry = self._emit("ISSUED", at, payload, actor_id=person_id, offline=offline)

        eq.status = EquipmentStatus.ISSUED
        eq.holder_id = person_id
        eq.expected_return_at = due
        return entry

    # ------------------------------------------------------------------
    # 业务操作：归还
    # ------------------------------------------------------------------

    def return_equipment(
        self,
        code: str,
        person_id: str,
        witness_id: str | None,
        device_time: datetime | str,
        supervisor_id: str | None = None,
        note: str = "",
        swipe_event_uid: str | None = None,
        offline: bool = False,
    ) -> dict[str, Any]:
        at = _to_dt(device_time)
        eq = self._require_equipment(code)
        person = self._require_person(person_id)

        if eq.status != EquipmentStatus.ISSUED:
            raise OperationRejected(
                RejectReason.ITEM_STATE_CONFLICT,
                f"装备 {code} 当前未处于领用状态，无法归还",
            )
        if eq.holder_id != person_id:
            raise OperationRejected(
                RejectReason.HOLDER_MISMATCH,
                f"装备 {code} 登记持用人为 {eq.holder_id}，{person_id} 不能代为归还",
            )
        self._assert_authorized(person, eq, at, action="归还")
        witness = self._check_dual_review(eq, person_id, witness_id, at, action="归还")
        swipe = self._check_linked_swipe(swipe_event_uid, person_id, eq.zone)

        overdue = eq.expected_return_at is not None and at > eq.expected_return_at
        if overdue and supervisor_id is None:
            raise OperationRejected(
                RejectReason.OVERDUE_RETURN,
                f"装备 {code} 已超过约定归还时间 "
                f"{_iso(eq.expected_return_at)}，普通归还被阻止；须由值班主管"
                "授权按逾期例外登记，并保留第二名复核人",
            )

        supervisor = None
        if overdue:
            supervisor = self._require_person(supervisor_id)
            if PersonRole.SUPERVISOR not in supervisor.roles:
                raise OperationRejected(
                    RejectReason.SUPERVISOR_REQUIRED,
                    f"逾期归还须由值班主管授权，{supervisor_id} 不具备主管角色",
                )
            ids = {person_id, witness.person_id if witness else None, supervisor_id}
            if len(ids) != 3:
                raise OperationRejected(
                    RejectReason.SECOND_REVIEWER_REQUIRED,
                    "逾期例外归还须由归还人、复核人、值班主管三人分别签署",
                )
            self._assert_authorized(supervisor, eq, at, action="授权逾期归还")

        payload = {
            "code": code,
            "person_id": person_id,
            "witness_id": witness.person_id if witness else None,
            "supervisor_id": supervisor_id,
            "overdue": overdue,
            "exception": overdue,
            "note": note,
            "swipe_event_id": swipe["id"] if swipe else None,
        }
        self._protect_with_confirmed_inventory(code, eq.zone, at)
        entry = self._emit("RETURNED", at, payload, actor_id=person_id, offline=offline)

        eq.status = EquipmentStatus.IN_STOCK
        eq.holder_id = None
        eq.expected_return_at = None
        return entry

    # ------------------------------------------------------------------
    # 业务操作：移库
    # ------------------------------------------------------------------

    def transfer_equipment(
        self,
        code: str,
        person_id: str,
        witness_id: str | None,
        from_zone: str,
        to_zone: str,
        device_time: datetime | str,
        offline: bool = False,
    ) -> dict[str, Any]:
        at = _to_dt(device_time)
        eq = self._require_equipment(code)
        person = self._require_person(person_id)

        if eq.status != EquipmentStatus.IN_STOCK:
            raise OperationRejected(
                RejectReason.ITEM_STATE_CONFLICT,
                f"装备 {code} 处于领用状态，须先归还再移库",
            )
        if eq.zone != from_zone:
            raise OperationRejected(
                RejectReason.ITEM_STATE_CONFLICT,
                f"装备 {code} 实际登记存放区为 {eq.zone}，与移库单 {from_zone} 不符",
            )
        if from_zone == to_zone:
            raise OperationRejected(
                RejectReason.ITEM_STATE_CONFLICT, "移库源区与目标区相同"
            )
        self._assert_authorized(person, eq, at, action="移库", extra_zone=to_zone)
        witness = self._check_dual_review(
            eq, person_id, witness_id, at, action="移库", extra_zone=to_zone
        )

        payload = {
            "code": code,
            "person_id": person_id,
            "witness_id": witness.person_id if witness else None,
            "from_zone": from_zone,
            "to_zone": to_zone,
        }
        # 移库跨越两个存放区，两侧任一盘点已确认都不允许迟到记录穿过边界
        self._protect_with_confirmed_inventory(code, from_zone, at)
        self._protect_with_confirmed_inventory(code, to_zone, at)
        entry = self._emit("TRANSFERRED", at, payload, actor_id=person_id, offline=offline)
        eq.zone = to_zone
        return entry

    # ------------------------------------------------------------------
    # 盘点与差异
    # ------------------------------------------------------------------

    def open_inventory(
        self,
        zone: str,
        person_id: str,
        device_time: datetime | str,
    ) -> dict[str, Any]:
        at = _to_dt(device_time)
        person = self._require_person(person_id)
        if zone not in person.authorized_zones:
            raise OperationRejected(
                RejectReason.ZONE_NOT_AUTHORIZED,
                f"{person_id} 不在存放区 {zone} 的授权范围内，不能发起盘点",
            )
        session_id = f"inv-{uuid.uuid4().hex[:12]}"
        payload = {"session_id": session_id, "zone": zone}
        entry = self._emit(
            "INVENTORY_OPENED", at, payload, actor_id=person_id
        )
        self.sessions[session_id] = InventorySession(
            session_id=session_id,
            zone=zone,
            opened_at=at,
            opened_by=person_id,
        )
        return entry

    def confirm_inventory(
        self,
        session_id: str,
        observed_codes: Iterable[str],
        person_id: str,
        witness_id: str,
        device_time: datetime | str,
    ) -> dict[str, Any]:
        """确认盘点结果并固化差异。

        盘点确认始终要求第二名复核人。确认后该存放区在确认时点之前的装备
        状态即被"冻结"：任何迟到（含离线补传）的领用/归还/移库记录都不得
        改写本结果，只能作为隔离证据另行留痕。
        """
        at = _to_dt(device_time)
        session = self.sessions.get(session_id)
        if session is None:
            raise OperationRejected(
                RejectReason.SESSION_NOT_OPEN, f"盘点任务 {session_id} 不存在"
            )
        if session.confirmed:
            raise OperationRejected(
                RejectReason.SESSION_ALREADY_CONFIRMED,
                f"盘点任务 {session_id} 已确认，结果不可覆盖",
            )

        person = self._require_person(person_id)
        witness = self._require_person(witness_id)
        if witness_id == person_id:
            raise OperationRejected(
                RejectReason.REVIEWER_SAME_AS_ACTOR, "盘点人与复核人不能为同一人"
            )
        self._assert_zone_authorized(person, session.zone, at, action="盘点")
        self._assert_zone_authorized(witness, session.zone, at, action="复核盘点")

        expected = {
            code
            for code, eq in self.equipment.items()
            if eq.zone == session.zone and eq.status == EquipmentStatus.IN_STOCK
        }
        observed = set(observed_codes)
        session.snapshot = sorted(expected)

        discrepancies: list[dict[str, Any]] = []
        for code in sorted(expected - observed):
            discrepancies.append(
                {
                    "code": code,
                    "kind": "MISSING",
                    "detail": "纸面登记在册但实物未盘点到，可能为临时借用未归还或登记遗漏",
                    "evidence": self._collect_discrepancy_evidence(session, code, at),
                }
            )
        for code in sorted(observed - expected):
            if code in self.equipment:
                discrepancies.append(
                    {
                        "code": code,
                        "kind": "MISPLACED",
                        "detail": f"实物出现在 {session.zone}，登记存放区为 "
                        f"{self.equipment[code].zone}",
                        "evidence": [],
                    }
                )
            else:
                discrepancies.append(
                    {
                        "code": code,
                        "kind": "UNKNOWN_ITEM",
                        "detail": "实物编码不在装备台账中",
                        "evidence": [],
                    }
                )

        payload = {
            "session_id": session_id,
            "zone": session.zone,
            "person_id": person_id,
            "witness_id": witness_id,
            "snapshot": session.snapshot,
            "observed": sorted(observed),
            "discrepancies": discrepancies,
        }
        entry = self._emit(
            "INVENTORY_CONFIRMED", at, payload, actor_id=person_id
        )
        session.confirmed_at = at
        session.confirmed_by = person_id
        session.discrepancies = discrepancies
        session.observed = sorted(observed)
        return entry

    def resolve_discrepancy(
        self,
        session_id: str,
        code: str,
        classification: str,
        explanation: str,
        person_id: str,
        witness_id: str,
        device_time: datetime | str,
    ) -> dict[str, Any]:
        """定性并关闭一条盘点差异，须双人复核。

        classification 取值：``UNRETURNED_LOAN``（临时借用未归还）、
        ``REGISTRY_OMISSION``（登记遗漏）、``OTHER``。
        """
        at = _to_dt(device_time)
        session = self.sessions.get(session_id)
        if session is None or not session.confirmed:
            raise OperationRejected(
                RejectReason.SESSION_NOT_OPEN, "差异所对应的盘点任务尚未确认"
            )
        if code not in {d["code"] for d in session.discrepancies}:
            raise OperationRejected(
                RejectReason.ITEM_STATE_CONFLICT,
                f"盘点任务 {session_id} 中不存在装备 {code} 的差异",
            )
        if classification not in {"UNRETURNED_LOAN", "REGISTRY_OMISSION", "OTHER"}:
            raise OperationRejected(
                RejectReason.ITEM_STATE_CONFLICT, f"未知差异定性：{classification}"
            )
        person = self._require_person(person_id)
        witness = self._require_person(witness_id)
        if witness_id == person_id:
            raise OperationRejected(
                RejectReason.REVIEWER_SAME_AS_ACTOR, "定性人与复核人不能为同一人"
            )
        self._assert_zone_authorized(person, session.zone, at, action="差异定性")
        self._assert_zone_authorized(witness, session.zone, at, action="复核差异定性")

        payload = {
            "session_id": session_id,
            "code": code,
            "classification": classification,
            "explanation": explanation,
            "person_id": person_id,
            "witness_id": witness_id,
        }
        entry = self._emit("DISCREPANCY_RESOLVED", at, payload, actor_id=person_id)
        session.resolutions[code] = {
            "classification": classification,
            "explanation": explanation,
            "by": person_id,
            "witness": witness_id,
            "at": _iso(at),
            "event_id": entry["id"],
        }
        return entry

    def _collect_discrepancy_evidence(
        self, session: InventorySession, code: str, confirmed_at: datetime
    ) -> list[dict[str, Any]]:
        """汇总本盘点周期内与差异有关但无法闭环的证据。

        证据窗口从上一次同区盘点确认之后开始（首次盘点则回溯全部历史），
        到本次盘点确认为止——交接班时关键刷卡往往发生在开盘前几分钟。
        """
        previous = [
            s.confirmed_at
            for s in self.sessions.values()
            if s is not session
            and s.zone == session.zone
            and s.confirmed
            and s.confirmed_at < session.opened_at
        ]
        window_start = max(previous) if previous else datetime.min.replace(tzinfo=timezone.utc)
        window_end = confirmed_at
        evidence: list[dict[str, Any]] = []
        for e in self._entries:
            t = _to_dt(e["device_time"])
            if not (window_start <= t <= window_end):
                continue
            if e["type"] == "SWIPE" and e["payload"]["zone"] == session.zone:
                evidence.append(
                    {"event_id": e["id"], "type": "SWIPE", "at": e["device_time"]}
                )
            elif e["type"] == "BACKFILL_REJECTED" and (
                e["payload"].get("original", {}).get("code") == code
            ):
                evidence.append(
                    {
                        "event_id": e["id"],
                        "type": "QUARANTINED_BACKFILL",
                        "at": e["device_time"],
                        "reason": e["payload"]["reason"],
                    }
                )
        return evidence

    # ------------------------------------------------------------------
    # 离线补传
    # ------------------------------------------------------------------

    def backfill(
        self,
        records: Iterable[dict[str, Any]],
        received_at: datetime | str | None = None,
    ) -> list[dict[str, Any]]:
        """补传一批离线门禁/业务记录。

        记录先按设备时间排序再逐条处理：

        * 门禁刷卡：正常并入时间线；重复刷卡只关联原事件；
        * 业务记录：与在线操作执行完全相同的资质/授权/双人/时效校验；
        * 任何试图改变"已确认盘点"时点之前状态的记录都被隔离
          （``BACKFILL_REJECTED`` 留痕），盘点结果保持不变。

        返回与输入等长的处理报告，不因单条失败中断整批补传。
        """
        got = _to_dt(received_at) if received_at else datetime.now(timezone.utc)
        ordered = sorted(records, key=lambda r: _to_dt(r["device_time"]))
        reports: list[dict[str, Any]] = []
        for record in ordered:
            kind = record.get("type")
            try:
                entry = self._dispatch_backfill(kind, record)
                reports.append(
                    {"accepted": True, "type": kind, "event_id": entry["id"]}
                )
            except OperationRejected as rejected:
                # 盘点保护（及其他校验失败）：原记录隔离留痕但不改状态
                safe_original = {
                    k: (_iso(_to_dt(v)) if k in _TIME_FIELDS and v is not None else v)
                    for k, v in record.items()
                    if k not in {"id_number"}
                }
                entry = self._emit(
                    "BACKFILL_REJECTED",
                    _to_dt(record.get("device_time", got)),
                    {
                        "original": safe_original,
                        "reason": rejected.reason,
                        "message": rejected.message,
                        "received_at": _iso(got),
                    },
                    actor_id=record.get("person_id") or record.get("card_id"),
                    offline=True,
                    quarantined=True,
                )
                reports.append(
                    {
                        "accepted": False,
                        "type": kind,
                        "reason": rejected.reason,
                        "message": rejected.message,
                        "quarantine_event_id": entry["id"],
                    }
                )
        return reports

    def _dispatch_backfill(self, kind: str, record: dict[str, Any]) -> dict[str, Any]:
        if kind == "swipe":
            return self.record_swipe(
                device_id=record["device_id"],
                card_id=record["card_id"],
                zone=record["zone"],
                direction=record["direction"],
                device_time=record["device_time"],
                event_uid=record.get("event_uid"),
                offline=True,
            )
        if kind == "issue":
            return self.issue_equipment(
                code=record["code"],
                person_id=record["person_id"],
                witness_id=record.get("witness_id"),
                device_time=record["device_time"],
                expected_return_at=record["expected_return_at"],
                swipe_event_uid=record.get("swipe_event_uid"),
                offline=True,
            )
        if kind == "return":
            return self.return_equipment(
                code=record["code"],
                person_id=record["person_id"],
                witness_id=record.get("witness_id"),
                device_time=record["device_time"],
                supervisor_id=record.get("supervisor_id"),
                note=record.get("note", ""),
                swipe_event_uid=record.get("swipe_event_uid"),
                offline=True,
            )
        if kind == "transfer":
            return self.transfer_equipment(
                code=record["code"],
                person_id=record["person_id"],
                witness_id=record.get("witness_id"),
                from_zone=record["from_zone"],
                to_zone=record["to_zone"],
                device_time=record["device_time"],
                offline=True,
            )
        raise OperationRejected(
            RejectReason.ITEM_STATE_CONFLICT, f"不支持的补传记录类型：{kind}"
        )

    # ------------------------------------------------------------------
    # 审计时间线、完整性校验与导出
    # ------------------------------------------------------------------

    def timeline(
        self,
        equipment_code: str | None = None,
        person_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """按设备时间排序返回时间线，可按装备或人员过滤。"""
        entries = [
            e
            for e in self._entries
            if (equipment_code is None or self._entry_refs_code(e, equipment_code))
            and (person_id is None or self._entry_refs_person(e, person_id))
        ]
        return sorted(entries, key=lambda e: (_to_dt(e["device_time"]), e["seq"]))

    def verify_integrity(self) -> list[str]:
        """重算哈希链，返回问题清单；空列表表示时间线完整未被篡改。"""
        problems: list[str] = []
        prev_hash = "GENESIS"
        seen_ids: set[str] = set()
        for e in self._entries:
            if e["id"] in seen_ids:
                problems.append(f"事件 {e['id']} 重复")
            seen_ids.add(e["id"])
            expected = self._hash_entry(e, prev_hash)
            if e.get("prev_hash") != prev_hash:
                problems.append(f"事件 {e['id']} 前向哈希断裂")
            if e.get("hash") != expected:
                problems.append(f"事件 {e['id']} 内容哈希不匹配，疑似被篡改")
            prev_hash = e.get("hash", expected)
        return problems

    def export_timeline(
        self,
        subject_type: str,
        subject_id: str,
        reveal: Iterable[str] = (),
        as_json: bool = False,
    ) -> dict[str, Any] | str:
        """导出面向"当前核查"的时间线。

        与本次核查无关的身份信息一律隐藏：默认仅显示人员角色与脱敏编号，
        姓名完整隐藏；被核查人员本人及 ``reveal`` 显式授权的相关人员可显示
        姓名，证件号最多保留末 4 位。
        """
        if subject_type == "equipment":
            entries = self.timeline(equipment_code=subject_id)
            subject = {"equipment_code": subject_id}
            revealed = set(reveal)
        elif subject_type == "person":
            entries = self.timeline(person_id=subject_id)
            subject = {"person_id": subject_id}
            revealed = {subject_id, *reveal}
        else:
            raise ValueError("subject_type 必须为 equipment 或 person")

        exported = [self._redact_entry(e, revealed) for e in entries]
        problems = self.verify_integrity()
        result = {
            "subject": subject,
            "exported_at": _iso(datetime.now(timezone.utc)),
            "integrity": {
                "ok": not problems,
                "note": "哈希链校验通过" if not problems else "存在篡改",
            },
            "events": exported,
        }
        if as_json:
            return json.dumps(result, ensure_ascii=False, indent=2)
        return result

    # ------------------------------------------------------------------
    # 内部：校验
    # ------------------------------------------------------------------

    def _require_person(self, person_id: str | None) -> Person:
        if not person_id or person_id not in self.persons:
            raise OperationRejected(
                RejectReason.PERSON_UNKNOWN, f"人员 {person_id} 不在在册人员名单中"
            )
        return self.persons[person_id]

    def _require_equipment(self, code: str) -> Equipment:
        if code not in self.equipment:
            raise OperationRejected(
                RejectReason.EQUIPMENT_UNKNOWN, f"装备编码 {code} 不在台账中"
            )
        return self.equipment[code]

    def _assert_zone_authorized(
        self, person: Person, zone: str, at: datetime, action: str
    ) -> None:
        if zone not in person.authorized_zones:
            raise OperationRejected(
                RejectReason.ZONE_NOT_AUTHORIZED,
                f"{person.person_id} 不在存放区 {zone} 授权范围内，不能执行{action}",
            )

    def _assert_authorized(
        self,
        person: Person,
        eq: Equipment,
        at: datetime,
        action: str,
        extra_zone: str | None = None,
    ) -> None:
        self._assert_zone_authorized(person, eq.zone, at, action)
        if extra_zone is not None and extra_zone not in person.authorized_zones:
            raise OperationRejected(
                RejectReason.ZONE_NOT_AUTHORIZED,
                f"{person.person_id} 不在目标存放区 {extra_zone} 授权范围内",
            )
        if eq.category not in person.authorized_categories:
            raise OperationRejected(
                RejectReason.CATEGORY_NOT_AUTHORIZED,
                f"{person.person_id} 无装备类别 {eq.category} 的{action}授权",
            )
        qual = person.qualifications.get(eq.required_qualification)
        if qual is None:
            raise OperationRejected(
                RejectReason.QUALIFICATION_MISSING,
                f"{person.person_id} 缺少资质 {eq.required_qualification}，"
                f"不能{action}装备 {eq.code}",
            )
        if not qual.valid_at(at):
            raise OperationRejected(
                RejectReason.QUALIFICATION_EXPIRED,
                f"{person.person_id} 的资质 {qual.code} 已于 "
                f"{_iso(qual.expires_at)} 失效，不能{action}装备 {eq.code}",
            )

    def _check_dual_review(
        self,
        eq: Equipment,
        actor_id: str,
        witness_id: str | None,
        at: datetime,
        action: str,
        extra_zone: str | None = None,
    ) -> Person | None:
        if not eq.dual_review_required:
            return None
        if not witness_id:
            raise OperationRejected(
                RejectReason.SECOND_REVIEWER_REQUIRED,
                f"装备 {eq.code} 要求双人复核，{action}缺少第二名复核人",
            )
        if witness_id == actor_id:
            raise OperationRejected(
                RejectReason.REVIEWER_SAME_AS_ACTOR,
                f"装备 {eq.code} 的复核人不能与{action}人为同一人",
            )
        witness = self._require_person(witness_id)
        # 复核人必须本人资质有效且在授权范围内，否则双人复核形同虚设
        self._assert_authorized(witness, eq, at, action=f"复核{action}", extra_zone=extra_zone)
        return witness

    def _assert_no_outstanding_overdue(self, person_id: str, at: datetime) -> None:
        for eq in self.equipment.values():
            if (
                eq.holder_id == person_id
                and eq.expected_return_at is not None
                and at > eq.expected_return_at
            ):
                raise OperationRejected(
                    RejectReason.OUTSTANDING_OVERDUE_LOAN,
                    f"{person_id} 尚有装备 {eq.code} 逾期未还（应于 "
                    f"{_iso(eq.expected_return_at)} 前归还），在逾期事项闭环前"
                    "不能领用新装备",
                )

    def _check_linked_swipe(
        self, event_uid: str | None, person_id: str, zone: str
    ) -> dict[str, Any] | None:
        if event_uid is None:
            return None
        swipe_id = next(
            (eid for (dev, uid), eid in self._swipe_index.items() if uid == event_uid),
            None,
        )
        if swipe_id is None:
            raise OperationRejected(
                RejectReason.SWIPE_INVALID,
                f"门禁流水号 {event_uid} 不存在，业务操作不能关联虚构刷卡记录",
            )
        swipe = next(e for e in self._entries if e["id"] == swipe_id)
        p = swipe["payload"]
        if not p["granted"]:
            raise OperationRejected(
                RejectReason.SWIPE_INVALID,
                f"门禁流水 {event_uid} 为未授权刷卡，不能作为{zone}出入凭证",
            )
        if swipe["actor_id"] != person_id or p["zone"] != zone:
            raise OperationRejected(
                RejectReason.SWIPE_INVALID,
                f"门禁流水 {event_uid} 的刷卡人/闸口与本操作不一致",
            )
        return swipe

    def _protect_with_confirmed_inventory(
        self, code: str, zone: str, at: datetime
    ) -> None:
        """已确认盘点的结果不允许被迟到记录（含离线补传）改写。

        存放区一经盘点确认，确认时点之前的该区域状态即整体冻结：任何设备
        时间早于确认时间的领用/归还/移库记录都不得再落入该区域，只能隔离
        留痕。装备编码一并写入原因，便于审计关联。
        """
        for session in self.sessions.values():
            if not session.confirmed or session.zone != zone:
                continue
            if at <= session.confirmed_at:
                raise OperationRejected(
                    RejectReason.INVENTORY_CONFIRMED_PROTECTED,
                    f"存放区 {zone} 的盘点 {session.session_id} 已于 "
                    f"{_iso(session.confirmed_at)} 确认，装备 {code} 在确认时点"
                    "之前的状态已冻结；该记录只能隔离留痕，不得覆盖盘点结果",
                )

    # ------------------------------------------------------------------
    # 内部：事件落盘、哈希链与回放
    # ------------------------------------------------------------------

    def _emit(
        self,
        event_type: str,
        device_time: datetime,
        payload: dict[str, Any],
        actor_id: str | None = None,
        offline: bool = False,
        duplicate_of: str | None = None,
        quarantined: bool = False,
    ) -> dict[str, Any]:
        self._seq += 1
        prev_hash = self._entries[-1]["hash"] if self._entries else "GENESIS"
        entry: dict[str, Any] = {
            "id": f"evt-{uuid.uuid4().hex[:16]}",
            "seq": self._seq,
            "type": event_type,
            "device_time": _iso(device_time),
            "received_at": _iso(datetime.now(timezone.utc)),
            "actor_id": actor_id,
            "offline": offline,
            "quarantined": quarantined,
            "duplicate_of": duplicate_of,
            "payload": payload,
        }
        entry["prev_hash"] = prev_hash
        entry["hash"] = self._hash_entry(entry, prev_hash)
        self._entries.append(entry)
        if self.store_path:
            with open(self.store_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    @staticmethod
    def _hash_entry(entry: dict[str, Any], prev_hash: str) -> str:
        body = {k: v for k, v in entry.items() if k != "hash"}
        canonical = json.dumps(
            body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256((prev_hash + "|" + canonical).encode("utf-8")).hexdigest()

    def _replay(self) -> None:
        with open(self.store_path, encoding="utf-8") as fh:
            raw = [json.loads(line) for line in fh if line.strip()]
        problems = []
        prev_hash = "GENESIS"
        for e in raw:
            if e.get("prev_hash") != prev_hash or e.get(
                "hash"
            ) != self._hash_entry(e, prev_hash):
                problems.append(e["id"])
            prev_hash = e.get("hash", prev_hash)
        if problems:
            raise RuntimeError(f"审计日志哈希链校验失败，问题事件：{problems}")
        self._entries = raw
        self._seq = max((e["seq"] for e in raw), default=0)
        for e in raw:
            self._apply_replayed(e)

    def _apply_replayed(self, e: dict[str, Any]) -> None:
        t = e["type"]
        p = e["payload"]
        at = _to_dt(e["device_time"])
        if t == "PERSON_REGISTERED":
            self.persons[p["person_id"]] = Person(
                person_id=p["person_id"],
                name=p["name"],
                # 完整证件号不入审计日志；回放后主数据中不可得，需重新登记提供
                id_number="",
                qualifications={
                    q["code"]: Qualification(q["code"], _to_dt(q["expires_at"]))
                    for q in p["qualifications"]
                },
                authorized_zones=set(p["authorized_zones"]),
                authorized_categories=set(p["authorized_categories"]),
                roles=set(p["roles"]),
            )
        elif t == "EQUIPMENT_REGISTERED":
            self.equipment[p["code"]] = Equipment(
                code=p["code"],
                category=p["category"],
                zone=p["zone"],
                required_qualification=p["required_qualification"],
                dual_review_required=p["dual_review_required"],
            )
        elif t == "SWIPE":
            if p.get("event_uid"):
                self._swipe_index[(p["device_id"], p["event_uid"])] = e["id"]
        elif t == "ISSUED":
            eq = self.equipment[p["code"]]
            eq.status = EquipmentStatus.ISSUED
            eq.holder_id = p["person_id"]
            eq.expected_return_at = _to_dt(p["expected_return_at"])
        elif t == "RETURNED":
            eq = self.equipment[p["code"]]
            eq.status = EquipmentStatus.IN_STOCK
            eq.holder_id = None
            eq.expected_return_at = None
        elif t == "TRANSFERRED":
            self.equipment[p["code"]].zone = p["to_zone"]
        elif t == "INVENTORY_OPENED":
            self.sessions[p["session_id"]] = InventorySession(
                session_id=p["session_id"],
                zone=p["zone"],
                opened_at=at,
                opened_by=e["actor_id"],
            )
        elif t == "INVENTORY_CONFIRMED":
            session = self.sessions[p["session_id"]]
            session.confirmed_at = at
            session.confirmed_by = p["person_id"]
            session.snapshot = list(p["snapshot"])
            session.observed = list(p.get("observed", []))
            session.discrepancies = list(p["discrepancies"])
        elif t in {"SWIPE_DUPLICATE", "BACKFILL_REJECTED", "DISCREPANCY_RESOLVED"}:
            if t == "DISCREPANCY_RESOLVED":
                session = self.sessions[p["session_id"]]
                session.resolutions[p["code"]] = {
                    "classification": p["classification"],
                    "explanation": p["explanation"],
                    "by": p["person_id"],
                    "witness": p["witness_id"],
                    "at": e["device_time"],
                    "event_id": e["id"],
                }

    # ------------------------------------------------------------------
    # 内部：时间线过滤与脱敏
    # ------------------------------------------------------------------

    @staticmethod
    def _entry_refs_code(entry: dict[str, Any], code: str) -> bool:
        p = entry["payload"]
        if p.get("code") == code:
            return True
        original = p.get("original")
        if isinstance(original, dict) and original.get("code") == code:
            return True
        for d in p.get("discrepancies", []):
            if isinstance(d, dict) and d.get("code") == code:
                return True
        return False

    @staticmethod
    def _entry_refs_person(entry: dict[str, Any], person_id: str) -> bool:
        if entry.get("actor_id") == person_id:
            return True
        p = entry["payload"]
        for key in ("person_id", "witness_id", "supervisor_id", "holder_id", "card_id"):
            if p.get(key) == person_id:
                return True
        original = p.get("original")
        if isinstance(original, dict):
            for key in ("person_id", "witness_id", "supervisor_id", "card_id"):
                if original.get(key) == person_id:
                    return True
        return False

    def _redact_entry(
        self, entry: dict[str, Any], revealed: set[str]
    ) -> dict[str, Any]:
        out = dict(entry)
        out["actor_id"] = self._mask_person_ref(entry.get("actor_id"), revealed)
        payload = dict(entry["payload"])
        for key in ("person_id", "witness_id", "supervisor_id", "holder_id", "card_id", "opened_by", "confirmed_by"):
            if key in payload:
                payload[key] = self._mask_person_ref(payload[key], revealed)
        original = payload.get("original")
        if isinstance(original, dict):
            original = dict(original)
            for key in ("person_id", "witness_id", "supervisor_id", "card_id"):
                if key in original:
                    original[key] = self._mask_person_ref(original[key], revealed)
            payload["original"] = original
        for d in payload.get("discrepancies", []):
            if isinstance(d, dict) and "evidence" in d:
                d["evidence"] = [
                    {**ev, "by": self._mask_person_ref(ev.get("by"), revealed)}
                    if "by" in ev
                    else ev
                    for ev in d["evidence"]
                ]
        out["payload"] = payload
        return out

    def _mask_person_ref(
        self, value: str | None, revealed: set[str]
    ) -> str | None:
        if value is None:
            return None
        if value in revealed and value in self.persons:
            person = self.persons[value]
            id_tail = person.id_number[-4:] if person.id_number else "****"
            # 姓名可显式揭示；证件号始终只留末 4 位
            return f"{value}（{person.name}，证件号 ****{id_tail}）"
        tail = value[-2:] if len(value) >= 2 else value
        return f"****{tail}（身份已隐藏）"
