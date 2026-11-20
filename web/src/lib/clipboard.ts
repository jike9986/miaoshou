/**
 * 右击图片 → 复制到剪贴板（负责人 2026-11-20）。
 *
 * 需求原话：「最好是详情图这里可以右击图片自动复制」。
 * 用在成品详情页的详情图/主图缩略图上 —— 运营要拿图去别处用，
 * 每次都要「点开大图 → 右键 → 另存为 → 找文件」太麻烦。
 *
 * ## 为什么复制的是「图片本身」而不是 URL
 *
 * 运营的实际动作是「把这张图贴到别的聊天窗口/文档里」，所以要的是位图。
 * 但也有人要 URL（去 1688 或妙手上搜同款），所以菜单里给两个选项。
 *
 * ## 为什么不用 navigator.clipboard.write(ClipboardItem)
 *
 * Electron 里那条路要走 Chromium 的剪贴板服务，对跨域图片（我们的图在
 * aohuang888.com，页面在 file:// 或别的源）会抛 NotAllowedError。
 * 改用主进程的 `clipboard.writeImage(nativeImage)`：主进程直接抓字节、
 * 不经沙箱，跨域也没问题。所以这里走 IPC。
 *
 * 本文件只放「浏览器/桌面端都能跑」的封装，主进程实现在 app/src/main.ts。
 */
import { toast } from "@/components/ui";

/** 桌面端是否可用（web-only 部署时 window.desktop 不存在）。 */
function desktopClipboard(): {
  copyImage?: (url: string) => Promise<{ ok: boolean; error?: string }>;
  copyText?: (text: string) => Promise<{ ok: boolean; error?: string }>;
} | undefined {
  return (window as unknown as {
    desktop?: {
      clipboard?: {
        copyImage?: (url: string) => Promise<{ ok: boolean; error?: string }>;
        copyText?: (text: string) => Promise<{ ok: boolean; error?: string }>;
      };
    };
  }).desktop?.clipboard;
}

/**
 * 把图片复制进系统剪贴板。
 *
 * 返回是否成功 —— 失败时调用方决定要不要退回「复制 URL」。
 * 常见失败：图还没下载完、图床 404、主进程抓取超时。
 */
export async function copyImageToClipboard(url: string): Promise<boolean> {
  const api = desktopClipboard();
  if (!api?.copyImage) {
    toast.error("复制图片需要桌面客户端");
    return false;
  }
  try {
    const result = await api.copyImage(url);
    if (result?.ok) {
      toast.success("图片已复制");
      return true;
    }
    toast.error(result?.error || "复制图片失败");
    return false;
  } catch (error) {
    toast.error(error instanceof Error ? error.message : "复制图片失败");
    return false;
  }
}

/** 把文本（图片 URL）复制进剪贴板。桌面端走主进程，浏览器走 navigator。 */
export async function copyTextToClipboard(text: string): Promise<boolean> {
  const api = desktopClipboard();
  if (api?.copyText) {
    try {
      const result = await api.copyText(text);
      if (result?.ok) {
        toast.success("图片地址已复制");
        return true;
      }
    } catch { /* 退回浏览器实现 */ }
  }
  try {
    await navigator.clipboard.writeText(text);
    toast.success("图片地址已复制");
    return true;
  } catch {
    toast.error("复制失败，请手动选择文本");
    return false;
  }
}