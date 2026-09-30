"""Local-only production flow. Public responses contain final outputs, never internals."""
import atexit
import base64
import copy
import fcntl
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import re
import secrets
import sys
import threading
import time
import uuid
import warnings
from urllib.parse import urlparse

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from cli import load_env
load_env()
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from PIL import Image, ImageOps
from pipeline.comfy_client import ComfyUI, RemoteEnded, build_ref2va
from pipeline import render
from pipeline import creative
from pipeline import llm
from pipeline.llm import LLMError

OUT = Path(os.environ.get('DIRECTOR_OUT', BASE / 'out'))
JOBS = OUT / 'jobs'
JOBS.mkdir(parents=True, exist_ok=True)
PRIVATE = BASE / '.private'
PRIVATE.mkdir(mode=0o700, exist_ok=True)
os.chmod(PRIVATE, 0o700)
password_file = PRIVATE / 'admin-password'
if not os.environ.get('DIRECTOR_ADMIN_PASSWORD') and not password_file.exists():
    fd = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(secrets.token_urlsafe(32))
if password_file.exists():
    os.chmod(password_file, 0o600)
ADMIN_PASSWORD = os.environ.get('DIRECTOR_ADMIN_PASSWORD') or password_file.read_text().strip()
if len(ADMIN_PASSWORD) < 16:
    raise RuntimeError('DIRECTOR_ADMIN_PASSWORD must have at least 16 characters')
app = FastAPI(title='一句话生成带货视频', docs_url=None, redoc_url=None, openapi_url=None)
security = HTTPBasic()
mutex = threading.RLock()
worker_lock = threading.Lock()
lease = None
ACTIVE = {'queued', 'generating', 'rendering', 'attention'}


def admin(credentials: HTTPBasicCredentials = Depends(security)):
    valid = hmac.compare_digest(credentials.username.encode(), b'admin') & hmac.compare_digest(credentials.password.encode(), ADMIN_PASSWORD.encode())
    if not valid:
        raise HTTPException(401, 'Authentication required', headers={'WWW-Authenticate': 'Basic'})


@app.middleware('http')
async def local_only(request: Request, call_next):
    from fastapi.responses import JSONResponse
    if request.headers.get('host', '').split(':')[0] not in {'127.0.0.1', 'localhost', 'testserver'}:
        return JSONResponse({'detail': 'Local access only'}, status_code=403)
    origin = request.headers.get('origin')
    if origin:
        # 同源请求（任意本地端口）放行；外部网站的跨站请求一律拒绝（防 CSRF）。
        parsed = urlparse(origin)
        same_origin = parsed.scheme in {'http', 'https'} and parsed.netloc == request.headers.get('host', '')
        allowed = same_origin or origin in {'http://127.0.0.1:8666', 'http://localhost:8666', 'http://testserver'}
        if not allowed:
            return JSONResponse({'detail': 'Cross-origin access denied'}, status_code=403)
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' blob:; media-src 'self'; frame-ancestors 'none'"
    return response


def job_path(job_id):
    if not re.fullmatch(r'[0-9a-f]{32}', job_id):
        raise HTTPException(404, '任务不存在')
    return JOBS / job_id


def read_job(job_id):
    try:
        return json.loads((job_path(job_id) / 'job.json').read_text())
    except FileNotFoundError:
        raise HTTPException(404, '任务不存在')


def save(job):
    with mutex:
        job['updated_at'] = time.time()
        path = job_path(job['job_id']) / 'job.json'
        temporary = path.with_suffix('.tmp')
        with temporary.open('w') as stream:
            json.dump(job, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)


def all_jobs():
    return sorted([json.loads(p.read_text()) for p in JOBS.glob('*/job.json')], key=lambda j: j['created_at'], reverse=True)


def public(job):
    return {k: job[k] for k in ('job_id', 'stage', 'progress', 'message', 'created_at') if k in job} | ({'video': f"/api/jobs/{job['job_id']}/video"} if job['stage'] == 'done' else {})


def validate_facts(sentence, price):
    sentence = sentence.strip()
    if not 1 <= len(sentence) <= 160 or any(ord(c) < 32 and c not in '\n\r\t' for c in sentence):
        raise HTTPException(422, '请输入 1–160 字商品事实介绍')
    # Refuse high-risk efficacy/absolute claims rather than rewrite or invent evidence.
    if re.search(r'减脂|减肥|燃脂|瘦身|治疗|治愈|降血糖|抗癌|最强|最好|第一|国家级|顶级|提神|抗疲劳', sentence):
        raise HTTPException(422, '请删除未经审核的功效或绝对化宣称，改为商品名称、规格、口感等可核实事实')
    price = price.strip()
    if price and (not re.fullmatch(r'(?:0|[1-9]\d{0,5})(?:\.\d{1,2})?', price) or float(price) <= 0):
        raise HTTPException(422, '价格须为明确的正数，最多两位小数；未提供请留空')
    return sentence, price or None


def normalized_image(data):
    if len(data) > 12 * 1024 * 1024:
        raise HTTPException(422, '商品图不得超过 12MB')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in {'JPEG', 'PNG', 'WEBP'} or getattr(image, 'n_frames', 1) != 1:
                    raise ValueError('unsupported')
                if min(image.size) < 64 or image.width * image.height > 24_000_000:
                    raise ValueError('dimensions')
                image.load()
                image = ImageOps.exif_transpose(image).convert('RGB')
                image.thumbnail((1024, 1024))
                return image.copy()
    except Exception:
        raise HTTPException(422, '请上传有效的静态 JPG / PNG / WebP 商品图（至少 64px，最多 2400 万像素）')


def acquire():
    global lease
    if not worker_lock.acquire(False):
        raise HTTPException(409, '已有任务处理中，请等待完成')
    handle = (OUT / 'generation.lock').open('a+')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if any(j['stage'] in ACTIVE for j in all_jobs()):
            raise HTTPException(409, '已有任务处理中或待恢复，请稍候')
        lease = handle
    except BlockingIOError:
        handle.close()
        worker_lock.release()
        raise HTTPException(409, '另一个制作进程正在运行')
    except Exception:
        handle.close()
        worker_lock.release()
        raise


def release():
    global lease
    if lease:
        lease.close()
        lease = None
    worker_lock.release()


@app.get('/')
def index():
    return FileResponse(BASE / 'webapp/static/index.html')


@app.get('/admin', dependencies=[Depends(admin)])
def admin_page():
    return FileResponse(BASE / 'webapp/static/admin.html')


SIZES = {
    ('standard', 'vertical'): {'gen': (448, 800), 'out': (432, 768), 'eta': '约 10 分钟'},
    ('standard', 'horizontal'): {'gen': (800, 448), 'out': (768, 432), 'eta': '约 10 分钟'},
    ('hd', 'vertical'): {'gen': (768, 1344), 'out': (720, 1280), 'eta': '约 20–30 分钟'},
    ('hd', 'horizontal'): {'gen': (1344, 768), 'out': (1280, 720), 'eta': '约 20–30 分钟'},
}


def launch_job(sentence, price, size, orientation, product_bytes, persona_bytes=None,
               via=None, message='已收到，正在制作完整视频'):
    """Persist one real job (atomic) and start its worker; single GPU lease held for the whole flow."""
    size_info = SIZES[(size, orientation)]
    picture = normalized_image(product_bytes)
    acquire()
    try:
        render.preflight()  # fail before consuming GPU
        job_id = uuid.uuid4().hex
        directory = job_path(job_id)
        directory.mkdir(mode=0o700)
        picture.save(directory / 'product.png')
        # Shot prompts are planned by the constrained LLM director inside the worker;
        # captions/scene/action may only restate user-provided facts.
        job = {'job_id': job_id, 'created_at': time.time(), 'stage': 'queued', 'progress': 2,
               'message': message, 'facts': {'sentence': sentence, 'price': price},
               'size': size, 'orientation': orientation,
               'gen_width': size_info['gen'][0], 'gen_height': size_info['gen'][1],
               'image_sha256': hashlib.sha256(product_bytes).hexdigest(),
               'width': size_info['out'][0], 'height': size_info['out'][1],
               'subtitles': [], 'creative': None,
               'persona': bool(persona_bytes),
               'shots': [{'index': i, 'status': 'pending', 'prompt': None, 'prompt_id': None,
                          'frames': 124, 'fps': 24,
                          'width': size_info['gen'][0], 'height': size_info['gen'][1],
                          'ref_image_size': 'match'}
                         for i in range(2)],
               'music': 'original procedural instrumental', 'ai_generated': True}
        if via:
            job['via'] = via
        if persona_bytes:
            (directory / 'persona.png').write_bytes(persona_bytes)
        save(job)
        threading.Thread(target=run_job, args=(job_id,), daemon=True).start()
        return job_id, job
    except Exception:
        release()
        raise


@app.post('/api/start', status_code=202)
async def start(sentence: str = Form(...), price: str = Form(''), size: str = Form('standard'),
                orientation: str = Form('vertical'),
                persona_authorized: str = Form(''),
                image: UploadFile = File(...), persona: UploadFile | None = File(None)):
    sentence, price = validate_facts(sentence, price)
    if size not in ('standard', 'hd'):
        raise HTTPException(422, '尺寸仅支持 standard（标准）或 hd（高清）')
    if orientation not in ('vertical', 'horizontal'):
        raise HTTPException(422, '方向仅支持 vertical（竖版）或 horizontal（横版）')
    persona_bytes = None
    if persona and persona.filename:
        if persona_authorized != 'true':
            raise HTTPException(422, '使用人物形象照必须勾选"我已获得该照片人物的肖像使用授权"')
        persona_bytes = await persona.read(12 * 1024 * 1024 + 1)
        normalized_image(persona_bytes)  # 校验不通过直接 422
    data = await image.read(12 * 1024 * 1024 + 1)
    _, job = launch_job(sentence, price, size, orientation, data, persona_bytes)
    return public(job)


@app.post('/api/jobs/{job_id}/persona', status_code=202)
async def remake_persona(job_id: str, persona_authorized: str = Form(''),
                         persona: UploadFile = File(...)):
    """一键换人物形象：复用已完成任务的商品图/文案/尺寸，仅用新的人物形象照重新制作。

    生成式换形象（人物参考图进入重拍），不对原视频做事后人脸替换；
    肖像授权与图片校验与首次提交完全一致。
    """
    previous = read_job(job_id)  # 未知任务 404
    if previous.get('stage') in ACTIVE:
        raise HTTPException(409, '原任务还在制作中，完成即可一键更换人物形象')
    if persona_authorized != 'true':
        raise HTTPException(422, '使用人物形象照必须勾选"我已获得该照片人物的肖像使用授权"')
    persona_bytes = await persona.read(12 * 1024 * 1024 + 1)
    normalized_image(persona_bytes)
    facts = previous.get('facts') or {}
    sentence = str(facts.get('sentence') or '')
    if not sentence:
        raise HTTPException(409, '原任务缺少商品信息，无法复用，请用商品图重新提交')
    sentence, price = validate_facts(sentence, facts.get('price') or '')
    size = previous.get('size') if previous.get('size') in ('standard', 'hd') else 'standard'
    orientation = previous.get('orientation') if previous.get('orientation') in ('vertical', 'horizontal') else 'vertical'
    product_file = job_path(job_id) / 'product.png'
    if not product_file.exists():
        raise HTTPException(409, '原任务商品图缺失，请用商品图重新提交')
    _, job = launch_job(sentence, price, size, orientation, product_file.read_bytes(),
                        persona_bytes, message='已收到新人物形象，正在按原创意重新制作')
    return public(job)


def frame_product_check(video_path: str, entry_late: bool) -> dict:
    """S7 质检门：抽首尾两帧，视觉模型判断商品时机与道具纪律是否符合脚本设定。"""
    import base64
    import tempfile
    result = {'checked': entry_late, 'early_product': None, 'late_product': None, 'multi_cup': None}
    if not entry_late:
        return result
    with tempfile.TemporaryDirectory() as td:
        for tag, second in (('early_product', 0.5), ('late_product', 4.5)):
            frame = Path(td) / f'{tag}.jpg'
            render.run(['-ss', str(second), '-i', video_path, '-frames:v', '1', '-q:v', '3', frame])
            data_url = 'data:image/jpeg;base64,' + base64.b64encode(frame.read_bytes()).decode()
            result[tag] = llm.ask_yesno(data_url, '这张视频截图里是否出现了杯装或瓶装饮品商品？')
        # 道具纪律：同类杯子不得超过一个（空杯幽灵道具检测）
        frame = Path(td) / 'late_product.jpg'
        if Path(frame).exists():
            data_url = 'data:image/jpeg;base64,' + base64.b64encode(frame.read_bytes()).decode()
            result['multi_cup'] = llm.ask_yesno(data_url, '这张视频截图里是否同时出现两个或以上的杯子（包括空杯）？')
    return result


def frame_drink_check(video_path: str) -> dict:
    """S7 质检门（饮用动作）：中段抽帧确认主人公真的在喝，而不是摆放/轻扶杯子冒充。"""
    import base64
    import tempfile
    result = {'checked': False, 'drinking': None}
    with tempfile.TemporaryDirectory() as td:
        frame = Path(td) / 'mid.jpg'
        render.run(['-ss', '2.5', '-i', video_path, '-frames:v', '1', '-q:v', '3', frame])
        if frame.exists():
            data_url = 'data:image/jpeg;base64,' + base64.b64encode(frame.read_bytes()).decode()
            result['drinking'] = llm.ask_yesno(
                data_url,
                '画面中的人物是否正在饮用饮品（杯口或吸管含入口中、或明显仰头吞咽）？'
                '只是手拿杯子、摆放或轻扶杯子而没有入口饮用，回答否。')
            result['checked'] = result['drinking'] is not None
    return result


def frame_liquid_check(video_path: str) -> dict:
    """S7 质检门（液面）：对比首尾两帧，杯中液体量必须可见减少（真实喝掉的证据）。"""
    import base64
    import tempfile
    result = {'checked': False, 'level_dropped': None}
    with tempfile.TemporaryDirectory() as td:
        first_frame = Path(td) / 'first.jpg'
        end_frame = Path(td) / 'end.jpg'
        render.run(['-ss', '0.4', '-i', video_path, '-frames:v', '1', '-q:v', '3', first_frame])
        render.run(['-sseof', '-0.6', '-i', video_path, '-frames:v', '1', '-q:v', '3', end_frame])
        if first_frame.exists() and end_frame.exists():
            first_url = 'data:image/jpeg;base64,' + base64.b64encode(first_frame.read_bytes()).decode()
            end_url = 'data:image/jpeg;base64,' + base64.b64encode(end_frame.read_bytes()).decode()
            result['level_dropped'] = llm.ask_yesno(
                first_url,
                '第一张图是视频开始时的画面，第二张图是视频结束时的画面。对比两帧：'
                '若画面中有杯装或瓶装饮品，杯中液体量在第二张里是否比第一张明显减少'
                '（液面下降、剩余量变少，或吸管杯中可见液体变少）？看不清或没有饮品时回答否。',
                extra_image=end_url)
            result['checked'] = result['level_dropped'] is not None
    return result


def run_job(job_id):
    job = read_job(job_id)
    remote_uncertain = False
    try:
        # Constrained LLM director first (it SEES the product image when vision is
        # configured); no GPU is touched unless planning succeeds.
        job.update(stage='planning', progress=6, message='AI 编导正在查看商品并策划创意脚本')
        save(job)
        import base64
        image_data_url = 'data:image/png;base64,' + base64.b64encode(
            (directory := job_path(job_id) / 'product.png').read_bytes()).decode()
        plan = creative.plan(job['facts']['sentence'], job['facts']['price'], image_data_url)
        job['creative'] = plan.creative.model_dump()
        job['vision_used'] = plan.vision_used
        if plan.note:
            job['planning_note'] = plan.note
        job['subtitles'] = [plan.creative.hook, plan.creative.cta]
        for shot, script in zip(job['shots'], plan.creative.shots):
            shot['prompt'] = creative.shot_prompt(script)
        save(job)

        client = ComfyUI.from_env()
        client.check()
        # GPU 被其他任务占用时礼貌排队等待，而不是直接失败（单用户本地工具）
        wait_started = time.time()
        wait_limit = float(os.environ.get('QUEUE_WAIT_S', '1800'))
        while True:
            occupied = client.queue_size()
            if not occupied:
                break
            if time.time() - wait_started > wait_limit:
                raise RuntimeError(f'GPU 仍被其他任务占用（{occupied} 个），已自动等待 {int(wait_limit / 60)} 分钟后放弃。'
                                   '可清空 ComfyUI 队列后重试')
            job.update(stage='queued', progress=3,
                       message=f'GPU 正被其他任务占用，自动排队等待中（已等待 {int(time.time() - wait_started)} 秒，无需操作）')
            save(job)
            time.sleep(15)
        directory = job_path(job_id)
        reference = client.upload_image(str(directory / 'product.png'), name=f'{job_id}.png')
        persona_name = None
        if (directory / 'persona.png').exists():
            persona_name = client.upload_image(str(directory / 'persona.png'), name=f'{job_id}_person.png')
            job['shots'][0]['prompt'] = creative.shot_prompt(job['creative']['shots'][0], person_ref=True)
            save(job)
        prev_video_name = None  # 镜 0 成片 → 镜 1 的参考视频（人物/服饰/场景/商品状态镜像）
        for shot, script in zip(job['shots'], job['creative']['shots']):
            entry_late = bool(script.get('product_entry')) and script['product_entry'] != '全程'
            for attempt in (1, 2):  # 质检不过自动换种子重试一次
                shot['status'] = 'submitting'
                job.update(stage='generating', progress=10 + shot['index'] * 35,
                           message='正在制作视频，请稍候' if attempt == 1 else '商品出现过早，正在自动重拍这一镜')
                save(job)
                if shot['index'] == 0:
                    workflow = build_ref2va(shot['prompt'], reference,
                                            width=job['gen_width'], height=job['gen_height'],
                                            length=124, ref_image_size='match',
                                            ref_person_image_name=persona_name)
                else:
                    # 衔接镜：商品参考图锁外观颜色 + 上一镜成片作参考视频镜像人物/场景
                    continuity_prompt = creative.shot_prompt(script, continuation=True)
                    shot['prompt'] = continuity_prompt
                    workflow = build_ref2va(continuity_prompt, reference,
                                            width=job['gen_width'], height=job['gen_height'],
                                            length=124, ref_image_size='match',
                                            ref_video_file=prev_video_name)
                workflow['17']['inputs']['filename_prefix'] = f'director/{job_id}/shot_{shot["index"]}'
                remote_uncertain = True
                shot['prompt_id'] = client.submit(workflow)
                shot['status'] = 'running'
                save(job)  # persist remote identity before polling
                entry = client.wait(shot['prompt_id'], timeout_s=3600)
                remote_uncertain = False
                paths = client.download_outputs(entry, str(directory / f'raw_{shot["index"]}'))
                videos = [p for p in paths if Path(p).suffix.lower() in {'.mp4', '.mov', '.webm'}]
                if len(videos) != 1:
                    raise RuntimeError('Expected one real video output')
                if render.frame_count(videos[0]) < 120:
                    raise RuntimeError('Generated footage is shorter than five seconds')
                shot['video'] = videos[0]
                check = frame_product_check(videos[0], entry_late and shot['index'] == 0)
                shot['product_entry_check'] = check
                # 末镜含喝饮动作：真实饮用动作 + 首尾帧液体量对比，缺一即重拍
                is_last = shot['index'] == len(job['shots']) - 1
                drink_shot = is_last and any(k in script.get('action', '') for k in ('喝', '饮', '液面'))
                if drink_shot:
                    shot['drink_check'] = frame_drink_check(videos[0])
                    shot['liquid_check'] = frame_liquid_check(videos[0])
                save(job)
                drink, liquid = shot.get('drink_check', {}), shot.get('liquid_check', {})
                gate_fail = (check.get('early_product') is True or check.get('multi_cup') is True
                             or drink.get('drinking') is False or liquid.get('level_dropped') is False)
                if (check['checked'] or drink.get('checked') or liquid.get('checked')) and gate_fail and attempt == 1:
                    if drink.get('drinking') is False:
                        job.update(message='饮用动作不明显，正在自动重拍这一镜')
                        save(job)
                    elif liquid.get('level_dropped') is False:
                        job.update(message='喝完液体量未见减少，正在自动重拍这一镜')
                        save(job)
                    continue
                break
            else:
                raise RuntimeError('Shot quality gate exhausted')
            shot['status'] = 'done'
            if shot['index'] == 0:
                # 上一镜只截取收尾 2 秒作参考视频：锁定人物/服饰/场景/商品"已接稳"的定格状态，
                # 不把递入动作喂给下一镜（否则镜 2 会把递咖啡再演一遍）
                continuity_ref = directory / 'continuity_ref.mp4'
                render.run(['-sseof', '-2.2', '-i', shot['video'], '-an',
                            '-c:v', 'libx264', '-preset', 'veryfast', '-pix_fmt', 'yuv420p', continuity_ref])
                prev_video_name = client.upload_image(continuity_ref, name=f'{job_id}_prev.mp4')
                shot['continuity'] = '下一镜以本镜收尾 2 秒为参考视频 + 商品参考图双锁定（递入动作不进入参考）'
            save(job)
        job.update(stage='rendering', progress=85, message='正在合成字幕、配乐和完整视频')
        save(job)
        job['final_path'] = render.compose([s['video'] for s in job['shots']], job['subtitles'],
                                           job['facts']['price'], directory,
                                           width=job['width'], height=job['height'])
        job.update(stage='done', progress=100, message='10 秒成片已完成（AI 生成 · 原创配乐）')
    except LLMError as error:
        detail = str(error)
        if '402' in detail or 'Insufficient' in detail:
            hint = 'DeepSeek 账户余额不足，请到 platform.deepseek.com 充值后重新提交（系统已尝试用备用模型接管）'
        else:
            hint = 'AI 编导服务暂时不可用，本次未制作视频。请稍后重新提交'
        job.update(stage='error', message=hint, diagnostic=detail[:2000])
    except RemoteEnded as error:
        # 远端已明确结束（报错/被中断）：不是不确定态，直接允许重试
        job.update(stage='error',
                   message='这一镜在 GPU 上被中断或报错，未生成视频。请稍后重新提交',
                   diagnostic=str(error)[:2000])
    except Exception as error:
        detail = str(error)
        if remote_uncertain:
            message = '制作暂时中断，任务正在等待确认，请勿重复提交'
        elif '占用' in detail or '队列' in detail:
            message = detail  # GPU 占用超时等运营性错误：具体原因直接透传给用户
        else:
            message = '本次制作失败，未生成替代视频。请稍后重新提交或联系维护人员'
        job.update(stage='attention' if remote_uncertain else 'error',
                   message=message,
                   diagnostic=detail[:2000])
    finally:
        save(job)
        release()


@app.get('/api/status')
def status(job_id: str | None = None):
    if job_id:
        return public(read_job(job_id))
    active = [j for j in all_jobs() if j['stage'] in ACTIVE]
    return public(active[0]) if active else {'stage': 'idle', 'version': '2.0', 'message': '可开始新的制作'}


@app.get('/api/jobs/{job_id}/video')
def final_video(job_id: str):
    job = read_job(job_id)
    if job['stage'] != 'done':
        raise HTTPException(404, '成片尚未完成')
    return FileResponse(job_path(job_id) / 'final.mp4', media_type='video/mp4', filename='商品视频-10秒.mp4')


@app.post('/api/generate')
def legacy_generate():
    raise HTTPException(410, '请刷新页面。新版本提交商品后自动完成全部制作')


@app.post('/api/reset')
def reset():
    if any(j['stage'] in ACTIVE for j in all_jobs()):
        raise HTTPException(409, '任务进行中，不能重置')
    return {'ok': True}  # history is immutable, reset only clears browser state


def _agent_token() -> str:
    return os.environ.get('DIRECTOR_AGENT_TOKEN') or ADMIN_PASSWORD


@app.post('/api/agent/submit', status_code=202)
async def agent_submit(request: Request):
    """智能体平台接入桥（Coze/Dify/GPTs 的 HTTP 工具节点可直接调用）。

    Header: Authorization: Bearer <DIRECTOR_AGENT_TOKEN 或管理员密码>
    Body(JSON): {sentence, price?, size?: standard|hd, orientation?: vertical|horizontal,
                 persona_authorized?: bool, image: dataURL, persona?: dataURL}
    返回: {job_id, status_url, video_url(完成后可用)}
    """
    auth = request.headers.get('authorization', '')
    token = auth[7:] if auth.startswith('Bearer ') else ''
    if not token or not hmac.compare_digest(token.encode(), _agent_token().encode()):
        raise HTTPException(401, 'Invalid agent token', headers={'WWW-Authenticate': 'Bearer'})

    body = await request.json()
    sentence = str(body.get('sentence', ''))
    price = str(body.get('price') or '')
    size = str(body.get('size') or 'standard')
    orientation = str(body.get('orientation') or 'vertical')
    sentence, price = validate_facts(sentence, price)
    if size not in ('standard', 'hd') or orientation not in ('vertical', 'horizontal'):
        raise HTTPException(422, 'size/orientation 参数不合法')

    def _decode(field):
        value = body.get(field)
        if not value or not str(value).startswith('data:image'):
            return None
        header, _, b64 = str(value).partition(',')
        return base64.b64decode(b64)

    try:
        data = _decode('image')
        assert data, 'image dataURL required'
    except Exception:
        raise HTTPException(422, 'image 必须是 data:image/...;base64,... 格式')
    persona_bytes = _decode('persona')
    if persona_bytes and body.get('persona_authorized') is not True:
        raise HTTPException(422, '使用人物形象照时 persona_authorized 必须为 true（肖像授权）')

    if persona_bytes:
        normalized_image(persona_bytes)  # 校验不通过直接 422

    job_id, _ = launch_job(sentence, price, size, orientation, data, persona_bytes, via='agent')
    return {'job_id': job_id, 'status_url': f'/api/status?job_id={job_id}',
            'video_url': f'/api/jobs/{job_id}/video'}


@app.get('/api/admin/jobs', dependencies=[Depends(admin)])
def admin_jobs():
    # Redact diagnostic strings: upstream exceptions may contain configuration/credentials.
    result = copy.deepcopy(all_jobs())
    for job in result:
        diagnostic = job.get('diagnostic', '')
        for key, value in os.environ.items():
            if value and len(value) >= 4 and any(tag in key for tag in ('KEY', 'PASS', 'TOKEN', 'SECRET', 'COMFYUI_HOST', 'SSH_HOST')):
                diagnostic = diagnostic.replace(value, '[REDACTED]')
        job['diagnostic'] = diagnostic
    return result


@app.post('/api/admin/jobs/{job_id}/resolve', dependencies=[Depends(admin)])
def resolve(job_id: str):
    """Explicitly acknowledge interrupted jobs only when THIS job has no live remote work;
    never cancels anything, and unrelated queue items are none of our business."""
    with mutex:
        if worker_lock.locked():
            raise HTTPException(409, 'Worker is still active')
        job = read_job(job_id)
        if job['stage'] != 'attention':
            raise HTTPException(409, 'Only interrupted tasks can be acknowledged')
        client = ComfyUI.from_env()
        queue = client.queue()
        live = {item[1] for key in ('queue_running', 'queue_pending') for item in queue.get(key, [])}
        mine = {s.get('prompt_id') for s in job.get('shots', []) if s.get('prompt_id')}
        if mine & live:
            raise HTTPException(409, 'This job still has active remote tasks; nothing was stopped')
        job.update(stage='error', message='上次任务已结束，可重新提交制作', acknowledged_at=time.time())
        save(job)
        return {'ok': True}


@app.on_event('startup')
def recover():
    # Never replay submission after a crash: remote work may still be running.
    handle = (OUT / 'generation.lock').open('a+')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for job in all_jobs():
            if job['stage'] in {'queued', 'generating', 'rendering'}:
                job.update(stage='attention', message='上次制作中断，等待确认后可重新提交', diagnostic='Process restarted; inspect persisted prompt IDs and remote queue. No automatic resubmission.')
                save(job)
    except BlockingIOError:
        pass
    finally:
        handle.close()
