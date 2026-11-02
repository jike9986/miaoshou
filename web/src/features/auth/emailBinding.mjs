/** Normalize one mailbox using the same basic constraints as the account API. */
export function normalizeEmailForVerification(value) {
  const email = value.trim();
  if (!email || email.length > 254 || (email.match(/@/g) ?? []).length !== 1) return null;

  const [local, domain] = email.split("@");
  if (!local || local.length > 64 || !domain || domain.length > 253
    || local.startsWith(".") || local.endsWith(".") || local.includes("..")
    || !/^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+$/.test(local)) return null;

  const labels = domain.split(".");
  if (labels.length < 2 || labels.some((label) => label.length > 63
    || !/^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$/.test(label))) return null;

  return email.toLowerCase();
}
