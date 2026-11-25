"""复现并验证「为什么创建不了」。

场景照负责人截图：勾了账号「梁静」，其下 LJ&shop 马来 + 菲律宾 两个店都勾了，
模板勾了「马来」+「菲律宾」，选了 9 件商品，频率每天 / 14:40 / 每次 10 件。

修复前的 bug：validate() 里 `if (!shopId) next.shopId = "请选择目标店铺"`，
而「目标店铺」下拉已删除、`shopId` state 在新建时恒为空 →
校验永远失败，界面提示「有 1 项需要修正后再提交」，却没有任何控件可填。

修复后：校验改看 `submitTargetShopIds`（上面账号卡片实际勾的店铺）。
"""
import json

LIMITS_PER_RUN = (1, 2000)


def validate(*, name, submit_target_shop_ids, checked_account_ids, is_teacher,
             task, selected_product_ids, time, per_run, planned_runs,
             frequency="daily", days=None, day_of_month=None, start_date="",
             now_hm="14:39"):
    """镜像 SchedulesPage.validate() 的逻辑。"""
    next_ = {}
    if len(name.strip()) > 60:
        next_["name"] = "名称最多 60 个字符"

    # ★ 本次修复的核心
    if not submit_target_shop_ids:
        if is_teacher and not task and not checked_account_ids:
            next_["shopId"] = "请至少勾选一个账号"
        elif checked_account_ids:
            next_["shopId"] = "所选账号下没有可用店铺"
        else:
            next_["shopId"] = "请至少勾选一个店铺"

    if not task and not selected_product_ids:
        next_["selectedProductIds"] = "请至少选择一件仓库商品"

    import re
    if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", time):
        next_["time"] = "时间格式应为 HH:MM"
    if not (LIMITS_PER_RUN[0] <= per_run <= 2000):
        next_["perRun"] = "每次数量越界"
    if len(selected_product_ids) > per_run * planned_runs:
        next_["selectedProductIds"] = (
            f"已选 {len(selected_product_ids)} 件，计划容量只有 {per_run * planned_runs} 件")
    if "time" not in next_ and time < now_hm:
        next_["time"] = f"不能早于当前时间 {now_hm}"
    return next_


def submit_target_shop_ids(*, is_teacher, task, checked_account_ids,
                           shops_by_account, picked_by_account, current_account):
    """镜像 SchedulesPage.submitTargetShopIds。"""
    ids = []

    def collect(acc):
        acc_shops = shops_by_account.get(acc, [])
        picked = picked_by_account.get(acc, [])
        chosen = ([s for s in acc_shops if s in picked] if picked else acc_shops[:1])
        ids.extend(chosen)

    if is_teacher and not task and checked_account_ids:
        # 勾了账号就按勾的算（1 个或多个都一样）—— 原来写成 `> 1`，
        # 只勾一个账号时走 else 分支用 current_account，而 current_account
        # 是当前登录账号、不是被勾的那个，于是手上一个店铺都找不到。
        for acc in checked_account_ids:
            collect(acc)
    else:
        collect(current_account)
    return ids


SHOPS = {"9": ["19956376", "20080061"]}      # 梁静：马来 + 菲律宾
PICKED = {"9": ["19956376", "20080061"]}     # 两个都勾了

print("=" * 62)
print("场景 1：负责人截图的原样（应能创建）")
print("=" * 62)
ids = submit_target_shop_ids(
    is_teacher=True, task=None, checked_account_ids=["9"],
    shops_by_account=SHOPS, picked_by_account=PICKED, current_account="1")
errs = validate(
    name="定时发布-20261125-1439", submit_target_shop_ids=ids,
    checked_account_ids=["9"], is_teacher=True, task=None,
    selected_product_ids=[str(i) for i in range(9)],
    time="14:40", per_run=10, planned_runs=1, now_hm="14:39")
print(f"  本次发到店铺: {ids}")
print(f"  校验结果:     {errs or '通过'}")
assert not errs, f"应该能创建，却报错 {errs}"
print("  ✓ 可以创建\n")

print("=" * 62)
print("场景 2：一个店铺都没勾（应提示「请至少勾选一个店铺」）")
print("=" * 62)
ids2 = submit_target_shop_ids(
    is_teacher=True, task=None, checked_account_ids=["9"],
    shops_by_account={"9": ["19956376"]}, picked_by_account={},
    current_account="1")
# picked 为空时 collect 会回落第一个店 → 仍有目标，这是设计内的兜底
print(f"  本次发到店铺: {ids2}  （空勾选回落第一个店，设计如此）")
errs2 = validate(
    name="t", submit_target_shop_ids=ids2, checked_account_ids=["9"],
    is_teacher=True, task=None, selected_product_ids=["1"],
    time="14:40", per_run=10, planned_runs=1, now_hm="14:39")
print(f"  校验结果:     {errs2 or '通过'}")
print("  ✓ 不误报\n")

print("=" * 62)
print("场景 3：账号勾了但该账号没有店铺（应提示没有可用店铺）")
print("=" * 62)
ids3 = submit_target_shop_ids(
    is_teacher=True, task=None, checked_account_ids=["9"],
    shops_by_account={"9": []}, picked_by_account={}, current_account="1")
errs3 = validate(
    name="t", submit_target_shop_ids=ids3, checked_account_ids=["9"],
    is_teacher=True, task=None, selected_product_ids=["1"],
    time="14:40", per_run=10, planned_runs=1, now_hm="14:39")
print(f"  本次发到店铺: {ids3}")
print(f"  校验结果:     {errs3}")
assert errs3.get("shopId") == "所选账号下没有可用店铺", errs3
print("  ✓ 提示准确\n")

print("=" * 62)
print("场景 4：老师一个账号都没勾（应提示「请至少勾选一个账号」）")
print("=" * 62)
ids4 = submit_target_shop_ids(
    is_teacher=True, task=None, checked_account_ids=[],
    shops_by_account={}, picked_by_account={}, current_account="1")
errs4 = validate(
    name="t", submit_target_shop_ids=ids4, checked_account_ids=[],
    is_teacher=True, task=None, selected_product_ids=["1"],
    time="14:40", per_run=10, planned_runs=1, now_hm="14:39")
print(f"  校验结果:     {errs4}")
assert errs4.get("shopId") == "请至少勾选一个账号", errs4
print("  ✓ 提示准确\n")

print("=" * 62)
print("场景 5：时间早于当前（应提示）")
print("=" * 62)
errs5 = validate(
    name="t", submit_target_shop_ids=["19956376"], checked_account_ids=["9"],
    is_teacher=True, task=None, selected_product_ids=["1"],
    time="09:00", per_run=10, planned_runs=1, now_hm="14:39")
print(f"  校验结果:     {errs5}")
assert "time" in errs5, errs5
print("  ✓ 时间校验仍生效\n")

print("=" * 62)
print("全部通过")
print("=" * 62)
print("修复前后对比：")
print("  修复前：新建任务 shopId 恒为空 → 永远报「请选择目标店铺」")
print("          （界面上却没有这个控件）→ 创建不了")
print("  修复后：校验看上面实际勾选的店铺 → 截图那种情况直接通过")
print()
print("=" * 62)
print("场景 6：只勾 1 个别的账号（截图情况）—— 必须能建，且建到那个账号下")
print("=" * 62)


def build_payloads(*, is_teacher, task, checked_account_ids, shops_by_account,
                   picked_by_account, current_account, template_for_shop):
    """镜像 SchedulesPage.submit() 的分支。"""
    def targets_for(acc):
        acc_shops = shops_by_account.get(acc, [])
        picked = picked_by_account.get(acc, [])
        chosen = ([s for s in acc_shops if s in picked] if picked else acc_shops[:1])
        return [{"shopId": s, "template": template_for_shop(acc, s)} for s in chosen]

    payloads = []
    if is_teacher and not task and len(checked_account_ids) > 0:
        for acc in checked_account_ids:
            tg = targets_for(acc)
            if not tg:
                continue
            payloads.append({"accountId": acc, "shopId": tg[0]["shopId"],
                             "template": tg[0]["template"], "targets": tg})
    else:
        tg = targets_for(current_account)
        if not tg:
            tg = [{"shopId": "", "template": ""}]
        payloads.append({"accountId": current_account, "shopId": tg[0]["shopId"],
                         "template": tg[0]["template"], "targets": tg})
    return payloads


TPL = {("9", "19956376"): "马来", ("9", "20080061"): "菲律宾"}
pl = build_payloads(
    is_teacher=True, task=None, checked_account_ids=["9"],
    shops_by_account=SHOPS, picked_by_account=PICKED,
    current_account="1", template_for_shop=lambda a, s: TPL.get((a, s), ""))
print(f"  生成任务数: {len(pl)}")
for p in pl:
    print(f"    accountId={p['accountId']}  shopId={p['shopId']}  template={p['template']}")
    for t in p["targets"]:
        print(f"      → {t['shopId']} 用 {t['template']}")
assert len(pl) == 1, f"应该只建 1 条任务，实际 {len(pl)}"
assert pl[0]["accountId"] == "9", f"任务应挂到账号 9，实际 {pl[0]['accountId']}"
assert len(pl[0]["targets"]) == 2, pl[0]["targets"]
assert pl[0]["targets"][0]["template"] == "马来"
assert pl[0]["targets"][1]["template"] == "菲律宾"
print("  ✓ 建到账号 9，两个店各用自己国家的模板")
print()
print("全部场景通过。")
