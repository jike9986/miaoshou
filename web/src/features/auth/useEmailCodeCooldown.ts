import { useCallback, useEffect, useState } from "react";
import {
  normalizeCooldownSeconds,
  remainingCooldownSeconds,
} from "@/features/auth/authFormHelpers.mjs";

export function useEmailCodeCooldown() {
  const [deadlineAt, setDeadlineAt] = useState<number | null>(null);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (deadlineAt === null) return;

    const tick = () => {
      const current = Date.now();
      setNow(current);
      if (remainingCooldownSeconds(deadlineAt, current) === 0) {
        setDeadlineAt(null);
      }
    };

    const timer = window.setInterval(tick, 1000);
    return () => window.clearInterval(timer);
  }, [deadlineAt]);

  const startCooldown = useCallback((value: number) => {
    const seconds = normalizeCooldownSeconds(value);
    const current = Date.now();
    setNow(current);
    setDeadlineAt(seconds > 0 ? current + seconds * 1000 : null);
  }, []);

  return {
    remainingSeconds: remainingCooldownSeconds(deadlineAt, now),
    startCooldown,
  };
}
