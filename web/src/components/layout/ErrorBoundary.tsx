// 渲染兜底：任何一处组件抛错都不允许整棵树被卸载成白屏。
//
// 为什么必须有：React 18 里未捕获的渲染异常会让最近的错误边界接管；没有边界时
// 直接卸载整棵 React 树，页面就只剩背景色 —— 用户看到的就是「全白、没报错、
// 没提示」。之前排查登录白屏时，控制台一条异常都没有，正是因为树已经被卸载，
// 而我们没有任何地方把它接住。
//
// 这里只做「显示错误 + 可重试」，不上报、不改业务状态。

import { Component, type ErrorInfo, type ReactNode } from "react";
import { AlertTriangle, RotateCcw } from "lucide-react";

interface Props {
  children: ReactNode;
  /** 出错时额外清理什么（例如清掉坏掉的登录态） */
  onError?: (error: Error) => void;
  /** 出错界面的标题，默认「页面出错了」 */
  title?: string;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // 控制台留一份完整堆栈，方便用 CDP 抓（页面上只显示摘要）
    console.error("[ErrorBoundary]", error, info.componentStack);
    this.props.onError?.(error);
  }

  private reset = (): void => {
    this.setState({ error: null });
  };

  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;

    return (
      <div className="grid min-h-0 flex-1 place-items-center bg-canvas p-8" role="alert">
        <div className="w-full max-w-[560px] rounded-lg border border-border bg-surface p-6">
          <div className="flex items-start gap-3">
            <AlertTriangle className="mt-0.5 size-5 shrink-0 text-warning-600" strokeWidth={2} />
            <div className="min-w-0 flex-1">
              <h2 className="text-[15px] font-medium text-fg">{this.props.title ?? "页面出错了"}</h2>
              <p className="mt-1 text-[13px] leading-5 text-fg-secondary">
                这一步没能渲染出来。可以重试；如果反复出现，把下面这行发给开发。
              </p>
              <pre className="mt-3 max-h-40 overflow-auto rounded-sm bg-canvas-inset p-3 text-[12px] leading-5 text-fg-secondary">
                {error.message || String(error)}
              </pre>
              <button
                type="button"
                onClick={this.reset}
                className="mt-4 inline-flex h-8 items-center gap-1.5 rounded-sm border border-border bg-surface px-3 text-[13px] text-fg transition-colors duration-fast hover:bg-surface-active focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-600/32"
              >
                <RotateCcw className="size-3.5" strokeWidth={2} />
                重试
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  }
}

export default ErrorBoundary;
