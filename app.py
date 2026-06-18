import os
import re
import random
import unicodedata
import ctypes
import subprocess
import hashlib
import tempfile
from collections import defaultdict
from flask import Flask, request, jsonify, render_template, send_file, abort
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ---------- Windows DPI 感知 ----------
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

import tkinter as tk
from tkinter import filedialog

# ---------- 全局缓存 ----------
CUE_TRACKS_CACHE = {}
CACHE_DIR = os.path.join(tempfile.gettempdir(), 'cue_audio_cache')
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

def get_audio_duration(filepath):
    try:
        cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
               '-of', 'default=noprint_wrappers=1:nokey=1', filepath]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except:
        pass
    return None

# ---------- 查找封面 ----------
def find_cover_image(cue_dir, cue_basename):
    if not os.path.isdir(cue_dir):
        return None
    exts = ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp']
    cue_base = os.path.splitext(cue_basename)[0]
    candidates = [cue_base, 'cover', 'folder', 'front', 'album', 'art']
    if '-' in cue_base:
        parts = cue_base.split('-')
        if parts[0]:
            candidates.append(parts[0])
    candidates = list(set(candidates))
    for name in candidates:
        for ext in exts:
            candidate_path = os.path.join(cue_dir, name + ext)
            if os.path.exists(candidate_path):
                return candidate_path
    for f in os.listdir(cue_dir):
        if f.lower().endswith(tuple(exts)):
            image_base = os.path.splitext(f)[0]
            if cue_base.startswith(image_base) and image_base:
                return os.path.join(cue_dir, f)
    return None

# ---------- 解析 CUE（支持 INDEX 00/01，返回 track 表演者） ----------
def parse_cue_with_times(content, cue_dir=''):
    audio_file = None
    tracks = []
    album_title = None
    album_artist = None
    global_performer = None
    current_track = None
    seen_track = False
    index00 = None
    index01 = None

    try:
        lines = content.splitlines()
        for line in lines:
            line = line.strip()
            if line.upper().startswith('FILE '):
                m = re.search(r'FILE\s+"(.*?)"', line, re.IGNORECASE)
                if m:
                    audio_file = m.group(1).strip()
                    if cue_dir and not os.path.isabs(audio_file):
                        audio_file = os.path.join(cue_dir, audio_file)
            elif line.upper().startswith('PERFORMER '):
                m = re.search(r'PERFORMER\s+"(.*?)"', line, re.IGNORECASE)
                if m:
                    performer = m.group(1).strip()
                    if not seen_track:
                        album_artist = performer
                        global_performer = performer
                    else:
                        if current_track:
                            current_track['performer'] = performer
            elif line.upper().startswith('TITLE '):
                m = re.search(r'TITLE\s+"(.*?)"', line, re.IGNORECASE)
                if m:
                    title = m.group(1).strip()
                    if not seen_track:
                        album_title = title
                    else:
                        if current_track:
                            current_track['title'] = title
            elif line.upper().startswith('TRACK '):
                if current_track is not None:
                    current_track['start'] = index01 if index01 is not None else index00
                    current_track['index00'] = index00
                    current_track['index01'] = index01
                    tracks.append(current_track)
                current_track = {
                    'title': None,
                    'performer': None,
                    'start': None,
                    'end': None,
                    'index00': None,
                    'index01': None
                }
                index00 = None
                index01 = None
                seen_track = True
            elif line.upper().startswith('INDEX 00 '):
                m = re.search(r'INDEX\s+00\s+(.*?)$', line, re.IGNORECASE)
                if m:
                    index00 = parse_index_time(m.group(1))
            elif line.upper().startswith('INDEX 01 '):
                m = re.search(r'INDEX\s+01\s+(.*?)$', line, re.IGNORECASE)
                if m:
                    index01 = parse_index_time(m.group(1))

        if current_track is not None:
            current_track['start'] = index01 if index01 is not None else index00
            current_track['index00'] = index00
            current_track['index01'] = index01
            tracks.append(current_track)

        for track in tracks:
            if not track.get('performer'):
                track['performer'] = album_artist

        # 计算结束时间
        if tracks and audio_file and os.path.exists(audio_file):
            duration = get_audio_duration(audio_file)
            for i in range(len(tracks)):
                if i + 1 < len(tracks):
                    next_track = tracks[i+1]
                    end_time = next_track.get('index00') or next_track.get('index01') or next_track.get('start')
                    tracks[i]['end'] = end_time
                else:
                    if duration is not None:
                        tracks[i]['end'] = duration
                    else:
                        tracks[i]['end'] = tracks[i]['start'] + 300
        else:
            for i in range(len(tracks)):
                if i + 1 < len(tracks):
                    next_track = tracks[i+1]
                    end_time = next_track.get('index00') or next_track.get('index01') or next_track.get('start')
                    tracks[i]['end'] = end_time
                else:
                    tracks[i]['end'] = tracks[i]['start'] + 300

    except Exception as e:
        print(f"解析CUE出错: {e}")

    return audio_file, tracks, album_title, album_artist

# ---------- 扫描多个目录 ----------
def scan_directories(dir_list):
    global CUE_TRACKS_CACHE
    song_map = defaultdict(lambda: {
        "norm_title": "",
        "original_titles": set(),
        "performers": set(),
        "cueFiles": set(),
        "audioFiles": set(),
        "trackIndexes": [],
        "cueCoverMap": {},
        "cueAlbumMap": {},
        "cueArtistMap": {},
        "cueTrackPerformerMap": {},
        "cueTrackTitleMap": {}   # 新增：每个 cue 对应的原始曲目标题
    })

    for root_dir in dir_list:
        if not os.path.isdir(root_dir):
            continue
        for dirpath, _, filenames in os.walk(root_dir):
            for f in filenames:
                if f.lower().endswith('.cue'):
                    full = os.path.join(dirpath, f)
                    cover_path = find_cover_image(dirpath, f)
                    try:
                        with open(full, 'r', encoding='utf-8-sig', errors='ignore') as fp:
                            content = fp.read()
                        audio_file, tracks, album_title, album_artist = parse_cue_with_times(content, dirpath)
                        if tracks:
                            CUE_TRACKS_CACHE[full] = {'audio_file': audio_file, 'tracks': tracks}
                            album_title = album_title or ''
                            album_artist = album_artist or ''
                            for idx, track in enumerate(tracks):
                                raw_title = track.get('title')
                                if not raw_title:
                                    continue
                                norm = normalize_title(raw_title)
                                info = song_map[norm]
                                info["norm_title"] = norm
                                info["original_titles"].add(raw_title)
                                performer = track.get('performer') or album_artist
                                if performer:
                                    info["performers"].add(performer)
                                info["cueFiles"].add(full)
                                if audio_file and os.path.exists(audio_file):
                                    info["audioFiles"].add(audio_file)
                                info["trackIndexes"].append((full, idx))
                                info["cueCoverMap"][full] = cover_path
                                info["cueAlbumMap"][full] = album_title
                                info["cueArtistMap"][full] = album_artist
                                info["cueTrackPerformerMap"][full] = performer or ''
                                info["cueTrackTitleMap"][full] = raw_title   # 新增
                    except Exception as e:
                        print(f"处理CUE {full} 出错: {e}")

    songs = []
    for norm, info in song_map.items():
        audio_list = [af for af in info["audioFiles"] if os.path.exists(af)]
        audio_path = audio_list[0] if audio_list else None
        track_ref = info["trackIndexes"][0] if info["trackIndexes"] else (None, 0)
        cover = next((c for c in info["cueCoverMap"].values() if c), None)
        songs.append({
            "id": norm,
            "original_titles": list(info["original_titles"]),
            "performers": list(info["performers"]),
            "cueFiles": list(info["cueFiles"]),
            "cueTrackMap": {cue: idx for cue, idx in info["trackIndexes"]},
            "cueCoverMap": info["cueCoverMap"],
            "cueAlbumMap": info["cueAlbumMap"],
            "cueArtistMap": info["cueArtistMap"],
            "cueTrackPerformerMap": info["cueTrackPerformerMap"],
            "cueTrackTitleMap": info["cueTrackTitleMap"],   # 新增
            "audioFile": audio_path,
            "cueRef": track_ref[0],
            "trackIndex": track_ref[1],
            "cover": cover,
            "path": list(info["cueFiles"])[0]
        })
    songs.sort(key=lambda x: x["id"])
    return {"songs": songs, "cueCount": len(CUE_TRACKS_CACHE)}

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
    # 扫描所有 song 数据，建立 cue -> 歌曲出现次数的映射
    cue_all_counts = defaultdict(lambda: defaultdict(int))  # cue_path -> {song_id: count}
    all_song_ids = set()

    for song in songs:
        sid = song.get('id')
        all_song_ids.add(sid)
        for cue_path in song.get('cueFiles', []):
            cue_all_counts[cue_path][sid] += 1

    # 只保留至少与一个目标歌曲有关的 cue？不，非目标歌曲也会带来 extra，所以即使没目标歌曲的 cue 也可能被选（但只会增加成本），
    # 我们可以提前丢弃“不含任何目标歌曲且不含任何非目标歌曲”的 cue（不可能发生），但保留空 cue 无意义。
    # 简单起见，保留所有 cue，由求解器决定。
    cue_paths = list(cue_all_counts.keys())
    if not cue_paths:
        return []

    # 分配歌曲 ID 索引
    all_songs = sorted(all_song_ids)
    song_to_idx = {sid: i for i, sid in enumerate(all_songs)}
    target_idx = {song_to_idx[t] for t in target_set}
    total_songs = len(all_songs)
    total_targets = len(target_set)

    # 构建每个 cue 的计数数组（长度为 total_songs）
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

        # 变量：是否选中 cue
        x = [model.NewBoolVar(f'x_{i}') for i in range(len(cue_paths))]

        # 每首歌的总出现次数（上界：所有 cue 计数之和）
        max_cnt = [sum(cue_counts[i][s] for i in range(len(cue_paths))) for s in range(total_songs)]
        cnt = [model.NewIntVar(0, max_cnt[s], f'cnt_{s}') for s in range(total_songs)]

        # 目标歌曲缺失标志（布尔）
        miss = [model.NewBoolVar(f'miss_{t}') for t in range(total_targets)]

        # 多余数（整数）
        extra = [model.NewIntVar(0, max_cnt[s], f'extra_{s}') for s in range(total_songs)]

        # 约束：计数
        for s in range(total_songs):
            model.Add(cnt[s] == sum(cue_counts[i][s] * x[i] for i in range(len(cue_paths))))

        # 缺失约束：对每个目标歌曲 t（原始索引 target_idx 转换为 miss 数组索引）
        # 建立 target_idx 到 miss 数组索引的映射
        target_list = sorted(target_idx)  # 保持固定顺序，与 miss 数组对应
        t_to_miss_idx = {t: idx for idx, t in enumerate(target_list)}
        for t in target_idx:
            mi = t_to_miss_idx[t]
            model.Add(cnt[t] >= 1 - miss[mi])

        # 多余约束
        for s in range(total_songs):
            need = 1 if s in target_idx else 0
            model.Add(extra[s] >= cnt[s] - need)
            model.Add(extra[s] >= 0)

        # 目标函数
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
    solver.parameters.num_search_workers = 8  # 多线程加速

    # ----- 3. 迭代收集前 K 个解 -----
    solutions = []   # 元素: (cost, selected_indices, miss_songs, extra_dict)
    # 用于禁止已找到解的约束存储
    forbidden_selections = []

    while len(solutions) < K:
        status = solver.Solve(model)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            break

        # 提取解
        selected = [i for i, var in enumerate(x) if solver.Value(var)]
        cost_val = solver.ObjectiveValue()

        # 计算缺失和多余详情
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

        # 禁止当前精确组合（不允许再次选出完全相同的 cue 子集）
        if not selected:
            # 极少情况：一个 cue 都不选，禁止空集
            model.Add(sum(x) >= 1)
        else:
            # 禁止该解的标准方式
            model.Add(
                sum(x[i] for i in selected) -
                sum(x[i] for i in range(len(cue_paths)) if i not in selected)
                <= len(selected) - 1
            )

        # 可选：若解的目标值过大，可设置全局上界提前停止，但迭代排除法无需此操作

    # ----- 4. 整理结果输出 -----
    schemes = []
    for cost_val, selected, miss_songs, extra_detail in solutions:
        # 选中的 cue 文件信息
        cue_files_info = []
        for idx in selected:
            fp = cue_paths[idx]
            # 该 cue 中包含的歌曲（按 song id 列出，去重显示）
            contained = [all_songs[s] for s, c in enumerate(cue_counts[idx]) if c > 0]
            cue_files_info.append({"path": fp, "songs": list(set(contained))})

        # 覆盖情况
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

    # 按 diff 和 coverage 二次排序（可选），但主排序已是 _cost 升序
    schemes.sort(key=lambda x: (x["_cost"], x["diff"], -x["coverage"]))
    return schemes[:K]

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
        return jsonify({"success": True, **result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/browse', methods=['GET'])
def api_browse():
    try:
        root = tk.Tk()
        root.withdraw()
        try:
            root.tk.call('tk', 'scaling', 1.5)
        except:
            pass
        root.attributes('-topmost', True)
        folder_path = filedialog.askdirectory(title="选择音乐目录")
        root.destroy()
        if folder_path:
            return jsonify({"success": True, "path": folder_path})
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
    cue_path = request.args.get('cue')
    try:
        track_index = int(request.args.get('index', 0))
    except:
        abort(400, "缺少有效的 index 参数")

    if not cue_path or not os.path.exists(cue_path):
        abort(400, "CUE 文件不存在")

    cache = CUE_TRACKS_CACHE.get(cue_path)
    if not cache:
        try:
            with open(cue_path, 'r', encoding='utf-8-sig', errors='ignore') as f:
                content = f.read()
            audio_file, tracks = parse_cue_with_times(content, os.path.dirname(cue_path))
            cache = {'audio_file': audio_file, 'tracks': tracks}
            CUE_TRACKS_CACHE[cue_path] = cache
        except:
            abort(500, "无法解析CUE文件")

    if track_index >= len(cache['tracks']):
        abort(404, "曲目索引超出范围")

    track = cache['tracks'][track_index]
    audio_file = cache['audio_file']
    if not audio_file or not os.path.exists(audio_file):
        abort(404, "音频文件不存在")

    start = track['start']
    end = track['end']
    duration = end - start
    if duration <= 0:
        duration = 60

    key = hashlib.md5(f"{audio_file}_{start}_{end}".encode()).hexdigest()
    cache_file = os.path.join(CACHE_DIR, f"{key}.mp3")

    if not os.path.exists(cache_file):
        cmd = ['ffmpeg', '-ss', str(start), '-to', str(end), '-i', audio_file,
               '-acodec', 'libmp3lame', '-ab', '192k', '-y', cache_file]
        try:
            subprocess.run(cmd, capture_output=True, check=True, timeout=60)
        except subprocess.CalledProcessError as e:
            abort(500, f"FFmpeg处理失败: {e.stderr.decode()}")
        except FileNotFoundError:
            abort(500, "FFmpeg未安装，请安装FFmpeg")

    return send_file(cache_file, as_attachment=False, conditional=True, mimetype='audio/mpeg')

@app.route('/api/cover')
def cover_image():
    path = request.args.get('path')
    if not path or not os.path.exists(path):
        abort(404)
    ext = os.path.splitext(path)[1].lower()
    if ext not in ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp']:
        abort(400)
    return send_file(path, conditional=True)

import platform
import subprocess

@app.route('/api/open_file', methods=['POST'])
def api_open_file():
    """在文件管理器中打开并选中指定文件"""
    data = request.get_json()
    filepath = data.get('path', '')
    if not filepath or not os.path.exists(filepath):
        return jsonify({"success": False, "error": "文件不存在"})
    try:
        system = platform.system()
        if system == 'Windows':
            subprocess.Popen(['explorer', '/select,', os.path.normpath(filepath)])
        elif system == 'Darwin':  # macOS
            subprocess.Popen(['open', '-R', filepath])
        else:  # Linux
            # 尝试常见的文件管理器
            for cmd in [['nautilus', '--select'], ['dolphin', '--select'], ['thunar', '--select']]:
                try:
                    subprocess.Popen(cmd + [filepath])
                    break
                except FileNotFoundError:
                    continue
            else:
                return jsonify({"success": False, "error": "未找到可用的文件管理器"})
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5050)