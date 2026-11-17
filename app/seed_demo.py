"""演示（测试）数据域种子。

给微信审核用的测试账号准备一套「点得动」的数据：覆盖小程序每个页面的每个状态，
且与真实生产数据完全隔离（所有行 is_test=1）。

用法（在 factory 目录下）：
    python -m app.seed_demo          # 已有演示数据时拒绝执行，避免重复灌
    python -m app.seed_demo --reset  # 先清空 is_test=1 的行，再重新灌一遍

安全约束：
  * 本脚本只新增、只删除 is_test=1 的行，删之前会逐表打印行数，误伤不了生产数据；
  * 编号沿用真实规则、只把号段挪到 8000/999990+ 段（长度与真实编号一致）：
    订单 WO80001AA、合同 HT2026999901、机台 MC8001、工号 999991–999999。
"""

import argparse
from datetime import date, datetime, timedelta

from app.database import SessionLocal
from app.invoice_storage import (
    VOUCHER_DIR,
    delete_invoice_file,
    save_image,
    save_invoice_image,
)
from app.models.contract import (
    CONTRACT_STATUS_CANCELLED,
    CONTRACT_STATUS_CLOSED,
    CONTRACT_STATUS_NORMAL,
    CONTRACT_STATUS_PAUSED,
    Contract,
    ContractItem,
)
from app.models.cost import CostInvoice, CostRecord
from app.models.cash import CashEntry, CashVoucher
from app.models.attendance import (
    AttendanceRecord,
    AttendanceSettings,
    LeaveBalance,
    LeaveRequest,
    LeaveType,
)
from app.models.revenue import RevenueRecord
from app.models.order import (
    MATERIAL_PREP_ENABLED,
    MATERIAL_STEP,
    PROGRAM_STEP,
    STEP_KEYS,
    STEP_LABELS,
    Order,
    OrderMaterial,
    OrderStep,
    QualityResult,
)
from app.models.production import (
    Employee,
    Machine,
    OrderContribution,
    OrderProgrammer,
    WorkOrder,
    WorkOrderEmployee,
    WorkOrderItem,
)
from app.models.quality import (
    DispositionOwner,
    QcInspection,
    RepairOrder,
    ReworkOrder,
    ScrapOrder,
)
from app.models.quote import Quote, QuoteItem
from app.work_order_code import format_code

# 测试账号本人。手机号与后端 .env 的 MP_SMS_TEST_PHONE 必须一致。
TEST_ACCOUNT_ID = "999999"
TEST_PHONE = "13800000000"
TEST_NAME = "陈志远"

TODAY = date.today()
NOW = datetime.now()

# 清空时的顺序：先子后父，否则外键会拦。
# 顺序即依赖顺序，改动模型时请同步维护这张表。
DELETE_ORDER = [
    # 发票/凭证先于父表：子表在前，父表在后
    CostInvoice,
    CostRecord,
    CashVoucher,
    CashEntry,
    RevenueRecord,
    AttendanceRecord,
    LeaveRequest,
    LeaveBalance,
    LeaveType,
    AttendanceSettings,
    DispositionOwner,
    RepairOrder,
    ReworkOrder,
    ScrapOrder,
    QcInspection,
    QualityResult,
    WorkOrderItem,
    WorkOrderEmployee,
    OrderContribution,
    OrderProgrammer,
    WorkOrder,
    OrderStep,
    OrderMaterial,
    ContractItem,
    Order,
    Contract,
    QuoteItem,
    Quote,
    Machine,
    Employee,
]

# 工号 → (姓名, 性别, 手机号, 岗位)
# 姓名一律用正常人名：这些名字会出现在参与人、质检员、工单负责人等界面位置上，
# 叫「测试张」之类的会让审核员一眼看出是演示环境（需求要求小程序里不出现测试字样）。
EMPLOYEES = [
    (TEST_ACCOUNT_ID, TEST_NAME, "男", TEST_PHONE, "管理员"),
    ("999991", "张建国", "男", "13800009991", "操作工"),
    ("999992", "李慧敏", "女", "13800009992", "操作工"),
    ("999993", "王磊", "男", "13800009993", "编程"),
    ("999994", "赵敏", "女", "13800009994", "质检"),
]

# 编号 → (名称, 型号)
MACHINES = [
    ("MC8001", "加工中心 A", "VMC-850"),
    ("MC8002", "数控车床 B", "CK6140"),
    ("MC8003", "慢走丝 C", "DK7732"),
    ("MC8004", "平面磨床 D", "M7130"),
]

# 订单：id, 阶段, 品名, 型号, 物料号, 数量, 预估工时, 交期偏移(天), 订单类型
ORDERS = [
    ("WO80001AA", 0, "定位板", "DB-200", "MAT-9001", 30, 2.0, 20, "自主"),
    ("WO80002AA", 0, "支撑座", "ZC-310", "MAT-9002", 18, 1.5, 15, "自主"),
    ("WO80003AA", 0, "导轨滑块", "HG-45", "MAT-9003", 24, 3.0, 25, "半外协"),
    ("WO80004AA", 1, "法兰盘", "FL-100", "MAT-9004", 40, 4.0, 12, "自主"),
    ("WO80005AA", 2, "连接法兰", "FL-110", "MAT-9005", 25, 3.5, 10, "自主"),
    ("WO80006AA", 2, "轴承座", "ZC-210", "MAT-9006", 32, 5.0, 18, "自主"),
    ("WO80007AA", 3, "连接法兰", "FL-120", "MAT-8801", 50, 6.0, 8, "自主"),
    ("WO80008AA", 3, "端盖", "DG-55", "MAT-9008", 60, 7.5, 6, "自主"),
    ("WO80009AA", 4, "主轴套", "ZZ-80", "MAT-9009", 20, 4.5, 5, "自主"),
    ("WO80010AA", 5, "齿轮轴", "CL-62", "MAT-9010", 36, 8.0, 4, "自主"),
    ("WO80011AA", 6, "压板", "YB-30", "MAT-9011", 44, 2.5, 3, "自主"),
    ("WO80012AA", 7, "传动轴", "CD-90", "MAT-9012", 28, 6.5, 2, "自主"),
    ("WO80013AA", 1, "垫块", "DK-20", "MAT-9013", 15, 1.0, 30, "自主"),
    ("WO80014AA", 1, "销轴", "XZ-12", "MAT-9014", 80, 3.0, 28, "自主"),
]

# 合同：id, 状态, 客户, 含哪些订单号
CONTRACTS = [
    ("HT2026999901", CONTRACT_STATUS_NORMAL, "苏州恒达精密机械有限公司", "PO-TEST-0001",
     [o[0] for o in ORDERS if o[0] in {
         "WO80001AA", "WO80002AA", "WO80003AA", "WO80004AA", "WO80005AA",
         "WO80006AA", "WO80007AA", "WO80008AA", "WO80009AA", "WO80010AA", "WO80011AA",
     }]),
    ("HT2026999902", CONTRACT_STATUS_PAUSED, "昆山正宏自动化有限公司", "PO-TEST-0002",
     ["WO80013AA"]),
    ("HT2026999903", CONTRACT_STATUS_CANCELLED, "无锡精拓模具厂", "PO-TEST-0003",
     ["WO80014AA"]),
    ("HT2026999904", CONTRACT_STATUS_CLOSED, "常州华驰机电有限公司", "PO-TEST-0004",
     ["WO80012AA"]),
]

# 报价单：id, [(物料号, 品名, 型号, 数量, 单价)]
QUOTES = [
    ("BJ20261102-1793000000001", [
        ("MAT-8801", "连接法兰", "FL-120", 50, 186.5),   # 与 WO80007AA 同款，历史报价能命中
        ("MAT-9020", "过渡接头", "GJ-40", 120, 42.0),
    ]),
    ("BJ20261102-1793000000002", [
        ("MAT-9021", "导向套", "DX-25", 60, 78.0),
        ("MAT-9022", "密封盖", "MF-18", 200, 15.5),
    ]),
]


def _reset(db) -> None:
    """清空测试域。逐表打印删除行数，便于确认没有误删生产数据。

    这个会话 deliberately 不带 is_test 上下文（db.info 为空），所以删除条件必须
    显式写成 is_test == True —— 全局过滤器不生效时更要想清楚删的是什么。
    """
    print("清空测试数据（只删 is_test=1 的行）：")
    # 发票图在磁盘上，删行不会带走文件：先把测试域的路径记下来，删完再清盘。
    # 不这么做的话，反复 --reset 会在 uploads 里堆一批谁也认不出的孤儿图片。
    stray_files = [
        path
        for (path,) in db.query(CostInvoice.file_path)
        .filter(CostInvoice.is_test.is_(True))
        .all()
    ]
    total = 0
    for model in DELETE_ORDER:
        n = db.query(model).filter(model.is_test.is_(True)).delete(synchronize_session=False)
        total += n
        if n:
            print(f"  {model.__tablename__:<24} -{n}")
    db.commit()
    removed = sum(1 for path in stray_files if delete_invoice_file(path))
    if removed:
        print(f"  {'uploads/invoices':<24} -{removed}")
    print(f"  共删除 {total} 行")


def _seed_employees(db) -> dict[str, Employee]:
    employees: dict[str, Employee] = {}
    for emp_id, name, gender, phone, position in EMPLOYEES:
        emp = Employee(
            id=emp_id,
            name=name,
            gender=gender,
            phone=phone,
            position=position,
            status="在职",
            hire_date=TODAY - timedelta(days=400),
            id_card="320500199001010000",
        )
        db.add(emp)
        employees[emp_id] = emp
    return employees


def _seed_machines(db) -> dict[str, Machine]:
    machines: dict[str, Machine] = {}
    for code, name, model in MACHINES:
        machine = Machine(id=code, name=name, model=model, active=True)
        db.add(machine)
        machines[code] = machine
    return machines


def _seed_orders(db) -> dict[str, Order]:
    orders: dict[str, Order] = {}
    for idx, (oid, step, product, model, material_no, qty, hours, due, order_type) in enumerate(
        ORDERS
    ):
        # 备料总开关关闭时，种子数据里「备料中」的两张单也一并挪到程序阶段：
        # 否则重灌演示数据后它们会卡在一个界面上看不到待办的阶段
        # （web 备料页此时只渲染空状态提示），看着像数据丢了。
        if not MATERIAL_PREP_ENABLED and step == MATERIAL_STEP:
            step = PROGRAM_STEP
        order = Order(
            id=oid,
            order_no=f"OD800{idx + 1:03d}",
            project_no=f"PJ-800{idx + 1:02d}",
            material_no=material_no,
            customer="苏州恒达精密机械有限公司",
            product=product,
            model=model,
            quantity=qty,
            estimated_hours=hours,
            # 程序阶段的两张单：一张留着待编程，一张已经填过时长
            machining_minutes=None if oid == "WO80005AA" else round(hours * 48, 0),
            priority="加急" if idx % 5 == 0 else "普通",
            order_type=order_type,
            current_step=step,
            create_date=TODAY - timedelta(days=10 - idx),
            deadline=TODAY + timedelta(days=due),
        )
        db.add(order)
        orders[oid] = order

        # 步骤行与 web 端 _build_steps 一致，只是把走过的阶段标成已完成
        for i, key in enumerate(STEP_KEYS):
            if i < step:
                status, operator, when = "completed", "正常生产", TODAY - timedelta(days=step - i)
            elif i == step:
                status, operator, when = "processing", "正常生产", None
            else:
                status, operator, when = "waiting", None, None
            db.add(
                OrderStep(
                    order_id=oid,
                    step_key=key,
                    step_label=STEP_LABELS[i],
                    status=status,
                    operator=operator,
                    time=when,
                    sort_order=i,
                )
            )
    return orders


def _seed_contracts(db, orders: dict[str, Order]) -> dict[str, Contract]:
    contracts: dict[str, Contract] = {}
    for cid, status, party, purchase_no, order_ids in CONTRACTS:
        contract = Contract(
            id=cid,
            purchase_no=purchase_no,
            status=status,
            party_a=party,
            address="苏州工业园区唯新路 88 号",
            bank_name="中国银行苏州工业园区支行",
            bank_account="5000000000000001",
            tax_no="91320500MA00000000",
            phone="0512-60000000",
            signed_at=TODAY - timedelta(days=20),
            source_file="采购合同-202611.xlsx",
        )
        db.add(contract)
        contracts[cid] = contract

        total = 0.0
        for seq, oid in enumerate(order_ids, start=1):
            order = orders[oid]
            unit_price = round(12.5 + seq * 7.3, 2)
            amount = round(unit_price * order.quantity, 2)
            total += amount
            db.add(
                ContractItem(
                    contract_id=cid,
                    seq=seq,
                    project_no=order.project_no,
                    brand="恒力",
                    product=order.product,
                    material_no=order.material_no,
                    model=order.model,
                    unit="件",
                    quantity=order.quantity,
                    unit_price=unit_price,
                    amount=amount,
                    delivery=str(seq),
                    order_id=oid,
                )
            )
        contract.total_amount = round(total, 2)
    return contracts


def _seed_work_orders(db, orders: dict[str, Order], machines: dict[str, Machine]) -> None:
    """两张已完工的历史开工单。

    测试账号名下刻意**没有未完工的开工单**：只要有一张，他点「开工」就会被
    「必须先完工 #N」挡住，审核员第一下就卡死。
    """
    batches = [
        (
            TODAY - timedelta(days=1),
            [TEST_ACCOUNT_ID, "999991"],
            [("WO80009AA", "MC8001"), ("WO80010AA", "MC8002")],
        ),
        (
            TODAY - timedelta(days=2),
            [TEST_ACCOUNT_ID],
            [("WO80011AA", "MC8003"), ("WO80012AA", "MC8001")],
        ),
    ]
    for work_date, employee_ids, items in batches:
        started = datetime.combine(work_date, datetime.min.time()) + timedelta(hours=8)
        wo = WorkOrder(
            work_date=work_date,
            # 工单号跟 created_at 对齐，和线上建单的口径一致（14 位开单时间戳）
            code=format_code(started),
            status="完工",
            created_at=started,
            completed_at=datetime.combine(work_date, datetime.min.time()) + timedelta(hours=17),
        )
        wo.employees = [
            WorkOrderEmployee(employee_id=eid, work_date=work_date) for eid in employee_ids
        ]
        wo.items = [
            WorkOrderItem(
                order_id=oid,
                machine_id=mid,
                completed_qty=orders[oid].quantity,
            )
            for oid, mid in items
        ]
        db.add(wo)


def _seed_quality(db, orders: dict[str, Order]) -> None:
    """质检批次：让每个阶段的"待检"数量都自洽。

    WO80009AA 停在质检1 且一条批次都没有 → 待检 = 全量（审核员能试判定）；
    其余往下走的单，上游阶段全部合格，待检落在当前阶段。
    """
    plan = {
        # 订单 → [(阶段, 合格数, 距今天数)]
        "WO80010AA": [("quality1", 36, 2)],
        "WO80011AA": [("quality1", 44, 3), ("surface", 44, 2)],
        "WO80012AA": [("quality1", 28, 4), ("surface", 28, 3), ("quality2", 28, 2)],
    }
    for oid, rows in plan.items():
        order = orders[oid]
        for stage, qty, days_ago in rows:
            created = NOW - timedelta(days=days_ago)
            db.add(
                QcInspection(
                    order_id=oid,
                    stage=stage,
                    quantity=qty,
                    pass_count=qty,
                    fail_count=0,
                    scrap_qty=0,
                    verdict="pass",
                    remark="正常生产",
                    employee_id="999994",
                    inspector_id="999994",
                    created_at=created,
                )
            )
            db.add(
                QualityResult(
                    order_id=oid,
                    stage=stage,
                    pass_count=qty,
                    fail_count=0,
                    rate=100.0,
                    qc_status="pass",
                    remark="正常生产",
                )
            )
        if oid == "WO80012AA":
            db.add(
                OrderContribution(
                    order_id=oid,
                    employee_id=TEST_ACCOUNT_ID,
                    percent=100,
                    filled_by=TEST_ACCOUNT_ID,
                )
            )
        else:
            db.add(
                OrderContribution(
                    order_id=oid,
                    employee_id="999991",
                    percent=100,
                    filled_by="999991",
                )
            )


def _seed_dispositions(db) -> None:
    """返工 / 维修 / 报废各一张，带负责人分摊 —— 否则「我的单据损耗」是空的。"""
    docs = [
        (ReworkOrder, {"order_id": "WO80010AA", "quantity": 3, "employee_id": "999991",
                       "stage": "quality1", "status": "待处理"}),
        (RepairOrder, {"order_id": "WO80011AA", "quantity": 2, "employee_id": "999991",
                       "stage": "quality2", "status": "处理中"}),
        (ScrapOrder, {"order_id": "WO80012AA", "quantity": 1, "employee_id": "999992",
                      "stage": "quality2"}),
    ]
    type_key = {ReworkOrder: "rework", RepairOrder: "repair", ScrapOrder: "scrap"}
    for model, kwargs in docs:
        doc = model(created_at=NOW - timedelta(days=1), **kwargs)
        db.add(doc)
        db.flush()
        db.add(
            DispositionOwner(
                disposition_type=type_key[model],
                disposition_id=doc.id,
                # 分摊给测试账号本人，审核员登录后「我的单据损耗」才有内容
                employee_id=TEST_ACCOUNT_ID,
                labor_hours=2.5,
                material_cost=120.0 if model is ScrapOrder else 0.0,
            )
        )


def _seed_quotes(db) -> None:
    for quote_id, items in QUOTES:
        quote = Quote(id=quote_id, source_file="报价单-202611.xlsx", created_at=NOW - timedelta(days=3))
        db.add(quote)
        for seq, (material_no, product, model, qty, price) in enumerate(items, start=1):
            db.add(
                QuoteItem(
                    quote_id=quote_id,
                    seq=seq,
                    category="机加工件",
                    material_no=material_no,
                    product=product,
                    model=model,
                    quantity=qty,
                    unit="件",
                    brand="恒力",
                    price=price,
                    delivery=str(seq + 10),
                )
            )


def _seed_programmers(db) -> None:
    """WO80006AA 已经填过程序时间，记下编程人。"""
    db.add(OrderProgrammer(order_id="WO80006AA", employee_id="999993"))


def _seed_materials(db, orders: dict[str, Order]) -> None:
    """给两张单挂上物料清单（BOM）。

    备料总开关关闭时它们已落到程序阶段，备料页不再展示，但清单本身要留着 ——
    缺料提醒、恢复备料后的待备料列表都靠它。
    """
    for oid, names in {
        "WO80004AA": ["45# 圆钢", "内六角螺钉"],
        "WO80013AA": ["尼龙垫片", "定位销"],
    }.items():
        order = orders[oid]
        for idx, name in enumerate(names):
            db.add(
                OrderMaterial(
                    order_id=oid,
                    name=name,
                    stock=order.quantity,
                    required=order.quantity,
                    status="ready",
                )
            )


# 成本样例（小程序成本页 + web 成本页都会看到）。
# 第二条故意「已开票但没图」：审核员点「只看缺票」时得有东西可看，
# 否则「发票可以后补」这个流程在演示里根本走不通。
# 字段：(类别, 规格, 数量, 单价, 供应商, 已开票, 备注, 距今天数)
COSTS = [
    ("刀具", "φ6 四刃铣刀（涂层）", 20, 68.0, "昆山锐工精密刀具", True, "车间常用规格，按月备货", 1),
    ("油品", "20L 桶装切削液", 4, 385.0, "苏州恒润化工", True, "票在财务处，还没拍照", 9),
]


def _sample_invoice_png() -> bytes:
    """画一张最简版式的发票样例图。

    为什么要费劲画：审核员点进成本页，那笔「已开票」的记录点开如果是空白或裂图，
    会让人以为发票功能没做完。画一张示意图最省事，也不用联网找素材。
    画不出来就跳过（_seed_costs 里已经兜住），绝不阻断种子。
    """
    import io

    from PIL import Image, ImageDraw

    from app.order_label import _font

    width, height = 900, 600
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([20, 20, width - 20, height - 20], outline=(190, 60, 60), width=3)

    def centered(text: str, y: int, size: int, fill=(30, 30, 30)) -> None:
        font = _font(size)
        draw.text(((width - draw.textlength(text, font=font)) / 2, y), text, font=font, fill=fill)

    centered("增值税电子普通发票", 44, 34, (170, 30, 30))
    centered("（样例，仅用于演示）", 92, 18, (150, 150, 150))
    draw.line([40, 126, width - 40, 126], fill=(190, 60, 60), width=2)

    f16, f18 = _font(18), _font(20)
    for row, text in enumerate(
        [
            "发票代码：032002100311",
            "发票号码：08873426",
            "开票日期：2026 年 11 月 02 日",
            "购买方：苏州展晟达精密科技有限公司",
            "销售方：昆山锐工精密刀具有限公司",
        ]
    ):
        draw.text((60, 150 + row * 30), text, font=f16, fill=(60, 60, 60))

    draw.line([60, 320, width - 60, 320], fill=(200, 200, 200), width=1)
    for x, text in ((60, "货物或应税劳务名称"), (480, "数量"), (600, "单价"), (740, "金额")):
        draw.text((x, 334), text, font=f16, fill=(90, 90, 90))
    draw.line([60, 366, width - 60, 366], fill=(200, 200, 200), width=1)
    for x, text in (
        (60, "φ6 四刃铣刀（涂层）"),
        (480, "20"),
        (600, "68.00"),
        (740, "1360.00"),
    ):
        draw.text((x, 380), text, font=f18, fill=(30, 30, 30))
    draw.line([60, 418, width - 60, 418], fill=(200, 200, 200), width=1)
    draw.text((60, 438), "价税合计（大写）壹仟叁佰陆拾元整", font=f18, fill=(30, 30, 30))
    draw.text((680, 438), "¥1360.00", font=_font(24), fill=(170, 30, 30))

    # 一枚红色椭圆章，让这张图一眼能认成发票
    draw.ellipse([640, 458, 860, 572], outline=(200, 40, 40), width=3)
    draw.text((702, 505), "发票专用章", font=_font(16), fill=(200, 40, 40))

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _seed_costs(db) -> None:
    """两条测试成本：一条带样例发票图，一条「已开票但没图」。

    第二条是故意留的：审核员点「只看缺票」时得有东西可看，
    否则后补这个流程在演示里根本走不通。
    """
    sample = None
    try:
        sample = _sample_invoice_png()
    except Exception as e:  # 字体缺失/绘制异常都不该让整个种子失败
        print(f"  样例发票生成失败（不影响其它数据）: {e}")

    for index, (category, spec, qty, price, supplier, invoiced, remark, days) in enumerate(
        COSTS
    ):
        record = CostRecord(
            category=category,
            spec=spec,
            quantity=qty,
            unit_price=price,
            supplier=supplier,
            invoiced=invoiced,
            remark=remark,
            purchase_date=TODAY - timedelta(days=days),
        )
        db.add(record)
        db.flush()  # 拿自增 id 才能挂发票

        if index == 0 and sample:
            try:
                meta = save_invoice_image(sample, "样例发票.png")
                db.add(
                    CostInvoice(
                        cost_id=record.id,
                        file_path=meta["file_path"],
                        filename=meta["filename"],
                        size=meta["size"],
                        content_type=meta["content_type"],
                        source="web",
                        uploaded_by="web",
                    )
                )
            except Exception as e:
                print(f"  样例发票落盘失败（不影响其它数据）: {e}")


def _period(months_back: int = 0) -> str:
    """当前月份往前推 N 个月的 "YYYY-MM"。用来造跨月的收入记录。"""
    year, month = TODAY.year, TODAY.month
    for _ in range(months_back):
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return f"{year}-{month:02d}"


def _sample_voucher_png() -> bytes:
    """一张「转账成功」的样例截图，给费用收支的凭证图用。

    审核员点开凭证要能看到图，空着会被当成功能没做完。画不出来就跳过（上层已兜住）。
    """
    import io

    from PIL import Image, ImageDraw

    from app.order_label import _font

    width, height = 720, 1000
    img = Image.new("RGB", (width, height), (245, 246, 248))
    draw = ImageDraw.Draw(img)

    draw.rectangle([0, 0, width, 120], fill=(7, 193, 96))
    draw.text((40, 40), "转账成功", font=_font(38), fill=(255, 255, 255))
    draw.text((40, 88), "¥620.00", font=_font(22), fill=(230, 255, 235))

    draw.rectangle([32, 150, width - 32, 470], fill=(255, 255, 255))
    rows = [
        ("收款方", "顺丰速运有限公司"),
        ("付款方式", "企业网银"),
        ("转账时间", f"{TODAY:%Y-%m-%d} 14:26:31"),
        ("转账单号", "20261116001426"),
        ("备注", "发货到昆山（样例）"),
    ]
    for i, (label, value) in enumerate(rows):
        y = 180 + i * 56
        draw.text((56, y), label, font=_font(18), fill=(140, 140, 140))
        draw.text((240, y), value, font=_font(18), fill=(40, 40, 40))

    draw.rectangle([32, 500, width - 32, 620], fill=(255, 255, 255))
    draw.text((56, 530), "说明", font=_font(18), fill=(140, 140, 140))
    draw.text((56, 566), "样例凭证图，仅用于演示，不具备法律效力", font=_font(17), fill=(90, 90, 90))

    draw.text((width / 2 - 90, 900), "—— 样例，非真实凭证 ——", font=_font(16), fill=(180, 180, 180))

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# (方向, 类别, 金额, 对方, 备注, 几天前)
CASH_ENTRIES = [
    ("out", "房租", 6500.0, "房东王", "上月厂房租金", 45),
    ("out", "水电", 1620.0, "供电所", "上月电费", 35),
    ("out", "房租", 6500.0, "房东王", "本月厂房租金", 15),
    ("out", "水电", 1830.0, "供电所", "本月电费", 9),
    ("out", "工资", 26500.0, "工资代发", "本月工资（5 人）", 6),
    ("out", "运费", 620.0, "顺丰速运", "发货到昆山", 4),
    ("in", "废料", 2600.0, "城东废品回收", "废铝屑 210kg", 8),
    ("in", "租赁", 1500.0, "同业加工", "外借铣床 3 天", 2),
]

# (客户, 往前几个月, 确认金额, 已收金额, 收款在几天前, 是否开票, 备注)
REVENUE_SEEDS = [
    ("昆山精工机械有限公司", 1, 62300.0, 62300.0, 13, True, "上月对账单，已结清"),
    ("昆山精工机械有限公司", 0, 48600.0, 20000.0, 4, True, "本月对账单，收了一部分"),
    ("苏州华翔电子有限公司", 0, 32400.0, 0.0, None, False, "本月对账单，未开票未收款"),
]


def _seed_cash(db) -> None:
    """费用收支：跨两个月、收支配齐，其中一笔带样例凭证图。

    这些表都继承 TenantScoped，测试域的记录与真实账目互不可见 ——
    所以这里可以放心造带金额的账，不会出现在厂里的真实流水里。
    """
    sample = None
    try:
        sample = _sample_voucher_png()
    except Exception as e:  # 缺字体/绘制异常都不该让整个种子失败
        print(f"  样例凭证生成失败（不影响其它数据）: {e}")

    for index, (direction, category, amount, party, remark, days) in enumerate(CASH_ENTRIES):
        entry = CashEntry(
            direction=direction,
            category=category,
            amount=amount,
            counterparty=party,
            remark=remark,
            entry_date=TODAY - timedelta(days=days),
        )
        db.add(entry)
        db.flush()  # 拿自增 id 才能挂凭证

        # 挂给「运费」那一笔：审核员点开一条支出就能看到凭证长什么样
        if category == "运费" and sample:
            try:
                meta = save_image(sample, "转账凭证.png", VOUCHER_DIR)
                db.add(
                    CashVoucher(
                        entry_id=entry.id,
                        file_path=meta["file_path"],
                        filename=meta["filename"],
                        size=meta["size"],
                        content_type=meta["content_type"],
                        source="mp",
                        uploaded_by=f"{TEST_ACCOUNT_ID} {TEST_NAME}",
                    )
                )
            except Exception as e:
                print(f"  样例凭证落盘失败（不影响其它数据）: {e}")


def _seed_revenue(db) -> None:
    """订单收入：三笔已确认（一个已结清、一个部分收款、一个未收）。

    刻意**不造送货单**：shipments/shipment_items 没有 is_test 列、不参与数据域隔离，
    往它们里面写测试数据会直接出现在厂里的真实送货单列表和月度对账单上。
    代价是测试域的「待确认清单」是空的（没有测试出货单可汇总），
    页面会显示「已出货的月份都确认完了」—— 这是一个合法的空状态，
    不会让审核员以为功能坏了；已确认列表和未收余额照样有数据可看。
    """
    for customer, months_back, amount, received, received_days, invoiced, remark in REVENUE_SEEDS:
        db.add(
            RevenueRecord(
                customer=customer,
                period=_period(months_back),
                amount=amount,
                shipped_amount=amount,
                invoiced=invoiced,
                received_amount=received,
                received_on=(TODAY - timedelta(days=received_days)) if received_days else None,
                remark=remark,
                confirmed_at=NOW - timedelta(days=received_days or 5),
            )
        )


# 假期类型：(名称, 扣薪, 需审批, 管余额, 颜色, 排序)
LEAVE_TYPES = [
    ("事假", True, True, False, "#fa8c16", 1),
    ("病假", True, True, False, "#eb2f96", 2),
    ("年假", False, True, True, "#52c41a", 3),
    ("调休", False, True, True, "#13c2c2", 4),
    ("其他", True, True, False, "#8c8c8c", 5),
]


def _seed_attendance(db, employees) -> None:
    """考勤：班次设置 + 假期类型 + 余额 + 本月打卡 + 三张请假单。

    打卡铺满「本月已过去的工作日」，而不是只造两三天：审核员打开看板看到
    一片红色缺勤，会以为这功能坏了。异常样本（迟到/缺卡/早退）各留一天，
    让「哪些是异常」这件事在演示里看得见。
    """
    from datetime import time as dt_time

    enabled_from = TODAY.replace(day=1)
    db.add(
        AttendanceSettings(
            enabled_from=enabled_from,
            work_start="08:00",
            work_end="17:00",
            rest_minutes=60,
            late_grace_minutes=5,
            workdays="1,2,3,4,5",
        )
    )

    types: dict[str, LeaveType] = {}
    for name, deduct, need, track, color, sort in LEAVE_TYPES:
        t = LeaveType(
            name=name, deduct_pay=deduct, need_approval=need,
            track_balance=track, color=color, sort=sort, active=True,
        )
        db.add(t)
        db.flush()
        types[name] = t

    # _seed_employees 返回 {工号: Employee}，这里一律按 values() 遍历
    staff_all = list(employees.values())

    # 余额：管余额的类型给每人发额度（年假 5 天、调休 2 天）
    for emp in staff_all:
        for key, quota in (("年假", 5), ("调休", 2)):
            db.add(
                LeaveBalance(
                    employee_id=emp.id, year=TODAY.year,
                    type_id=types[key].id, granted=quota, used=0,
                )
            )

    staff = [e for e in staff_all if e.position != "管理员"]
    # 本月已过去的工作日（且不早于启用日）
    workdays = [
        d
        for d in (enabled_from + timedelta(days=i) for i in range((TODAY - enabled_from).days + 1))
        if d.isoweekday() <= 5
    ]

    # 异常样本：工号 → {日期偏移: 情况}
    anomalies = {
        "999991": {"late": 6, "missing": 4},   # 第 6 天迟到、第 4 天只打上班
        "999992": {"early": 5},                # 第 5 天早退
        "999993": {"absent": 3},               # 第 3 天没来
    }

    for emp in staff:
        plan = anomalies.get(emp.id, {})
        for index, day in enumerate(workdays):
            if plan.get("absent") == index:
                continue  # 缺勤：那天不造记录
            in_h, in_m = 7, 52 + (index * 3) % 9          # 07:52 ~ 08:00 之间
            out_h, out_m = 17, 2 + (index * 5) % 12       # 17:02 ~ 17:13
            if plan.get("late") == index:
                in_h, in_m = 8, 28                        # 迟到 28 分钟
            clock_out = datetime.combine(day, dt_time(out_h, out_m))
            if plan.get("early") == index:
                clock_out = datetime.combine(day, dt_time(16, 15))  # 早退 45 分钟
            if plan.get("missing") == index:
                clock_out = None                          # 只打上班
            db.add(
                AttendanceRecord(
                    employee_id=emp.id,
                    work_date=day,
                    clock_in=datetime.combine(day, dt_time(in_h, in_m)),
                    clock_out=clock_out,
                    in_addr="昆山市玉山镇厂区",
                    out_addr="昆山市玉山镇厂区" if clock_out else "",
                    source="mp",
                    operator_id=emp.id,
                )
            )

    # 测试账号（管理员）单独铺满本月工作日：审核员登录后第一眼看到的是自己的考勤，
    # 只造两三天的话，日历上会是一片缺勤，像功能坏了。
    # 管理员不进全厂矩阵（他不用被考核），只在个人视角里可见。
    for index, day in enumerate(workdays):
        if day >= TODAY:
            continue  # 今天及以后不打，留给审核员自己点打卡按钮
        db.add(
            AttendanceRecord(
                employee_id=TEST_ACCOUNT_ID,
                work_date=day,
                clock_in=datetime.combine(day, dt_time(8, 1 + index % 4)),
                clock_out=datetime.combine(day, dt_time(17, 5 + index % 7)),
                in_addr="昆山市玉山镇厂区",
                out_addr="昆山市玉山镇厂区",
                source="mp",
                operator_id=TEST_ACCOUNT_ID,
            )
        )

    # 三张请假单：一张待批、一张已批（扣了年假余额）、一张已驳回
    tomorrow = TODAY + timedelta(days=1)
    db.add(
        LeaveRequest(
            employee_id="999992", type_id=types["年假"].id,
            start_date=tomorrow, end_date=tomorrow, days=1,
            reason="家里有事，请一天年假",
            status="pending", created_by="999992",
        )
    )
    approved_day = workdays[-2] if len(workdays) >= 2 else TODAY
    db.add(
        LeaveRequest(
            employee_id="999991", type_id=types["年假"].id,
            start_date=approved_day, end_date=approved_day, days=1,
            reason="调休一天",
            status="approved", approver_id=TEST_ACCOUNT_ID,
            approved_at=NOW - timedelta(days=3), created_by="999991",
        )
    )
    # 已批的那张年假要占掉 1 天余额。必须先 flush 再查 —— 前面 add 的余额行
    # 还没进库，直接发 UPDATE 会命中 0 行，页面上就变成「批了假但余额没动」。
    db.flush()
    bal = (
        db.query(LeaveBalance)
        .filter(
            LeaveBalance.employee_id == "999991",
            LeaveBalance.type_id == types["年假"].id,
        )
        .first()
    )
    if bal:
        bal.used = 1
    db.add(
        LeaveRequest(
            employee_id="999993", type_id=types["病假"].id,
            start_date=TODAY - timedelta(days=7), end_date=TODAY - timedelta(days=7), days=1,
            half="pm", reason="感冒去医院",
            status="rejected", approver_id=TEST_ACCOUNT_ID,
            approved_at=NOW - timedelta(days=8),
            reject_reason="当天要交样件，改天再请",
            created_by="999993",
        )
    )


def seed() -> None:
    db = SessionLocal()
    try:
        # 打上测试域标记：after 这里新建的每个对象都会被自动写上 is_test=1，
        # 而且后续查询只看得到测试域的数据
        db.info["is_test"] = True
        employees = _seed_employees(db)
        machines = _seed_machines(db)
        orders = _seed_orders(db)
        _seed_contracts(db, orders)
        _seed_work_orders(db, orders, machines)
        _seed_quality(db, orders)
        _seed_dispositions(db)
        _seed_quotes(db)
        _seed_programmers(db)
        _seed_materials(db, orders)
        _seed_costs(db)
        _seed_cash(db)
        _seed_revenue(db)
        _seed_attendance(db, employees)
        db.commit()

        print("演示数据已写入（is_test=1）：")
        print(f"  员工 {len(employees)} 人（测试账号 {TEST_ACCOUNT_ID} / {TEST_PHONE}）")
        print(f"  机台 {len(machines)} 台（MC8001–MC8004）")
        print(f"  订单 {len(orders)} 张（阶段 0–7 全覆盖，其中 3 张待接单）")
        print(f"  合同 {len(CONTRACTS)} 份（正常/暂停/取消/结案各一）")
        print(f"  报价单 {len(QUOTES)} 张、完工开工单 2 张、质检批次 6 条、异常单 3 张")
        print(f"  成本 {len(COSTS)} 笔（其中 1 笔带样例发票、1 笔已开票待补图）")
        print(f"  费用收支 {len(CASH_ENTRIES)} 笔（其中 1 笔带样例凭证），订单收入 {len(REVENUE_SEEDS)} 笔已确认")
        print("  说明：测试域没有测试送货单，所以「待确认收入」为空是正常的")
        print(f"  考勤：{len(LEAVE_TYPES)} 类假期、全月打卡（含迟到/缺卡/早退/缺勤样本）、3 张请假单")
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="写入小程序提审用的演示数据")
    parser.add_argument(
        "--reset", action="store_true", help="先清空所有 is_test=1 的数据再重新写入"
    )
    parser.add_argument(
        "--skip-schema",
        action="store_true",
        help="跳过灌数据前的表结构对齐（默认会先补齐模型里新增的列）",
    )
    args = parser.parse_args()

    # 灌数据前先对齐表结构。这个项目没有独立的迁移执行环节（deploy.sh 只 pull + build），
    # 生产库已经因此连着踩过两次：缺 is_test 列全站 500、缺 machines.model 让种子写不进去。
    # 放在这里而不是只靠启动钩子，是为了「单独跑一次 seed 就能修好」。
    if not args.skip_schema:
        from app.database import ensure_schema

        fixed = ensure_schema()
        if fixed.get("added"):
            print(f"灌数据前已补齐 {len(fixed['added'])} 列：{', '.join(fixed['added'])}")
        if fixed.get("failed"):
            print("以下列补失败（灌数据可能中断）：", "; ".join(fixed["failed"]))

    db = SessionLocal()
    try:
        existing = db.query(Employee).filter(Employee.is_test.is_(True)).count()
    finally:
        db.close()

    if existing and not args.reset:
        print(f"库里已有 {existing} 个测试员工。要重灌请加 --reset（只会删 is_test=1 的行）")
        raise SystemExit(1)

    if args.reset:
        db = SessionLocal()
        try:
            _reset(db)
        finally:
            db.close()

    seed()


if __name__ == "__main__":
    main()
