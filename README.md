# 军械库出入核验

服务于国防安全业务的军械库出入核验服务：维护装备编码、存放区、人员资质
与双人复核要求，按授权范围处理领用、归还、移库和盘点差异；失效资质、
超时归还或缺少第二名复核人时阻止操作并给出原因码；门禁与业务事件按
设备时间进入哈希链审计时间线，重复刷卡只关联原事件，离线补传不得覆盖
已确认的盘点结果。

运行环境：Python 3.11，仅标准库。代码位于 `src` 目录。

## 设计要点

- **事件溯源 + 哈希链**：所有门禁与业务操作仅追加写入 JSONL 审计日志，
  每条记录含前一条记录的 SHA-256 哈希；`verify_integrity()` 可发现任何
  事后篡改，重启时先校验链再回放重建状态。
- **按设备时间排序**：事件按自身携带的 `device_time` 排序入时间线（乱序
  到达、离线补传都能正确归位）；哈希链顺序是接收顺序，两者分离，补传
  不会顶掉旧事件。
- **重复刷卡去重**：设备流水号相同（或无流水号时同人/同设备/同闸口/同
  方向落在 60 秒去重窗内）的刷卡不生成新事件，只产生一条
  `SWIPE_DUPLICATE` 关联到原事件。
- **盘点冻结**：盘点确认须双人，确认后该存放区确认时点之前的状态冻结；
  迟到或离线补传的领用/归还/移库一律隔离为 `BACKFILL_REJECTED`
  （`quarantined=true`），盘点快照与差异结论不变。
- **差异定性闭环**：`MISSING / MISPLACED / UNKNOWN_ITEM` 三类差异，
  确认后可双人定性为"临时借用未归还（UNRETURNED_LOAN）""登记遗漏
  （REGISTRY_OMISSION）"或其他，差异证据自动汇总盘点窗口内的门禁记录
  与被隔离的补传记录。
- **身份最小暴露**：审计导出默认隐藏无关人员姓名、仅给脱敏编号；被
  核查人本人或显式 `reveal` 的相关人员可显示姓名，证件号始终只留末 4 位。

## 阻止原因码

| reason | 含义 |
| --- | --- |
| `PERSON_UNKNOWN` / `EQUIPMENT_UNKNOWN` | 人员或装备不在台账 |
| `QUALIFICATION_MISSING` / `QUALIFICATION_EXPIRED` | 缺少资质 / 资质已失效 |
| `ZONE_NOT_AUTHORIZED` / `CATEGORY_NOT_AUTHORIZED` | 超出存放区或装备类别授权范围 |
| `SECOND_REVIEWER_REQUIRED` | 双控装备操作缺少第二名复核人 |
| `REVIEWER_SAME_AS_ACTOR` | 复核人与操作人为同一人 |
| `SUPERVISOR_REQUIRED` | 逾期例外归还缺少值班主管授权 |
| `OVERDUE_RETURN` / `OUTSTANDING_OVERDUE_LOAN` | 超时归还 / 有逾期未闭环不能再领用 |
| `ITEM_STATE_CONFLICT` / `HOLDER_MISMATCH` | 装备状态冲突 / 非持用人归还 |
| `SWIPE_INVALID` | 业务操作关联的门禁流水不存在、未授权或人闸不符 |
| `SESSION_NOT_OPEN` / `SESSION_ALREADY_CONFIRMED` | 盘点任务不存在 / 已确认不可覆盖 |
| `INVENTORY_CONFIRMED_PROTECTED` | 记录试图改写已确认盘点，已隔离 |

## 最小用法

```python
from datetime import datetime, timedelta, timezone
from src.service import Service, OperationRejected

svc = Service(store_path="audit.jsonl")  # 不传路径则纯内存

svc.register_person(
    "P001", "张三", "110101199001011234",
    qualifications=[("CAT:步枪", datetime(2027, 1, 1, tzinfo=timezone.utc))],
    authorized_zones={"A区"}, authorized_categories={"步枪"},
)
svc.register_person(
    "P002", "李四", "…",
    qualifications=[("CAT:步枪", datetime(2027, 1, 1, tzinfo=timezone.utc))],
    authorized_zones={"A区"}, authorized_categories={"步枪"},
)
svc.register_equipment("E001", "步枪", "A区",
                       required_qualification="CAT:步枪", dual_review_required=True)

# 门禁刷卡（重复刷卡/离线补传同接口）
svc.record_swipe("GATE-A1", "P001", "A区", "IN", device_time, event_uid="u-1001")

# 双控装备领用必须带第二名复核人，否则抛 OperationRejected
svc.issue_equipment("E001", "P001", witness_id="P002",
                    device_time=now, expected_return_at=now + timedelta(hours=4),
                    swipe_event_uid="u-1001")

# 盘点（双人确认）→ 差异 → 定性
sid = svc.open_inventory("A区", "P001", now)["payload"]["session_id"]
svc.confirm_inventory(sid, observed_codes=["E002"], person_id="P001",
                      witness_id="P002", device_time=now)
svc.resolve_discrepancy(sid, "E001", "UNRETURNED_LOAN",
                        "演习临时借用，门禁记录可印证", "P001", "P002", now)

# 离线补传（按设备时间排序；违反盘点冻结的记录被隔离而非覆盖）
svc.backfill([{"type": "issue", "code": "E001", "person_id": "P002",
               "device_time": earlier, "expected_return_at": later}])

# 审计：不可篡改时间线 + 按当前核查脱敏导出
assert svc.verify_integrity() == []
svc.timeline(equipment_code="E001")
svc.timeline(person_id="P001")
svc.export_timeline("equipment", "E001", reveal=["P002"], as_json=True)
```

## 测试

```bash
python3 -m unittest discover -s tests -v
```
