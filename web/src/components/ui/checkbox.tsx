// design-system §5.5 复选框 / 单选 / 开关 · §6.2（勾选 90ms bg + 120ms 对勾 / 开关 160ms out）
// Checkbox 16×16 rounded-xs border-[1.5px]；Radio 16×16 内点 6px；Switch 36×20 位移 16px。
// label 与控件间距 8px，label 13/20，整块可点。

import {
  forwardRef,
  useEffect,
  useId,
  useRef,
  type InputHTMLAttributes,
  type ReactNode,
} from "react";
import { Check, Minus } from "lucide-react";
import { cn } from "@/lib/utils";

/* ------------------------------------------------------------------ *
 * Checkbox（支持 indeterminate 不确定态，表头全选需要）
 * ------------------------------------------------------------------ */

export interface CheckboxProps
  extends Omit<InputHTMLAttributes<HTMLInputElement>, "type" | "size" | "onChange" | "checked"> {
  checked: boolean;
  indeterminate?: boolean;
  onChange: (checked: boolean) => void;
  label?: ReactNode;
  /** label 在右（默认）/ 在左 */
  labelPosition?: "right" | "left";
  /** 表格内密集场景：label 用 13/20 */
  size?: "sm" | "md";
}

export const Checkbox = forwardRef<HTMLInputElement, CheckboxProps>(function Checkbox(
  {
    checked,
    indeterminate = false,
    onChange,
    label,
    labelPosition = "right",
    size = "md",
    disabled,
    className,
    id,
    ...props
  },
  ref
) {
  const innerRef = useRef<HTMLInputElement | null>(null);
  const generatedId = useId();
  const inputId = id ?? `checkbox-${generatedId}`;

  useEffect(() => {
    if (innerRef.current) innerRef.current.indeterminate = indeterminate && !checked;
  }, [indeterminate, checked]);

  const setRefs = (node: HTMLInputElement | null): void => {
    innerRef.current = node;
    if (typeof ref === "function") ref(node);
    else if (ref) ref.current = node;
  };

  const active = checked || indeterminate;

  const control = (
    <span className="relative grid size-4 shrink-0 place-items-center">
      <input
        ref={setRefs}
        id={inputId}
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.currentTarget.checked)}
        className="peer absolute inset-0 z-10 size-4 cursor-pointer opacity-0 disabled:cursor-not-allowed"
        {...props}
      />
      <span
        aria-hidden
        className={cn(
          "grid size-4 place-items-center rounded-xs border-[1.5px] transition-colors duration-fast ease-in-out",
          active ? "border-brand-600 bg-brand-600" : "border-border-strong bg-surface",
          "peer-hover:border-brand-600/70",
          "peer-focus-visible:ring-2 peer-focus-visible:ring-brand-600/32 peer-focus-visible:ring-offset-2 peer-focus-visible:ring-offset-surface",
          "peer-disabled:opacity-50"
        )}
      >
        {checked ? (
          <Check className="size-3 text-white" strokeWidth={3} />
        ) : indeterminate ? (
          <Minus className="size-3 text-white" strokeWidth={3} />
        ) : null}
      </span>
    </span>
  );

  if (label === undefined) {
    return <span className={cn("inline-flex", className)}>{control}</span>;
  }

  return (
    <label
      htmlFor={inputId}
      className={cn(
        "inline-flex cursor-pointer items-center gap-2 select-none",
        size === "sm" ? "text-[13px] leading-5" : "text-sm leading-[20px]",
        disabled && "cursor-not-allowed opacity-50",
        className
      )}
    >
      {labelPosition === "left" && <span className="text-fg">{label}</span>}
      {control}
      {labelPosition === "right" && <span className="text-fg">{label}</span>}
    </label>
  );
});

/* ------------------------------------------------------------------ *
 * Radio（16×16，选中内点 6px）
 * ------------------------------------------------------------------ */

export interface RadioProps
  extends Omit<InputHTMLAttributes<HTMLInputElement>, "type" | "size" | "onChange" | "checked"> {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label?: ReactNode;
  value?: string;
}

export const Radio = forwardRef<HTMLInputElement, RadioProps>(function Radio(
  { checked, onChange, label, value, disabled, className, id, ...props },
  ref
) {
  const generatedId = useId();
  const inputId = id ?? `radio-${generatedId}`;

  const control = (
    <span className="relative grid size-4 shrink-0 place-items-center">
      <input
        ref={ref}
        id={inputId}
        type="radio"
        value={value}
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.currentTarget.checked)}
        className="peer absolute inset-0 z-10 size-4 cursor-pointer opacity-0 disabled:cursor-not-allowed"
        {...props}
      />
      <span
        aria-hidden
        className={cn(
          "grid size-4 place-items-center rounded-full border-[1.5px] transition-colors duration-fast ease-in-out",
          checked ? "border-brand-600 bg-surface" : "border-border-strong bg-surface",
          "peer-hover:border-brand-600/70",
          "peer-focus-visible:ring-2 peer-focus-visible:ring-brand-600/32 peer-focus-visible:ring-offset-2 peer-focus-visible:ring-offset-surface",
          "peer-disabled:opacity-50"
        )}
      >
        {checked && <span className="size-1.5 rounded-full bg-brand-600" />}
      </span>
    </span>
  );

  if (label === undefined) return <span className={cn("inline-flex", className)}>{control}</span>;

  return (
    <label
      htmlFor={inputId}
      className={cn(
        "inline-flex cursor-pointer items-center gap-2 select-none text-sm leading-[20px]",
        disabled && "cursor-not-allowed opacity-50",
        className
      )}
    >
      {control}
      <span className="text-fg">{label}</span>
    </label>
  );
});

/** 单选组（行内 gap-6 / 纵向 gap-3，§5.5） */
export interface RadioGroupProps<T extends string> {
  value: T;
  onChange: (value: T) => void;
  options: Array<{ value: T; label: ReactNode; disabled?: boolean }>;
  direction?: "row" | "column";
  name?: string;
  className?: string;
}

export function RadioGroup<T extends string>({
  value,
  onChange,
  options,
  direction = "row",
  name,
  className,
}: RadioGroupProps<T>) {
  const generatedName = useId();
  const groupName = name ?? `radio-group-${generatedName}`;
  return (
    <div
      role="radiogroup"
      className={cn("flex", direction === "row" ? "flex-row gap-6" : "flex-col gap-3", className)}
    >
      {options.map((option) => (
        <Radio
          key={option.value}
          name={groupName}
          value={option.value}
          checked={value === option.value}
          disabled={option.disabled}
          onChange={() => onChange(option.value)}
          label={option.label}
        />
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * Switch（36×20 轨道，16px 滑块，位移 16px，160ms ease-out）
 * ------------------------------------------------------------------ */

export interface SwitchProps
  extends Omit<InputHTMLAttributes<HTMLInputElement>, "type" | "size" | "onChange" | "checked"> {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label?: ReactNode;
  /** 开关右侧的状态文案（开/关） */
  showState?: boolean;
  stateLabels?: { on: string; off: string };
}

export const Switch = forwardRef<HTMLInputElement, SwitchProps>(function Switch(
  { checked, onChange, label, showState = false, stateLabels = { on: "开", off: "关" }, disabled, className, id, ...props },
  ref
) {
  const generatedId = useId();
  const inputId = id ?? `switch-${generatedId}`;

  const control = (
    <span className="relative inline-flex h-5 w-9 shrink-0 items-center">
      <input
        ref={ref}
        id={inputId}
        type="checkbox"
        role="switch"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.currentTarget.checked)}
        className="peer absolute inset-0 z-10 size-full cursor-pointer opacity-0 disabled:cursor-not-allowed"
        {...props}
      />
      <span
        aria-hidden
        className={cn(
          "pointer-events-none h-5 w-9 rounded-full transition-colors duration-[160ms] ease-out",
          checked ? "bg-brand-600" : "bg-border-strong",
          "peer-focus-visible:ring-2 peer-focus-visible:ring-brand-600/32 peer-focus-visible:ring-offset-2 peer-focus-visible:ring-offset-surface",
          "peer-disabled:opacity-50"
        )}
      />
      <span
        aria-hidden
        className={cn(
          "pointer-events-none absolute left-0.5 top-0.5 size-4 rounded-full bg-white shadow-1",
          "transition-transform duration-[160ms] ease-out",
          checked ? "translate-x-4" : "translate-x-0"
        )}
      />
    </span>
  );

  if (label === undefined && !showState) {
    return <span className={cn("inline-flex", className)}>{control}</span>;
  }

  return (
    <label
      htmlFor={inputId}
      className={cn(
        "inline-flex cursor-pointer items-center gap-2 select-none text-sm leading-[20px]",
        disabled && "cursor-not-allowed opacity-50",
        className
      )}
    >
      {control}
      {label !== undefined && <span className="text-fg">{label}</span>}
      {showState && (
        <span className="text-[13px] text-fg-muted">{checked ? stateLabels.on : stateLabels.off}</span>
      )}
    </label>
  );
});
