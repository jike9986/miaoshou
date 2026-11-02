export const EMAIL_CODE_RESEND_COOLDOWN_SECONDS: 60;

export function validateSoftwareLogin(
  identifier: string,
  password: string,
): { identifier: string | null; password: string | null };

export function normalizeCooldownSeconds(value: unknown): number;
export function retryAfterSecondsFromError(error: unknown): number;
export function remainingCooldownSeconds(deadlineAt: number | null, now: number): number;
