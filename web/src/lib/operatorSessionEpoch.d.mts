export function advanceOperatorSessionEpoch(): number;
export function getOperatorSessionEpoch(): number;
export function runIfOperatorSessionCurrent(epoch: number, action: () => void): boolean;
