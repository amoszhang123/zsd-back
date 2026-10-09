import random
from datetime import date, timedelta

from app.database import SessionLocal
from app.models.order import Order, OrderStep, OrderMaterial, STEP_KEYS, STEP_LABELS
from app.models.production import Employee, Machine

CUSTOMERS = ["深圳华为技术", "东莞比亚迪", "苏州三星半导体", "上海中芯国际", "杭州海康威视", "北京小米科技", "广州格力电器", "成都京东方"]
PRODUCTS = ["PCB主板A-301", "铝合金外壳B-205", "不锈钢法兰C-118", "铜基散热器D-407", "钛合金连接器E-092", "碳纤维面板F-330"]
MATERIALS = ["铝锭6063-T5", "不锈钢板304", "铜排C1100", "FR-4覆铜板", "环氧树脂", "钛丝TA1", "碳纤维预浸料", "导热硅胶"]
EMPLOYEES = [
    ("100001", "张伟", "男", "440301199001011234", "2020-03-15", "在职", "操作工"),
    ("100002", "李强", "男", "440301198805052345", "2019-07-20", "在职", "操作工"),
    ("100003", "王芳", "女", "440301199203033456", "2021-01-10", "在职", "质检"),
    ("100004", "赵明", "男", "440301199506064567", "2022-09-01", "在职", "操作工"),
    ("100005", "陈刚", "男", "440301198712125678", "2018-04-18", "在职", "质检"),
    ("100006", "刘洋", "女", "440301199808086789", "2024-02-01", "实习", "操作工"),
    ("100007", "孙丽", "女", "440301199107097890", "2023-06-15", "在职", "质检"),
    ("100008", "周磊", "男", "440301198603108901", "2017-11-20", "离职", "操作工"),
]
MACHINES = ["MC-A01", "MC-A02", "MC-B01", "MC-B02", "MC-C01", "MC-C02"]


def seed_if_empty():
    db = SessionLocal()
    try:
        if db.query(Order).count() > 0:
            return

        for eid, ename, egender, eid_card, ehire, estatus, eposition in EMPLOYEES:
            db.merge(Employee(id=eid, name=ename, gender=egender, id_card=eid_card,
                              hire_date=date.fromisoformat(ehire), status=estatus, position=eposition))
        for mid in MACHINES:
            db.merge(Machine(id=mid, name=mid))

        for i in range(24):
            ts = 1726400000 + i
            order_id = f"WO{ts % 100000:05d}{chr(65 + i % 26)}{chr(65 + (i // 26) % 26)}"
            current_step = random.randint(0, 7)
            order = Order(
                id=order_id,
                order_no=f"OD{2024000 + i}",
                project_no=f"PJ-{chr(65 + random.randint(0, 25))}{random.randint(100, 999)}",
                material_no=f"PN-{random.randint(10000, 99999)}",
                customer=random.choice(CUSTOMERS),
                product=random.choice(PRODUCTS),
                model=f"M-{random.randint(100, 999)}",
                quantity=random.randint(100, 5000),
                estimated_hours=random.randint(20, 200),
                priority=random.choice(["普通", "加急", "特急"]),
                current_step=current_step,
                create_date=date.today() - timedelta(days=random.randint(0, 30)),
                deadline=date.today() + timedelta(days=random.randint(1, 15)),
            )
            db.add(order)
            db.flush()

            for j in range(8):
                if j < current_step:
                    status, operator, t = "completed", random.choice([e[1] for e in EMPLOYEES]), date.today() - timedelta(days=random.randint(1, 15))
                elif j == current_step:
                    status, operator, t = "processing", random.choice([e[1] for e in EMPLOYEES]), None
                else:
                    status, operator, t = "waiting", None, None
                db.add(OrderStep(
                    order_id=order_id, step_key=STEP_KEYS[j], step_label=STEP_LABELS[j],
                    status=status, operator=operator, time=t, sort_order=j,
                ))

            for _ in range(random.randint(2, 4)):
                mat_status = random.choice(["ready", "short", "ordering"])
                req = random.randint(100, 2000)
                db.add(OrderMaterial(
                    order_id=order_id,
                    name=random.choice(MATERIALS),
                    stock=random.randint(0, 10000) if mat_status == "ready" else random.randint(0, req - 1),
                    required=req,
                    status=mat_status,
                ))

        db.commit()
    finally:
        db.close()
