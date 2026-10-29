// 统一出口：业务侧只从 "@/components/ui" 导入，避免深路径散落。
// 每个组件的文件名与导出名对应 design-system 第 5 章章节号（见各文件顶部注释）。

export { Button, buttonVariants } from "@/components/ui/button";
export type { ButtonProps, ButtonSize, ButtonVariant } from "@/components/ui/button";

export { IconButton, iconButtonVariants } from "@/components/ui/icon-button";
export type { IconButtonProps } from "@/components/ui/icon-button";

export { Field, Input, InlineInput, NumberInput, Textarea, fieldBase, useFieldId } from "@/components/ui/input";
export type { FieldProps, FieldSize, InputProps, NumberInputProps, TextareaProps } from "@/components/ui/input";

export { DropdownSelect, Kbd, SearchInput, Select } from "@/components/ui/select";
export type {
  DropdownOption,
  DropdownSelectProps,
  SearchInputProps,
  SelectOption,
  SelectProps,
} from "@/components/ui/select";

export { Checkbox, Radio, RadioGroup, Switch } from "@/components/ui/checkbox";
export type { CheckboxProps, RadioGroupProps, RadioProps, SwitchProps } from "@/components/ui/checkbox";

export {
  Badge,
  StatusBadge,
  StatusDot,
  Tag,
  badgeVariants,
  productStatusMeta,
} from "@/components/ui/badge";
export type {
  BadgeProps,
  BadgeVariant,
  StatusBadgeProps,
  StatusDotProps,
  StatusMeta,
  StatusTone,
  TagProps,
} from "@/components/ui/badge";

export {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
  KpiCard,
  cardVariants,
} from "@/components/ui/card";
export type { CardProps, KpiCardProps } from "@/components/ui/card";

export {
  Skeleton,
  SkeletonCard,
  SkeletonCardGrid,
  SkeletonGroup,
  SkeletonRows,
  SkeletonTable,
  SkeletonText,
} from "@/components/ui/skeleton";
export type {
  SkeletonGroupProps,
  SkeletonProps,
  SkeletonTableProps,
  SkeletonTextProps,
} from "@/components/ui/skeleton";

export { EmptyState, ErrorState, InlineError } from "@/components/ui/empty-state";
export type { EmptyStateAction, EmptyStateProps, ErrorStateProps, InlineErrorProps } from "@/components/ui/empty-state";

export { ConfirmDialog, Dialog, useDialogState } from "@/components/ui/dialog";
export type { ConfirmDialogProps, DialogProps, DialogSize } from "@/components/ui/dialog";

export { Drawer } from "@/components/ui/drawer";
export type { DrawerProps, DrawerSize } from "@/components/ui/drawer";

export { Tooltip } from "@/components/ui/tooltip";
export type { TooltipProps, TooltipSide } from "@/components/ui/tooltip";

export { ClientOnly, InverseSurface, Popover, Tabs, tabId, tabPanelId } from "@/components/ui/tabs";
export type { PopoverProps, TabItem, TabsProps } from "@/components/ui/tabs";

export { AppToaster, TOAST_DURATION, sonnerToast, toast } from "@/components/ui/toast";
export type { AppToasterProps, ToastOptions } from "@/components/ui/toast";

export { Spinner } from "@/components/ui/spinner";
export type { SpinnerProps } from "@/components/ui/spinner";

export { BulkActionBar } from "@/components/ui/bulk-action-bar";
export type { BulkAction, BulkActionBarProps } from "@/components/ui/bulk-action-bar";

export { Progress, ScrollArea, Separator } from "@/components/ui/separator";
export type { ProgressProps, ScrollAreaProps, SeparatorProps } from "@/components/ui/separator";
