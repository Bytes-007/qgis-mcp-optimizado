"""A/B quality check: upstream (9876) vs optimizacion fork (9877).

Verifies the token optimizations do not change *what* data comes back, only its
size/shape. Run with QGIS sessions open on both ports:

    python benchmarks/quality_ab.py

Exits non-zero if any check fails.
"""
import random
import sys

sys.path.insert(0, "src")
from qgis_mcp.client import QgisMCPClient

UP = QgisMCPClient(port=9876)
FORK = QgisMCPClient(port=9877)
VEREDAS = "Veredas_Chinacota_9b8210dc_0fe9_4b81_a245_c70231b8e20d"

results = []


def check(name, ok, detail=""):
    results.append((ok, name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def send(c, cmd, params, timeout=120):
    r = c.send_command(cmd, params, timeout=timeout)
    if r.get("status") != "success":
        return r
    return r["result"]


UP.connect()
FORK.connect()

# --- 1. get_layers: same layers, fork only adds pagination metadata --------
u = send(UP, "get_layers", {})
f = send(FORK, "get_layers", {})
u_layers = [l["id"] for l in u["layers"]]
f_layers = [l["id"] for l in f["layers"]]
check("get_layers: same layer ids+order", u_layers == f_layers,
      f"up={len(u_layers)} fork={len(f_layers)}")
f_extra = set(f) - set(u)
import json as _json
shared_ok = all(
    _json.dumps(l_u, sort_keys=True, default=str) == _json.dumps(l_f, sort_keys=True, default=str)
    for l_u, l_f in zip(u["layers"], f["layers"]))
check("get_layers: shared content identical", shared_ok)
check("get_layers: fork adds only pagination keys",
      f_extra <= {"total_layers", "offset", "truncated", "count", "limit"},
      f"extra keys={sorted(f_extra)}")

# --- 2. get_project_info ---------------------------------------------------
u = send(UP, "get_project_info", {})
f = send(FORK, "get_project_info", {})
for key in ("filename", "title", "crs", "layers", "layer_count"):
    if key in u and key in f:
        check(f"get_project_info.{key} equal", u[key] == f[key])

# --- 3. get_layer_features all fields: must be byte-identical content -------
u = send(UP, "get_layer_features", {"layer_id": VEREDAS, "limit": 10})
f = send(FORK, "get_layer_features", {"layer_id": VEREDAS, "limit": 10})
check("features(all fields): identical", u == f)

# --- 4. get_layer_features fields projection: values must match upstream ----
FC = ["NOMBRE_VER", "NOMB_MPIO"]
u = send(UP, "get_layer_features", {"layer_id": VEREDAS, "limit": 10})
f = send(FORK, "get_layer_features", {"layer_id": VEREDAS, "limit": 10, "fields": FC})
uf = [{k: row[k] for k in FC} for row in u["features"]]
ff = [{k: row[k] for k in FC} for row in f["features"]]
check("features(fields): projected values equal upstream", uf == ff)
check("features(fields): fork returns only requested fields",
      all(set(row) <= set(FC) | {"_fid"} for row in f["features"]))

# --- 5. expression filter: same result both --------------------------------
expr = "upper('NOMBRE_VER') LIKE '%PANTANO%'" if True else None
# upstream may not accept expression? it does (documented); test equality
try:
    u = send(UP, "get_layer_features", {"layer_id": VEREDAS, "limit": 10, "expression": expr})
    f = send(FORK, "get_layer_features", {"layer_id": VEREDAS, "limit": 10, "expression": expr})
    check("features(expression): identical", u == f)
except Exception as e:
    check("features(expression): identical", False, str(e))

# --- 6. list_processing_algorithms: fork summary matches upstream counts ----
u = send(UP, "list_processing_algorithms", {})["algorithms"]
f = send(FORK, "list_processing_algorithms", {})
u_counts = {}
for a in u:
    u_counts[a["provider"]] = u_counts.get(a["provider"], 0) + 1
f_counts = f["providers"]
common = set(u_counts) & set(f_counts)
diffs = {p: (u_counts[p], f_counts[p]) for p in common if u_counts[p] != f_counts[p]}
only_up = set(u_counts) - set(f_counts)
check("algorithms: counts equal on common providers", not diffs, f"diffs={diffs}")
check("algorithms: providers only upstream are external plugins",
      {p for p in only_up} == {"quickosm"} or not only_up,
      f"only_upstream={sorted(only_up)} (environment: third-party plugin QGIS-providers)")
# sample discoverable via fork provider paging (only from common providers)
random.seed(42)
pool = [a for a in u if a["provider"] in common]
sample = random.sample(pool, min(20, len(pool)))
fork_ids = set()
for prov in u_counts:
    off = 0
    while True:
        page = send(FORK, "list_processing_algorithms", {"provider": prov, "limit": 100,
                                                         "offset": off})
        fork_ids |= {a["id"] for a in page["algorithms"]}
        if not page["truncated"]:
            break
        off += 100
missing = [a["id"] for a in sample if a["id"] not in fork_ids]
check("algorithms: sampled 20 ids discoverable in fork", not missing,
      f"missing={missing}")
# search still finds one known alg by substring
f = send(FORK, "list_processing_algorithms", {"search": "buffer"})
u_search = [a["id"] for a in u if "buffer" in a["id"].lower()
            or "buffer" in a["name"].lower()]
f_search = [a["id"] for a in f["algorithms"]]
check("algorithms: search 'buffer' same hits", sorted(u_search) == sorted(f_search),
      f"n={len(f_search)}")

# --- 7. execute_code: output cap keeps head, small result survives ----------
code_big = "\n".join(f"line {i:05d}" for i in range(2000))
f = send(FORK, "execute_code", {"code": f"print({code_big!r})"})
check("execute_code: marks truncation", bool(f.get("stdout_truncated")),
      f"chars={len(f.get('stdout',''))}")
check("execute_code: head preserved", "line 00000" in f.get("stdout", ""))
u = send(UP, "execute_code", {"code": "print(2+2)"})
f = send(FORK, "execute_code", {"code": "print(2+2)"})
check("execute_code: small result identical", u["stdout"].strip() == "4"
      and f["stdout"].strip() == "4")

# --- 8. get_message_log: structure parity (content is a live event stream,
#         each instance logs its own commands, so values are not comparable) --
u = send(UP, "get_message_log", {"limit": 50})
f = send(FORK, "get_message_log", {"limit": 50})
check("message_log: same keys per entry", u.get("messages", [{}])[0].keys()
      == f.get("messages", [{}])[0].keys())

# --- 9. layer fields metadata ------------------------------------------------
u = send(UP, "get_layer_info", {"layer_id": VEREDAS})
f = send(FORK, "get_layer_info", {"layer_id": VEREDAS})
u_names = [x["name"] for x in u.get("fields", [])]
f_names = [x["name"] for x in f.get("fields", [])]
check("layer_info: field names equal", u_names == f_names)

print(f"\n{sum(1 for ok,_,_ in results if ok)}/{len(results)} PASS")
sys.exit(0 if all(ok for ok, _, _ in results) else 1)

