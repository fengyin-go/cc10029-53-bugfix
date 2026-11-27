"""试卷分析领域服务：分析记录登记、审核、退回与存档，含跨模块触发改进方案及退回联动。"""
from __future__ import annotations

import re
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class ExamAnalysisService(BaseModuleService):
    MODULE = "exam_analysis"
    ENTITY = "分析记录"
    OPERATION_TERM = "审核分析"
    LIST_FIELDS = ["考试名称", "任课教师", "参考人数", "平均得分", "难度系数", "区分度", "分析日期", "备注说明"]
    REQUIRED_FIELDS = ["考试名称", "任课教师", "参考人数"]
    STATUS_ORDER = ["草稿", "待审核", "审核通过", "已存档"]
    ACTION_RULES = {"审核分析": "审核通过", "退回重填": "草稿", "存档分析": "已存档"}
    NEGATIVE_ACTIONS = []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """执行动作，含状态约束、审核留痕和跨模块联动。

        - 审核分析：通过后按难度触发改进方案；退回后的再次审核按复核办理，
          对照退回前的旧审核意见。
        - 退回重填：冻结本场考试派生方案的承接资格，送回方案待办复核。
        - 重复审核/重复退回：辨识为同一次处理结果，不重复执行。
        """
        entry = self.get_entry(entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        status = entry.get("status")

        # 审核分析：待审核/草稿可审；已通过的重复审核辨识为同一次处理结果
        if action == "审核分析":
            if entry.get("status") == "审核通过":
                last_review = self._last_trail(entry, "审核分析")
                if last_review is not None:
                    return None, (
                        f"{self.ENTITY} {entry_id} 已是审核通过状态，"
                        f"本次审核与第{last_review['序号']}次审核为同一处理结果，无需重复审核"
                    )
                return None, f"{self.ENTITY} {entry_id} 已是审核通过状态，无需重复审核"
            if entry.get("status") == "已存档":
                return None, f"{self.ENTITY} {entry_id} 已存档，不可再审核"

        # 退回重填：已存档结项的不可退回；重复退回辨识为同一次处理结果
        if action == "退回重填":
            if entry.get("status") == "已存档":
                return None, f"{self.ENTITY} {entry_id} 已存档结项，不可退回重填"
            if entry.get("status") == "草稿":
                last_return = self._last_trail(entry, "退回重填")
                if last_return is not None:
                    return None, (
                        f"{self.ENTITY} {entry_id} 已是草稿状态，"
                        f"本次退回与第{last_return['序号']}次退回为同一处理结果，无需重复退回"
                    )
                return None, f"{self.ENTITY} {entry_id} 当前状态为'草稿'，无需退回重填"

        # 存档分析：必须审核通过；重复存档辨识为同一次处理结果
        if action == "存档分析":
            if entry.get("status") == "已存档":
                return None, f"{self.ENTITY} {entry_id} 已是已存档状态，无需重复存档"
            if entry.get("status") != "审核通过":
                return None, (
                    f"{self.ENTITY} {entry_id} 当前状态为'{entry.get('status')}'，"
                    f"必须先审核通过再存档"
                )

        # 退回后再次审核即为复核：需要能对照退回前的旧审核意见
        prev_review = self._last_trail(entry, "审核分析") if action == "审核分析" else None

        result_entry, message = super().run_action(entry_id, action)
        if result_entry is None:
            return result_entry, message

        if action in ("审核分析", "退回重填"):
            self._append_trail(entry, action, status)

        # 审核通过后发现薄弱点，触发改进方案（幂等：同一场考试只授予一份承接资格）
        if action == "审核分析":
            difficulty = entry.get("难度系数", "")
            try:
                diff_val = float(str(difficulty)) if difficulty else 0
                if diff_val > 0.5:
                    self._maybe_create_improve_plan(entry, diff_val, entry_id)
            except (ValueError, TypeError):
                pass
            if prev_review is not None:
                message = f"{message}（复核：已对照第{prev_review['序号']}次审核意见办理）"

        # 退回重填：冻结本场考试派生方案的承接资格，送回方案待办
        if action == "退回重填":
            frozen = self._freeze_derived_plans(entry_id)
            if frozen:
                message = f"{message}，已冻结{len(frozen)}份关联方案的承接资格并送回方案待办复核"

        return result_entry, message

    @staticmethod
    def _references_exam(source: Any, analysis_id: int) -> bool:
        """判断引用是否指向本场考试（exam:1 不应误匹配 exam:11）。"""
        return re.search(rf"exam:{analysis_id}(?!\d)", str(source or "")) is not None

    @staticmethod
    def _trail(entry: dict[str, Any]) -> list[dict[str, Any]]:
        """读取审核留痕；未留痕的记录返回空表，不改动原数据。"""
        trail = entry.get("审核留痕")
        return trail if isinstance(trail, list) else []

    def _last_trail(self, entry: dict[str, Any], action: str) -> dict[str, Any] | None:
        """找最近一次同类处理记录，供幂等辨识和复核对照。"""
        for record in reversed(self._trail(entry)):
            if record.get("处理") == action:
                return record
        return None

    def _append_trail(self, entry: dict[str, Any], action: str, from_status: Any) -> None:
        """把本次审核/退回处理记入审核留痕，供复核对照旧审核、归档后回查。"""
        trail = entry.setdefault("审核留痕", [])
        trail.append({
            "序号": len(trail) + 1,
            "处理": action,
            "原状态": from_status,
            "新状态": entry.get("status", ""),
            "意见": str(entry.get("备注说明", "") or ""),
        })

    def _freeze_derived_plans(self, analysis_id: int) -> list[dict[str, Any]]:
        """冻结本场考试派生方案的承接资格，送回方案待办复核。

        只处理「关联反馈」引用 exam:{analysis_id} 且当前审核通过的方案；
        教学反馈等其它来源的方案不在本场考试范围内，继续办理。
        """
        frozen: list[dict[str, Any]] = []
        note = f"试卷分析#{analysis_id}已退回重填，承接资格冻结，待复核"
        for plan in store.rows("improve_plan"):
            if not self._references_exam(plan.get("关联反馈"), analysis_id):
                continue
            if plan.get("status") != "审核通过":
                continue
            plan["status"] = "待审核"
            plan["pending"] = True
            plan["abnormal"] = True
            existing = str(plan.get("备注说明", "") or "")
            if note not in existing:
                plan["备注说明"] = f"{existing}；{note}" if existing else note
            frozen.append(plan)
        return frozen

    def _maybe_create_improve_plan(
        self, exam_entry: dict[str, Any], difficulty: float, analysis_id: int
    ) -> None:
        """试卷难度过高时，自动触发改进方案创建（幂等：同一场考试只授予一份承接资格）。"""
        plan_rows = store.rows("improve_plan")
        # 幂等检查：本场考试已有派生方案（含退回冻结中的）就不再新建
        for row in plan_rows:
            if self._references_exam(row.get("关联反馈"), analysis_id):
                return

        new_id = max((int(r.get("id", 0)) for r in plan_rows), default=0) + 1
        teacher = exam_entry.get("任课教师", "")
        exam_name = exam_entry.get("考试名称", "")
        plan_rows.append({
            "id": new_id,
            "关联反馈": f"exam:{analysis_id}",
            "改进目标": f"降低{exam_name}难度系数至合理范围",
            "改进措施": f"当前难度系数{difficulty}偏大，需调整教学策略和考核方式",
            "预期产出": "",
            "负责教师": teacher,
            "启动日期": "",
            "完成期限": "",
            "备注说明": "由试卷分析审核通过后自动生成",
            "status": "待审核",
            "pending": True,
            "abnormal": False,
        })
