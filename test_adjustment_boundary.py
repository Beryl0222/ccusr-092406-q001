"""奖励调整与资金流水边界回归。

修复目标：追加决定只改变当前应付上限，补付/追回必须基于此前真实支付额。
覆盖：
1. 零付款降额：不产生资金记录，只压低可付上限；
2. 部分付款后降额：追回不超过已付超额；已付仍低于新上限时不追回；
3. 先降后升：升额不绕过支付岗位自动出款，只形成可付余额，跨会签线仍须会签；
4. 调整驳回/在途审核不影响余额，在途期间冻结支付；
5. 并发竞态：支付与调整、支付与支付只能得到一个可解释顺序，余额恒成立。
"""

import threading
import unittest

from reward_center import (
    RewardCenter, DomainError, PermissionDenied, InvalidStateError,
    ROLE_INTAKE as INTAKE, ROLE_HANDLER as HANDLER,
    ROLE_REVIEWER as REVIEWER, ROLE_FINANCE as FINANCE,
    ROLE_PAYER as PAYER,
)

# 价格违法（严重度 0.7）二级举报：
# 100 万罚没 × 4% × 0.7 = 28,000 元（低于会签线，便于多数场景）
INITIAL_AMOUNT = 28_000
LOWER_PENALTY = 500_000          # 50 万 × 4% × 0.7 = 14,000
LOWER_AMOUNT = 14_000
HIGH_PENALTY = 8_000_000         # 800 万 × 4% × 0.7 = 22.4 万，跨会签线
HIGH_AMOUNT = 224_000


def settled_decision(penalty=1_000_000, grade=2, category="价格违法",
                     anonymous=False):
    """走完 受理→结案→可奖励→认定→建议→审核（→会签），返回生效决定。"""
    center = RewardCenter(today=lambda: "2026-09-22")
    identity = None if anonymous else {"name": "测试举报人"}
    alias, case_id, code = center.intake_report(
        "intake-1", INTAKE, category, ["事实A：价格串通"],
        received_at="2026-02-01", identity=identity)
    center.close_case(case_id, penalty, closed_at="2026-03-01")
    center.enter_reward_stage(case_id, entered_at="2026-03-05")
    center.assess_contributions(case_id, [
        {"alias": alias, "grade": grade, "new_facts": ["事实A：价格串通"]}
    ], "intake-1", INTAKE)
    did = center.propose_rewards(case_id, "handler-1", HANDLER)[0]
    center.approve_decision(did, "reviewer-1", REVIEWER)
    if center.decisions[did]["needs_cosign"]:
        center.cosign_decision(did, "finance-1", FINANCE)
    assert center.decisions[did]["status"] == "已生效"
    return center, alias, case_id, did, code


def person_view(center, case_id):
    return center.explain_case(case_id)["reporters"][0]


def lower_to(center, did, penalty=LOWER_PENALTY, approve=True):
    adj_id = center.adjust_decision(
        did, "reconsideration", "handler-1", HANDLER,
        new_penalty_amount=penalty, changed_at="2026-06-01")
    if approve:
        center.review_adjustment(adj_id, "reviewer-2", REVIEWER)
    return adj_id


class ZeroPaymentReductionTest(unittest.TestCase):
    """零付款降额：不得写入负数追回。"""

    def test_no_funds_record_until_real_payment(self):
        c, alias, case_id, did, _ = settled_decision()
        self.assertEqual(c.decisions[did]["amount"], INITIAL_AMOUNT)

        adj_id = lower_to(c, did)
        adj = c.adjustments[adj_id]
        self.assertEqual(adj["new_amount"], LOWER_AMOUNT)
        self.assertEqual(adj["clawback_amount"], 0)
        # 未付款：生效不产生任何资金记录
        self.assertEqual(c.payments, [])

        view = person_view(c, case_id)
        self.assertEqual(view["approved_amount"], LOWER_AMOUNT)
        self.assertEqual(view["paid_amount"], 0)
        self.assertEqual(view["payable_balance"], LOWER_AMOUNT)
        self.assertEqual(view["recoverable_amount"], 0)
        self.assertEqual(view["paid_total"], 0)
        self.assertEqual(view["payments"], [])

    def test_later_real_payment_uses_lowered_cap(self):
        c, alias, case_id, did, code = settled_decision(anonymous=True)
        lower_to(c, did)
        # 后续真正付款沿新上限计算：超过 14,000 不可付
        with self.assertRaises(DomainError):
            c.pay_decision(did, "payer-1", PAYER, amount=INITIAL_AMOUNT,
                           claim_code=code)
        record = c.pay_decision(did, "payer-1", PAYER, claim_code=code)
        self.assertEqual(record["amount"], LOWER_AMOUNT)
        view = person_view(c, case_id)
        self.assertEqual(view["paid_amount"], LOWER_AMOUNT)
        self.assertEqual(view["payable_balance"], 0)

    def test_pending_reduction_does_not_change_balance(self):
        c, alias, case_id, did, _ = settled_decision()
        # 发起降额但尚未审核：余额仍按原核准额
        lower_to(c, did, approve=False)
        view = person_view(c, case_id)
        self.assertEqual(view["approved_amount"], INITIAL_AMOUNT)
        self.assertEqual(view["payable_balance"], INITIAL_AMOUNT)
        self.assertEqual(c.payments, [])
        # 在途调整期间冻结支付，避免沿将被压低的上限多付
        with self.assertRaises(InvalidStateError):
            c.pay_decision(did, "payer-1", PAYER)


class PartialPaymentReductionTest(unittest.TestCase):
    """部分付款后降额：追回只能针对已付超额。"""

    def test_clawback_capped_at_overpayment(self):
        c, alias, case_id, did, _ = settled_decision()
        c.pay_decision(did, "payer-1", PAYER, amount=20_000)
        # 旧实现会按 delta=-14,000 追回；正确追回仅为已付超额 20,000-14,000
        adj_id = lower_to(c, did)
        self.assertEqual(c.adjustments[adj_id]["clawback_amount"], 6_000)

        clawbacks = [p for p in c.payments if p["amount"] < 0]
        self.assertEqual(len(clawbacks), 1)
        self.assertEqual(clawbacks[0]["amount"], -6_000)
        self.assertEqual(clawbacks[0]["note"], "追回")
        self.assertNotIn("补付", [p.get("note") for p in c.payments])

        view = person_view(c, case_id)
        self.assertEqual(view["approved_amount"], LOWER_AMOUNT)
        self.assertEqual(view["paid_amount"], LOWER_AMOUNT)  # 20,000-6,000
        self.assertEqual(view["payable_balance"], 0)
        self.assertEqual(view["recoverable_amount"], 0)

    def test_partial_below_new_cap_creates_no_record(self):
        c, alias, case_id, did, _ = settled_decision()
        c.pay_decision(did, "payer-1", PAYER, amount=5_000)
        payments_before = len(c.payments)
        lower_to(c, did)
        # 已付 5,000 仍低于新核准 14,000：无追回、无任何新资金记录
        self.assertEqual(len(c.payments), payments_before)
        view = person_view(c, case_id)
        self.assertEqual(view["paid_amount"], 5_000)
        self.assertEqual(view["payable_balance"], 9_000)
        self.assertEqual(view["recoverable_amount"], 0)
        # 余额可继续补付，支付岗位逐笔操作
        c.pay_decision(did, "payer-1", PAYER, amount=9_000)
        self.assertEqual(person_view(c, case_id)["paid_amount"], LOWER_AMOUNT)

    def test_recoverable_reported_before_settlement(self):
        # 已生效降额若已付超额，结算后立即追回；四字段口径自洽
        c, alias, case_id, did, _ = settled_decision()
        c.pay_decision(did, "payer-1", PAYER)  # 全额 28,000
        lower_to(c, did)
        view = person_view(c, case_id)
        self.assertEqual(view["approved_amount"], LOWER_AMOUNT)
        self.assertEqual(view["paid_amount"], LOWER_AMOUNT)
        self.assertEqual(view["recoverable_amount"], 0)


class DecreaseThenIncreaseTest(unittest.TestCase):
    """先降后升：升额只放开上限，绝不由系统自动出款。"""

    def test_increase_after_full_clawback_pays_through_payment_role(self):
        c, alias, case_id, did, _ = settled_decision()
        c.pay_decision(did, "payer-1", PAYER)
        # 降为 0（重复举报确认）：全额追回 28,000
        down = c.adjust_decision(did, "duplicate", "handler-1", HANDLER)
        c.review_adjustment(down, "reviewer-2", REVIEWER)
        self.assertEqual(person_view(c, case_id)["paid_amount"], 0)
        ledger_count_after_down = len(c.payments)

        # 再升回 28,000
        up = c.adjust_decision(
            did, "reconsideration", "handler-1", HANDLER,
            new_penalty_amount=1_000_000, changed_at="2026-07-01")
        self.assertEqual(c.adjustments[up]["new_amount"], INITIAL_AMOUNT)
        c.review_adjustment(up, "reviewer-2", REVIEWER)

        self.assertEqual(c.adjustments[up]["clawback_amount"], 0)
        # 关键：升额不自动补付，资金流水数量不变，没有 "system-adjustment" 正向出款
        self.assertEqual(len(c.payments), ledger_count_after_down)
        self.assertFalse(any(p["amount"] > 0 and p["paid_by"] == "system-adjustment"
                             for p in c.payments))
        view = person_view(c, case_id)
        self.assertEqual(view["approved_amount"], INITIAL_AMOUNT)
        self.assertEqual(view["paid_amount"], 0)
        self.assertEqual(view["payable_balance"], INITIAL_AMOUNT)

        # 只能由支付执行人出款
        with self.assertRaises(PermissionDenied):
            c.pay_decision(did, "handler-1", HANDLER)
        c.pay_decision(did, "payer-1", PAYER)
        self.assertEqual(person_view(c, case_id)["paid_amount"], INITIAL_AMOUNT)

    def test_increase_crossing_cosign_waits_for_finance(self):
        c, alias, case_id, did, _ = settled_decision()
        down = c.adjust_decision(did, "duplicate", "handler-1", HANDLER)
        c.review_adjustment(down, "reviewer-1", REVIEWER)
        up = c.adjust_decision(
            did, "judgment", "handler-1", HANDLER,
            new_penalty_amount=HIGH_PENALTY, changed_at="2026-07-01")
        self.assertTrue(c.adjustments[up]["needs_cosign"])
        c.review_adjustment(up, "reviewer-1", REVIEWER)
        # 会签前：升额尚未生效，仍无可付余额，也不自动出款
        self.assertEqual(c.adjustments[up]["status"], "待调整会签")
        self.assertEqual(person_view(c, case_id)["payable_balance"], 0)
        c.cosign_adjustment(up, "finance-1", FINANCE)
        view = person_view(c, case_id)
        self.assertEqual(view["approved_amount"], HIGH_AMOUNT)
        self.assertEqual(view["paid_amount"], 0)
        self.assertEqual(view["payable_balance"], HIGH_AMOUNT)
        self.assertEqual(c.payments, [])  # 全程没有系统自动出款


class RejectedAdjustmentTest(unittest.TestCase):
    """被驳回或尚在审核的调整不影响余额，原结论维持。"""

    def test_rejected_keeps_balance_and_allows_payment(self):
        c, alias, case_id, did, _ = settled_decision()
        adj_id = lower_to(c, did, approve=False)
        c.review_adjustment(adj_id, "reviewer-2", REVIEWER, approve=False)
        self.assertEqual(c.adjustments[adj_id]["status"], "已驳回")
        self.assertEqual(c.adjustments[adj_id]["clawback_amount"], None)
        self.assertEqual(c.payments, [])
        view = person_view(c, case_id)
        self.assertEqual(view["approved_amount"], INITIAL_AMOUNT)
        self.assertEqual(view["payable_balance"], INITIAL_AMOUNT)
        self.assertEqual(view["adjustments"][0]["status"], "已驳回")
        # 驳回后可正常全额支付
        c.pay_decision(did, "payer-1", PAYER)
        self.assertEqual(person_view(c, case_id)["paid_amount"], INITIAL_AMOUNT)

    def test_chain_is_append_only_with_original_intact(self):
        c, alias, case_id, did, _ = settled_decision()
        rejected = lower_to(c, did, approve=False)
        c.review_adjustment(rejected, "reviewer-2", REVIEWER, approve=False)
        approved = lower_to(c, did)
        # 原决定金额原样保留；两次调整均在谱系中
        self.assertEqual(c.decisions[did]["amount"], INITIAL_AMOUNT)
        self.assertEqual(c.decisions[did]["superseded_by"], rejected)
        chain = [a["adjustment_id"] for a in c._adjustment_chain(c.decisions[did])]
        self.assertEqual(chain, [rejected, approved])
        self.assertEqual(c.adjustments[approved]["prev_adjustment_id"], rejected)
        view = person_view(c, case_id)
        self.assertEqual(view["current_decision"]["amount"], INITIAL_AMOUNT)
        self.assertEqual(view["approved_amount"], LOWER_AMOUNT)


class ConcurrentPayAndAdjustmentTest(unittest.TestCase):
    """并发：支付与调整生效只能得到一个可解释顺序，余额恒成立。"""

    def _fresh(self):
        return settled_decision()

    def test_deterministic_both_orders(self):
        # 顺序一：支付先完成，降额后结算 → 28,000 支付 + 28,000 追回
        c, _, case_id, did, _ = self._fresh()
        c.pay_decision(did, "payer-1", PAYER)
        adj = c.adjust_decision(did, "duplicate", "handler-1", HANDLER)
        c.review_adjustment(adj, "reviewer-1", REVIEWER)
        view = person_view(c, case_id)
        self.assertEqual([p["amount"] for p in view["payments"]],
                         [INITIAL_AMOUNT, -INITIAL_AMOUNT])
        self.assertEqual(view["paid_amount"], 0)

        # 顺序二：降额先生效（未付款），支付被新上限挡住 → 无资金记录
        c2, _, case_id2, did2, _ = self._fresh()
        adj2 = c2.adjust_decision(did2, "duplicate", "handler-1", HANDLER)
        c2.review_adjustment(adj2, "reviewer-1", REVIEWER)
        with self.assertRaises(DomainError):
            c2.pay_decision(did2, "payer-1", PAYER)
        self.assertEqual(c2.payments, [])
        self.assertEqual(person_view(c2, case_id2)["payable_balance"], 0)

    def test_concurrent_pay_vs_adjustment_hammer(self):
        # 高并发下不变量必须恒成立；两种先后顺序的具体数值已由
        # test_deterministic_both_orders 逐一断言，此处不要求调度一定
        # 均匀产生两种顺序（依赖线程调度，强求会导致偶发失败）。
        observed = {"pay_first": 0, "adjust_first": 0}
        for _ in range(60):
            c, _, case_id, did, _ = self._fresh()
            barrier = threading.Barrier(2)
            outcome = {}

            def do_pay():
                barrier.wait()
                try:
                    c.pay_decision(did, "payer-1", PAYER, amount=INITIAL_AMOUNT)
                    outcome["pay"] = "paid"
                except DomainError:
                    # 在途调整冻结支付（InvalidStateError）或新上限为 0
                    outcome["pay"] = "blocked"

            def do_adjust():
                barrier.wait()
                adj = c.adjust_decision(did, "duplicate", "handler-1", HANDLER)
                c.review_adjustment(adj, "reviewer-1", REVIEWER)
                outcome["adjust"] = "effective"

            threads = [threading.Thread(target=do_pay),
                       threading.Thread(target=do_adjust)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(outcome["adjust"], "effective")
            observed["pay_first" if outcome["pay"] == "paid"
                     else "adjust_first"] += 1

            # 无论哪个顺序，最终口径必须自洽：核准 0 / 净已付 0
            view = person_view(c, case_id)
            self.assertEqual(view["approved_amount"], 0)
            self.assertEqual(view["paid_amount"], 0)
            self.assertEqual(view["payable_balance"], 0)
            self.assertEqual(view["recoverable_amount"], 0)
            ledger = view["payments"]
            if outcome["pay"] == "paid":
                # 支付先成功 → 调整生效时按真实已付全额追回，恰好对冲
                self.assertEqual([p["amount"] for p in ledger],
                                 [INITIAL_AMOUNT, -INITIAL_AMOUNT])
            else:
                # 调整先入在途/先生效 → 支付被冻结或被零上限挡住，无任何记录
                self.assertEqual(ledger, [])
            # 全局顺序号唯一递增，先后可解释
            seqs = [p["ledger_seq"] for p in ledger]
            self.assertEqual(seqs, sorted(seqs))
            self.assertEqual(len(seqs), len(set(seqs)))
        # 锁保护下任何一种实际发生的调度都通过了上面的不变量校验
        self.assertEqual(observed["pay_first"] + observed["adjust_first"], 60)

    def test_concurrent_two_payers_only_one_succeeds(self):
        for _ in range(20):
            c, _, case_id, did, _ = self._fresh()
            barrier = threading.Barrier(2)
            results = []

            def do_pay():
                barrier.wait()
                try:
                    c.pay_decision(did, "payer-1", PAYER, amount=INITIAL_AMOUNT)
                    results.append("ok")
                except DomainError:
                    results.append("short")

            threads = [threading.Thread(target=do_pay),
                       threading.Thread(target=do_pay)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(sorted(results), ["ok", "short"])
            self.assertEqual(len(c.payments), 1)
            self.assertEqual(c.payments[0]["amount"], INITIAL_AMOUNT)
            view = person_view(c, case_id)
            self.assertEqual(view["paid_amount"], INITIAL_AMOUNT)
            self.assertEqual(view["payable_balance"], 0)


if __name__ == "__main__":
    unittest.main()
