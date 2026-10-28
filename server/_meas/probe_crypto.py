import sys, os, json, base64
sys.path.insert(0, os.getcwd())
from services import crypto

out = []
# 1. 往返
plain = "my-secret-value-12345"
token = crypto.encrypt(plain)
out.append({"case": "加密前缀", "prefix": token[:6], "isDpapi": token.startswith("dpapi:")})
out.append({"case": "往返一致", "ok": crypto.decrypt(token) == plain})

# 2. 空值
out.append({"case": "空值", "enc": crypto.encrypt(""), "dec": crypto.decrypt("")})

# 3. 损坏密文必须抛错，不能返回空串
for bad in ["gAAAAABcorrupted", "dpapi:bm90LXZhbGlk"]:
    try:
        got = crypto.decrypt(bad)
        out.append({"case": "损坏密文", "bad": bad[:16], "returned": repr(got), "shouldRaise": True})
    except crypto.CredentialError as e:
        out.append({"case": "损坏密文", "bad": bad[:16], "raised": True, "msg": str(e)[:60]})

# 4. try_decrypt 降级
out.append({"case": "try_decrypt 损坏", "value": repr(crypto.try_decrypt("gAAAAAbroken"))})

open("_meas/crypto.json","w",encoding="utf-8").write(json.dumps(out, ensure_ascii=False, indent=1))
print("done")
