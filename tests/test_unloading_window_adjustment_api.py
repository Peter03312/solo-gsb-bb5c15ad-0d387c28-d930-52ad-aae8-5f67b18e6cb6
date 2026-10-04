"""执行中收紧剩余力矩安全区间（区间调整申请）API 测试。"""
from app import models
from app.database import SessionLocal


# 与 test_unloading_execution_api 相同的配载：
# C001(10,港1)->A02、C002(30,港2)->A01、C003(10,港1)->B01，初始力矩 -30
SLOT_PAYLOAD = {
    "slots": [
        {"code": "A01", "stack": "A", "tier": 1, "capacity": 40, "arm": -2.0},
        {"code": "A02", "stack": "A", "tier": 2, "capacity": 15, "arm": -2.0},
        {"code": "B01", "stack": "B", "tier": 1, "capacity": 15, "arm": 5.0},
    ],
    "adjacency": [],
    "incompatible_classes": [],
    "min_moment": -100000,
    "max_moment": 100000,
}

MANIFEST_PAYLOAD = {
    "containers": [
        {"no": "C001", "weight": 10, "hazmat": None, "discharge_order": 1},
        {"no": "C002", "weight": 30, "hazmat": None, "discharge_order": 2},
        {"no": "C003", "weight": 10, "hazmat": None, "discharge_order": 1},
    ]
}

PORTS = {
    "ports": [
        {"discharge_order": 1, "min_remaining_moment": -100, "max_remaining_moment": 100},
        {"discharge_order": 2, "min_remaining_moment": -100, "max_remaining_moment": 100},
    ]
}


def _seed_and_confirm(client, ports=PORTS, prefix=""):
    slots = {
        "slots": [
            {"code": f"{prefix}A01", "stack": f"{prefix}A", "tier": 1,
             "capacity": 40, "arm": -2.0},
            {"code": f"{prefix}A02", "stack": f"{prefix}A", "tier": 2,
             "capacity": 15, "arm": -2.0},
            {"code": f"{prefix}B01", "stack": f"{prefix}B", "tier": 1,
             "capacity": 15, "arm": 5.0},
        ],
        "adjacency": [],
        "incompatible_classes": [],
        "min_moment": -100000,
        "max_moment": 100000,
    }
    sr = client.post("/api/slot-revisions", json=slots).json()["slots_revision"]
    mr = client.post("/api/manifest-revisions", json=MANIFEST_PAYLOAD) \
        .json()["manifest_revision"]
    plan = client.post("/api/plans/compute", json={}).json()
    client.post(f"/api/plans/{plan['plan_id']}/confirm", json={
        "slots_revision": sr, "manifest_revision": mr
    })
    r = client.post(f"/api/plans/{plan['plan_id']}/unloading", json=ports)
    assert r.status_code == 201, r.text
    return plan


def _execute(client, plan_id, container, op, version):
    return client.post(
        f"/api/plans/{plan_id}/unloading/execute",
        json={
            "container_no": container,
            "operation_no": op,
            "expected_version": version,
        },
    )


def _adjust(client, plan_id, application_no, progress_ver, window_ver, ports):
    return client.post(
        f"/api/plans/{plan_id}/unloading/window-adjustments",
        json={
            "application_no": application_no,
            "expected_progress_version": progress_ver,
            "expected_window_version": window_ver,
            "ports": ports,
        },
    )


def _first_unload(client, pid, op="OP-1"):
    r = _execute(client, pid, "C001", op, 0)
    assert r.status_code == 201, r.text
    return r.json()


def test_view_carries_window_version_and_port_sets(client):
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    # 首次登记前：区间版本 0，冻结/生效区间同形
    g0 = client.get(f"/api/plans/{pid}/unloading/execution").json()
    assert g0["window_version"] == 0
    assert g0["frozen_ports"] == PORTS["ports"]
    assert g0["effective_ports"] == PORTS["ports"]
    assert g0["adjustments"] == []

    d = _first_unload(client, pid)
    # 首次登记后：区间版本仍为 0，冻结与生效区间均为原定区间
    assert d["window_version"] == 0
    assert d["frozen_ports"] == PORTS["ports"]
    assert d["effective_ports"] == PORTS["ports"]
    assert d["adjustments"] == []


def test_adjust_happy_path_tightens_current_and_future_ports(client):
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    _first_unload(client, pid)  # 剩余 C003(港1)、C002(港2)，当前剩余力矩 -10

    # 收紧港1为 [-80,-10]、港2为 [-60,0]（均包含于原 [-100,100]）；
    # 当前剩余力矩 -10 ∈ [-80,-10]
    body_ports = [
        {"discharge_order": 1, "min_remaining_moment": -80, "max_remaining_moment": -10},
        {"discharge_order": 2, "min_remaining_moment": -60, "max_remaining_moment": 0},
    ]
    r = _adjust(client, pid, "ADJ-1", 1, 0, body_ports)
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["window_version"] == 1
    assert d["progress_version"] == 1
    assert d["effective_ports"] == body_ports
    # 冻结原区间保留不变
    assert d["frozen_ports"] == PORTS["ports"]
    # 调整历史
    assert len(d["adjustments"]) == 1
    h = d["adjustments"][0]
    assert h["application_no"] == "ADJ-1"
    assert h["from_window_version"] == 0
    assert h["to_window_version"] == 1
    assert h["expected_progress_version"] == 1
    assert h["expected_window_version"] == 0
    assert h["ports"] == body_ports
    assert h["created_at"]
    # 推荐顺序按新区间给出
    assert [s["container_no"] for s in d["recommendation"]] == ["C003", "C002"]

    # GET 同样展示生效区间、版本与历史
    g = client.get(f"/api/plans/{pid}/unloading/execution").json()
    assert g["window_version"] == 1
    assert g["effective_ports"] == body_ports
    assert len(g["adjustments"]) == 1


def test_unlisted_port_keeps_current_effective_window(client):
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    _first_unload(client, pid)
    # 只列港1；港2沿用当前生效区间 [-100,100]
    ports1 = [
        {"discharge_order": 1, "min_remaining_moment": -80, "max_remaining_moment": -10},
    ]
    r = _adjust(client, pid, "ADJ-P1", 1, 0, ports1)
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["effective_ports"] == [
        {"discharge_order": 1, "min_remaining_moment": -80, "max_remaining_moment": -10},
        {"discharge_order": 2, "min_remaining_moment": -100, "max_remaining_moment": 100},
    ]
    # 历史条目只含显式提交的港1
    assert d["adjustments"][0]["ports"] == ports1

    # 第二次调整只收紧港2；港1沿用刚生效的 [-80,-10]
    ports2 = [
        {"discharge_order": 2, "min_remaining_moment": -60, "max_remaining_moment": 0},
    ]
    r2 = _adjust(client, pid, "ADJ-P2", 1, 1, ports2)
    assert r2.status_code == 201, r2.text
    d2 = r2.json()
    assert d2["window_version"] == 2
    assert d2["effective_ports"] == [
        {"discharge_order": 1, "min_remaining_moment": -80, "max_remaining_moment": -10},
        {"discharge_order": 2, "min_remaining_moment": -60, "max_remaining_moment": 0},
    ]
    assert [a["application_no"] for a in d2["adjustments"]] == ["ADJ-P1", "ADJ-P2"]
    assert [a["to_window_version"] for a in d2["adjustments"]] == [1, 2]


def test_subsequent_registration_uses_effective_window(client):
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    _first_unload(client, pid)  # 剩 -10
    # 收紧港1为 [-80,-60]：当前 -10 落在区间外 → 申请被拒
    bad = _adjust(client, pid, "ADJ-BAD", 1, 0, [
        {"discharge_order": 1, "min_remaining_moment": -80, "max_remaining_moment": -60},
    ])
    assert bad.status_code == 422
    assert "当前剩余总力矩" in bad.json()["detail"]

    # 收紧港1为 [-80,-10]：卸 C003 后剩 -60，须在新区间内
    ok = _adjust(client, pid, "ADJ-OK", 1, 0, [
        {"discharge_order": 1, "min_remaining_moment": -80, "max_remaining_moment": -10},
        {"discharge_order": 2, "min_remaining_moment": -100, "max_remaining_moment": 100},
    ])
    assert ok.status_code == 201, ok.text

    # 收紧港1到 [-60,-10]：当前 -10 在区间内；卸 C003 后剩 -60 亦在区间内
    ok2 = _adjust(client, pid, "ADJ-OK2", 1, 1, [
        {"discharge_order": 1, "min_remaining_moment": -60, "max_remaining_moment": -10},
    ])
    assert ok2.status_code == 201, ok2.text
    assert ok2.json()["effective_ports"][0] == {
        "discharge_order": 1, "min_remaining_moment": -60, "max_remaining_moment": -10
    }

    # 用生效区间登记 C003：卸后 -60 合法
    r = _execute(client, pid, "C003", "OP-2", 1)
    assert r.status_code == 201, r.text
    assert r.json()["remaining_moment"] == -60
    assert r.json()["window_version"] == 2
    # 已卸步骤保留
    assert [s["container_no"] for s in r.json()["steps"]] == ["C001", "C003"]
    # 当前港已推进到 2；冻结区间仍是原定区间
    assert r.json()["current_port"] == 2
    assert r.json()["frozen_ports"] == PORTS["ports"]

    # 卸完 C002（剩 0），港2区间 [-100,100] 包含 0
    r3 = _execute(client, pid, "C002", "OP-3", 2)
    assert r3.status_code == 201, r3.text
    assert r3.json()["completed"] is True


def test_adjustment_infeasible_remaining_rejected_422_no_effect(client):
    """新区间使剩余箱无法按港序/栈顶/逐步区间卸完 → 422 且不生效。"""
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    _first_unload(client, pid)  # 剩 C003(港1,+50)、C002(港2,-60)，当前 -10
    # 港1收紧到 [-10,-10]：卸 C003 后剩 -60 ≠ -10，无可行卸法
    r = _adjust(client, pid, "ADJ-INF", 1, 0, [
        {"discharge_order": 1, "min_remaining_moment": -10, "max_remaining_moment": -10},
    ])
    assert r.status_code == 422, r.text
    assert "无法" in r.json()["detail"]
    # 状态不变：版本、生效区间、历史均无变化
    g = client.get(f"/api/plans/{pid}/unloading/execution").json()
    assert g["window_version"] == 0
    assert g["effective_ports"] == PORTS["ports"]
    assert g["adjustments"] == []
    db = SessionLocal()
    try:
        assert db.query(models.UnloadingWindowAdjustment).count() == 0
        assert db.query(models.UnloadingEffectivePort).count() == 2
    finally:
        db.close()


def test_widening_rejected_window_must_be_contained(client):
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    _first_unload(client, pid)
    # 下界放宽（-120 < -100）
    r1 = _adjust(client, pid, "ADJ-W1", 1, 0, [
        {"discharge_order": 1, "min_remaining_moment": -120, "max_remaining_moment": 0},
    ])
    assert r1.status_code == 422 and "包含于" in r1.json()["detail"]
    # 上界放宽（120 > 100）
    r2 = _adjust(client, pid, "ADJ-W2", 1, 0, [
        {"discharge_order": 2, "min_remaining_moment": -100, "max_remaining_moment": 120},
    ])
    assert r2.status_code == 422 and "包含于" in r2.json()["detail"]
    g = client.get(f"/api/plans/{pid}/unloading/execution").json()
    assert g["window_version"] == 0 and g["adjustments"] == []


def test_completed_port_and_unknown_port_rejected_422(client):
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    # 卸完港1：C001、C003 均卸，当前港推进到 2
    assert _execute(client, pid, "C001", "OP-1", 0).status_code == 201
    assert _execute(client, pid, "C003", "OP-2", 1).status_code == 201
    # 港1已完成，不得再改
    r = _adjust(client, pid, "ADJ-PAST", 2, 0, [
        {"discharge_order": 1, "min_remaining_moment": -100, "max_remaining_moment": 100},
    ])
    assert r.status_code == 422 and "已完成港" in r.json()["detail"]
    # 不存在的卸港序号
    r2 = _adjust(client, pid, "ADJ-UNK", 2, 0, [
        {"discharge_order": 9, "min_remaining_moment": -100, "max_remaining_moment": 100},
    ])
    assert r2.status_code == 422 and "不存在的卸港" in r2.json()["detail"]


def test_stale_versions_rejected_409(client):
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    _first_unload(client, pid)
    # 期望进度版本过期（当前为 1）
    r1 = _adjust(client, pid, "ADJ-S1", 0, 0, [
        {"discharge_order": 1, "min_remaining_moment": -80, "max_remaining_moment": -10},
    ])
    assert r1.status_code == 409 and "进度版本" in r1.json()["detail"]
    # 期望区间版本过期（先成功一次，再以 0 申请）
    ok = _adjust(client, pid, "ADJ-V", 1, 0, [
        {"discharge_order": 1, "min_remaining_moment": -80, "max_remaining_moment": -10},
    ])
    assert ok.status_code == 201
    r2 = _adjust(client, pid, "ADJ-S2", 1, 0, [
        {"discharge_order": 1, "min_remaining_moment": -80, "max_remaining_moment": -20},
    ])
    assert r2.status_code == 409 and "区间版本" in r2.json()["detail"]
    # 状态停留在第一次调整
    g = client.get(f"/api/plans/{pid}/unloading/execution").json()
    assert g["window_version"] == 1


def test_not_started_and_completed_and_superseded_rejected(client):
    # 尚未开始现场卸箱：409
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    r = _adjust(client, pid, "ADJ-EARLY", 0, 0, [
        {"discharge_order": 1, "min_remaining_moment": -100, "max_remaining_moment": 100},
    ])
    assert r.status_code == 409 and "尚未开始" in r.json()["detail"]

    # 已完结：409
    _first_unload(client, pid)
    _adjust(client, pid, "ADJ-1", 1, 0, [
        {"discharge_order": 1, "min_remaining_moment": -100, "max_remaining_moment": 100},
        {"discharge_order": 2, "min_remaining_moment": -100, "max_remaining_moment": 100},
    ])
    assert _execute(client, pid, "C003", "OP-2", 1).status_code == 201
    assert _execute(client, pid, "C002", "OP-3", 2).status_code == 201
    rdone = _adjust(client, pid, "ADJ-DONE", 3, 1, [
        {"discharge_order": 2, "min_remaining_moment": 0, "max_remaining_moment": 0},
    ])
    assert rdone.status_code == 409 and "完结" in rdone.json()["detail"]

    # 方案被新修订取代：只读 409（另起方案验证）
    p2_seed = _seed_and_confirm(client, prefix="Z")
    p2 = p2_seed["plan_id"]
    _first_unload(client, p2, op="OP-Z1")
    client.post("/api/manifest-revisions", json={
        "containers": [
            {"no": "C001", "weight": 15, "hazmat": None, "discharge_order": 1}
        ]
    })
    amend = client.post(f"/api/plans/{p2}/amend", json={}).json()
    # p2 使用槽位修订 2；新箱清单修订为 3（修订 1、2 已被前两个方案占用）
    assert client.post(f"/api/plans/{amend['plan_id']}/confirm", json={
        "slots_revision": 2, "manifest_revision": 3
    }).status_code == 200
    rs = _adjust(client, p2, "ADJ-RO", 1, 0, [
        {"discharge_order": 1, "min_remaining_moment": -100, "max_remaining_moment": 100},
        {"discharge_order": 2, "min_remaining_moment": -100, "max_remaining_moment": 100},
    ])
    assert rs.status_code == 409 and "只读" in rs.json()["detail"]
    # 历史进度仍可查
    g = client.get(f"/api/plans/{p2}/unloading/execution").json()
    assert g["status"] == "historical" and g["window_version"] == 0


def test_same_application_retry_returns_first_snapshot_200(client):
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    _first_unload(client, pid)
    body_ports = [
        {"discharge_order": 1, "min_remaining_moment": -80, "max_remaining_moment": -10},
        {"discharge_order": 2, "min_remaining_moment": -60, "max_remaining_moment": 0},
    ]
    r1 = _adjust(client, pid, "ADJ-IDEM", 1, 0, body_ports)
    assert r1.status_code == 201
    snap1 = r1.json()
    # 同申请号同内容重试（即便携带的期望版本已过期）→ 200 首次快照
    r2 = _adjust(client, pid, "ADJ-IDEM", 1, 0, body_ports)
    assert r2.status_code == 200
    assert r2.json() == snap1

    # 重试不推进版本；再用新申请号成功第二次调整
    other = _adjust(client, pid, "ADJ-IDEM2", 1, 1, [
        {"discharge_order": 2, "min_remaining_moment": -30, "max_remaining_moment": 0},
    ])
    assert other.status_code == 201, other.text
    assert other.json()["window_version"] == 2
    # 旧申请号再次重试仍返回其首次快照（版本 1）
    r3 = _adjust(client, pid, "ADJ-IDEM", 1, 0, body_ports)
    assert r3.status_code == 200
    assert r3.json()["window_version"] == 1
    assert r3.json() == snap1


def test_same_application_different_content_conflict_409(client):
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    _first_unload(client, pid)
    assert _adjust(client, pid, "ADJ-C", 1, 0, [
        {"discharge_order": 1, "min_remaining_moment": -80, "max_remaining_moment": -10},
    ]).status_code == 201
    # 同申请号换区间内容
    r1 = _adjust(client, pid, "ADJ-C", 1, 1, [
        {"discharge_order": 1, "min_remaining_moment": -80, "max_remaining_moment": -20},
    ])
    assert r1.status_code == 409 and "异内容冲突" in r1.json()["detail"]
    # 同申请号换期望区间版本（区间内容不变，仍属异内容）
    r2 = _adjust(client, pid, "ADJ-C", 1, 1, [
        {"discharge_order": 1, "min_remaining_moment": -80, "max_remaining_moment": -10},
    ])
    assert r2.status_code == 409 and "异内容冲突" in r2.json()["detail"]
    g = client.get(f"/api/plans/{pid}/unloading/execution").json()
    assert g["window_version"] == 1
    assert len(g["adjustments"]) == 1


def test_application_no_cross_plan_conflict_409(client):
    p1 = _seed_and_confirm(client)
    p2 = _seed_and_confirm(client, prefix="Z")
    _first_unload(client, p1["plan_id"])
    _first_unload(client, p2["plan_id"], op="OP-Z1")
    ports = [
        {"discharge_order": 1, "min_remaining_moment": -100, "max_remaining_moment": 100},
        {"discharge_order": 2, "min_remaining_moment": -100, "max_remaining_moment": 100},
    ]
    assert _adjust(client, p1["plan_id"], "ADJ-GLOBAL", 1, 0, ports).status_code == 201
    r = _adjust(client, p2["plan_id"], "ADJ-GLOBAL", 1, 0, ports)
    assert r.status_code == 409 and "异内容冲突" in r.json()["detail"]


def test_validation_errors_422(client):
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    _first_unload(client, pid)
    base = "/api/plans/%s/unloading/window-adjustments" % pid
    # 空申请号
    r1 = client.post(base, json={
        "application_no": "  ",
        "expected_progress_version": 1,
        "expected_window_version": 0,
        "ports": [{"discharge_order": 1, "min_remaining_moment": -80,
                  "max_remaining_moment": -10}],
    })
    assert r1.status_code == 422
    # 空区间列表
    r2 = client.post(base, json={
        "application_no": "ADJ-V",
        "expected_progress_version": 1,
        "expected_window_version": 0,
        "ports": [],
    })
    assert r2.status_code == 422
    # 区间倒置
    r3 = client.post(base, json={
        "application_no": "ADJ-V",
        "expected_progress_version": 1,
        "expected_window_version": 0,
        "ports": [{"discharge_order": 1, "min_remaining_moment": 0,
                  "max_remaining_moment": -10}],
    })
    assert r3.status_code == 422
    # 非有限数值
    r4 = client.post(base, json={
        "application_no": "ADJ-V",
        "expected_progress_version": 1,
        "expected_window_version": 0,
        "ports": [{"discharge_order": 1, "min_remaining_moment": "-inf",
                  "max_remaining_moment": 0}],
    })
    assert r4.status_code == 422
    # 重复港序号
    r5 = client.post(base, json={
        "application_no": "ADJ-V",
        "expected_progress_version": 1,
        "expected_window_version": 0,
        "ports": [
            {"discharge_order": 1, "min_remaining_moment": -80,
             "max_remaining_moment": -10},
            {"discharge_order": 1, "min_remaining_moment": -50,
             "max_remaining_moment": -20},
        ],
    })
    assert r5.status_code == 422
    # 缺字段
    r6 = client.post(base, json={"application_no": "ADJ-V"})
    assert r6.status_code == 422
    # 状态不变
    g = client.get(f"/api/plans/{pid}/unloading/execution").json()
    assert g["window_version"] == 0


def test_adjustment_history_retained_after_completion(client):
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    _first_unload(client, pid)
    assert _adjust(client, pid, "ADJ-1", 1, 0, [
        {"discharge_order": 1, "min_remaining_moment": -80, "max_remaining_moment": -10},
        {"discharge_order": 2, "min_remaining_moment": -100, "max_remaining_moment": 100},
    ]).status_code == 201
    assert _execute(client, pid, "C003", "OP-2", 1).status_code == 201
    # 当前港为 2 后只能提交港2
    assert _adjust(client, pid, "ADJ-2", 2, 1, [
        {"discharge_order": 2, "min_remaining_moment": -60, "max_remaining_moment": 0},
    ]).status_code == 201
    assert _execute(client, pid, "C002", "OP-3", 2).status_code == 201
    g = client.get(f"/api/plans/{pid}/unloading/execution").json()
    assert g["completed"] is True
    assert g["window_version"] == 2
    assert [a["application_no"] for a in g["adjustments"]] == ["ADJ-1", "ADJ-2"]
    # 冻结原区间、已卸步骤仍保留
    assert g["frozen_ports"] == PORTS["ports"]
    assert [s["container_no"] for s in g["steps"]] == ["C001", "C003", "C002"]
