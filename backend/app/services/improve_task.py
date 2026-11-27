"""诊改任务领域服务：任务分解、认领、执行、验收，含跨模块关联方案校验。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.cross_module import check_entity_status


class ImproveTaskService(BaseModuleService):
    MODULE = "improve_task"
    ENTITY = "诊改任务"
    OPERATION_TERM = "认领任务"
    LIST_FIELDS = ["任务描述", "承接方案", "执行人员", "起止日期", "验收标准", "督办人员", "任务权重", "备注说明"]
    REQUIRED_FIELDS = ["任务描述", "承接方案", "执行人员"]
    STATUS_ORDER = ["草稿", "待认领", "执行中", "已验收"]
    ACTION_RULES = {"认领任务": "执行中", "启动执行": "执行中", "申请验收": "已验收"}
    NEGATIVE_ACTIONS = []

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """创建诊改任务：承接方案引用必须是仍持有承接资格、且无争议的改进方案。"""
        source_plan = str(values.get("承接方案", "") or "").strip()

        # 尝试解析方案ID引用
        if source_plan.startswith("plan:"):
            plan_id_str = source_plan.split(":", 1)[1]
            try:
                plan_id = int(plan_id_str)
            except ValueError:
                plan_id = None  # 非数字ID不阻止创建
            if plan_id is not None:
                # 核对关联方案是否仍有承接资格（校验失败必须拦住登记）
                plan = check_entity_status(
                    "improve_plan", plan_id, ["审核通过"],
                    "诊改任务的承接方案"
                )
                # 处于争议（如试卷退回冻结待复核）的材料不能派出新任务
                if plan.get("abnormal"):
                    raise ValueError(
                        f"跨模块校验失败：诊改任务的承接方案 improve_plan #{plan_id} "
                        f"处于争议处理中，不能派出新任务"
                    )

        return super().create_entry(values)

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """执行动作，含状态推进约束。"""
        entry = self.get_entry(entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        # 认领任务：只能从待认领状态认领
        if action == "认领任务":
            if entry.get("status") not in ("待认领", "草稿"):
                return None, f"{self.ENTITY} {entry_id} 当前状态为'{entry.get('status')}'，不可认领"

        # 申请验收：必须已在执行中
        if action == "申请验收":
            if entry.get("status") != "执行中":
                return None, f"{self.ENTITY} {entry_id} 当前状态为'{entry.get('status')}'，必须先执行再申请验收"

        return super().run_action(entry_id, action)