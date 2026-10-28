// design-system §6.1 时长与缓动 —— 逐字对应文档，禁止新增曲线/时长
// 使用原则（§6.3）：进入慢于退出（退出 ≈ 进入 × 0.7）；位移 ≤ 8px（抽屉 ≤ 24px）；
// 同时动画属性 ≤ 3 个；不做数字滚动、不做循环呼吸（审核中状态点除外）。

import type { Transition, Variants } from "framer-motion";

/** 缓动曲线（§6.1） */
export const ease = {
  out: [0.16, 1, 0.3, 1] as const,
  in: [0.4, 0, 1, 1] as const,
  inOut: [0.4, 0, 0.2, 1] as const,
  spring: [0.34, 1.56, 0.64, 1] as const,
};

/** 时长（秒，Framer Motion 单位） */
export const dur = {
  instant: 0.09,
  fast: 0.12,
  base: 0.18,
  medium: 0.24,
  slow: 0.32,
};

export const t = {
  hover: { duration: dur.fast, ease: ease.inOut } as Transition,
  press: { duration: dur.instant, ease: ease.inOut } as Transition,
  enter: { duration: dur.base, ease: ease.out } as Transition,
  exit: { duration: 0.14, ease: ease.in } as Transition,
  overlay: { duration: dur.medium, ease: ease.out } as Transition,
};

/** 列表项进场：y 6 → 0，前 8 项 stagger 24ms（§6.2） */
export const listItem: Variants = {
  hidden: { opacity: 0, y: 6 },
  show: (i: number) => ({
    opacity: 1,
    y: 0,
    transition: { duration: dur.base, ease: ease.out, delay: Math.min(i, 8) * 0.024 },
  }),
  exit: { opacity: 0, transition: { duration: 0.12, ease: ease.in } },
};

/** 分区/目录进入：y 4，180ms */
export const fadeUp: Variants = {
  hidden: { opacity: 0, y: 4 },
  show: { opacity: 1, y: 0, transition: t.enter },
  exit: { opacity: 0, transition: t.exit },
};

/** 弹窗内容：scale .96 → 1 + y 4 → 0，200ms out；退出 scale .98，140ms in */
export const scaleIn: Variants = {
  hidden: { opacity: 0, scale: 0.96, y: 4 },
  show: { opacity: 1, scale: 1, y: 0, transition: { duration: 0.2, ease: ease.out } },
  exit: { opacity: 0, scale: 0.98, transition: t.exit },
};

/** 抽屉：x 24 → 0，240ms out；退出 x 0 → 16，180ms in */
export const drawerRight: Variants = {
  hidden: { x: 24, opacity: 0 },
  show: { x: 0, opacity: 1, transition: t.overlay },
  exit: { x: 16, opacity: 0, transition: { duration: 0.18, ease: ease.in } },
};

/** 遮罩：opacity，160ms in-out */
export const overlayFade: Variants = {
  hidden: { opacity: 0 },
  show: { opacity: 1, transition: { duration: 0.16, ease: ease.inOut } },
  exit: { opacity: 0, transition: { duration: 0.14, ease: ease.in } },
};

/** 下拉/Popover：opacity + scale .97 + y -2，140ms out；关闭 100ms */
export const popIn: Variants = {
  hidden: { opacity: 0, scale: 0.97, y: -2 },
  show: { opacity: 1, scale: 1, y: 0, transition: { duration: 0.14, ease: ease.out } },
  exit: { opacity: 0, scale: 0.97, transition: { duration: 0.1, ease: ease.in } },
};
