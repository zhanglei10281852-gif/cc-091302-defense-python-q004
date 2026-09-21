"""军械库出入核验服务。

能力概览
========
* 主数据维护：装备编码、存放区、人员资质（含有效期）、授权区域、双人复核要求。
* 门禁刷卡：按设备时间入链；重复刷卡只关联原事件，不产生新事件。
* 业务操作：领用、归还、移库、盘点（上报/确认差异）。
  - 资质失效、越权区域、未在授权时段刷卡进入、存在逾期未还、缺少第二名合格
    复核人等情况一律阻止，并给出原因码。
  - 超时归还有专门的异常通道，须主官 + 第二名复核人共同处理并留痕。
* 离线补传：按设备时间回放；与已确认盘点结果冲突的记录被隔离（quarantine），
  绝不覆盖已确认结论。
* 审计：按装备或人员抽取不可篡改时间线（设备哈希链 + 事件体哈希），导出时对
  与本次核查无关的身份信息做最小化脱敏。

仅使用 Python 3.11 标准库。时间统一使用带时区的 UTC。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# 异常与原因码
# ---------------------------------------------------------------------------


class ArmoryError(Exception):
    """服务层基础异常。"""


class PolicyViolation(ArmoryError):
    """操作被策略阻止。

    :param reasons: 原因码列表，每项为 ``(code, detail)``，便于调用方与审计
        记录精确说明被阻止的原因。
    """

    def __init__(self, reasons: str | list[str] | list[tuple[str, str]]):
        if isinstance(reasons, str):
            reasons = [reasons]
        self.reasons: list[tuple[str, str]] = []
        for r in reasons:
            if isinstance(r, tuple):
                self.reasons.append(r)
            else:
                self.reasons.append((r, ""))
        msg = "; ".join(f"[{code}] {detail}".strip() for code, detail in self.reasons)
        super().__init__(msg)


# ---------------------------------------------------------------------------
# 主数据
# ---------------------------------------------------------------------------


@dataclass
class Equipment:
    """一件装备的主数据。"""

    code: str
    name: str
    zone: str
    # 领用/移库该装备所需资质码；None 表示无资质要求。
    required_qualification: str | None = None
    # 是否双人双控装备：领用、移库、差异确认均需第二名合格复核人。
    dual_control: bool = False
    # 领用时限（分钟），超过即逾期。
    loan_minutes: int = 24 * 60


@dataclass
class Person:
    """一名人员的主数据。"""

    person_id: str
    name: str
    id_number: str
    # 资质码 -> 到期时间（aware datetime）
    qualifications: dict[str, datetime] = field(default_factory=dict)
    # 可进入/作业的存放区
    authorized_zones: set[str] = field(default_factory=set)
    # 角色，如 armory_supervisor（军械主官）、security_audit（审计，可见证件号）
    roles: set[str] = field(default_factory=set)

    def qualified(self, code: str | None, at: datetime) -> bool:
        if code is None:
            return True
        expiry = self.qualifications.get(code)
        return expiry is not None and expiry >= at


@dataclass
class Loan:
    person_id: str
    issued_at: datetime
    due_at: datetime
    issue_event_id: str


@dataclass
class ConfirmedCount:
    """已确认的盘点结论（受保护，离线补传不得覆盖）。"""

    batch_id: str
    equipment_code: str
    finding: str  # PRESENT / MISSING / LOCATION_MISMATCH / UNEXPECTED_PRESENT
    zone: str
    cutoff: datetime
    counter: str
    confirmer: str
    second_person: str


# ---------------------------------------------------------------------------
# 事件存储
# ---------------------------------------------------------------------------


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(value)


def _canonical(body: dict[str, Any]) -> bytes:
    return json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class EventStore:
    """按设备维护哈希链的只增事件存储。

    * 每台设备各自成链：事件按接收顺序分配 ``device_seq``，``prev_hash`` 指向前
      一条同设备事件，任何对历史事件的删改都会在完整性校验中暴露。
    * 跨设备的统一视图始终按（设备时间, 设备, 序号）排序，满足"按设备时间排序
      保存"的要求；设备链与合并顺序解耦，因此离线补传早期事件不需要改写任何
      既有哈希。
    """

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        # event_id -> event
        self._events: dict[str, dict[str, Any]] = {}
        # device_id -> 链头 event
        self._heads: dict[str, dict[str, Any]] = {}
        # 重复刷卡（或重复提交）的 event_id -> 原事件 event_id
        self.aliases: dict[str, str] = {}

    # -- 基本操作 ----------------------------------------------------------

    def append(
        self,
        *,
        event_id: str,
        device_id: str,
        device_time: datetime,
        kind: str,
        actor: str | None,
        payload: dict[str, Any],
        applied: bool = True,
    ) -> dict[str, Any]:
        if event_id in self._events:
            raise ArmoryError(f"事件已存在: {event_id}")
        device_time = _parse(device_time)
        head = self._heads.get(device_id)
        body = {
            "event_id": event_id,
            "device_id": device_id,
            "device_seq": (head["device_seq"] + 1) if head else 1,
            "device_time": _iso(device_time),
            "received_at": _iso(utcnow()),
            "kind": kind,
            "actor": actor,
            "payload": payload,
            "applied": applied,
            "prev_hash": head["hash"] if head else None,
        }
        body["hash"] = hashlib.sha256(_canonical(body)).hexdigest()
        self._events[event_id] = body
        self._heads[device_id] = body
        return body

    def alias(self, duplicate_id: str, original_id: str) -> None:
        """登记一次重复刷卡/重复提交，只关联原事件。"""
        if duplicate_id != original_id:
            self.aliases[duplicate_id] = original_id

    def get(self, event_id: str) -> dict[str, Any] | None:
        return self._events.get(event_id)

    def sorted_events(self, *, applied_only: bool = False) -> list[dict[str, Any]]:
        events = self._events.values()
        if applied_only:
            events = (e for e in events if e.get("applied", True))
        return sorted(
            events,
            key=lambda e: (e["device_time"], e["device_id"], e["device_seq"]),
        )

    def device_events(self, device_id: str) -> list[dict[str, Any]]:
        return sorted(
            (e for e in self._events.values() if e["device_id"] == device_id),
            key=lambda e: e["device_seq"],
        )

    # -- 完整性 ------------------------------------------------------------

    @staticmethod
    def recompute_hash(event: dict[str, Any]) -> str:
        body = {k: v for k, v in event.items() if k != "hash"}
        return hashlib.sha256(_canonical(body)).hexdigest()

    def verify(self) -> list[str]:
        """校验全部设备链。返回问题描述列表；空列表表示完好。"""
        problems: list[str] = []
        for device_id in {e["device_id"] for e in self._events.values()}:
            prev_hash: str | None = None
            for seq, event in enumerate(self.device_events(device_id), start=1):
                if event["device_seq"] != seq:
                    problems.append(f"{device_id}: 序号断裂于 {event['event_id']}")
                if event["prev_hash"] != prev_hash:
                    problems.append(f"{device_id}: 链断裂于 {event['event_id']}")
                if self.recompute_hash(event) != event["hash"]:
                    problems.append(f"{device_id}: 内容被篡改于 {event['event_id']}")
                prev_hash = event["hash"]
        return problems

    # -- 持久化 ------------------------------------------------------------

    def save(self) -> None:
        if not self.path:
            return
        data = {
            "events": self.sorted_events(),
            "aliases": self.aliases,
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def load(self, registry_loader: "Any" = None) -> None:
        if not self.path or not self.path.exists():
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self._events.clear()
        self._heads.clear()
        for event in data.get("events", []):
            self._events[event["event_id"]] = event
        # 按序号重建链头
        for device_id in {e["device_id"] for e in self._events.values()}:
            chain = self.device_events(device_id)
            if chain:
                self._heads[device_id] = chain[-1]
        self.aliases = dict(data.get("aliases", {}))
        problems = self.verify()
        if problems:
            raise ArmoryError("事件日志完整性校验失败: " + "; ".join(problems))
        if registry_loader is not None:
            registry_loader(data)

    def export_raw(self) -> dict[str, Any]:
        return {"events": self.sorted_events(), "aliases": self.aliases}


# ---------------------------------------------------------------------------
# 派生状态（事件溯源 fold）
# ---------------------------------------------------------------------------


@dataclass
class _State:
    # equipment_code -> 当前所在区（未被领用时）
    locations: dict[str, str] = field(default_factory=dict)
    # equipment_code -> Loan
    loans: dict[str, Loan] = field(default_factory=dict)
    # equipment_code -> "MISSING" 标记（经确认盘点）
    missing: set[str] = field(default_factory=set)
    # 最近一次合格入场刷卡：person_id|zone -> event
    last_entry: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    # 盘点批次：batch_id -> meta / reports
    batches: dict[str, dict[str, Any]] = field(default_factory=dict)
    # equipment_code -> 最新已确认盘点结论
    confirmed: dict[str, ConfirmedCount] = field(default_factory=dict)
    # 被隔离的离线记录
    quarantined: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 主服务
# ---------------------------------------------------------------------------


# 重复刷卡的判定窗口：同一人在同一闸门同方向 30 秒内再次刷卡视为重复。
DEFAULT_DEDUP_WINDOW = timedelta(seconds=30)
# 业务操作与入场刷卡的最大时间间隔。
DEFAULT_ACCESS_WINDOW = timedelta(minutes=240)


class Service:
    """军械库出入核验领域服务。"""

    def __init__(self, store_path: str | Path | None = None):
        self.equipment: dict[str, Equipment] = {}
        self.people: dict[str, Person] = {}
        self.store = EventStore(store_path)
        self._state = _State()
        self.ready = True
        if store_path:
            self.store.load(self._load_registry)
            self._rebuild()

    # ------------------------------------------------------------------
    # 主数据维护
    # ------------------------------------------------------------------

    def register_equipment(self, equipment: Equipment) -> None:
        self.equipment[equipment.code] = equipment

    def register_person(self, person: Person) -> None:
        self.people[person.person_id] = person

    def _load_registry(self, data: dict[str, Any]) -> None:  # pragma: no cover
        reg = data.get("registry")
        if not reg:
            return
        for kw in reg.get("equipment", []):
            self.register_equipment(Equipment(**kw))
        for p in reg.get("people", []):
            p = dict(p)
            p["qualifications"] = {k: _parse(v) for k, v in p["qualifications"].items()}
            p["authorized_zones"] = set(p["authorized_zones"])
            p["roles"] = set(p["roles"])
            self.register_person(Person(**p))

    def save(self) -> None:
        """持久化事件日志（按设备时间排序）与主数据。"""
        if not self.store.path:
            return
        data = self.store.export_raw()
        data["registry"] = {
            "equipment": [vars(e) for e in self.equipment.values()],
            "people": [
                {
                    **vars(p),
                    "qualifications": {k: _iso(v) for k, v in p.qualifications.items()},
                    "authorized_zones": sorted(p.authorized_zones),
                    "roles": sorted(p.roles),
                }
                for p in self.people.values()
            ],
        }
        tmp = self.store.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.store.path)

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    def _person(self, person_id: str) -> Person:
        person = self.people.get(person_id)
        if person is None:
            raise PolicyViolation([("UNKNOWN_PERSON", f"人员不存在: {person_id}")])
        return person

    def _equipment(self, code: str) -> Equipment:
        equipment = self.equipment.get(code)
        if equipment is None:
            raise PolicyViolation([("UNKNOWN_EQUIPMENT", f"装备不存在: {code}")])
        return equipment

    @staticmethod
    def _qualification_reasons(
        person: Person, qualification: str | None, zone: str, at: datetime, *, prefix: str = ""
    ) -> list[tuple[str, str]]:
        reasons: list[tuple[str, str]] = []
        if qualification:
            expiry = person.qualifications.get(qualification)
            label = prefix + "QUALIFICATION_EXPIRED" if prefix else "QUALIFICATION_EXPIRED"
            missing = prefix + "NOT_QUALIFIED" if prefix else "NOT_QUALIFIED"
            if expiry is None:
                reasons.append((missing, f"{person.person_id} 缺少资质 {qualification}"))
            elif expiry < at:
                reasons.append((label, f"{person.person_id} 的资质 {qualification} 已于 {_iso(expiry)} 失效"))
        zone_code = (prefix + "ZONE_NOT_AUTHORIZED") if prefix else "ZONE_NOT_AUTHORIZED"
        if zone not in person.authorized_zones:
            reasons.append((zone_code, f"{person.person_id} 未获授权进入 {zone}"))
        return reasons

    def _second_review_reasons(
        self,
        operator: Person,
        second_id: str | None,
        qualification: str | None,
        zone: str,
        at: datetime,
        *,
        required: bool,
    ) -> list[tuple[str, str]]:
        if not required:
            return []
        if not second_id:
            return [("MISSING_SECOND_REVIEWER", "该装备/差异需双人复核，缺少第二名复核人")]
        if second_id == operator.person_id:
            return [("SECOND_REVIEWER_SAME_PERSON", "第二名复核人不得与经办人相同")]
        second = self.people.get(second_id)
        if second is None:
            return [("SECOND_NOT_QUALIFIED", f"复核人不存在: {second_id}")]
        return self._qualification_reasons(
            second, qualification, zone, at, prefix="SECOND_"
        )

    def _check_access(
        self, person: Person, zone: str, at: datetime, access_event_id: str | None
    ) -> list[tuple[str, str]]:
        if not access_event_id:
            return [("ACCESS_REQUIRED", "业务操作必须关联一次授权入场刷卡")]
        event = self.store.get(access_event_id)
        if event is None or event["kind"] != "SWIPE":
            return [("ACCESS_REQUIRED", "关联的门禁事件不存在")]
        if not event.get("applied", True) or event["payload"].get("result") != "GRANTED":
            return [("ACCESS_DENIED", "关联的刷卡未获授权")]
        if event["actor"] != person.person_id:
            return [("ACCESS_DENIED", "入场刷卡人与经办人不一致")]
        if event["payload"].get("zone") != zone or event["payload"].get("direction") != "IN":
            return [("ACCESS_DENIED", "入场区域/方向与业务操作不匹配")]
        if at < _parse(event["device_time"]) or at - _parse(event["device_time"]) > DEFAULT_ACCESS_WINDOW:
            return [("ACCESS_OUTSIDE_WINDOW", "业务操作与入场刷卡时间超出允许间隔")]
        return []

    def _has_overdue(self, person_id: str, at: datetime) -> bool:
        return any(
            loan.person_id == person_id and at > loan.due_at
            for loan in self._state.loans.values()
        )

    # ------------------------------------------------------------------
    # 门禁刷卡
    # ------------------------------------------------------------------

    def record_swipe(
        self,
        *,
        event_id: str,
        device_id: str,
        person_id: str,
        gate: str,
        zone: str,
        direction: str,
        device_time: datetime | str,
    ) -> dict[str, Any]:
        """登记一次门禁刷卡。

        重复刷卡（同 event_id，或同人同闸同方向 30 秒内）只关联原事件，返回原
        事件而不新增任何记录。
        """
        device_time = _parse(device_time)

        existing = self.store.get(event_id)
        if existing is not None:
            self.store.alias(event_id, existing["event_id"])
            return existing

        for event in self.store.sorted_events():
            if event["kind"] != "SWIPE":
                continue
            if (
                event["actor"] == person_id
                and event["payload"].get("gate") == gate
                and event["payload"].get("direction") == direction
                and abs(_parse(event["device_time"]) - device_time) <= DEFAULT_DEDUP_WINDOW
            ):
                self.store.alias(event_id, event["event_id"])
                return event

        person = self.people.get(person_id)
        result = "GRANTED"
        reasons: list[tuple[str, str]] = []
        if person is None:
            result, reasons = "DENIED", [("UNKNOWN_PERSON", f"人员不存在: {person_id}")]
        elif zone not in person.authorized_zones:
            result = "DENIED"
            reasons = [("ZONE_NOT_AUTHORIZED", f"{person_id} 未获授权进入 {zone}")]

        return self.store.append(
            event_id=event_id,
            device_id=device_id,
            device_time=device_time,
            kind="SWIPE",
            actor=person_id,
            payload={
                "gate": gate,
                "zone": zone,
                "direction": direction,
                "result": result,
                "reasons": [{"code": c, "detail": d} for c, d in reasons],
            },
        )

    # ------------------------------------------------------------------
    # 事件提交管线（实时 / 离线共用）
    # ------------------------------------------------------------------

    def _submit(
        self,
        *,
        event_id: str,
        device_id: str,
        device_time: datetime | str,
        kind: str,
        actor: str | None,
        payload: dict[str, Any],
        reasons: list[tuple[str, str]],
        offline: bool,
        equipment_code: str | None = None,
    ) -> dict[str, Any]:
        device_time = _parse(device_time)

        # 幂等：重复提交只关联原事件。
        existing = self.store.get(event_id)
        if existing is not None:
            self.store.alias(event_id, existing["event_id"])
            payload["_receipt"] = "LINKED_EXISTING"
            return existing

        # 离线补传：与已确认盘点冲突 -> 隔离，绝不覆盖。
        backfill_reasons: list[tuple[str, str]] = []
        if offline:
            backfill_reasons = self._backfill_conflicts(kind, equipment_code, device_time, payload)

        all_reasons = reasons + backfill_reasons
        if all_reasons:
            if offline:
                event = self.store.append(
                    event_id=event_id,
                    device_id=device_id,
                    device_time=device_time,
                    kind=kind,
                    actor=actor,
                    payload={**payload, "quarantine_reasons": [
                        {"code": c, "detail": d} for c, d in all_reasons
                    ]},
                    applied=False,
                )
                self._state.quarantined.append(event)
                self._rebuild()
                return event
            # 实时操作：先留证，再阻止。
            denial_payload = {
                "attempted_event_id": event_id,
                "attempted_kind": kind,
                "reasons": [{"code": c, "detail": d} for c, d in all_reasons],
            }
            if equipment_code:
                denial_payload["equipment_code"] = equipment_code
            self.store.append(
                event_id=event_id + ":denial",
                device_id=device_id,
                device_time=device_time,
                kind="POLICY_DENIAL",
                actor=actor,
                payload=denial_payload,
                applied=True,
            )
            raise PolicyViolation(all_reasons)

        event = self.store.append(
            event_id=event_id,
            device_id=device_id,
            device_time=device_time,
            kind=kind,
            actor=actor,
            payload=payload,
        )
        self._rebuild()
        return event

    def _backfill_conflicts(
        self, kind: str, equipment_code: str | None, device_time: datetime, payload: dict[str, Any]
    ) -> list[tuple[str, str]]:
        # 盘点类记录指向已确认批次 -> 一律隔离。
        batch_id = payload.get("batch_id")
        if batch_id and batch_id in self._state.batches:
            batch = self._state.batches[batch_id]
            if batch.get("confirmed"):
                return [(
                    "INVENTORY_ALREADY_CONFIRMED",
                    f"批次 {batch_id} 已确认，离线补传不得修改盘点结果",
                )]

        if kind not in {"ISSUE", "RETURN", "OVERDUE_RETURN", "TRANSFER"}:
            return []
        code = equipment_code or payload.get("equipment_code")
        confirmed = self._state.confirmed.get(code) if code else None
        if confirmed and device_time <= confirmed.cutoff:
            return [(
                "INVENTORY_ALREADY_CONFIRMED",
                f"{code} 的盘点已于 {_iso(confirmed.cutoff)} 确认，"
                f"设备时间 {_iso(device_time)} 的 {kind} 记录与该结论冲突，已隔离",
            )]
        return []

    # ------------------------------------------------------------------
    # 领用 / 归还 / 移库
    # ------------------------------------------------------------------

    def issue_equipment(
        self,
        *,
        event_id: str,
        device_id: str,
        equipment_code: str,
        person_id: str,
        second_person_id: str | None = None,
        access_event_id: str,
        device_time: datetime | str,
        offline: bool = False,
    ) -> dict[str, Any]:
        """领用装备。"""
        at = _parse(device_time)
        equipment = self._equipment(equipment_code)
        person = self._person(person_id)
        zone = self._state.locations.get(equipment.code, equipment.zone)

        reasons = self._qualification_reasons(person, equipment.required_qualification, zone, at)
        reasons += self._check_access(person, zone, at, access_event_id)
        reasons += self._second_review_reasons(
            person, second_person_id, equipment.required_qualification, zone, at,
            required=equipment.dual_control,
        )
        if equipment_code in self._state.loans:
            reasons.append(("EQUIPMENT_NOT_AVAILABLE", f"{equipment_code} 已被领用，尚未归还"))
        if equipment_code in self._state.missing:
            reasons.append(("EQUIPMENT_MISSING", f"{equipment_code} 经盘点确认缺失，禁止领用"))
        if self._has_overdue(person_id, at):
            reasons.append(("OVERDUE_LOAN_EXISTS", f"{person_id} 存在逾期未还装备，禁止继续领用"))

        due_at = at + timedelta(minutes=equipment.loan_minutes)
        return self._submit(
            event_id=event_id,
            device_id=device_id,
            device_time=at,
            kind="ISSUE",
            actor=person_id,
            payload={
                "equipment_code": equipment_code,
                "second_person_id": second_person_id,
                "access_event_id": access_event_id,
                "zone": zone,
                "due_at": _iso(due_at),
            },
            reasons=reasons,
            offline=offline,
            equipment_code=equipment_code,
        )

    def return_equipment(
        self,
        *,
        event_id: str,
        device_id: str,
        equipment_code: str,
        person_id: str,
        access_event_id: str,
        device_time: datetime | str,
        offline: bool = False,
    ) -> dict[str, Any]:
        """归还装备。超过归还时限的，走正常通道会被阻止，须走异常通道。"""
        at = _parse(device_time)
        equipment = self._equipment(equipment_code)
        person = self._person(person_id)
        zone = self._state.locations.get(equipment.code, equipment.zone)

        reasons: list[tuple[str, str]] = []
        loan = self._state.loans.get(equipment_code)
        if loan is None:
            reasons.append(("NOT_ON_LOAN", f"{equipment_code} 当前不在领用状态"))
        elif loan.person_id != person_id:
            reasons.append(("NOT_HELD_BY_PERSON",
                            f"{equipment_code} 由 {loan.person_id} 领用，不能由 {person_id} 归还"))
        reasons += self._qualification_reasons(person, equipment.required_qualification, zone, at)
        reasons += self._check_access(person, zone, at, access_event_id)
        if loan is not None and at > loan.due_at:
            reasons.append((
                "OVERDUE_RETURN",
                f"{equipment_code} 应于 {_iso(loan.due_at)} 前归还，已超时；"
                "普通归还通道关闭，须由主官与第二名复核人走异常归还",
            ))

        payload = {
            "equipment_code": equipment_code,
            "access_event_id": access_event_id,
            "zone": zone,
            "issued_at": _iso(loan.issued_at) if loan else None,
            "due_at": _iso(loan.due_at) if loan else None,
        }
        return self._submit(
            event_id=event_id,
            device_id=device_id,
            device_time=at,
            kind="RETURN",
            actor=person_id,
            payload=payload,
            reasons=reasons,
            offline=offline,
            equipment_code=equipment_code,
        )

    def receive_overdue_return(
        self,
        *,
        event_id: str,
        device_id: str,
        equipment_code: str,
        person_id: str,
        supervisor_id: str,
        second_person_id: str,
        reason: str,
        device_time: datetime | str,
        offline: bool = False,
    ) -> dict[str, Any]:
        """超时归还的异常通道：主官 + 第二名合格复核人共同签收并留痕。"""
        at = _parse(device_time)
        equipment = self._equipment(equipment_code)
        zone = self._state.locations.get(equipment.code, equipment.zone)

        reasons: list[tuple[str, str]] = []
        loan = self._state.loans.get(equipment_code)
        if loan is None:
            reasons.append(("NOT_ON_LOAN", f"{equipment_code} 当前不在领用状态"))
        elif loan.person_id != person_id:
            reasons.append(("NOT_HELD_BY_PERSON", f"{equipment_code} 非 {person_id} 领用"))
        elif at <= loan.due_at:
            reasons.append(("NOT_OVERDUE", "该装备尚未逾期，应走普通归还通道"))

        supervisor = self.people.get(supervisor_id)
        if supervisor is None or "armory_supervisor" not in supervisor.roles:
            reasons.append(("SUPERVISOR_REQUIRED", "超时归还须由军械主官受理"))
        elif zone not in supervisor.authorized_zones:
            reasons.append(("SUPERVISOR_ZONE_NOT_AUTHORIZED", f"主官未获授权进入 {zone}"))

        # 第二名复核人：与领用人、主官均不同，且具备装备资质与区域授权。
        if not second_person_id:
            reasons.append(("MISSING_SECOND_REVIEWER", "超时归还缺少第二名复核人"))
        elif len({person_id, supervisor_id, second_person_id}) < 3:
            reasons.append(("SECOND_REVIEWER_SAME_PERSON", "领用人、主官、复核人必须为三人"))
        else:
            second = self.people.get(second_person_id)
            if second is None:
                reasons.append(("SECOND_NOT_QUALIFIED", f"复核人不存在: {second_person_id}"))
            else:
                reasons += self._qualification_reasons(
                    second, equipment.required_qualification, zone, at, prefix="SECOND_"
                )

        payload = {
            "equipment_code": equipment_code,
            "person_id": person_id,
            "supervisor_id": supervisor_id,
            "second_person_id": second_person_id,
            "reason": reason,
            "zone": zone,
            "issued_at": _iso(loan.issued_at) if loan else None,
            "due_at": _iso(loan.due_at) if loan else None,
            "late_seconds": int((at - loan.due_at).total_seconds()) if loan and at > loan.due_at else 0,
        }
        return self._submit(
            event_id=event_id,
            device_id=device_id,
            device_time=at,
            kind="OVERDUE_RETURN",
            actor=supervisor_id,
            payload=payload,
            reasons=reasons,
            offline=offline,
            equipment_code=equipment_code,
        )

    def transfer_equipment(
        self,
        *,
        event_id: str,
        device_id: str,
        equipment_code: str,
        to_zone: str,
        person_id: str,
        second_person_id: str | None = None,
        device_time: datetime | str,
        offline: bool = False,
    ) -> dict[str, Any]:
        """移库：把装备从当前存放区调往另一存放区。"""
        at = _parse(device_time)
        equipment = self._equipment(equipment_code)
        person = self._person(person_id)
        from_zone = self._state.locations.get(equipment.code, equipment.zone)

        reasons: list[tuple[str, str]] = []
        if equipment_code in self._state.loans:
            reasons.append(("EQUIPMENT_NOT_AVAILABLE", "装备处于领用状态，不能移库"))
        if equipment_code in self._state.missing:
            reasons.append(("EQUIPMENT_MISSING", "装备经盘点确认缺失，不能移库"))
        if from_zone == to_zone:
            reasons.append(("TRANSFER_SAME_ZONE", "目标存放区与当前存放区相同"))
        reasons += self._qualification_reasons(person, equipment.required_qualification, from_zone, at)
        if to_zone not in person.authorized_zones:
            reasons.append(("ZONE_NOT_AUTHORIZED", f"{person_id} 未获授权进入目标区 {to_zone}"))
        reasons += self._second_review_reasons(
            person, second_person_id, equipment.required_qualification, from_zone, at,
            required=equipment.dual_control,
        )

        return self._submit(
            event_id=event_id,
            device_id=device_id,
            device_time=at,
            kind="TRANSFER",
            actor=person_id,
            payload={
                "equipment_code": equipment_code,
                "from_zone": from_zone,
                "to_zone": to_zone,
                "second_person_id": second_person_id,
            },
            reasons=reasons,
            offline=offline,
            equipment_code=equipment_code,
        )

    # ------------------------------------------------------------------
    # 盘点
    # ------------------------------------------------------------------

    def begin_inventory(
        self,
        *,
        batch_id: str,
        event_id: str,
        device_id: str,
        zone: str,
        counter_id: str,
        device_time: datetime | str,
    ) -> dict[str, Any]:
        """开始一次盘点。"""
        at = _parse(device_time)
        counter = self._person(counter_id)
        reasons = self._qualification_reasons(counter, None, zone, at)
        if batch_id in self._state.batches:
            reasons.append(("BATCH_EXISTS", f"盘点批次已存在: {batch_id}"))

        return self._submit(
            event_id=event_id,
            device_id=device_id,
            device_time=at,
            kind="INVENTORY_BEGIN",
            actor=counter_id,
            payload={"batch_id": batch_id, "zone": zone},
            reasons=reasons,
            offline=False,
        )

    def report_count(
        self,
        *,
        batch_id: str,
        event_id: str,
        device_id: str,
        equipment_code: str,
        seen: bool,
        seen_zone: str | None = None,
        counter_id: str,
        device_time: datetime | str,
    ) -> dict[str, Any]:
        """上报一件装备的盘点情况。每件装备一条事件，便于按装备审计。"""
        at = _parse(device_time)
        batch = self._state.batches.get(batch_id)
        reasons: list[tuple[str, str]] = []
        if batch is None:
            reasons.append(("BATCH_NOT_FOUND", f"盘点批次不存在: {batch_id}"))
            zone = seen_zone or ""
        else:
            zone = batch["zone"]
            if batch.get("confirmed"):
                reasons.append(("INVENTORY_ALREADY_CONFIRMED", f"批次 {batch_id} 已确认，禁止改动"))
        counter = self._person(counter_id)
        reasons += self._qualification_reasons(counter, None, zone, at)
        equipment = self.equipment.get(equipment_code)
        if equipment is None:
            reasons.append(("UNKNOWN_EQUIPMENT", f"装备不存在: {equipment_code}"))
        elif seen and seen_zone is None:
            reasons.append(("SEEN_ZONE_REQUIRED", "清点到装备时必须上报所在区"))

        return self._submit(
            event_id=event_id,
            device_id=device_id,
            device_time=at,
            kind="INVENTORY_COUNT",
            actor=counter_id,
            payload={
                "batch_id": batch_id,
                "zone": zone,
                "equipment_code": equipment_code,
                "seen": seen,
                "seen_zone": seen_zone,
            },
            reasons=reasons,
            offline=False,
            equipment_code=equipment_code,
        )

    def confirm_inventory(
        self,
        *,
        batch_id: str,
        event_id: str,
        device_id: str,
        confirmer_id: str,
        second_person_id: str,
        device_time: datetime | str,
    ) -> dict[str, Any]:
        """确认盘点结果并固化差异。

        * 必须两人共同确认且均获该区域授权；
        * 差异涉及双人双控装备时，两名确认人都须持有该装备资质；
        * 确认后写入受保护结论，后续离线补传只能隔离、不能覆盖。
        """
        at = _parse(device_time)
        batch = self._state.batches.get(batch_id)
        reasons: list[tuple[str, str]] = []
        if batch is None:
            reasons.append(("BATCH_NOT_FOUND", f"盘点批次不存在: {batch_id}"))
            batch = {"zone": "", "reports": {}}
        elif batch.get("confirmed"):
            reasons.append(("INVENTORY_ALREADY_CONFIRMED", f"批次 {batch_id} 已确认"))

        zone = batch["zone"]
        confirmer = self.people.get(confirmer_id)
        if confirmer is None:
            reasons.append(("UNKNOWN_PERSON", f"确认人不存在: {confirmer_id}"))
        else:
            reasons += self._qualification_reasons(confirmer, None, zone, at)
        second = self.people.get(second_person_id)
        if not second_person_id:
            reasons.append(("MISSING_SECOND_REVIEWER", "盘点确认缺少第二名复核人"))
        elif second_person_id == confirmer_id:
            reasons.append(("SECOND_REVIEWER_SAME_PERSON", "两名确认人不得相同"))
        elif second is None:
            reasons.append(("SECOND_NOT_QUALIFIED", f"复核人不存在: {second_person_id}"))
        else:
            reasons += self._qualification_reasons(second, None, zone, at, prefix="SECOND_")

        # 计算差异。
        results = self._inventory_results(batch)
        for finding in results:
            if finding["finding"] != "PRESENT":
                equipment = self.equipment[finding["equipment_code"]]
                if equipment.dual_control:
                    for person, prefix in ((confirmer, ""), (second, "SECOND_")):
                        if person is not None and not person.qualified(
                            equipment.required_qualification, at
                        ):
                            reasons.append((
                                (prefix + "NOT_QUALIFIED") if prefix else "NOT_QUALIFIED",
                                f"双控装备 {equipment.code} 的差异确认要求 {person.person_id} "
                                f"持有效资质 {equipment.required_qualification}",
                            ))

        cutoff = at
        return self._submit(
            event_id=event_id,
            device_id=device_id,
            device_time=at,
            kind="INVENTORY_CONFIRM",
            actor=confirmer_id,
            payload={
                "batch_id": batch_id,
                "zone": zone,
                "second_person_id": second_person_id,
                "cutoff": _iso(cutoff),
                "results": results,
            },
            reasons=reasons,
            offline=False,
        )

    def _inventory_results(self, batch: dict[str, Any]) -> list[dict[str, Any]]:
        zone = batch["zone"]
        reports: dict[str, dict[str, Any]] = batch.get("reports", {})
        results: list[dict[str, Any]] = []
        for code, equipment in self.equipment.items():
            home = self._state.locations.get(code, equipment.zone)
            loan = self._state.loans.get(code)
            report = reports.get(code)
            if home != zone and loan is None and code not in self._state.missing:
                continue  # 不在本区、也未借出，不属于本次盘点范围
            if report is None:
                if loan is not None:
                    continue  # 已借出，不要求清点
                results.append({"equipment_code": code, "finding": "MISSING",
                                "expected": "PRESENT", "zone": zone})
                continue
            if loan is not None:
                if report["seen"]:
                    results.append({"equipment_code": code, "finding": "UNEXPECTED_PRESENT",
                                    "expected": "ON_LOAN", "zone": zone})
                else:
                    results.append({"equipment_code": code, "finding": "ON_LOAN_CONFIRMED",
                                    "expected": "ON_LOAN", "zone": zone})
            elif not report["seen"]:
                results.append({"equipment_code": code, "finding": "MISSING",
                                "expected": "PRESENT", "zone": zone})
            elif report["seen_zone"] != zone:
                results.append({"equipment_code": code, "finding": "LOCATION_MISMATCH",
                                "expected": zone, "actual": report["seen_zone"], "zone": zone})
            else:
                results.append({"equipment_code": code, "finding": "PRESENT",
                                "expected": "PRESENT", "zone": zone})
        return results

    # ------------------------------------------------------------------
    # 离线补传
    # ------------------------------------------------------------------

    def ingest_offline(
        self,
        *,
        device_id: str,
        records: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """批量补传一台设备离线期间缓存的记录。

        :param records: 操作描述列表，每项形如
            ``{"op": "issue", "event_id": ..., "device_time": ..., 业务参数...}``。
            支持 swipe / issue / return / overdue_return / transfer。盘点类记录
            不允许补传（盘点必须当场确认）。
        :returns: 每条记录对应的事件；被隔离事件 ``applied=False`` 且带
            ``quarantine_reasons``；重复记录为 ``LINKED_EXISTING``。
        """
        dispatch = {
            "swipe": lambda kw: self.record_swipe(device_id=device_id, **kw),
            "issue": lambda kw: self.issue_equipment(device_id=device_id, offline=True, **kw),
            "return": lambda kw: self.return_equipment(device_id=device_id, offline=True, **kw),
            "overdue_return": lambda kw: self.receive_overdue_return(
                device_id=device_id, offline=True, **kw
            ),
            "transfer": lambda kw: self.transfer_equipment(device_id=device_id, offline=True, **kw),
        }
        records = sorted(records, key=lambda r: _parse(r["device_time"]))
        outcomes: list[dict[str, Any]] = []
        for record in records:
            op = record["op"]
            if op not in dispatch:
                raise ArmoryError(f"离线记录类型不支持补传: {op}")
            kwargs = {k: v for k, v in record.items() if k != "op"}
            outcomes.append(dispatch[op](kwargs))
        return outcomes

    @property
    def quarantined(self) -> list[dict[str, Any]]:
        return list(self._state.quarantined)

    # ------------------------------------------------------------------
    # 状态重建（fold）
    # ------------------------------------------------------------------

    def _rebuild(self) -> None:
        state = _State()
        for equipment in self.equipment.values():
            state.locations[equipment.code] = equipment.zone

        for event in self.store.sorted_events(applied_only=True):
            kind = event["kind"]
            payload = event["payload"]
            at = _parse(event["device_time"])

            if kind == "SWIPE" and payload.get("result") == "GRANTED" and payload.get("direction") == "IN":
                state.last_entry[(event["actor"], payload["zone"])] = event

            elif kind == "ISSUE":
                code = payload["equipment_code"]
                state.loans[code] = Loan(
                    person_id=event["actor"],
                    issued_at=at,
                    due_at=_parse(payload["due_at"]),
                    issue_event_id=event["event_id"],
                )

            elif kind in {"RETURN", "OVERDUE_RETURN"}:
                state.loans.pop(payload["equipment_code"], None)

            elif kind == "TRANSFER":
                state.locations[payload["equipment_code"]] = payload["to_zone"]

            elif kind == "INVENTORY_BEGIN":
                state.batches[payload["batch_id"]] = {
                    "zone": payload["zone"], "counter": event["actor"],
                    "reports": {}, "confirmed": False,
                }

            elif kind == "INVENTORY_COUNT":
                batch = state.batches.get(payload["batch_id"])
                if batch is not None:
                    batch["reports"][payload["equipment_code"]] = {
                        "seen": payload["seen"],
                        "seen_zone": payload["seen_zone"],
                        "at": at,
                    }

            elif kind == "INVENTORY_CONFIRM":
                batch = state.batches.get(payload["batch_id"])
                cutoff = _parse(payload["cutoff"])
                if batch is not None:
                    batch["confirmed"] = True
                    batch["confirmed_at"] = at
                for result in payload["results"]:
                    state.confirmed[result["equipment_code"]] = ConfirmedCount(
                        batch_id=payload["batch_id"],
                        equipment_code=result["equipment_code"],
                        finding=result["finding"],
                        zone=result["zone"],
                        cutoff=cutoff,
                        counter=batch["counter"] if batch else event["actor"],
                        confirmer=event["actor"],
                        second_person=payload["second_person_id"],
                    )
                    if result["finding"] == "MISSING":
                        state.missing.add(result["equipment_code"])
                    elif result["finding"] == "PRESENT":
                        state.missing.discard(result["equipment_code"])

        # 隔离记录（applied=False）单独汇集。
        state.quarantined = [
            e for e in self.store.sorted_events() if not e.get("applied", True)
        ]
        self._state = state

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def status(self, equipment_code: str) -> dict[str, Any]:
        equipment = self._equipment(equipment_code)
        loan = self._state.loans.get(equipment_code)
        confirmed = self._state.confirmed.get(equipment_code)
        return {
            "equipment_code": equipment_code,
            "zone": self._state.locations.get(equipment_code, equipment.zone),
            "state": ("MISSING" if equipment_code in self._state.missing
                      else "ON_LOAN" if loan else "IN_STORE"),
            "holder": loan.person_id if loan else None,
            "due_at": _iso(loan.due_at) if loan else None,
            "last_confirmed_count": (
                {
                    "batch_id": confirmed.batch_id,
                    "finding": confirmed.finding,
                    "cutoff": _iso(confirmed.cutoff),
                }
                if confirmed
                else None
            ),
        }

    @staticmethod
    def _mentions(event: dict[str, Any]) -> set[str]:
        """提取事件涉及的全部人员（含复核人、主官等）。"""
        payload = event["payload"]
        people = {event["actor"]} if event["actor"] else set()
        for key in ("person_id", "second_person_id", "supervisor_id"):
            if payload.get(key):
                people.add(payload[key])
        return people

    def timeline_by_equipment(self, equipment_code: str) -> list[dict[str, Any]]:
        self._equipment(equipment_code)
        result = []
        linked_ids: set[str] = set()
        for event in self.store.sorted_events():
            payload = event["payload"]
            matched = False
            if payload.get("equipment_code") == equipment_code:
                matched = True
            elif event["kind"] == "INVENTORY_CONFIRM" and any(
                r["equipment_code"] == equipment_code for r in payload.get("results", [])
            ):
                matched = True
            if matched:
                result.append(event)
                if payload.get("access_event_id"):
                    linked_ids.add(payload["access_event_id"])
        # 补入业务事件关联的授权入场刷卡，时间线仍保持设备时间有序。
        for event_id in linked_ids:
            event = self.store.get(event_id)
            if event is not None and event not in result:
                result.append(event)
        return sorted(result, key=lambda e: (e["device_time"], e["device_id"], e["device_seq"]))

    def timeline_by_person(self, person_id: str) -> list[dict[str, Any]]:
        self._person(person_id)
        result = []
        for event in self.store.sorted_events():
            if person_id in self._mentions(event):
                result.append(event)
        return result

    def verify_timeline(self, events: Iterable[dict[str, Any]] | None = None) -> dict[str, Any]:
        """校验时间线完整性；缺省校验整库所有设备链。"""
        problems = self.store.verify()
        return {"ok": not problems, "problems": problems}

    # ------------------------------------------------------------------
    # 导出（最小化脱敏）
    # ------------------------------------------------------------------

    def export_timeline(
        self,
        *,
        focus: str,
        focus_id: str,
        requester_id: str,
    ) -> dict[str, Any]:
        """按核查对象导出时间线，并隐藏无关身份信息。

        * ``focus="equipment"``：导出该装备全链路；经办相关人员保留姓名（属业务
          必要信息），证件号默认掩码。
        * ``focus="person"``：仅核查对象保留姓名；其他偶然出现在事件里的人员
          （复核人、主官等）一律匿名化。
        * 仅 ``security_audit`` 角色的请求方可看到证件号明文。
        """
        if focus == "equipment":
            events = self.timeline_by_equipment(focus_id)
            scope_label = f"装备 {focus_id}"
        elif focus == "person":
            events = self.timeline_by_person(focus_id)
            scope_label = f"人员 {focus_id}"
        else:
            raise ArmoryError("focus 必须是 equipment 或 person")

        requester = self.people.get(requester_id)
        show_id_number = bool(requester and "security_audit" in requester.roles)

        redacted = [
            self._redact_event(e, focus, focus_id, show_id_number) for e in events
        ]
        integrity = self.verify_timeline()
        return {
            "scope": focus,
            "scope_id": focus_id,
            "scope_label": scope_label,
            "generated_at": _iso(utcnow()),
            "integrity": integrity,
            "events": redacted,
            "redaction_notice": "与本次核查无关的身份信息已隐去"
            if not show_id_number
            else "审计权限：身份信息明文导出已记录",
        }

    def _redact_event(
        self, event: dict[str, Any], focus: str, focus_id: str, show_id_number: bool
    ) -> dict[str, Any]:
        payload = dict(event["payload"])
        person_refs = {
            "actor": event["actor"],
            **{
                k: payload.get(k)
                for k in ("person_id", "second_person_id", "supervisor_id")
                if payload.get(k)
            },
        }
        masked_people = {}
        for role_key, person_id in person_refs.items():
            if person_id is None:
                continue
            person = self.people.get(person_id)
            # person 焦点：只有核查对象保留身份；equipment 焦点：参与者均属业务相关。
            relevant = focus == "equipment" or person_id == focus_id
            id_number = person.id_number if person else ""
            masked_people[role_key] = {
                "person_id": person_id if relevant else self._mask_id(person_id),
                "name": (person.name if person else "未知") if relevant else "（非核查对象，已隐去）",
                "id_number": (
                    id_number if show_id_number and relevant else self._mask_id_number(id_number)
                ),
            }

        reasons = payload.get("reasons")
        if isinstance(reasons, list):
            payload["reasons"] = [
                {"code": r.get("code"), "detail": self._redact_detail(r.get("detail", ""), focus, focus_id)}
                if isinstance(r, dict)
                else r
                for r in reasons
            ]
        if "quarantine_reasons" in payload:
            payload["quarantine_reasons"] = [
                {"code": r.get("code"), "detail": self._redact_detail(r.get("detail", ""), focus, focus_id)}
                for r in payload["quarantine_reasons"]
            ]

        return {
            "event_id": event["event_id"],
            "device_id": event["device_id"],
            "device_seq": event["device_seq"],
            "device_time": event["device_time"],
            "kind": event["kind"],
            "actor": masked_people.get("actor", event["actor"]),
            "payload": payload,
            "people": masked_people,
            "applied": event.get("applied", True),
            "hash": event["hash"],
        }

    def _redact_detail(self, detail: str, focus: str, focus_id: str) -> str:
        """原因文本里可能带出他人姓名/编号，统一按人员主数据替换为编号。"""
        if focus != "person":
            return detail
        for person in self.people.values():
            if person.person_id == focus_id:
                continue
            detail = detail.replace(person.name, "（非核查对象）")
            if person.id_number:
                detail = detail.replace(person.id_number, self._mask_id_number(person.id_number))
        return detail

    @staticmethod
    def _mask_id_number(value: str) -> str:
        if not value:
            return ""
        return value[:1] + "*" * (len(value) - 2) + value[-1:] if len(value) > 2 else "**"

    @staticmethod
    def _mask_id(value: str) -> str:
        return value[:2] + "***" if len(value) > 2 else "***"
