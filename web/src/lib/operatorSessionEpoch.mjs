let operatorSessionEpoch = 0;

export function advanceOperatorSessionEpoch() {
  operatorSessionEpoch += 1;
  return operatorSessionEpoch;
}

export function getOperatorSessionEpoch() {
  return operatorSessionEpoch;
}

export function runIfOperatorSessionCurrent(epoch, action) {
  if (epoch !== operatorSessionEpoch) return false;
  action();
  return true;
}
