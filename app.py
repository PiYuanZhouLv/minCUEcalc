import os
import re
import unicodedata
import ctypes
import subprocess
import hashlib
import tempfile
import json
import glob
from pathlib import Path
from collections import defaultdict
from flask import Flask, request, jsonify, render_template, send_file, abort, Response, stream_with_context
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

AUDIO_EXT = ['.wav', '.flac', '.mp3', '.m4a', '.tak', '.ogg', '.acc']
IMAGE_EXT = ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp']

# ---------- Windows DPI 感知 ----------
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

import tkinter as tk
from tkinter import filedialog

# ---------- 全局缓存 ----------
CACHE_DIR = os.path.join(tempfile.gettempdir(), 'minCUEcalc_cache')
os.makedirs(CACHE_DIR, exist_ok=True)

# ---------- 规范化 ----------
def normalize_title(title):
    nfkd = unicodedata.normalize('NFKD', title)
    without_accents = ''.join(c for c in nfkd if not unicodedata.combining(c))
    normalized = ''.join(c for c in without_accents if c.isalnum()).lower()
    return normalized

# ---------- 解析时间 ----------
def parse_index_time(time_str):
    parts = time_str.strip().split(':')
    if len(parts) == 3:
        m, s, f = parts
        return int(m) * 60 + int(s) + int(f) / 75.0
    elif len(parts) == 2:
        m, s = parts
        return int(m) * 60 + int(s)
    else:
        return 0.0

def ffprobe(entries, file):
    try:
        cmd = ['ffprobe', '-v', 'error', '-show_entries', entries,
               '-of', 'default=noprint_wrappers=1:nokey=1', file]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10, encoding='utf-8')
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except:
        pass
    return None

def get_audio_duration(filepath):
    try:
        return float(ffprobe('format=duration', filepath))
    except:
        pass
    return None

# ---------- 查找封面 ----------
def find_cover_image(cue):
    cue_dir, cue_basename = os.path.split(cue)
    exts = IMAGE_EXT
    cue_base = os.path.splitext(cue_basename)[0]
    candidates = [cue_base, 'cover', 'front', 'album', 'art']
    if '-' in cue_base:
        candidates.append(cue_base.rsplit('-', 1)[0])
    candidates = list(set(candidates))
    for name in candidates:
        for ext in exts:
            candidate_path = os.path.join(cue_dir, name + ext)
            if os.path.exists(candidate_path):
                return Path(candidate_path).resolve().as_posix()
    imgs = list(filter(lambda x: any(x.endswith(y) for y in exts), os.listdir(cue_dir)))
    if len(imgs) == 1:
        return Path(os.path.join(cue_dir, imgs[0])).resolve().as_posix()
    for f in imgs:
        image_base = os.path.splitext(f)[0]
        if cue_base.startswith(image_base) and image_base:
            return Path(os.path.join(cue_dir, f)).resolve().as_posix()
    return None

def has_embedded_cover(fn):
    return 'video' in ffprobe('stream=codec_type', fn)

# ---------- 解析 CUE（支持 INDEX 00/01，返回 track 表演者） ----------
def parse_cue(cue):
    audio = None
    tracks = []
    album_title = None
    album_artist = None
    current_track = None
    head = True
    first = False

    for line in open(cue, encoding='utf-8-sig', errors='ignore'):
        line = line.strip()

        if m := re.search(r'FILE\s+"(.*?)"', line, re.IGNORECASE):
            if audio:
                tracks[-1]['duration'] = max(0, (get_audio_duration(audio) or 0) - tracks[-1]['start'])
            audio = Path(os.path.join(os.path.dirname(cue), m.group(1).strip())).resolve().as_posix()
            first = True
        elif m := re.search(r'PERFORMER\s+"(.*?)"', line, re.IGNORECASE):
            performer = m.group(1).strip()
            if head:
                album_artist = performer
            else:
                current_track['performer'] = performer
        elif m := re.search(r'TITLE\s+"(.*?)"', line, re.IGNORECASE):
            title = m.group(1).strip()
            if head:
                album_title = title
            else:
                current_track['title'] = title
                current_track['norm'] = normalize_title(title)
        elif line.upper().startswith('TRACK'):
            head = False
            if current_track:
                tracks.append(current_track)
            current_track = {
                "title": "Untitled",
                "norm": "Untitled",
                "performer": album_artist or "Unknown Artist",
                "file": audio,
                "start": None,
                "end": None
            }
        elif m := re.search(r'INDEX\s+00\s+(.*?)$', line, re.IGNORECASE):
            t = parse_index_time(m.group(1).strip())
            tracks[-1]['end'] = max(t, tracks[-1]['start'])
            tracks[-1]['duration'] = max(0, t - tracks[-1]['start'])
        elif m := re.search(r'INDEX\s+01\s+(.*?)$', line, re.IGNORECASE):
            t = parse_index_time(m.group(1).strip())
            current_track['start'] = t
            if not first and not tracks[-1]['end']:
                tracks[-1]['end'] = max(t, tracks[-1]['start'])
                tracks[-1]['duration'] = max(0, t - tracks[-1]['start'])
            first = False
    tracks.append(current_track)
    tracks[-1]['duration'] = max(0, (get_audio_duration(audio) or 0) - tracks[-1]['start'])
    return {
        "name": Path(cue).name,
        "title": album_title,
        "type": "album",
        "performer": album_artist,
        "file": Path(cue).resolve().as_posix(),
        "cover": find_cover_image(cue),
        "tracks": tracks
    }

def parse_audios(dir):
    audios = list(filter(lambda fn: any(fn.endswith(ext) for ext in AUDIO_EXT), os.listdir(dir)))
    tracks = []
    for fn in audios:
        tracks.append({
            "title": (title := (ffprobe('format_tags=title', os.path.join(dir, fn)) or os.path.splitext(os.path.split(fn)[1])[0])),
            "norm": normalize_title(title),
            "performer": ffprobe('format_tags=artist', os.path.join(dir, fn)) or ffprobe('format_tags=album_artist', os.path.join(dir, fn)) or "Unknown Artist",
            "file": (Path(dir) / fn).resolve().as_posix(),
            "start": None,
            "end": None,
            "duration": get_audio_duration(os.path.join(dir, fn)),
            "_album": ffprobe('format_tags=album', os.path.join(dir, fn)),
            "_album_performer": ffprobe('format_tags=album_artist', os.path.join(dir, fn)),
            "_index": int(ffprobe('format_tags=track', os.path.join(dir, fn)) or 0),
            "_fn": os.path.splitext(fn)[0]
        })
    albums = {}
    for track in tracks:
        album_name = track.pop('_album') or Path(dir).name
        if album_name not in albums:
            albums[album_name] = {
                "name": album_name,
                "title": album_name,
                "type": "album",
                "performer": None,
                "cover": None,
                "tracks": []
            }
        albums[album_name]['tracks'].append(track)
    result = []
    for album in albums.values():
        album['tracks'].sort(key=lambda x: (x['_index'], tuple(map(lambda y: int(y) if y.isdigit() else y.lower(), re.split(r'(\d+)', track['_fn'])))))
        album['performer'] = next(filter(lambda x: x, [track['_album_performer'] for track in album['tracks']]), None)
        album['cover'] = '[EMBEDDED]' if any(has_embedded_cover(track['file']) for track in album['tracks']) else find_cover_image(Path(dir) / album['name'])
        [[track.pop(k) for k in list(track.keys()) if k.startswith('_')] for track in album['tracks']]
        result.append(album)
    return result

def scan_dir(dir, sub=False):
    content = []
    for subdir in filter(os.path.isdir, map(lambda x: os.path.join(dir, x), os.listdir(dir))):
        content.append(scan_dir(os.path.join(dir, subdir), sub=True))
    if any(any(fn.endswith(ext) for ext in AUDIO_EXT) for fn in os.listdir(dir)):
        if any(fn.endswith('.cue') for fn in os.listdir(dir)):

            for cue in filter(lambda fn: fn.endswith('.cue'), os.listdir(dir)):
                content.append(parse_cue(os.path.join(dir, cue)))
        else:
            content.extend(parse_audios(dir))
    return {
        "name": Path(dir).name if sub else Path(dir).resolve().as_posix(),
        "type": "dir",
        "content": content
    }

def simplify_scan_result(scan):
    if scan['type'] == 'album':
        return scan
    simplified = list(filter(lambda x: x, [simplify_scan_result(i) for i in scan['content']]))
    if not simplified:
        return None
    if len(simplified) == 1 and simplified[0]['type'] == 'dir':
        return simplified[0] | {
            'name': f"{scan['name']}/{simplified[0]['name']}"
        }
    return scan | {
        "content": list(sorted(simplified, key=lambda x: (x['type'] == 'album', x['name'])))
    }


# ---------- 扫描多个目录 ----------
def scan_directories(dir_list):
    return [simplify_scan_result(scan_dir(dir)) for dir in dir_list]

'''
# For frontpage debug use only, do NOT use in the production.
try:
    DEBUG_CACHE = __import__('pickle').load(open('.debug-cache.pkl', 'rb'))
except:
    DEBUG_CACHE = {}
def scan_directories(dir_list):
    key = tuple(sorted(Path(dir).resolve().as_posix() for dir in dir_list))
    if key in DEBUG_CACHE:
        return DEBUG_CACHE[key]
    DEBUG_CACHE[key] = [simplify_scan_result(scan_dir(dir)) for dir in dir_list]
    __import__('pickle').dump(DEBUG_CACHE, open('.debug-cache.pkl', 'wb'))
    return DEBUG_CACHE[key]
'''

from ortools.sat.python import cp_model
from collections import defaultdict

def compute_schemes(songs, selected_titles, K=20, w_c=10, w_m=5, w_e=1, time_limit=60):
    """
    返回前 K 个最优方案（按总代价升序，代价相同可并列）。
    总代价 = w_c * (选中cue数) + w_m * (缺失目标歌曲数) + w_e * (多余歌曲总数)
    """
    if not selected_titles:
        return []

    target_set = set(selected_titles)

    # ----- 1. 构建全量歌曲信息 -----
    cue_all_counts = defaultdict(lambda: defaultdict(int))
    all_song_ids = set()

    for song in songs:
        sid = song.get('id')
        all_song_ids.add(sid)
        for cue_path in song.get('cueFiles', []):
            cue_all_counts[cue_path][sid] += 1

    cue_paths = list(cue_all_counts.keys())
    if not cue_paths:
        return []

    all_songs = sorted(all_song_ids)
    song_to_idx = {sid: i for i, sid in enumerate(all_songs)}
    target_idx = {song_to_idx[t] for t in target_set}
    total_songs = len(all_songs)
    total_targets = len(target_set)

    cue_counts = []
    for fp in cue_paths:
        cnt_dict = cue_all_counts[fp]
        arr = [0] * total_songs
        for sid, c in cnt_dict.items():
            arr[song_to_idx[sid]] = c
        cue_counts.append(arr)

    # ----- 2. 构建基础 ILP 模型 -----
    def build_base_model():
        model = cp_model.CpModel()

        x = [model.NewBoolVar(f'x_{i}') for i in range(len(cue_paths))]

        max_cnt = [sum(cue_counts[i][s] for i in range(len(cue_paths))) for s in range(total_songs)]
        cnt = [model.NewIntVar(0, max_cnt[s], f'cnt_{s}') for s in range(total_songs)]

        miss = [model.NewBoolVar(f'miss_{t}') for t in range(total_targets)]

        extra = [model.NewIntVar(0, max_cnt[s], f'extra_{s}') for s in range(total_songs)]

        for s in range(total_songs):
            model.Add(cnt[s] == sum(cue_counts[i][s] * x[i] for i in range(len(cue_paths))))

        target_list = sorted(target_idx)
        t_to_miss_idx = {t: idx for idx, t in enumerate(target_list)}
        for t in target_idx:
            mi = t_to_miss_idx[t]
            model.Add(cnt[t] >= 1 - miss[mi])

        for s in range(total_songs):
            need = 1 if s in target_idx else 0
            model.Add(extra[s] >= cnt[s] - need)
            model.Add(extra[s] >= 0)

        cue_cost = w_c * sum(x)
        miss_cost = w_m * sum(miss)
        extra_cost = w_e * sum(extra)
        total_cost = cue_cost + miss_cost + extra_cost
        model.Minimize(total_cost)

        return model, x, miss, extra, total_cost, target_list

    model, x, miss, extra, total_cost, target_list = build_base_model()
    solver = cp_model.CpSolver()
    if time_limit:
        solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_search_workers = 8

    solutions = []
    while len(solutions) < K:
        status = solver.Solve(model)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            break

        selected = [i for i, var in enumerate(x) if solver.Value(var)]
        cost_val = solver.ObjectiveValue()

        miss_songs = []
        for t in target_idx:
            mi = target_list.index(t)
            if solver.Value(miss[mi]) == 1:
                miss_songs.append(all_songs[t])

        extra_detail = {}
        for s in range(total_songs):
            e_val = solver.Value(extra[s])
            if e_val > 0:
                extra_detail[all_songs[s]] = e_val

        solutions.append((cost_val, selected, miss_songs, extra_detail))

        if not selected:
            model.Add(sum(x) >= 1)
        else:
            model.Add(
                sum(x[i] for i in selected) -
                sum(x[i] for i in range(len(cue_paths)) if i not in selected)
                <= len(selected) - 1
            )

    schemes = []
    for cost_val, selected, miss_songs, extra_detail in solutions:
        cue_files_info = []
        for idx in selected:
            fp = cue_paths[idx]
            contained = [all_songs[s] for s, c in enumerate(cue_counts[idx]) if c > 0]
            cue_files_info.append({"path": fp, "songs": list(set(contained))})

        covered = [sid for sid in target_set if sid not in miss_songs]
        extra_ids = []
        for sid, cnt in extra_detail.items():
            extra_ids.extend([sid] * cnt)
        coverage_count = len(covered)
        miss_count = len(miss_songs)
        extra_count = len(extra_ids)
        diff = miss_count + extra_count

        schemes.append({
            "cueFiles": cue_files_info,
            "covered": covered,
            "extra": extra_ids,
            "miss": miss_songs,
            "diff": diff,
            "coverage": coverage_count,
            "extra_count": extra_count,
            "_cost": cost_val
        })

    schemes.sort(key=lambda x: (x["_cost"], x["diff"], -x["coverage"]))
    return schemes[:K]

# ---------- 导出辅助函数 ----------
def extract_cover(audio):
    key = hashlib.md5(Path(audio).resolve().as_posix().encode()).hexdigest()
    if cached := glob.glob(os.path.join(CACHE_DIR, f'{key}.*')):
        return cached[0]
    
    try:
        cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                '-show_entries', 'stream=codec_name',
                '-of', 'default=noprint_wrappers=1:nokey=1', audio]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0 and result.stdout.strip():
            codec = result.stdout.strip()
        else:
            return None
    except:
        return None

    ext = {
        'mjpeg': '.jpg',
        'jpeg': '.jpg',
        'png': '.png',
        'gif': '.gif',
        'bmp': '.bmp',
        'webp': '.webp'
    }.get(codec, '.jpg')

    extract_cmd = [
        'ffmpeg', '-i', audio, '-an', '-c:v', 'copy', outpath:=os.path.join(CACHE_DIR, key+ext)
    ]
    result = subprocess.run(extract_cmd, capture_output=True, timeout=10)
    if result.returncode == 0:
        return outpath

def export_song(output, format, file, start=None, end=None, extra=None):
    print(output, format, file, start, end, extra)
    if not extra:
        extra = {}
    target = os.path.splitext(file)[1].lower().lstrip('.') if format == 'keep' else format

    codec_map = {
        'mp3':  {'codec': 'libmp3lame', 'bitrate': '192k', 'ext': 'mp3', 'extra_args': []},
        'flac': {'codec': 'flac',       'bitrate': None,  'ext': 'flac', 'extra_args': []},
        'aac':  {'codec': 'aac',        'bitrate': '192k', 'ext': 'm4a', 'extra_args': ['-movflags', '+faststart']},
        'ogg':  {'codec': 'libvorbis',  'bitrate': '192k', 'ext': 'ogg', 'extra_args': []},
        'opus': {'codec': 'libopus',    'bitrate': '128k', 'ext': 'opus', 'extra_args': []},
        'wav':  {'codec': 'pcm_s16le',  'bitrate': None,  'ext': 'wav', 'extra_args': []},
    }
    fmt_info = codec_map[target]

    cmd = ['ffmpeg']

    if start:
        cmd += ['-ss', str(start)]
    if end:
        cmd += ['-to', str(end)]
    cmd += ['-i', file]

    has_cover = False
    if extra.get('cover') and target in ['mp3', 'flac', 'aac']:
        has_cover = True
        if any(extra['cover'].endswith(ext) for ext in ['mp3', 'flac', 'm4a']):
            extra['cover'] = extract_cover(file)
        cmd += [
            '-i', extra['cover'],
            '-map', '0:a',
            '-map', '1',
            '-disposition:v:0', 'attached_pic'
        ]
    else:
        cmd += ['-map', '0:a']

    cmd += ['-map_metadata', '-1']
    # if format != 'keep':
    cmd += ['-acodec', fmt_info['codec']]
    if fmt_info['bitrate']:
        cmd += ['-b:a', fmt_info['bitrate']]
    if fmt_info['extra_args']:
        cmd += fmt_info['extra_args']
    # else:
    #     cmd += ['-c:a', 'copy']

    if fmt_info['codec'] == 'libmp3lame':
        cmd += ['-id3v2_version', '3']
    if has_cover:
        cmd += ['-metadata:s:v', 'title=Album cover']
        cmd += ['-metadata:s:v', 'comment=Cover (front)']

    if 'title' in extra:
        cmd.extend(['-metadata', f'title={extra["title"]}'])
    if 'artist' in extra:
        cmd.extend(['-metadata', f'artist={extra["artist"]}'])
    if 'album' in extra:
        cmd.extend(['-metadata', f'album={extra["album"]}'])
    if 'album_artist' in extra:
        cmd.extend(['-metadata', f'album_artist={extra["album_artist"]}'])

    cmd += ['-y', output]

    print(cmd)

    subprocess.run(cmd, capture_output=True, check=True, text=True, encoding='utf-8', timeout=120)

def get_cached_segment(audio_file, start = None, end = None, extra = None):
    """生成音频片段并返回缓存文件路径，支持 album_artist 元数据"""
    if not extra:
        extra = {}
    key = hashlib.md5(f"{Path(audio_file).resolve().as_posix()}_{start or 'start'}_{end or 'end'}".encode()).hexdigest()
    cache_file = os.path.join(CACHE_DIR, f"{key}.mp3")

    if not os.path.exists(cache_file):
        export_song(cache_file, 'mp3', audio_file, start, end, extra)
    return cache_file

def generate_unique_filename(target_dir, base_filename, ext, duplicate_mode):
    """根据重名策略生成唯一文件名，返回 (final_path, action)"""
    ext = ext.lstrip('.')
    base_path = os.path.join(target_dir, base_filename)
    final_path = f"{base_path}.{ext}"
    if not os.path.exists(final_path):
        return final_path, 'new'
    if duplicate_mode == 'overwrite':
        return final_path, 'overwrite'
    elif duplicate_mode == 'skip':
        return final_path, 'skip'
    elif duplicate_mode == 'rename':
        counter = 1
        while True:
            new_name = f"{base_path} ({counter}).{ext}"
            if not os.path.exists(new_name):
                return new_name, 'renamed'
            counter += 1
    else:
        return final_path, 'overwrite'  # fallback

def export_selected_songs(target_dir, commands, ext, template, duplicate_mode):
    """
    导出选中的歌曲列表，生成器用于流式输出进度
    songs: 列表，每个元素包含基本字段，且必须包含 'selectedCue' 和 'selectedIndex'（若缺省则使用默认）
    meta_data: 已有的元数据字典（用于更新）
    """
    print(target_dir, commands, ext, template, duplicate_mode)
    total = len(commands)
    success_count = 0
    skip_count = 0
    error_count = 0

    os.makedirs(target_dir, exist_ok=True)

    for idx, song in enumerate(commands):
        filename_template = template
        filename = filename_template.replace('{title}', song['extra'].get('title', '未知标题'))\
                                    .replace('{artist}', song['extra'].get('artist', '未知演奏者'))\
                                    .replace('{album}', song['extra'].get('album', '未知专辑'))\
                                    .replace('{track}', str(song['extra'].get('index', '')))\
                                    .replace('{ext}', os.path.splitext(song['file'])[1] if ext == 'keep' else ext)
        illegal_chars = r'[\\/:*?"<>|]'
        filename = re.sub(illegal_chars, '_', filename)

        final_path, action = generate_unique_filename(target_dir, filename, os.path.splitext(song['file'])[1] if ext == 'keep' else ext, duplicate_mode)
        if action == 'skip':
            skip_count += 1
            yield {'event': 'progress', 'current': idx+1, 'total': total, 'title': song['extra'].get('title', '未知标题'), 'status': 'skipped', 'msg': '文件已存在，已跳过'}
            continue
        try:
            export_song(final_path, ext, **song)
            yield {'event': 'progress', 'current': idx+1, 'total': total, 'title': song['extra'].get('title', '未知标题'), 'status': 'success' if action == 'new' else action, 'path': final_path}

        except subprocess.CalledProcessError as e:
            error_count += 1
            yield {'event': 'progress', 'current': idx+1, 'total': total, 'title': song['extra'].get('title', '未知标题'), 'status': 'error', 'msg': f'FFmpeg错误: {e.stderr.decode()}'}
        except Exception as e:
            error_count += 1
            yield {'event': 'progress', 'current': idx+1, 'total': total, 'title': song['extra'].get('title', '未知标题'), 'status': 'error', 'msg': str(e)}

    yield {'event': 'complete', 'total': total, 'success': success_count, 'skipped': skip_count, 'errors': error_count}

# ---------- Flask 路由 ----------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/scan', methods=['POST'])
def api_scan():
    data = request.get_json()
    dirs = data.get('dirs', [])
    if not dirs:
        return jsonify({"success": False, "error": "目录列表为空"})
    try:
        result = scan_directories(dirs)
        return jsonify({"success": True, "result": result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/browse_dir', methods=['GET'])
def api_browse_dir():
    initial = request.args.get('initial')
    title = request.args.get('title', "选择目录")
    try:
        root = tk.Tk()
        root.withdraw()
        try:
            root.tk.call('tk', 'scaling', 1.5)
        except:
            pass
        root.attributes('-topmost', True)
        folder_path = filedialog.askdirectory(title=title, initialdir=initial)
        root.destroy()
        if folder_path:
            return jsonify({"success": True, "path": folder_path})
        else:
            return jsonify({"success": False, "error": "未选择任何目录"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/browse_file', methods=['GET'])
def api_browse_file():
    initial = request.args.get('initial')
    title = request.args.get('title', "选择文件")
    try:
        root = tk.Tk()
        root.withdraw()
        try:
            root.tk.call('tk', 'scaling', 1.5)
        except:
            pass
        root.attributes('-topmost', True)
        file_path = filedialog.askopenfile(title=title, initialdir=initial)
        root.destroy()
        if file_path:
            return jsonify({"success": True, "path": file_path})
        else:
            return jsonify({"success": False, "error": "未选择任何目录"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/compute', methods=['POST'])
def api_compute():
    data = request.get_json()
    songs = data.get('songs', [])
    selected = data.get('selected', [])
    K = data.get('K', 20)
    w_c = data.get('w_c', 10.0)
    w_m = data.get('w_m', 5.0)
    w_e = data.get('w_e', 1.0)
    if not songs or not selected:
        return jsonify({"success": False, "error": "数据不足"})
    try:
        schemes = compute_schemes(songs, selected, K, w_c, w_m, w_e)
        return jsonify({
            "success": True,
            "schemes": schemes,
            "totalSelected": len(selected)
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/audio_segment')
def audio_segment():
    file = request.args.get('file')
    start = request.args.get('start')
    end = request.args.get('end')
    extra = json.loads(request.args.get('extra', '{}'))
    if not any(file.endswith(ext) for ext in AUDIO_EXT):
        abort(400)
    path = get_cached_segment(file, start, end, extra)
    return send_file(path, conditional=True)

@app.route('/api/cover')
def cover_image():
    path = request.args.get('path')
    if not path or not os.path.exists(path):
        abort(404)
    ext = os.path.splitext(path)[1].lower()
    if ext in ['.mp3', '.flac', '.m4a']:
        path = extract_cover(path)
        return send_file(path, conditional=True)
    elif ext not in ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp']:
        abort(400)
    return send_file(path, conditional=True)

import platform
import subprocess

@app.route('/api/open_explorer', methods=['POST'])
def api_open_file():
    """在文件管理器中打开并选中指定文件"""
    data = request.get_json()
    filepath = data.get('path', '')
    select = data.get('select', True)
    if not filepath or not os.path.exists(filepath):
        return jsonify({"success": False, "error": "文件不存在"})
    try:
        system = platform.system()
        if system == 'Windows':
            subprocess.Popen(['explorer', '/select,', os.path.normpath(filepath)]
                             if select else ['explorer', os.path.normpath(filepath)])
        elif system == 'Darwin':  # macOS
            subprocess.Popen(['open', '-R', filepath]
                             if select else ['open', filepath])
        else:  # Linux
            for cmd in ['nautilus', 'dolphin', 'thunar']:
                try:
                    subprocess.Popen([cmd] + (['--select'] if select else []) + [filepath])
                    break
                except FileNotFoundError:
                    continue
            else:
                return jsonify({"success": False, "error": "未找到可用的文件管理器"})
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/export', methods=['POST'])
def api_export():
    data = request.get_json()
    commands = data.get('commands', [])
    target_dir = data.get('targetDir', '')
    format = data.get('format', 'keep')
    template = data.get('template', '{title} - {artist}')
    duplicate = data.get('duplicate', 'rename')

    if not commands:
        return jsonify({"success": False, "error": "没有要导出的歌曲"}), 400
    if not target_dir:
        return jsonify({"success": False, "error": "未指定保存目录"}), 400
    if not os.path.exists(target_dir):
        try:
            os.makedirs(target_dir, exist_ok=True)
        except Exception as e:
            return jsonify({"success": False, "error": f"无法创建目录: {str(e)}"}), 400

    def generate():
        for event_data in export_selected_songs(target_dir, commands, format, template, duplicate):
            yield f"data: {json.dumps(event_data)}\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream')
    
if __name__ == '__main__':
    app.run(debug=True, host='127.0.0.1', port=5050)