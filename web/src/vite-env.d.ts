/// <reference types="vite/client" />

// Vite 环境变量类型（web/.env.development 只放接口基地址，不注入密钥）
interface ImportMetaEnv {
  readonly DEV: boolean;
  readonly PROD: boolean;
  readonly MODE: string;
  readonly BASE_URL: string;
  /** 后端基地址（不含 /api/v1），Electron 注入 __DSH_BOOT__.apiBase 时以后者为准 */
  readonly VITE_API_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
