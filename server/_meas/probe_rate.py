import sys, os, json
sys.path.insert(0, os.getcwd())
from services.price_template import display_price, apply_template

out = []
# 完整费率
full = {"name":"完整","vatChargePercent":6,"paymentChargePercent":2,"platformCommissionPercent":5,
        "bcpChargePercent":1,"withdrawChargePercent":0.5,"sfpChargePercent":1,
        "affiliateChargePercent":2,"lastMileFreightServiceTaxPercent":8,
        "profitPercent":20,"profitType":"bySalePriceProfit",
        "firstWeightGrams":30,"firstWeightPrice":0.18,"continuedUnitGrams":10,"continuedUnitPrice":0.12}
r = display_price(30, 0.5, full, rate="1.64")
out.append({"case":"完整","price":r["price"],"missing":r["missingRateFields"],"estimate":r["isEstimate"]})

# 缺费率
thin = {"name":"精简","vatChargePercent":6,"profitPercent":20,"profitType":"bySalePriceProfit",
        "firstWeightGrams":30,"firstWeightPrice":0.18,"continuedUnitGrams":10,"continuedUnitPrice":0.12}
r2 = display_price(30, 0.5, thin, rate="1.64")
out.append({"case":"缺费率","price":r2["price"],"missing":r2["missingRateFields"],"estimate":r2["isEstimate"]})

# apply_template 的 rateSource
item = {"weight":"0.5","skuMap":{"a":{"originPrice":"30","price":"50"}}}
_, rep = apply_template(item, thin, rate=None)
out.append({"case":"rateSource","src":rep["rateSource"],"rate":rep["rate"],"changed":rep["changedCount"]})
open("_meas/rate.json","w",encoding="utf-8").write(json.dumps(out, ensure_ascii=False, indent=1))
print("done")
