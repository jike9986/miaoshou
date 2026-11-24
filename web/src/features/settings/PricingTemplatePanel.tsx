// features/settings —— 定价模板设置面板
//
// 负责人 2026-11-23：「定价模板加到设置里面，能够让用户自己调节里面的东西，
// 布局可以按照妙手这里来，左边空右边有个价格计算，像这样一个界面里面
// 切换各个模板」。
//
// 所以这里是**内联**的编辑器，不是弹窗：设置页里直接铺开，左边参数、
// 右边试算，顶部下拉切换模板。弹窗形态（PriceTemplateDialog）留着，
// 自动化页那边还要用（那里是「边处理边改模板」的场景，弹窗更合适）。
//
// 实体只有一份：`PriceTemplateEditor`。两个入口共用它，避免参数列表
// 在两边各写一遍然后漂移 —— 之前就是因为只在一个地方加了字段，另一个
// 入口改不了，用户看到的价一直对不上。

import { PriceTemplateEditor } from "@/features/warehouse/PriceTemplateEditor";

export function PricingTemplatePanel() {
  return <PriceTemplateEditor />;
}