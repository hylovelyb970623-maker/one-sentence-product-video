import base64
import io
import json
import os
from pathlib import Path
import sys
import threading
import pytest
from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from pipeline import creative, render
from pipeline.creative import Creative, PlanResult, ShotScript
from webapp import server


def fake_creative():
    return Creative(product_seen='蓝色塑料水杯', hook='蓝色水杯，冰感随行', cta='下单带回家',
                    points_shown=['冰感'], shots=[
        ShotScript(scene='办公室工位', action='①白领撑着额头，缓慢眨眼，另一只手有气无力地敲键盘 ②同事的手从画外把杯子递到桌角 ③主人公右手握住杯身接过，同事的手撤出画面', mood='疲惫：眼神发直、肩膀垮',
                   expression='眉心微蹙、眼皮沉、目光涣散', product_entry='第3秒同事从画外递入', shot_size='中景', camera_move='缓推'),
        ShotScript(scene='浅色桌面', action='①她右手握住杯身举到嘴边 ②仰头畅快饮水一大口后放下杯子 ③坐直身体，手回到键盘恢复利落敲击', mood='回神：肩膀松了、重新看向屏幕',
                   expression='眼睛亮起来、嘴角上扬、长舒一口气', product_entry='全程', shot_size='特写', camera_move='固定'),
    ])


def fake_plan(sentence='蓝色水杯，300ml', price=None, image_data_url=None):
    return PlanResult(creative=fake_creative(), vision_used=bool(image_data_url),
                      note='' if image_data_url else '视觉未用')


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'OUT', tmp_path)
    monkeypatch.setattr(server, 'JOBS', tmp_path / 'jobs')
    server.JOBS.mkdir()
    monkeypatch.setattr(server.creative, 'plan', fake_plan)
    calls = {'n': 0}

    def fake_yesno(url, question, extra_image=None):
        if '两个或以上' in question:
            return False                      # 无幽灵多杯
        if '明显减少' in question or '明显低于杯口' in question:
            return True                       # 首尾帧对比：液体量可见减少（真实喝掉）
        if '饮用饮品' in question:
            return True                       # 饮用动作真实拍出
        if '杯装或瓶装饮品' in question:
            calls['n'] += 1
            return calls['n'] % 2 == 0        # 首帧无商品(早=False)，末帧有商品(晚=True)
        return None

    monkeypatch.setattr(server.llm, 'ask_yesno', fake_yesno)
    with TestClient(server.app) as client:
        yield client


def picture():
    stream = io.BytesIO()
    Image.new('RGB', (128, 128), '#ed6020').save(stream, 'PNG')
    return stream.getvalue()


def submit(api, **data):
    return api.post('/api/start', data={'sentence': '蓝色水杯，300ml', **data}, files={'image': ('product.png', picture(), 'image/png')})


def test_validation_and_auth(api):
    assert api.get('/api/admin/jobs').status_code == 401
    assert api.get('/admin').status_code == 401
    assert api.get('/media/session.json').status_code == 404
    assert api.get('/static/admin.html').status_code == 404
    assert api.get('/api/status', headers={'host': 'evil.test'}).status_code == 403
    assert submit(api, price='-10').status_code == 422
    assert submit(api, sentence='减脂神器').status_code == 422
    assert submit(api, size='giant').status_code == 422
    r = api.post('/api/start', data={'sentence':'water'}, files={'image':('a.png', b'not an image','image/png')})
    assert r.status_code == 422
    assert api.post('/api/generate').status_code == 410
    assert not list(server.JOBS.iterdir())


def test_same_origin_any_port_allowed_foreign_blocked(api):
    """同源请求不限端口放行（422 表示穿过中间件到达校验层），外部来源仍然 403。"""
    r = api.post('/api/start', data={'sentence': '蓝色水杯'},
                 headers={'Host': '127.0.0.1:8765', 'Origin': 'http://127.0.0.1:8765'})
    assert r.status_code == 422                      # 必填商品图缺失 → 校验层 422，而非中间件 403
    r = api.get('/api/status', headers={'Host': '127.0.0.1:8765', 'Origin': 'http://127.0.0.1:8765'})
    assert r.status_code == 200
    r = api.post('/api/start', data={'sentence': '蓝色水杯'},
                 headers={'Host': '127.0.0.1:8765', 'Origin': 'https://evil.example'})
    assert r.status_code == 403 and 'Cross-origin' in r.json()['detail']
    assert not list(server.JOBS.iterdir())           # 拦截与校验失败都不产生任务


def test_image_and_original_facts():
    assert server.normalized_image(picture()).size == (128,128)
    assert server.validate_facts('蓝色杯子', '') == ('蓝色杯子', None)
    assert server.validate_facts('蓝色杯子', '19.90') == ('蓝色杯子', '19.90')
    with pytest.raises(Exception):
        server.normalized_image(b'<svg>')


@pytest.fixture(scope='module')
def raw_media(tmp_path_factory):
    folder=tmp_path_factory.mktemp('raw')
    files=[]
    for i, color in enumerate(['blue','green']):
        path=folder/f'raw{i}.mp4'
        render.run(['-f','lavfi','-i',f'color=c={color}:s=432x768:r=24','-t','5.166667','-c:v','libx264','-pix_fmt','yuv420p',path])
        files.append(str(path))
    return files


def test_real_render(raw_media, tmp_path):
    output=render.compose(raw_media, ['蓝色水杯，300ml','查看商品详情'], '19.90', tmp_path)
    assert render.frame_count(output)==240
    info=render.run(['-i',output,'-f','null','-']).stderr
    assert '00:00:10.00' in info and 'Audio: aac' in info and '432x768' in info
    assert 'ai_generated' in info
    assert (tmp_path/'original_bgm.wav').stat().st_size>100000
    # Inspect overlay pixels to verify optional price banner and real CJK glyph rasterization.
    a=Image.open(tmp_path/'overlay_0.png'); b=Image.open(tmp_path/'overlay_1.png')
    assert a.getpixel((30,90))[3]==0 and b.getpixel((30,90))[3]>0
    assert len(set(a.crop((20,590,410,710)).getdata()))>20


def test_short_video_rejected(tmp_path):
    raw=tmp_path/'short.mp4'
    render.run(['-f','lavfi','-i','color=s=64x64:r=24','-t','1','-c:v','libx264',raw])
    with pytest.raises(ValueError, match='shorter'):
        render.compose([raw,raw], ['a','b'], None, tmp_path)


def test_creative_guardrails():
    plan = fake_plan()
    creative_obj = plan.creative
    assert len(creative_obj.shots) == 2
    prompt = creative.shot_prompt(creative_obj.shots[0])
    assert '<Picture 1>' in prompt and '{@图1}' not in prompt
    # Fabricated efficacy claims are rejected by validators (triggers LLM retry upstream).
    with pytest.raises(Exception):
        Creative(hook='喝了立刻提神醒脑', cta='点击下单', shots=creative_obj.shots)
    with pytest.raises(Exception):
        Creative(hook='蓝色水杯，冰感随行', cta='只要19.9元', shots=creative_obj.shots)
    with pytest.raises(Exception):
        Creative(hook='蓝色水杯', cta='下单', shots=creative_obj.shots[:1])


def test_job_uses_llm_creative_and_tag(api, monkeypatch, raw_media):
    finished=threading.Event(); submitted=[]
    class Fake:
        def check(self): return {}
        def queue_size(self): return 0
        def upload_image(self,*args,**kwargs): return 'reference.png'
        def submit(self,wf):
            submitted.append(wf); return f'prompt-{len(submitted)}'
        def wait(self,prompt_id,**kwargs): return {}
        def download_outputs(self,entry,directory): return [raw_media[len(submitted)-1]]
    monkeypatch.setattr(server.ComfyUI,'from_env',lambda:Fake())
    original=server.release
    def release():
        original();finished.set()
    monkeypatch.setattr(server,'release',release)
    r=submit(api,price='19.9');assert r.status_code==202
    job_id=r.json()['job_id']
    assert finished.wait(45)
    job=server.read_job(job_id)
    # LLM creative actually drove captions and shot prompts
    assert job['creative']['hook']=='蓝色水杯，冰感随行'
    assert job['subtitles']==['蓝色水杯，冰感随行','下单带回家']
    for shot in job['shots']:
        assert '<Picture 1>' in shot['prompt'] and '{@图1}' not in shot['prompt']
        assert '人物状态' in shot['prompt']  # 转变弧的情绪状态进入 H3 提示词
        assert '面部表演（特写级）' in shot['prompt']  # 表情演绎指令进入 H3 提示词
        assert '眼睛亮起来' in shot['prompt'] or '眉心微蹙' in shot['prompt']
        assert '纪录片式生活化表演' in shot['prompt'] and '禁止打哈欠' in shot['prompt']
        assert '白领撑着额头' in shot['prompt'] or '畅快饮水' in shot['prompt']
    # 首镜商品迟到约定；衔接镜 = 商品参考图 + 收尾参考视频双锁定（递入动作被禁）
    assert '进入之前画面中不得出现该商品' in job['shots'][0]['prompt']
    assert '参考视频片段' in job['shots'][1]['prompt']
    assert '禁止再次发生' in job['shots'][1]['prompt']
    # 液面硬约束独立成段进入末镜提示词；液面质检已执行
    assert '物理连续性硬约束' in job['shots'][1]['prompt']
    assert '禁止放下后液面与喝之前相同' in job['shots'][1]['prompt']
    assert job['shots'][1]['liquid_check']['checked'] is True
    assert job['shots'][0].get('continuity') == '下一镜以本镜收尾 2 秒为参考视频 + 商品参考图双锁定（递入动作不进入参考）'
    assert job['shots'][0]['product_entry_check']['checked'] is True
    assert job['shots'][1]['product_entry_check']['checked'] is False
    for wf_idx, wf in enumerate(submitted):
        assert wf['3']['class_type'] == 'MiniMaxH3ReferenceToVideo'   # 两镜都走 Ref2VA
        assert '<Picture 1>' in wf['3']['inputs']['prompt']
        assert wf['3']['inputs']['ref_image_size'] == 'match'
        if wf_idx == 1:
            assert wf['3']['inputs']['ref_videos.ref_video_0'] == ['16', 0]  # 参考视频接通
            assert '参考视频片段' in wf['3']['inputs']['prompt']
    assert api.get('/api/admin/jobs',auth=('admin',server.ADMIN_PASSWORD)).status_code==200
    assert api.get('/api/admin/jobs',auth=('admin',server.ADMIN_PASSWORD)).status_code==200


@pytest.mark.parametrize('size,orient,gen_w,out_size', [
    ('standard', 'vertical', 448, '432x768'),
    ('standard', 'horizontal', 800, '768x432'),
    ('hd', 'vertical', 768, '720x1280')])
def test_complete_job_mock_remote_actual_render(api, monkeypatch, raw_media, size, orient, gen_w, out_size):
    finished=threading.Event(); submitted=[]; uploads=[]
    class Fake:
        def check(self): return {}
        def queue_size(self): return 0
        def upload_image(self,*args,**kwargs):
            uploads.append(os.path.basename(str(path_ := args[0] if args else kwargs.get('path','x'))))
            return f'ref-{len(uploads)}'
        def submit(self,wf):
            submitted.append(wf)
            assert wf['3']['inputs']['length']==124
            assert wf['3']['inputs']['ref_image_size']=='match'
            assert wf['3']['inputs']['width']==gen_w
            if len(submitted)==2:  # 衔接镜必须携带参考视频（人物/场景/商品状态镜像）
                assert wf['3']['inputs']['ref_videos.ref_video_0']==['16',0]
            return f'prompt-{len(submitted)}'
        def upload_image(self,path,*args,**kwargs):
            uploads.append(os.path.basename(str(path)))
            return f'ref-{len(uploads)}'
        def wait(self,prompt_id,**kwargs):
            jobs=server.all_jobs()
            assert jobs[0]['shots'][len(submitted)-1]['prompt_id']==prompt_id
            return {}
        def download_outputs(self,entry,directory): return [raw_media[len(submitted)-1]]
    monkeypatch.setattr(server.ComfyUI,'from_env',lambda:Fake())
    original=server.release
    def release():
        original();finished.set()
    monkeypatch.setattr(server,'release',release)
    r=submit(api,price='19.9',size=size,orientation=orient);assert r.status_code==202
    job_id=r.json()['job_id']
    assert finished.wait(45)
    status=api.get('/api/status',params={'job_id':job_id}).json()
    assert status['stage']=='done' and len(submitted)==2
    assert 'shots' not in status and 'facts' not in status
    assert api.get(status['video']).status_code==200
    job=server.read_job(job_id)
    assert job['facts']=={'sentence':'蓝色水杯，300ml','price':'19.9'}
    assert job['size']==size and job['gen_width']==gen_w
    assert job['shots'][1]['prompt_id']=='prompt-2'
    info=render.run(['-i',job['final_path'],'-f','null','-']).stderr
    assert out_size in info and '00:00:10.00' in info
    assert api.get('/api/admin/jobs',auth=('admin',server.ADMIN_PASSWORD)).status_code==200


def test_two_stage_perceive_then_director(monkeypatch):
    """眼睛看图、笔杆子策划：识别结果注入 DeepSeek 的策划输入。"""
    captured = {}

    def fake_call_json(stage, system, user, model_cls, fallback, image_data_url=None):
        if stage == 'perceive':
            assert image_data_url == 'data:image/png;base64,AAA'
            assert '视觉商品分析师' in system
            return creative.ProductSeen(product_seen='蓝色塑料水杯，杯盖白色')
        captured['system'] = system
        captured['user'] = user
        c = fake_creative()
        c.product_seen = ''
        return c

    monkeypatch.setattr(creative.llm, 'vision_ready', lambda: True)
    monkeypatch.setattr(creative, 'call_json', fake_call_json)
    result = creative.plan('蓝色水杯，300ml', None, 'data:image/png;base64,AAA')
    assert result.vision_used is True
    assert result.creative.product_seen == '蓝色塑料水杯，杯盖白色'
    assert '蓝色塑料水杯，杯盖白色' in captured['user']       # 看图事实注入策划输入
    assert '三十年' in captured['system']                    # 资深编导人设
    assert 'MiniMax H3' in captured['system']                # 最终交付物是本地模型提示词
    # 视觉不可用 → 显式降级标注
    monkeypatch.setattr(creative.llm, 'vision_ready', lambda: False)
    result = creative.plan('蓝色水杯，300ml', None, 'data:image/png;base64,AAA')
    assert result.vision_used is False and '视觉模型未配置' in result.note
    assert result.creative.product_seen == ''


def test_deepseek_outage_falls_back_to_vision_with_note(monkeypatch):
    """DeepSeek 不可用（如余额不足）→ 备用视觉模型接管策划，明确标注不静默。"""
    calls = []

    def fake_call_json(stage, system, user, model_cls, fallback, image_data_url=None, use_vision=False):
        calls.append((stage, use_vision))
        if stage == 'perceive':
            return creative.ProductSeen(product_seen='蓝色塑料水杯')
        if not use_vision:
            raise creative.llm.LLMError('creative 阶段 LLM 调用失败：402 Insufficient Balance')
        c = fake_creative()
        c.product_seen = ''
        return c

    monkeypatch.setattr(creative.llm, 'vision_ready', lambda: True)
    monkeypatch.setattr(creative, 'call_json', fake_call_json)
    result = creative.plan('蓝色水杯，300ml', None, 'data:image/png;base64,AAA')
    assert result.creative.product_seen == '蓝色塑料水杯'
    assert any(s == ('creative', True) for s in calls)          # 接管调用走了视觉模型
    assert 'DeepSeek 不可用' in result.note and '接管' in result.note


def test_shot_regenerated_when_product_too_early(api, monkeypatch, raw_media):
    """质检门：首帧检出商品过早出现 → 自动换种子重拍一次，结果落库。"""
    finished=threading.Event(); submitted=[]
    class Fake:
        def check(self): return {}
        def queue_size(self): return 0
        def upload_image(self,*args,**kwargs): return 'ref.png'
        def submit(self,wf):
            submitted.append(wf); return f'prompt-{len(submitted)}'
        def wait(self,prompt_id,**kwargs): return {}
        def download_outputs(self,entry,directory): return [raw_media[min(len(submitted)-1,1)]]
    monkeypatch.setattr(server.ComfyUI,'from_env',lambda:Fake())
    monkeypatch.setattr(server.llm,'ask_yesno',lambda *a,**k: True)  # 首帧永远检出商品
    original=server.release
    def release(): original();finished.set()
    monkeypatch.setattr(server,'release',release)
    r=submit(api);assert r.status_code==202
    job_id=r.json()['job_id']
    assert finished.wait(60)
    job=server.read_job(job_id)
    assert job['stage']=='done'
    # 第一镜重拍一次（2 次提交）+ 第二镜 1 次 = 3
    assert len(submitted)==3
    assert job['shots'][0]['product_entry_check']['early_product'] is True
    assert len(job['shots'][0]['video'])>0


def test_shot_regenerated_when_drink_action_missing(api, monkeypatch, raw_media):
    """质检门：末镜未拍出真实饮用动作（假喝）→ 自动换种子重拍一次。"""
    finished=threading.Event(); submitted=[]; calls={'n':0}
    class Fake:
        def check(self): return {}
        def queue_size(self): return 0
        def upload_image(self,*args,**kwargs): return 'ref.png'
        def submit(self,wf):
            submitted.append(wf); return f'prompt-{len(submitted)}'
        def wait(self,prompt_id,**kwargs): return {}
        def download_outputs(self,entry,directory): return [raw_media[min(len(submitted)-1,1)]]
    monkeypatch.setattr(server.ComfyUI,'from_env',lambda:Fake())
    def fake_yesno(url, question, extra_image=None):
        if '两个或以上' in question: return False
        if '饮用饮品' in question: return len(submitted) >= 3  # 第一次末镜假喝 → False，重拍后真实饮用
        if '明显减少' in question: return True
        if '杯装或瓶装饮品' in question:
            calls['n'] += 1
            return calls['n'] % 2 == 0
        return None
    monkeypatch.setattr(server.llm,'ask_yesno',fake_yesno)
    original=server.release
    def release(): original();finished.set()
    monkeypatch.setattr(server,'release',release)
    r=submit(api);assert r.status_code==202
    job_id=r.json()['job_id']
    assert finished.wait(60)
    job=server.read_job(job_id)
    assert job['stage']=='done'
    assert len(submitted)==3                              # 末镜假喝重拍一次：2+1
    assert job['shots'][1]['drink_check']['drinking'] is True
    assert job['shots'][1]['liquid_check']['level_dropped'] is True


def test_caption_notes_never_reach_video_prompt():
    """动作链里的"（字幕：…）"备注必须被剥掉：字幕由系统烧录，混入会被模型画进画面。"""
    script = fake_creative().shots[1]
    script = script.model_copy(update={'action': '①含住吸管喝下两大口（字幕：这口冰的，太爽了）②放下杯子，液面明显下降'})
    prompt = creative.shot_prompt(script)
    assert '字幕：' not in prompt.replace('画面中不出现任何字幕', '')
    assert '这口冰的' not in prompt
    assert '含住吸管喝下两大口' in prompt and '液面明显下降' in prompt


def test_extract_json_repairs_embedded_quotes():
    """视觉模型把标识文字写成未转义英文引号时，提取器自动修复（如：印有"ROOST"字样）。"""
    raw = '```json\n{\n  "product_seen": "透明杯装，印有"ROOST COFFEE"与"栖雾咖啡"文字"\n}\n```'
    assert creative.llm._extract_json(raw)['product_seen'] == '透明杯装，印有"ROOST COFFEE"与"栖雾咖啡"文字'
    # 正常 JSON 与尾逗号仍走原路径
    assert creative.llm._extract_json('{"a": 1,}') == {'a': 1}


def test_caption_note_director_rule_present():
    """编导规则明确禁止在动作链里写字幕说明。"""
    assert '禁止】写"字幕：' in creative.DIRECTOR_SYSTEM


def test_remote_execution_error_is_retryable_not_attention(api, monkeypatch):
    """远端明确报错/被中断 → 普通错误可重试，不进等待确认死锁。"""
    finished=threading.Event()
    class Fake:
        def check(self): return {}
        def queue_size(self): return 0
        def upload_image(self,*args,**kwargs): return 'ref.png'
        def submit(self,wf): return 'prompt-x'
        def wait(self,prompt_id,**kwargs):
            from pipeline.comfy_client import RemoteEnded
            raise RemoteEnded('执行失败：execution_interrupted')
    monkeypatch.setattr(server.ComfyUI,'from_env',lambda:Fake())
    original=server.release
    def release(): original();finished.set()
    monkeypatch.setattr(server,'release',release)
    r=submit(api);assert r.status_code==202
    job_id=r.json()['job_id']
    assert finished.wait(30)
    job=server.read_job(job_id)
    assert job['stage']=='error'          # 不是 attention
    assert '中断' in job['message'] or '报错' in job['message']


def test_queue_occupied_waits_then_proceeds(api, monkeypatch, raw_media):
    """GPU 被占用时自动排队等待，空闲后继续制作；不再直接失败。"""
    finished=threading.Event(); submitted=[]; queue_states=[1,1,0]  # 占用两次后空闲
    class Fake:
        def check(self): return {}
        def queue_size(self): return queue_states.pop(0) if queue_states else 0
        def upload_image(self,*args,**kwargs): return 'ref.png'
        def submit(self,wf):
            submitted.append(wf); return f'prompt-{len(submitted)}'
        def wait(self,prompt_id,**kwargs): return {}
        def download_outputs(self,entry,directory): return [raw_media[len(submitted)-1]]
    monkeypatch.setattr(server.ComfyUI,'from_env',lambda:Fake())
    original=server.release
    def release(): original();finished.set()
    monkeypatch.setattr(server,'release',release)
    r=submit(api);assert r.status_code==202
    job_id=r.json()['job_id']
    assert finished.wait(60)
    job=server.read_job(job_id)
    assert job['stage']=='done' and len(submitted)==2


def test_queue_occupied_timeout_clear_message(api, monkeypatch):
    """占用超时：明确告知用户原因，而不是笼统失败。"""
    finished=threading.Event()
    class Fake:
        def check(self): return {}
        def queue_size(self): return 2
    monkeypatch.setattr(server.ComfyUI,'from_env',lambda:Fake())
    monkeypatch.setenv('QUEUE_WAIT_S','1')
    original=server.release
    def release(): original();finished.set()
    monkeypatch.setattr(server,'release',release)
    r=submit(api);assert r.status_code==202
    job_id=r.json()['job_id']
    assert finished.wait(30)
    job=server.read_job(job_id)
    assert job['stage']=='error' and '占用' in job['message'] and 'ComfyUI' in job['message']


def test_persona_reference_flow(api, monkeypatch, raw_media):
    """人物形象照：授权必勾、双参考图接进工作流、<Picture 2> 进首镜提示词。"""
    finished=threading.Event(); submitted=[]
    class Fake:
        def check(self): return {}
        def queue_size(self): return 0
        def upload_image(self,*args,**kwargs): return 'uploaded-ref'
        def submit(self,wf):
            submitted.append(wf); return f'prompt-{len(submitted)}'
        def wait(self,prompt_id,**kwargs): return {}
        def download_outputs(self,entry,directory): return [raw_media[0]]
    monkeypatch.setattr(server.ComfyUI,'from_env',lambda:Fake())
    original=server.release
    def release(): original();finished.set()
    monkeypatch.setattr(server,'release',release)
    # 未勾授权 → 422
    r=api.post('/api/start', data={'sentence':'蓝色水杯，300ml'},
               files={'image':('p.png',picture(),'image/png'),'persona':('me.png',picture(),'image/png')})
    assert r.status_code==422 and '授权' in r.json()['detail']
    # 勾选授权 → 正常
    r=api.post('/api/start', data={'sentence':'蓝色水杯，300ml','persona_authorized':'true'},
               files={'image':('p.png',picture(),'image/png'),'persona':('me.png',picture(),'image/png')})
    assert r.status_code==202
    assert finished.wait(45)
    job_id=r.json()['job_id']; job=server.read_job(job_id)
    assert job['persona'] is True
    assert '<Picture 2>' in job['shots'][0]['prompt']           # 人物参考进首镜提示词
    wf0=submitted[0]
    assert wf0['3']['inputs']['ref_images.ref_image_1']==['28',0]  # 双参考图连线
    assert wf0['28']['class_type']=='LoadImage'


def test_persona_remake_reuses_facts(api, monkeypatch, raw_media):
    """一键换人物形象：复用已完成任务的商品/文案/尺寸，仅替换人物形象照重新制作。"""
    finished=threading.Event(); submitted=[]; uploads=[]
    class Fake:
        def check(self): return {}
        def queue_size(self): return 0
        def upload_image(self,path,*args,**kwargs):
            uploads.append(os.path.basename(str(path))); return f'ref-{len(uploads)}'
        def submit(self,wf):
            submitted.append(wf); return f'prompt-{len(submitted)}'
        def wait(self,prompt_id,**kwargs): return {}
        def download_outputs(self,entry,directory): return [raw_media[min(len(submitted)-1,1)]]
    monkeypatch.setattr(server.ComfyUI,'from_env',lambda:Fake())
    original=server.release
    def release(): original();finished.set()
    monkeypatch.setattr(server,'release',release)
    # 先完成一支原始任务
    r=submit(api); assert r.status_code==202
    job_id=r.json()['job_id']; assert finished.wait(45)
    original_job=server.read_job(job_id)
    assert original_job['stage']=='done'
    finished.clear()
    # 未知任务 404
    r=api.post('/api/jobs/'+'b'*32+'/persona', data={'persona_authorized':'true'},
               files={'persona':('new.png',picture(),'image/png')})
    assert r.status_code==404
    # 未勾肖像授权 422
    r=api.post(f'/api/jobs/{job_id}/persona', files={'persona':('new.png',picture(),'image/png')})
    assert r.status_code==422 and '授权' in r.json()['detail']
    assert not any(j['job_id']!=job_id for j in server.all_jobs())  # 校验失败不落新任务
    # 一键换形象 → 新任务复用 facts/尺寸，人物参考进入首镜提示词
    r=api.post(f'/api/jobs/{job_id}/persona', data={'persona_authorized':'true'},
               files={'persona':('new.png',picture(),'image/png')})
    assert r.status_code==202
    new_id=r.json()['job_id']; assert new_id!=job_id
    assert finished.wait(45)
    new=server.read_job(new_id)
    assert new['stage']=='done' and new['persona'] is True
    assert new['facts']==original_job['facts']
    assert new['size']==original_job['size'] and new['orientation']==original_job['orientation']
    assert new['gen_width']==original_job['gen_width'] and new['gen_height']==original_job['gen_height']
    assert new['image_sha256']==original_job['image_sha256']   # 复用同一张商品图
    assert '<Picture 2>' in new['shots'][0]['prompt']
    assert (server.job_path(new_id)/'persona.png').exists()
    # 公共状态不暴露内部字段
    status=api.get('/api/status',params={'job_id':new_id}).json()
    assert status['stage']=='done' and 'facts' not in status and 'shots' not in status
    # 新任务完成后原任务不受影响
    assert server.read_job(job_id)['stage']=='done'


def test_agent_endpoint_auth_and_submit(api, monkeypatch, raw_media):
    """智能体桥：Bearer 鉴权 + dataURL 提交 + 竖/横版参数透传。"""
    finished=threading.Event()
    class Fake:
        def check(self): return {}
        def queue_size(self): return 0
        def upload_image(self,*args,**kwargs): return 'ref.png'
        def submit(self,wf): return 'prompt-1'
        def wait(self,prompt_id,**kwargs): return {}
        def download_outputs(self,entry,directory): return [raw_media[0]]
    monkeypatch.setattr(server.ComfyUI,'from_env',lambda:Fake())
    original=server.release
    def release(): original();finished.set()
    monkeypatch.setattr(server,'release',release)
    token=server.ADMIN_PASSWORD
    body={'sentence':'蓝色水杯，300ml','price':'19.9','orientation':'horizontal',
          'image':'data:image/png;base64,'+__import__('base64').b64encode(picture()).decode()}
    r=api.post('/api/agent/submit',json=body)
    assert r.status_code==401                                    # 无 token 拒绝
    r=api.post('/api/agent/submit',json=body,headers={'Authorization':'Bearer wrong'})
    assert r.status_code==401
    r=api.post('/api/agent/submit',json=body,headers={'Authorization':f'Bearer {token}'})
    assert r.status_code==202
    j=r.json(); assert 'job_id' in j and j['video_url'].endswith('/video')
    assert finished.wait(45)
    job=server.read_job(j['job_id'])
    assert job['orientation']=='horizontal' and job['via']=='agent'
    assert job['gen_width']==800 and job['gen_height']==448     # 横版生成分辨率


def test_concurrency_and_uncertain_remote(api,monkeypatch):
    entered=threading.Event();unblock=threading.Event();finished=threading.Event()
    class Fake:
        def check(self): return {}
        def queue_size(self): return 0
        def queue(self): return {'queue_running': [], 'queue_pending': []}
        def upload_image(self,*args,**kwargs): return 'ref.png'
        def submit(self,wf): entered.set();unblock.wait(5);raise TimeoutError('uncertain submit')
    monkeypatch.setattr(server.ComfyUI,'from_env',lambda:Fake())
    original=server.release
    def release(): original();finished.set()
    monkeypatch.setattr(server,'release',release)
    r=submit(api);assert entered.wait(5)
    assert submit(api).status_code==409
    assert api.post('/api/reset').status_code==409
    unblock.set();assert finished.wait(5)
    job_id=r.json()['job_id']
    assert server.read_job(job_id)['stage']=='attention'
    assert submit(api).status_code==409
    assert api.post(f'/api/admin/jobs/{job_id}/resolve',auth=('admin',server.ADMIN_PASSWORD)).status_code==200
    assert server.read_job(job_id)['stage']=='error'


def test_restart_never_resubmits(api):
    job_id='a'*32
    server.job_path(job_id).mkdir()
    server.save({'job_id':job_id,'stage':'generating','created_at':1,'shots':[{'prompt_id':'remote-existing'}]})
    server.recover()
    job=server.read_job(job_id)
    assert job['stage']=='attention' and job['shots'][0]['prompt_id']=='remote-existing'
