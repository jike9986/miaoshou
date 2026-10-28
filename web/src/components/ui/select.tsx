// design-system §5.3 下拉选择 · §5.4 搜索框 · §5.15 键盘按键
// Select 用原生 <select> 承载语义与键盘行为，外层自绘触发器样式（零额外依赖，符合「少加对象」）。
// SearchInput 带清除按钮 + 防抖 + 快捷键提示（kbd）。

import {
  forwardRef,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
} from "react";
import { Check, ChevronDown, Search, X } from "lucide-react";
import { cn } from "@/lib/utils";
import { fieldBase, type FieldSize } from "@/components/ui/input";

/* ------------------------------------------------------------------ *
 * Select（原生 select + 自绘外观）
 * ------------------------------------------------------------------ */

export interface SelectOption<Value extends string = string> {
  value: Value;
  label: string;
  disabled?: boolean;
  /** 分组标题（显式渲染 <optgroup>） */
  group?: string;
}

export interface SelectProps
  extends Omit<SelectHTMLAttributes<HTMLSelectElement>, "size" | "onChange" | "value"> {
  value: string;
  onChange: (value: string) => void;
  options: SelectOption[];
  /** 占位项（值为空字符串，disabled） */
  placeholder?: string;
  size?: FieldSize;
  invalid?: boolean;
  /** 触发器左侧图标 */
  icon?: ReactNode;
  /** 触发器最小宽度，如 "w-40" */
  className?: string;
}

export const Select = forwardRef<HTMLSelectElement, SelectProps>(function Select(
  { value, onChange, options, placeholder, size = "md", invalid, icon, className, disabled, ...props },
  ref
) {
  // 按 group 归并（保持首次出现顺序）
  const grouped = useMemo(() => {
    const map = new Map<string, SelectOption[]>();
    const ungrouped: SelectOption[] = [];
    for (const option of options) {
      if (!option.group) {
        ungrouped.push(option);
        continue;
      }
      const bucket = map.get(option.group);
      if (bucket) bucket.push(option);
      else map.set(option.group, [option]);
    }
    return { ungrouped, groups: Array.from(map.entries()) };
  }, [options]);

  return (
    <div className={cn("relative flex items-center", className)}>
      {icon && (
        <span
          aria-hidden
          className="pointer-events-none absolute left-3 grid place-items-center text-fg-muted [&_svg]:size-4"
        >
          {icon}
        </span>
      )}
      <select
        ref={ref}
        value={value}
        disabled={disabled}
        aria-invalid={invalid || undefined}
        onChange={(event) => onChange(event.currentTarget.value)}
        className={cn(
          fieldBase,
          "cursor-pointer appearance-none pr-8",
          size === "sm" ? "h-7 pl-2.5 text-[13px]" : size === "lg" ? "h-11 pl-4 text-[15px]" : "h-9 pl-3.5",
          icon && "pl-9",
          !value && "text-fg-muted",
          className
        )}
        {...props}
      >
        {placeholder !== undefined && (
          <option value="" disabled>
            {placeholder}
          </option>
        )}
        {grouped.ungrouped.map((option) => (
          <option key={option.value} value={option.value} disabled={option.disabled}>
            {option.label}
          </option>
        ))}
        {grouped.groups.map(([groupName, groupOptions]) => (
          <optgroup key={groupName} label={groupName}>
            {groupOptions.map((option) => (
              <option key={option.value} value={option.value} disabled={option.disabled}>
                {option.label}
              </option>
            ))}
          </optgroup>
        ))}
      </select>
      <ChevronDown
        aria-hidden
        className="pointer-events-none absolute right-2.5 size-4 text-fg-muted"
        strokeWidth={1.75}
      />
    </div>
  );
});

/* ------------------------------------------------------------------ *
 * 自绘下拉（需要搜索/多选/图标选项时用，§5.3 面板规格）
 * 轻量实现：触发器 + 绝对定位面板 + 键盘 ↑↓ Enter Esc，点击外部关闭。
 * ------------------------------------------------------------------ */

export interface DropdownOption<Value extends string = string> {
  value: Value;
  label: string;
  description?: string;
  icon?: ReactNode;
  disabled?: boolean;
}

export interface DropdownSelectProps<Value extends string = string> {
  value: Value | null;
  onChange: (value: Value) => void;
  options: DropdownOption<Value>[];
  placeholder?: string;
  /** 面板顶部搜索框（类目/店铺场景，§5.3） */
  searchable?: boolean;
  searchPlaceholder?: string;
  invalid?: boolean;
  disabled?: boolean;
  size?: FieldSize;
  className?: string;
  /** 面板最大高度，默认 280px（§5.3） */
  maxHeight?: number;
  emptyText?: string;
  id?: string;
  "aria-label"?: string;
}

export function DropdownSelect<Value extends string = string>({
  value,
  onChange,
  options,
  placeholder = "请选择",
  searchable = false,
  searchPlaceholder = "搜索…",
  invalid,
  disabled,
  size = "md",
  className,
  maxHeight = 280,
  emptyText = "没有匹配项",
  id,
  "aria-label": ariaLabel,
}: DropdownSelectProps<Value>) {
  const [open, setOpen] = useState(false);
  const [keyword, setKeyword] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const listId = useId();

  const selected = options.find((option) => option.value === value) ?? null;
  const filtered = useMemo(() => {
    if (!keyword.trim()) return options;
    const lower = keyword.trim().toLowerCase();
    return options.filter((option) => option.label.toLowerCase().includes(lower));
  }, [keyword, options]);

  // 点击外部关闭
  useEffect(() => {
    if (!open) return;
    const handler = (event: MouseEvent): void => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  useEffect(() => {
    if (open) {
      setActiveIndex(Math.max(0, filtered.findIndex((option) => option.value === value)));
      if (searchable) window.setTimeout(() => searchRef.current?.focus(), 0);
    } else {
      setKeyword("");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const commit = (option: DropdownOption<Value>): void => {
    if (option.disabled) return;
    onChange(option.value);
    setOpen(false);
  };

  return (
    <div ref={rootRef} className={cn("relative", className)}>
      <button
        type="button"
        id={id}
        disabled={disabled}
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        aria-invalid={invalid || undefined}
        onClick={() => setOpen((prev) => !prev)}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown" || event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            setOpen(true);
          }
        }}
        className={cn(
          fieldBase,
          "flex cursor-pointer items-center justify-between gap-2 pr-2.5 text-left",
          size === "sm" ? "h-7 text-[13px]" : size === "lg" ? "h-11 text-[15px]" : "h-9",
          !selected && "text-fg-muted",
          disabled && "cursor-not-allowed"
        )}
      >
        <span className="flex min-w-0 items-center gap-2">
          {selected?.icon}
          <span className={cn("truncate", !selected && "text-fg-muted")}>
            {selected ? selected.label : placeholder}
          </span>
        </span>
        <ChevronDown
          aria-hidden
          className={cn(
            "size-4 shrink-0 text-fg-muted transition-transform duration-[140ms] ease-out",
            open && "rotate-180"
          )}
          strokeWidth={1.75}
        />
      </button>

      {open && (
        <div
          ref={panelRef}
          id={listId}
          role="listbox"
          tabIndex={-1}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              setOpen(false);
            } else if (event.key === "ArrowDown") {
              event.preventDefault();
              setActiveIndex((prev) => Math.min(prev + 1, filtered.length - 1));
            } else if (event.key === "ArrowUp") {
              event.preventDefault();
              setActiveIndex((prev) => Math.max(prev - 1, 0));
            } else if (event.key === "Enter") {
              event.preventDefault();
              const option = filtered[activeIndex];
              if (option) commit(option);
            }
          }}
          className={cn(
            "anim-pop-in absolute left-0 right-0 top-[calc(100%+4px)] z-40",
            "overflow-hidden rounded-md border border-border bg-elevated shadow-2"
          )}
        >
          {searchable && (
            <div className="border-b border-border-subtle p-1">
              <div className="relative flex items-center">
                <Search aria-hidden className="absolute left-2 size-3.5 text-fg-muted" strokeWidth={2} />
                <input
                  ref={searchRef}
                  value={keyword}
                  onChange={(event) => {
                    setKeyword(event.target.value);
                    setActiveIndex(0);
                  }}
                  placeholder={searchPlaceholder}
                  className="h-8 w-full rounded-sm bg-transparent pl-7 pr-2 text-sm text-fg placeholder:text-fg-muted focus:outline-none"
                />
              </div>
            </div>
          )}
          <div className="overflow-y-auto p-1" style={{ maxHeight }}>
            {filtered.length === 0 && (
              <p className="px-2.5 py-2 text-sm text-fg-muted">{emptyText}</p>
            )}
            {filtered.map((option, index) => {
              const isSelected = option.value === value;
              return (
                <div
                  key={option.value}
                  role="option"
                  aria-selected={isSelected}
                  aria-disabled={option.disabled || undefined}
                  onMouseEnter={() => setActiveIndex(index)}
                  onMouseDown={(event) => {
                    event.preventDefault();
                    commit(option);
                  }}
                  className={cn(
                    "relative flex h-8 cursor-default select-none items-center gap-2 rounded-sm pl-2.5 pr-8 text-sm outline-none",
                    index === activeIndex ? "bg-surface-active" : "text-fg",
                    isSelected && "font-medium text-brand-700",
                    option.disabled && "pointer-events-none opacity-50"
                  )}
                >
                  <span className="min-w-0 flex-1 truncate">{option.label}</span>
                  {option.description && (
                    <span className="shrink-0 text-xs text-fg-muted">{option.description}</span>
                  )}
                  {isSelected && (
                    <Check aria-hidden className="absolute right-2 size-4 text-brand-600" strokeWidth={2.4} />
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * 快捷键提示（§5.15 键盘按键）
 * ------------------------------------------------------------------ */

export function Kbd({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <kbd
      className={cn(
        "grid h-5 min-w-5 place-items-center rounded-xs border border-border bg-canvas-inset px-1",
        "num text-[11px] font-normal text-fg-muted",
        className
      )}
    >
      {children}
    </kbd>
  );
}

/* ------------------------------------------------------------------ *
 * SearchInput（§5.4）
 * 高度 36；Search 图标 left 12；快捷提示聚焦后隐藏；有值时出现清除按钮；防抖 250ms。
 * ------------------------------------------------------------------ */

export interface SearchInputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "size" | "onChange" | "value"> {
  value: string;
  onChange: (value: string) => void;
  /** 防抖毫秒；传 0 关闭（本地过滤场景） */
  debounceMs?: number;
  /** 快捷键提示，如 "Ctrl K"；传 null 隐藏 */
  shortcut?: string | null;
  /** 展示尺寸：表格筛选 240px / 页面级 320px */
  width?: "sm" | "md" | "lg" | string;
  size?: FieldSize;
  invalid?: boolean;
  onClear?: () => void;
  containerClassName?: string;
}

export const SearchInput = forwardRef<HTMLInputElement, SearchInputProps>(function SearchInput(
  {
    value,
    onChange,
    debounceMs = 250,
    shortcut = "Ctrl K",
    width = "md",
    size = "md",
    invalid,
    onClear,
    className,
    containerClassName,
    placeholder = "搜索…",
    onFocus,
    onBlur,
    onKeyDown,
    ...props
  },
  ref
) {
  const [draft, setDraft] = useState(value);
  const [focused, setFocused] = useState(false);
  const timerRef = useRef<number | null>(null);

  // 外部 value 变化（如「清空筛选」）同步到草稿；输入过程中两者已一致，不会回写覆盖
  useEffect(() => {
    setDraft((prev) => (prev === value ? prev : value));
  }, [value]);

  const emit = (next: string): void => {
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    if (debounceMs <= 0) {
      onChange(next);
      return;
    }
    timerRef.current = window.setTimeout(() => onChange(next), debounceMs);
  };

  useEffect(() => {
    return () => {
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    };
  }, []);

  const widthClass =
    width === "sm" ? "w-60" : width === "lg" ? "w-80" : width === "md" ? "w-full" : width;

  return (
    <div className={cn("relative flex items-center", widthClass, containerClassName)}>
      <Search
        aria-hidden
        className={cn(
          "pointer-events-none absolute left-3 size-4 transition-colors duration-fast",
          focused ? "text-brand-600" : "text-fg-muted"
        )}
        strokeWidth={2}
      />
      <input
        ref={ref}
        type="search"
        value={draft}
        placeholder={placeholder}
        aria-invalid={invalid || undefined}
        onChange={(event) => {
          const next = event.target.value;
          setDraft(next);
          emit(next);
        }}
        onFocus={(event) => {
          setFocused(true);
          onFocus?.(event);
        }}
        onBlur={(event) => {
          setFocused(false);
          onBlur?.(event);
        }}
        onKeyDown={(event) => {
          if (event.key === "Escape" && draft) {
            event.preventDefault();
            setDraft("");
            emit("");
            onClear?.();
          }
          onKeyDown?.(event);
        }}
        className={cn(
          fieldBase,
          "[&::-webkit-search-cancel-button]:hidden",
          size === "sm" ? "h-7 text-[13px]" : size === "lg" ? "h-11 text-[15px]" : "h-9",
          "pl-9 pr-16",
          className
        )}
        {...props}
      />
      <div className="absolute right-2 top-1/2 flex -translate-y-1/2 items-center gap-1">
        {draft ? (
          <button
            type="button"
            aria-label="清除搜索"
            onClick={() => {
              setDraft("");
              emit("");
              onClear?.();
            }}
            className="grid size-5 place-items-center rounded-xs text-fg-muted transition-colors duration-fast hover:bg-surface-active hover:text-fg"
          >
            <X className="size-3.5" strokeWidth={2} />
          </button>
        ) : (
          shortcut &&
          !focused && (
            <span className="hidden sm:block">
              <Kbd>{shortcut}</Kbd>
            </span>
          )
        )}
      </div>
    </div>
  );
});
