// design-system §5.2 输入框 / 文本域 / 数字输入 · §5.0 通用状态约定
// 高度 36(md)/28(sm)/44(lg)；内边距 14；带图标左侧 36；聚焦 3px 品牌光环；
// 错误态 danger 边框 + 3px danger 光环 + 下方 12px 文案。

import {
  forwardRef,
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type InputHTMLAttributes,
  type ReactNode,
  type TextareaHTMLAttributes,
} from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

/* ------------------------------------------------------------------ *
 * 基础类（§5.2「基础类」逐条落成 tailwind）
 * ------------------------------------------------------------------ */

export const fieldBase = [
  "w-full rounded-md border border-border bg-surface px-3.5 text-sm text-fg",
  "placeholder:text-fg-muted",
  "shadow-inset",
  "transition-[border-color,box-shadow] duration-fast ease-in-out",
  "hover:border-border-strong",
  "focus:border-brand-600 focus:outline-none focus:ring-[3px] focus:ring-brand-600/[0.18]",
  "disabled:cursor-not-allowed disabled:bg-surface-active disabled:text-fg-disabled",
  "aria-[invalid=true]:border-danger-600 aria-[invalid=true]:focus:ring-danger-600/[0.15]",
].join(" ");

const fieldSize = {
  sm: "h-7 text-[13px] leading-5 px-2.5",
  md: "h-9",
  lg: "h-11 text-[15px] px-4",
} as const;

export type FieldSize = keyof typeof fieldSize;

/* ------------------------------------------------------------------ *
 * Field 包装：标签 13/18 500 · 必填 * · 帮助文案 12/18 · 错误文案 12/18 danger
 * ------------------------------------------------------------------ */

export interface FieldProps {
  label?: ReactNode;
  required?: boolean;
  hint?: ReactNode;
  error?: string | null;
  htmlFor?: string;
  className?: string;
  /** 标签右侧内容（如「全部清除」文字按钮） */
  action?: ReactNode;
  children: ReactNode;
}

export function Field({ label, required, hint, error, htmlFor, className, action, children }: FieldProps) {
  return (
    <div className={cn("flex flex-col gap-1.5", className)}>
      {(label || action) && (
        <div className="flex items-baseline justify-between gap-2">
          {label && (
            <label
              htmlFor={htmlFor}
              className="text-sm font-medium leading-[18px] text-fg-secondary"
            >
              {label}
              {required && <span className="ml-0.5 text-danger-600">*</span>}
            </label>
          )}
          {action}
        </div>
      )}
      {children}
      {error ? (
        <p className="text-xs leading-[18px] text-danger-600" role="alert">
          {error}
        </p>
      ) : (
        hint && <p className="text-xs leading-[18px] text-fg-muted">{hint}</p>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * Input
 * ------------------------------------------------------------------ */

export interface InputProps
  extends Omit<InputHTMLAttributes<HTMLInputElement>, "size">,
    VariantProps<typeof inputVariants> {
  /** 左侧图标（16px，left 12px 居中；有值时左内边距 36px） */
  icon?: ReactNode;
  /** 右侧附加内容（快捷键提示、单位、清除按钮） */
  suffix?: ReactNode;
  invalid?: boolean;
  /** 数字类内容使用等宽数字（§3.3） */
  numeric?: boolean;
}

const inputVariants = cva("", {
  variants: { size: fieldSize },
  defaultVariants: { size: "md" },
});

export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  { className, size, icon, suffix, invalid, numeric, disabled, ...props },
  ref
) {
  if (!icon && !suffix) {
    return (
      <input
        ref={ref}
        aria-invalid={invalid || undefined}
        disabled={disabled}
        className={cn(fieldBase, inputVariants({ size }), numeric && "num", className)}
        {...props}
      />
    );
  }

  return (
    <div className="group relative flex items-center">
      {icon && (
        <span
          aria-hidden
          className={cn(
            "pointer-events-none absolute left-3 grid place-items-center text-fg-muted [&_svg]:size-4",
            "transition-colors duration-fast group-focus-within:text-brand-600"
          )}
        >
          {icon}
        </span>
      )}
      <input
        ref={ref}
        aria-invalid={invalid || undefined}
        disabled={disabled}
        className={cn(
          fieldBase,
          inputVariants({ size }),
          numeric && "num",
          icon && "pl-9",
          suffix && "pr-11",
          className
        )}
        {...props}
      />
      {suffix && (
        <span className="pointer-events-none absolute right-2 flex items-center gap-1 text-xs text-fg-muted">
          {suffix}
        </span>
      )}
    </div>
  );
});

/* ------------------------------------------------------------------ *
 * Textarea（min-h 88px，右下角字数 12/200，超限 danger）
 * ------------------------------------------------------------------ */

export interface TextareaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  invalid?: boolean;
  /** 显示字数计数，传 maxLength 后自动渲染 */
  showCount?: boolean;
}

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(function Textarea(
  { className, invalid, showCount, maxLength, value, defaultValue, onChange, ...props },
  ref
) {
  // 受控时由 value 计算；非受控时跟踪 defaultValue 与输入长度
  const isControlled = value !== undefined;
  const [uncontrolledLength, setUncontrolledLength] = useState(
    () => Array.from(String(defaultValue ?? "")).length
  );
  const length = isControlled
    ? Array.from(String(value)).length
    : uncontrolledLength;
  const over = typeof maxLength === "number" && length > maxLength;

  return (
    <div className="relative">
      <textarea
        ref={ref}
        value={value}
        defaultValue={defaultValue}
        maxLength={maxLength}
        aria-invalid={invalid || undefined}
        onChange={(event) => {
          if (!isControlled) setUncontrolledLength(Array.from(event.currentTarget.value).length);
          onChange?.(event);
        }}
        className={cn(
          fieldBase,
          "min-h-[88px] py-2.5 leading-[22px] resize-y",
          showCount || maxLength ? "pb-6" : "",
          className
        )}
        {...props}
      />
      {(showCount || typeof maxLength === "number") && (
        <span
          className={cn(
            "pointer-events-none absolute bottom-1.5 right-2.5 num text-[11px] leading-4",
            over ? "text-danger-600" : "text-fg-muted"
          )}
        >
          {length}
          {typeof maxLength === "number" ? `/${maxLength}` : ""}
        </span>
      )}
    </div>
  );
});

/* ------------------------------------------------------------------ *
 * NumberInput（价格 / 库存）
 * 右侧固定单位；inputMode=decimal；聚焦全选；上下键步进。
 * ------------------------------------------------------------------ */

export interface NumberInputProps {
  value: number | string | null;
  onChange: (value: number | null) => void;
  /** 单位标签：价格「¥」在左用 prefix，库存「件」在右用 suffix */
  prefix?: ReactNode;
  suffix?: ReactNode;
  min?: number;
  max?: number;
  /** 步进值，价格 0.01，库存 1（默认 1） */
  step?: number;
  /** 小数位，默认 2（step 为 1 时可传 0） */
  decimals?: number;
  placeholder?: string;
  disabled?: boolean;
  invalid?: boolean;
  size?: FieldSize;
  id?: string;
  name?: string;
  className?: string;
  autoFocus?: boolean;
  onBlur?: () => void;
}

export const NumberInput = forwardRef<HTMLInputElement, NumberInputProps>(function NumberInput(
  {
    value,
    onChange,
    prefix,
    suffix,
    min,
    max,
    step = 1,
    decimals = 2,
    placeholder,
    disabled,
    invalid,
    size = "md",
    id,
    name,
    className,
    autoFocus,
    onBlur,
  },
  ref
) {
  const [focused, setFocused] = useState(false);
  const text = value === null || value === undefined || value === "" ? "" : String(value);
  const [draft, setDraft] = useState(text);
  const lastValue = useRef(text);

  // 外部值变化时同步草稿（编辑中不打断输入）
  useEffect(() => {
    if (focused) return;
    if (text === lastValue.current) return;
    lastValue.current = text;
    setDraft(text);
  }, [text, focused]);

  const commit = useCallback(
    (raw: string) => {
      const trimmed = raw.trim();
      if (trimmed === "") {
        onChange(null);
        return;
      }
      const parsed = Number(trimmed);
      if (!Number.isFinite(parsed)) return;
      let next = parsed;
      if (typeof min === "number") next = Math.max(min, next);
      if (typeof max === "number") next = Math.min(max, next);
      const factor = 10 ** decimals;
      next = Math.round(next * factor) / factor;
      onChange(next);
    },
    [decimals, max, min, onChange]
  );

  const stepBy = (dir: 1 | -1): void => {
    const current = Number(draft);
    const base = Number.isFinite(current) ? current : 0;
    const factor = 10 ** decimals;
    let next = Math.round((base + dir * step) * factor) / factor;
    if (typeof min === "number") next = Math.max(min, next);
    if (typeof max === "number") next = Math.min(max, next);
    setDraft(String(next));
    lastValue.current = String(next);
    onChange(next);
  };

  return (
    <div className="relative flex items-center">
      {prefix && (
        <span className="pointer-events-none absolute left-3.5 text-sm text-fg-muted">{prefix}</span>
      )}
      <input
        ref={ref}
        id={id}
        name={name}
        type="text"
        inputMode="decimal"
        autoComplete="off"
        autoFocus={autoFocus}
        value={draft}
        placeholder={placeholder}
        disabled={disabled}
        aria-invalid={invalid || undefined}
        onFocus={(event) => {
          setFocused(true);
          event.currentTarget.select();
        }}
        onBlur={() => {
          setFocused(false);
          commit(draft);
          onBlur?.();
        }}
        onChange={(event) => {
          const next = event.target.value.replace(/[^\d.-]/g, "");
          setDraft(next);
        }}
        onKeyDown={(event) => {
          if (event.key === "ArrowUp") {
            event.preventDefault();
            stepBy(1);
          } else if (event.key === "ArrowDown") {
            event.preventDefault();
            stepBy(-1);
          } else if (event.key === "Enter") {
            event.currentTarget.blur();
          }
        }}
        className={cn(
          fieldBase,
          fieldSize[size],
          "num",
          prefix && "pl-8",
          suffix && "pr-10",
          className
        )}
      />
      {suffix && (
        <span className="pointer-events-none absolute right-3.5 text-sm text-fg-muted">{suffix}</span>
      )}
    </div>
  );
});

/* ------------------------------------------------------------------ *
 * 内联编辑输入（表格单元格，§5.2 内联编辑：28px 无边框 + ring-1 brand）
 * Enter 提交 / Esc 取消 / 失焦提交，由使用方处理。
 * ------------------------------------------------------------------ */

export interface InlineInputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "size"> {
  invalid?: boolean;
}

export const InlineInput = forwardRef<HTMLInputElement, InlineInputProps>(function InlineInput(
  { className, invalid, ...props },
  ref
) {
  return (
    <input
      ref={ref}
      aria-invalid={invalid || undefined}
      className={cn(
        "h-7 w-full rounded-sm bg-surface px-1.5 text-[13px] text-fg",
        "ring-1 ring-brand-600 focus:outline-none",
        "aria-[invalid=true]:ring-danger-600",
        className
      )}
      {...props}
    />
  );
});

/** 生成稳定的控件 id（配合 Field htmlFor） */
export function useFieldId(provided?: string, prefix = "field"): string {
  const generated = useId();
  return provided ?? `${prefix}-${generated}`;
}
