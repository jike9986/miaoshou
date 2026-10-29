import type { ProductQualityReviewRequest } from "@/lib/types";

export interface QualityReviewValues {
  weightKg: string;
  packageLength: string;
  packageWidth: string;
  packageHeight: string;
  source: string;
  evidence: string;
}

export function buildQualityReviewRequest(
  values: QualityReviewValues,
  expectedRevision: number
): { request: ProductQualityReviewRequest } | { error: string } {
  const measurements = [
    Number(values.weightKg),
    Number(values.packageLength),
    Number(values.packageWidth),
    Number(values.packageHeight),
  ];
  if (measurements.some((value) => !Number.isFinite(value) || value <= 0)) {
    return { error: "重量和长、宽、高都必须是大于 0 的有效数字。" };
  }
  if (measurements[0] > 1000 || measurements.slice(1).some((value) => value > 10000)) {
    return { error: "重量不能超过 1000 kg，尺寸不能超过 10000 cm。" };
  }
  const source = values.source.trim();
  const evidence = values.evidence.trim();
  if (!source || evidence.length < 3) {
    return { error: "请填写核验来源和至少 3 个字符的核验依据。" };
  }
  if (!Number.isInteger(expectedRevision) || expectedRevision < 1) {
    return { error: "商品版本号缺失，请刷新详情后再提交。" };
  }
  return {
    request: {
      weightKg: measurements[0],
      packageLength: measurements[1],
      packageWidth: measurements[2],
      packageHeight: measurements[3],
      source,
      evidence,
      expectedRevision,
    },
  };
}
