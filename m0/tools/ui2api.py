"""把 ComfyUI UI 格式工作流转成 API 格式（提交 /prompt 用）。

用法：python tools/ui2api.py <ui.json> <out.json>
依赖同目录 ../workflows/object_info_all.json（服务器 /object_info 全量导出）。
"""
import json
import os
import sys

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "workflows")
SCALAR_TYPES = {"STRING", "INT", "FLOAT", "COMBO", "BOOLEAN"}
SEED_CTRL_VALUES = {"fixed", "increment", "decrement", "random"}  # seed 控件附带项，跳过


def convert(ui_path: str, api_path: str) -> dict:
    ui = json.load(open(ui_path, encoding="utf-8"))
    obj = json.load(open(os.path.join(BASE, "object_info_all.json"), encoding="utf-8"))
    links = {l[0]: l for l in ui["links"]}  # [id, from_node, from_slot, to_node, to_slot, type]

    api = {}
    for node in ui["nodes"]:
        ntype = node["type"]
        if ntype in ("Note", "MarkdownNote") or ntype not in obj:
            continue
        info = obj[ntype]["input"]

        scalar_names = []
        for section in ("required", "optional"):
            for name, spec in (info.get(section) or {}).items():
                if not (isinstance(spec, list) and spec):
                    continue
                typ = spec[0]
                # COMBO 类型的首元素是选项列表而非类型字符串
                if isinstance(typ, list) or (isinstance(typ, str) and typ.upper() in SCALAR_TYPES):
                    scalar_names.append(name)

        link_inputs = {inp["name"]: inp.get("link") for inp in (node.get("inputs") or [])}

        widgets = list(node.get("widgets_values") or [])
        wi = 0
        inputs = {}
        for name in scalar_names:
            if name in link_inputs and link_inputs[name] is not None:
                continue  # 该输入被连线接管（如 PrimitiveFloat），不吃 widget 值
            if wi >= len(widgets):
                break
            inputs[name] = widgets[wi]
            wi += 1
            if isinstance(widgets[wi - 1], int) and name.lower() in ("seed", "noise_seed") \
                    and wi < len(widgets) and widgets[wi] in SEED_CTRL_VALUES:
                wi += 1  # 跳过 seed 后附带的 control_after_generate

        for name, lid in link_inputs.items():
            if lid is None:
                continue
            l = links[lid]
            inputs[name] = [str(l[1]), l[2]]

        api[str(node["id"])] = {"class_type": ntype, "inputs": inputs}

    with open(api_path, "w", encoding="utf-8") as f:
        json.dump(api, f, ensure_ascii=False, indent=2)
    return api


if __name__ == "__main__":
    result = convert(sys.argv[1], sys.argv[2])
    print(f"转换完成：{len(result)} 节点 → {sys.argv[2]}\n")
    for nid, n in sorted(result.items(), key=lambda x: int(x[0])):
        preview = json.dumps(n["inputs"], ensure_ascii=False)
        print(f"[{nid}] {n['class_type']}: {preview[:150]}")
