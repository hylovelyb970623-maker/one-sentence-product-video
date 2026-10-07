"""M0 演示 CLI：一句话 → S1→S2→S3→S4→S5→S6(mock) → 产物落盘 out/。

用法：
  python cli.py "夏季冰丝阔腿裤，凉感显瘦不闷汗，通勤女性，59元"
  python cli.py "..." --plans 5 --pick 2     # 生成5个方案选第2个
  python cli.py "..." --replay               # LLM 用样例回放（不调 DeepSeek）
"""
import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pipeline import s1_product, s2_creative, s3_storyboard, s4_compliance, s5_adapter, s6_gateway  # noqa: E402
from pipeline.config import load_env


def banner(title: str) -> None:
    print(f"\n{'=' * 12} {title} {'=' * 12}")


def main() -> None:
    ap = argparse.ArgumentParser(description="带货视频生成管线 M0")
    ap.add_argument("sentence", help="商品一句话介绍")
    ap.add_argument("--plans", type=int, default=3, help="生成创意方案数量（默认3）")
    ap.add_argument("--pick", type=int, default=1, help="选定第几个方案（默认1）")
    ap.add_argument("--replay", action="store_true", help="LLM 用样例回放，不调 DeepSeek")
    ap.add_argument("--real", action="store_true",
                    help="S6 用真实 H3 引擎（ComfyUI Ref2VA）生成视频，默认仅第 1 镜")
    ap.add_argument("--real-shots", type=int, default=1, help="真实生成的镜头数（默认 1，其余走 mock）")
    ap.add_argument("--product-image", help="商品白底图路径（--real 时未提供则用合成测试图）")
    args = ap.parse_args()

    load_env()

    banner("S1 商品结构化（DeepSeek）")
    product = s1_product.run(args.sentence, replay=args.replay)
    print(f"商品：{product.product_name}（{product.category}）｜人群：{product.audience}｜价格带：{product.price_band}")
    for sp in product.selling_points:
        print(f"  · 卖点：{sp.point} —— {sp.evidence}")

    banner(f"S2 创意方案（{args.plans} 个，钩子策略差异化）")
    plans = s2_creative.run(product, n=args.plans, replay=args.replay)
    for i, p in enumerate(plans.plans, 1):
        print(f"  [{i}] {p.hook_type}｜{p.hook_line}")
        print(f"      角度：{p.angle}")
    pick = min(max(args.pick, 1), len(plans.plans))
    plan = plans.plans[pick - 1]
    print(f"→ 选定方案 [{pick}] {plan.hook_type}")

    banner("S3 分镜展开 + S4 极限词净化")
    board = s3_storyboard.run(plan, product, replay=args.replay)
    hits = s4_compliance.check_storyboard(board)
    total = sum(s.duration for s in board.shots)
    for s in board.shots:
        print(f"  镜{s.shot_id} {s.duration}s｜{s.shot_size}·{s.camera_move}｜{s.subject}，{s.action}")
        print(f"        字幕：{s.subtitle}｜首帧策略：{s.first_frame_policy}")
    if hits:
        print(f"  [S4] 极限词命中并已改写：{hits}")
    else:
        print(f"  [S4] 极限词检查通过（合计 {total}s）")

    banner("S5 引擎适配（同一 ShotPrompt → 双引擎语法）")
    h3_prompts = [s5_adapter.to_h3(s, product) for s in board.shots]
    seed_prompts = [s5_adapter.to_seedance(s, product) for s in board.shots]
    print(f"  [H3]      {h3_prompts[0]['prompt']}")
    print(f"  [Seedance] {seed_prompts[0]['prompt']}")

    banner("S6 生成任务（--real 走 ComfyUI Ref2VA，否则 mock）")
    product_image = args.product_image
    if args.real and not product_image:
        from pipeline.s0_assets import make_test_product

        product_image = make_test_product(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                       "out", "test_product.png"))
        print(f"  未提供商品图，使用合成测试图：{product_image}")
    gateway = s6_gateway.get_engine(product_image if args.real else None)
    mock = s6_gateway.MockEngine()
    tasks = []
    for i, (shot, payload) in enumerate(zip(board.shots, h3_prompts), 1):
        if args.real and i <= args.real_shots:
            print(f"  镜{shot.shot_id}｜h3 真实生成中（约数分钟）…", flush=True)
            t = gateway.submit_and_run(shot.shot_id, "h3", "draft", payload)
            res = t.get("result") or {}
            print(f"  镜{shot.shot_id}｜h3 打样｜{t['status']}｜耗时 {res.get('elapsed_s')}s｜"
                  f"产物 {res.get('video_path')}")
        else:
            t = mock.submit_and_run(shot.shot_id, "h3", "draft", payload)
            print(f"  镜{shot.shot_id}｜h3 打样(mock)｜{t['status']}")
        tasks.append(t)
    t_final = mock.submit_and_run(board.shots[0].shot_id, "seedance", "final", seed_prompts[0])
    tasks.append(t_final)
    print(f"  镜1｜seedance 定稿（演示协议）｜{t_final['status']}｜cost={t_final['cost_credits']}")
    est = round(total * s6_gateway.SEEDANCE_PER_SEC, 2)
    print(f"  定稿全片成本估算：{est} 元（Seedance 约 1 元/s）｜打样成本：0 元（本地 H3）")

    out = {
        "input_sentence": args.sentence,
        "product": product.model_dump(),
        "plans": plans.model_dump(),
        "selected_plan_index": pick,
        "storyboard": board.model_dump(),
        "compliance_hits": [[sid, h] for sid, h in hits],
        "engine_prompts": {"h3": h3_prompts, "seedance": seed_prompts},
        "tasks": tasks,
    }
    run_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out",
                           dt.datetime.now().strftime("run_%Y%m%d_%H%M%S"))
    os.makedirs(run_dir, exist_ok=True)
    out_path = os.path.join(run_dir, "pipeline.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n✔ 管线完成，全产物：{out_path}")


if __name__ == "__main__":
    main()
