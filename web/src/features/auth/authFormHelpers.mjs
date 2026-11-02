export const EMAIL_CODE_RESEND_COOLDOWN_SECONDS = 60;

export function validateSoftwareLogin(identifier, password) {
  return {
    identifier: identifier.trim() ? null : "请输入邮箱或用户名",
    password: password.length > 0 ? null : "请输入密码",
  };
}

export function normalizeCooldownSeconds(value) {
  const seconds = Number(value);
  return Number.isFinite(seconds) && seconds > 0 ? Math.ceil(seconds) : 0;
}

export function retryAfterSecondsFromError(error) {
  if (!error || typeof error !== "object" || error.status !== 429) return 0;
  const details = error.details;
  if (!details || typeof details !== "object") return 0;
  return normalizeCooldownSeconds(details.retryAfterSeconds);
}

export function remainingCooldownSeconds(deadlineAt, now) {
  if (!Number.isFinite(deadlineAt) || !Number.isFinite(now)) return 0;
  return normalizeCooldownSeconds((deadlineAt - now) / 1000);
}
