// design-system §5.4 全局搜索触发 Ctrl/Cmd + K
// 通用快捷键 hook：支持组合键（mod+shift+k）、序列判定、输入框内忽略。

import { useEffect, useRef } from "react";

export interface HotkeyOptions {
  /** 在 input/textarea/contenteditable 内是否也触发（默认 false） */
  enableInInput?: boolean;
  /** 是否阻止默认行为（Ctrl+K 会聚焦地址栏/搜索，默认 true） */
  preventDefault?: boolean;
  /** 禁用 */
  enabled?: boolean;
  /** 触发的事件目标（默认 window） */
  target?: Window | HTMLElement | null;
}

/** 解析 "mod+k" / "shift+alt+p" / "escape" 这类表达式 */
interface ParsedHotkey {
  key: string;
  ctrl: boolean;
  meta: boolean;
  shift: boolean;
  alt: boolean;
}

function parseHotkey(combo: string): ParsedHotkey {
  const parts = combo
    .toLowerCase()
    .split("+")
    .map((part) => part.trim())
    .filter(Boolean);
  const result: ParsedHotkey = { key: "", ctrl: false, meta: false, shift: false, alt: false };
  for (const part of parts) {
    switch (part) {
      case "ctrl":
      case "control":
        result.ctrl = true;
        break;
      case "cmd":
      case "meta":
        result.meta = true;
        break;
      case "mod":
        // 平台差异：Windows 用 Ctrl，macOS 用 Cmd
        if (typeof navigator !== "undefined" && /mac/i.test(navigator.platform)) result.meta = true;
        else result.ctrl = true;
        break;
      case "shift":
        result.shift = true;
        break;
      case "alt":
      case "option":
        result.alt = true;
        break;
      default:
        result.key = part;
    }
  }
  return result;
}

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return (
    tag === "INPUT" ||
    tag === "TEXTAREA" ||
    tag === "SELECT" ||
    target.isContentEditable ||
    target.getAttribute("role") === "textbox"
  );
}

/**
 * useHotkey("mod+k", () => setOpen(true))
 * useHotkey("escape", close, { enableInInput: true })
 */
export function useHotkey(
  combo: string | string[],
  handler: (event: KeyboardEvent) => void,
  options: HotkeyOptions = {}
): void {
  const { enableInInput = false, preventDefault = true, enabled = true, target } = options;
  const handlerRef = useRef(handler);
  handlerRef.current = handler;

  const combos = Array.isArray(combo) ? combo : [combo];
  const comboKey = combos.join("|");

  useEffect(() => {
    if (!enabled) return;
    const host: Window | HTMLElement = target ?? window;
    const parsed = comboKey.split("|").map(parseHotkey);

    const listener = (event: Event): void => {
      const keyboard = event as KeyboardEvent;
      if (keyboard.defaultPrevented) return;
      if (!enableInInput && isEditableTarget(keyboard.target)) return;

      const key = keyboard.key.toLowerCase();
      for (const item of parsed) {
        // "esc" 别名
        const expected = item.key === "esc" ? "escape" : item.key;
        const keyMatches = expected === key || (expected === "space" && key === " ");
        if (!keyMatches) continue;
        if (item.ctrl !== keyboard.ctrlKey) continue;
        if (item.meta !== keyboard.metaKey) continue;
        if (item.shift !== keyboard.shiftKey) continue;
        if (item.alt !== keyboard.altKey) continue;
        if (preventDefault) keyboard.preventDefault();
        handlerRef.current(keyboard);
        return;
      }
    };

    host.addEventListener("keydown", listener as EventListener);
    return () => host.removeEventListener("keydown", listener as EventListener);
  }, [comboKey, enableInInput, preventDefault, enabled, target]);
}

/** 按平台返回修饰键文案：「Ctrl」/「⌘」 */
export function isMac(): boolean {
  return typeof navigator !== "undefined" && /mac/i.test(navigator.platform);
}

/** 快捷键展示文案（Ctrl K / ⌘K） */
export function hotkeyLabel(key: string): string {
  return isMac() ? `⌘${key.toUpperCase()}` : `Ctrl ${key.toUpperCase()}`;
}
