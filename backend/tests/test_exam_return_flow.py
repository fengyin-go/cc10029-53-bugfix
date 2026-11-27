"""试卷退回与方案承接资格联动测试：覆盖 E-21 场景的修复行为。

链路：试卷分析退回 → 本场考试派生方案承接资格冻结送回待办 →
诊改登记核对承接资格 → 复核（按旧审核或更正样本）恢复唯一有效资格。
"""
import pytest

from app.store import store
from app.services.exam_analysis import ExamAnalysisService
from app.services.feedback import FeedbackService
from app.services.improve_plan import ImprovePlanService
from app.services.improve_task import ImproveTaskService


def clear_all():
    for name in store.module_names():
        store.rows(name).clear()


def make_exam_with_plan(exam_svc, plan_svc, name="E-21 期末统考", difficulty="0.72"):
    """登记并审核通过一场高难度考试，返回 (考试, 自动派生的方案)。"""
    exam, _ = exam_svc.create_entry({
        "考试名称": name,
        "任课教师": "郑老师",
        "参考人数": "120",
        "难度系数": difficulty,
        "备注说明": "分析人初审意见：区分度偏低，建议复核",
    })
    exam_svc.run_action(exam["id"], "审核分析")
    plans = [
        row for row in store.rows("improve_plan")
        if str(row.get("关联反馈", "")) == f"exam:{exam['id']}"
    ]
    assert len(plans) == 1
    return exam, plans[0]


class TestExamReturnFreezesPlanQualification:
    """试卷退回后，本场考试派生方案的承接资格必须冻结并送回方案待办。"""

    def setup_method(self):
        clear_all()
        self.exam_svc = ExamAnalysisService()
        self.plan_svc = ImprovePlanService()
        self.task_svc = ImproveTaskService()

    def test_return_freezes_approved_plan_back_to_todo(self):
        exam, plan = make_exam_with_plan(self.exam_svc, self.plan_svc)
        self.plan_svc.run_action(plan["id"], "审核方案")
        assert store.find("improve_plan", plan["id"])["status"] == "审核通过"

        result, msg = self.exam_svc.run_action(exam["id"], "退回重填")
        assert result is not None
        assert result["status"] == "草稿"

        frozen = store.find("improve_plan", plan["id"])
        assert frozen["status"] == "待审核", "审核通过标记不得残留"
        assert frozen["pending"] is True, "退回结果要送到方案待办"
        assert frozen["abnormal"] is True, "冻结期间方案处于争议中"
        assert "承接资格冻结" in frozen["备注说明"]
        assert "冻结" in msg and "方案待办" in msg

    def test_return_only_freezes_plans_of_this_exam(self):
        exam, plan = make_exam_with_plan(self.exam_svc, self.plan_svc)
        self.plan_svc.run_action(plan["id"], "审核方案")

        # 教学反馈来源的方案：继续办理，不受本场考试退回影响
        fb_svc = FeedbackService()
        fb, _ = fb_svc.create_entry({
            "反馈对象": "王老师", "反馈要点": "互动不足", "改进建议": "增加提问",
        })
        fb_svc.run_action(fb["id"], "签收反馈")
        fb_plan, _ = self.plan_svc.create_entry({
            "关联反馈": f"feedback:{fb['id']}",
            "改进目标": "提升互动",
            "改进措施": "每节课两次提问",
        })
        self.plan_svc.run_action(fb_plan["id"], "审核方案")

        # 另一场考试（编号含本场编号前缀，防止 exam:1 误匹配 exam:11 类问题）
        other_exam, other_plan = make_exam_with_plan(
            self.exam_svc, self.plan_svc, name="E-22 阶段测验")
        self.plan_svc.run_action(other_plan["id"], "审核方案")

        self.exam_svc.run_action(exam["id"], "退回重填")

        assert store.find("improve_plan", plan["id"])["status"] == "待审核"
        assert store.find("improve_plan", fb_plan["id"])["status"] == "审核通过", \
            "教学反馈产生的其他方案继续办理"
        assert store.find("improve_plan", other_plan["id"])["status"] == "审核通过", \
            "只变更本场考试的方案审核资格"

    def test_task_registration_rejected_after_exam_return(self):
        exam, plan = make_exam_with_plan(self.exam_svc, self.plan_svc)
        self.plan_svc.run_action(plan["id"], "审核方案")
        self.exam_svc.run_action(exam["id"], "退回重填")

        with pytest.raises(ValueError, match="跨模块校验失败"):
            self.task_svc.create_entry({
                "任务描述": "调整命题策略",
                "承接方案": f"plan:{plan['id']}",
                "执行人员": "赵老师",
            })

    def test_disputed_plan_cannot_dispatch_new_task(self):
        exam, plan = make_exam_with_plan(self.exam_svc, self.plan_svc)
        self.plan_svc.run_action(plan["id"], "审核方案")
        self.exam_svc.run_action(exam["id"], "退回重填")
        # 冻结中的方案即便被强行改回审核通过，争议标记仍拦截派工
        row = store.find("improve_plan", plan["id"])
        row["status"] = "审核通过"
        with pytest.raises(ValueError, match="争议"):
            self.task_svc.create_entry({
                "任务描述": "争议材料派工",
                "承接方案": f"plan:{plan['id']}",
                "执行人员": "赵老师",
            })


class TestReReviewFlow:
    """E-21 复核场景：按旧审核复核，或带到更正后的样本上核对后再办。"""

    def setup_method(self):
        clear_all()
        self.exam_svc = ExamAnalysisService()
        self.plan_svc = ImprovePlanService()
        self.task_svc = ImproveTaskService()

    def test_return_keeps_old_review_trail_for_recheck(self):
        exam, plan = make_exam_with_plan(self.exam_svc, self.plan_svc)
        self.plan_svc.run_action(plan["id"], "审核方案")
        self.exam_svc.run_action(exam["id"], "退回重填")

        entry = store.find("exam_analysis", exam["id"])
        trail = entry.get("审核留痕")
        assert trail and trail[0]["处理"] == "审核分析"
        assert trail[0]["意见"] == "分析人初审意见：区分度偏低，建议复核", \
            "旧审核意见必须留痕，供主任复核时对照"
        assert trail[1]["处理"] == "退回重填"

    def test_re_review_references_old_review_and_grants_single_qualification(self):
        exam, plan = make_exam_with_plan(self.exam_svc, self.plan_svc)
        self.plan_svc.run_action(plan["id"], "审核方案")
        self.exam_svc.run_action(exam["id"], "退回重填")

        # 更正样本后再次审核：按复核办理，且不得重复授予承接资格
        result, msg = self.exam_svc.run_action(exam["id"], "审核分析")
        assert result is not None and result["status"] == "审核通过"
        assert "复核" in msg and "第1次审核" in msg

        plans = [
            row for row in store.rows("improve_plan")
            if str(row.get("关联反馈", "")) == f"exam:{exam['id']}"
        ]
        assert len(plans) == 1, "结果只能授予一份有效处理资格"
        assert plans[0]["status"] == "待审核", "复核不自动恢复资格，须主任在方案待办复核"

        # 主任对照更正后的样本复核方案，恢复承接资格
        self.plan_svc.run_action(plan["id"], "审核方案")
        restored = store.find("improve_plan", plan["id"])
        assert restored["status"] == "审核通过"
        assert restored["abnormal"] is False

        task, missing = self.task_svc.create_entry({
            "任务描述": "调整命题策略",
            "承接方案": f"plan:{plan['id']}",
            "执行人员": "赵老师",
        })
        assert task is not None and missing == []

    def test_plan_re_review_by_old_review_before_corrected_sample(self):
        """主任也可不等更正样本，直接按旧审核把方案送去复核。"""
        exam, plan = make_exam_with_plan(self.exam_svc, self.plan_svc)
        self.plan_svc.run_action(plan["id"], "审核方案")
        self.exam_svc.run_action(exam["id"], "退回重填")

        # 样本仍在更正（考试还是草稿），主任按旧审核意见复核方案
        self.plan_svc.run_action(plan["id"], "审核方案")
        row = store.find("improve_plan", plan["id"])
        assert row["status"] == "审核通过"
        assert row["abnormal"] is False


class TestExamActionIdempotency:
    """重试原审核或退回操作时，能辨认同一次的处理结果。"""

    def setup_method(self):
        clear_all()
        self.exam_svc = ExamAnalysisService()
        self.plan_svc = ImprovePlanService()

    def test_retry_review_identifies_same_result(self):
        exam, plan = make_exam_with_plan(self.exam_svc, self.plan_svc)
        result, msg = self.exam_svc.run_action(exam["id"], "审核分析")
        assert result is None
        assert "无需重复审核" in msg and "同一处理结果" in msg
        plans = [
            row for row in store.rows("improve_plan")
            if str(row.get("关联反馈", "")) == f"exam:{exam['id']}"
        ]
        assert len(plans) == 1, "重复审核不得重复授予承接资格"

    def test_retry_return_identifies_same_result(self):
        exam, plan = make_exam_with_plan(self.exam_svc, self.plan_svc)
        self.plan_svc.run_action(plan["id"], "审核方案")
        self.exam_svc.run_action(exam["id"], "退回重填")

        result, msg = self.exam_svc.run_action(exam["id"], "退回重填")
        assert result is None
        assert "无需重复退回" in msg and "同一处理结果" in msg

        frozen = store.find("improve_plan", plan["id"])
        assert frozen["status"] == "待审核"
        assert frozen["备注说明"].count("承接资格冻结") == 1, "重复退回不得重复留痕"

    def test_fresh_draft_return_is_noop(self):
        exam, _ = self.exam_svc.create_entry({
            "考试名称": "E-23 随堂测", "任课教师": "郑老师", "参考人数": "30",
        })
        result, msg = self.exam_svc.run_action(exam["id"], "退回重填")
        assert result is None
        assert "无需退回重填" in msg


class TestExamArchiveFlow:
    """后续状态流转：存档必须先审核通过；已结项的不可退回，仍可按原编号回查。"""

    def setup_method(self):
        clear_all()
        self.exam_svc = ExamAnalysisService()
        self.plan_svc = ImprovePlanService()

    def test_archive_requires_approval(self):
        exam, _ = self.exam_svc.create_entry({
            "考试名称": "E-24 期中考试", "任课教师": "郑老师", "参考人数": "80",
        })
        result, msg = self.exam_svc.run_action(exam["id"], "存档分析")
        assert result is None
        assert "先审核通过" in msg

    def test_archived_exam_cannot_return_and_stays_queryable(self):
        exam, plan = make_exam_with_plan(self.exam_svc, self.plan_svc)
        self.plan_svc.run_action(plan["id"], "审核方案")
        result, _ = self.exam_svc.run_action(exam["id"], "存档分析")
        assert result["status"] == "已存档"

        result, msg = self.exam_svc.run_action(exam["id"], "退回重填")
        assert result is None
        assert "不可退回重填" in msg

        # 已结项的仍按归档时的方案编号和试卷样本回查
        archived = store.find("exam_analysis", exam["id"])
        assert archived is not None and archived["考试名称"] == "E-21 期末统考"
        assert store.find("improve_plan", plan["id"])["status"] == "审核通过"

        result, msg = self.exam_svc.run_action(exam["id"], "存档分析")
        assert result is None
        assert "无需重复存档" in msg


class TestPlanQualificationCheckEnforced:
    """诊改登记必须核对关联方案的承接资格（校验不再被吞掉）。"""

    def setup_method(self):
        clear_all()
        self.task_svc = ImproveTaskService()
        self.plan_svc = ImprovePlanService()
        self.fb_svc = FeedbackService()

    def _signed_plan(self):
        fb, _ = self.fb_svc.create_entry({
            "反馈对象": "王老师", "反馈要点": "作业正确率低", "改进建议": "增加练习",
        })
        self.fb_svc.run_action(fb["id"], "签收反馈")
        plan, _ = self.plan_svc.create_entry({
            "关联反馈": f"feedback:{fb['id']}",
            "改进目标": "正确率提升至85%",
            "改进措施": "增加课堂练习",
        })
        return plan

    def test_pending_plan_cannot_accept_task(self):
        plan = self._signed_plan()
        with pytest.raises(ValueError, match="跨模块校验失败"):
            self.task_svc.create_entry({
                "任务描述": "提前派工", "承接方案": f"plan:{plan['id']}", "执行人员": "赵老师",
            })

    def test_rejected_plan_cannot_accept_task(self):
        plan = self._signed_plan()
        self.plan_svc.run_action(plan["id"], "驳回方案")
        with pytest.raises(ValueError, match="跨模块校验失败"):
            self.task_svc.create_entry({
                "任务描述": "驳回方案派工", "承接方案": f"plan:{plan['id']}", "执行人员": "赵老师",
            })

    def test_missing_plan_cannot_accept_task(self):
        with pytest.raises(ValueError, match="跨模块校验失败"):
            self.task_svc.create_entry({
                "任务描述": "幽灵方案派工", "承接方案": "plan:9999", "执行人员": "赵老师",
            })

    def test_approved_plan_accepts_task(self):
        plan = self._signed_plan()
        self.plan_svc.run_action(plan["id"], "审核方案")
        task, missing = self.task_svc.create_entry({
            "任务描述": "正常派工", "承接方案": f"plan:{plan['id']}", "执行人员": "赵老师",
        })
        assert task is not None and missing == []

    def test_plan_rejects_unsigned_feedback_reference(self):
        fb, _ = self.fb_svc.create_entry({
            "反馈对象": "王老师", "反馈要点": "互动不足", "改进建议": "增加提问",
        })
        with pytest.raises(ValueError, match="跨模块校验失败"):
            self.plan_svc.create_entry({
                "关联反馈": f"feedback:{fb['id']}",
                "改进目标": "提升互动",
                "改进措施": "每节课两次提问",
            })
