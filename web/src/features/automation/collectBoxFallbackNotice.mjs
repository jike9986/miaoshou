export function getCollectBoxFallbackNotice({ connected, degraded, refreshFailedWithItems }) {
  if (!connected) return null;
  if (degraded) {
    return "显示的是上次缓存，刷新未能联系妙手。当前列表只可查看；恢复连接并刷新成功后才能开始处理。";
  }
  if (refreshFailedWithItems) {
    return "刷新失败，以下为上次成功加载的列表。当前列表只可查看；刷新成功后才能开始处理。";
  }
  return null;
}

export function shouldShowCollectBoxReadError({ connected, loading, error, itemCount }) {
  return Boolean(connected && !loading && error && itemCount === 0);
}
