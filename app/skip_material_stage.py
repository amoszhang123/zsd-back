"""把「备料中」的存量工单一次性推进到「程序」阶段。

备料总开关关闭（app.models.order.MATERIAL_PREP_ENABLED = False）之后，新接单不再
进入备料；但开关生效之前接的单还停在 step 1，没人领料就永远不动。这个脚本把它们
一次推进过去：备料节点标记为已完成，current_step 落到程序阶段。

作用域是「库里所有 is_test 域」—— 运维脚本不带 token，会话上没有数据域标记，
查询不过滤，生产数据与测试数据一起处理（这正是想要的）。

幂等：只处理 current_step == 1 的单，跑第二遍就是 0 条。
仅改订单本身与其 steps，不动 contract / quality / production 任何数据。

    # 先看会动哪些单，不落库
    python -m app.skip_material_stage --dry-run
    # 真的推进
    python -m app.skip_material_stage

容器里跑：

    docker compose exec -T web python -m app.skip_material_stage --dry-run
"""

import sys
from datetime import date

from sqlalchemy.orm import joinedload

from app.database import SessionLocal
from app.models.order import (
    MATERIAL_PREP_ENABLED,
    MATERIAL_STEP,
    PROGRAM_STEP,
    STEP_KEYS,
    Order,
    resolve_order_status,
)


def main() -> int:
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    force = "--force" in args

    if MATERIAL_PREP_ENABLED and not force:
        print(
            "备料总开关当前为 True（已恢复备料流程），推进存量会让真实待备料的单子"
            "跳过备料。确认要执行请加 --force。"
        )
        return 2

    db = SessionLocal()
    try:
        orders = (
            db.query(Order)
            .options(joinedload(Order.steps))
            .filter(Order.current_step == MATERIAL_STEP)
            .order_by(Order.id)
            .all()
        )

        if not orders:
            print("没有停在「备料中」的工单，无需处理")
            return 0

        head = "将推进" if dry_run else "已推进"
        print(f"共找到 {len(orders)} 张停在「{STEP_KEYS[MATERIAL_STEP]} / 备料」阶段的工单：")

        today = date.today()
        moved = 0
        for order in orders:
            steps = sorted(order.steps, key=lambda s: s.sort_order)
            # steps 行可能不全（历史数据/reset 过的库），越界就跳过并报警，
            # 不要硬改 current_step 造成「状态说程序、步骤条没这一步」的错位
            if len(steps) <= PROGRAM_STEP:
                print(f"  ! {order.id} 步骤记录不足 {PROGRAM_STEP + 1} 条，已跳过")
                continue

            status = resolve_order_status(order)
            print(f"  {order.id}  {order.product or '—'}  （{status} → 齐备/程序）")
            if dry_run:
                continue

            for idx in range(PROGRAM_STEP):
                steps[idx].status = "completed"
                steps[idx].time = today
            order.current_step = PROGRAM_STEP
            steps[PROGRAM_STEP].status = "processing"
            moved += 1

        if dry_run:
            print("\n预演完成，未落库。确认无误后去掉 --dry-run 即执行。")
            return 0

        db.commit()
        print(f"\n{head} {moved} 张工单到「程序」阶段")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
