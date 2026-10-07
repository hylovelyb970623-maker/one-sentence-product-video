"""S4 文案合规：广告法极限词正则层（LLM 语义复检层后续接入）。"""
import re

# (模式, 替换)：命中即替换并记录。词表需法务审校、按月更新（pipeline-design.md §3）。
RULES: list[tuple[str, str]] = [
    (r"全网最低", "全网超值"),
    (r"最低价", "超值价"),
    (r"史上最", "非常有竞争力"),
    (r"最(佳|好|强|快|美|多|热|新|便|靓|划算)", r"超\1"),
    (r"第一(品牌|名|家|款|选择)", r"知名\1"),
    (r"国家级", "高级"),
    (r"世界级", "高端"),
    (r"绝对", "十分"),
    (r"百分百|100%有效", "口碑很好"),
    (r"根治", "改善"),
    (r"秒瘦|暴瘦", "显瘦"),
    (r"永久", "长效"),
]


def clean_text(text: str) -> tuple[str, list[str]]:
    hits: list[str] = []
    out = text
    for pat, rep in RULES:
        found = re.findall(pat, out)
        if found:
            hits.extend(f"{pat} → 命中『{w}』" if isinstance(w, str) and w not in pat else pat for w in found)
            out = re.sub(pat, rep, out)
    return out, hits


def check_storyboard(board) -> list[tuple[int, list[str]]]:
    """逐镜净化字幕，原地修改并返回命中报告 [(shot_id, hits)]。"""
    report = []
    for shot in board.shots:
        new_text, hits = clean_text(shot.subtitle)
        if hits:
            shot.subtitle = new_text
            report.append((shot.shot_id, hits))
    return report
