import os
import subprocess
import threading
import time
import signal
import requests
import hashlib
from flask import Flask, render_template, send_from_directory, abort, request, redirect, url_for, flash, jsonify
from flask_cors import CORS
from collections import deque
# traceback import removed as it's not used
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode
import shutil # For removing directories

DEFAULT_VIDEO_URL = "https://www.dropbox.com/scl/fi/2w5ai1fda804zfruoj8yn/assets_staytuned0.ts?rlkey=jixrs4b1v3keu4q6hpebmbw5v&st=b1teebao&raw=1"
DEFAULT_VIDEO_FILENAME = "default_video.ts"

VIDEO_DIR = "videos"
STREAM_OUTPUT_DIR = "stream_output"
# The master playlist will be the main entry point
HLS_MASTER_PLAYLIST_NAME = "master.m3u8"
HLS_MASTER_PLAYLIST_PATH = os.path.join(STREAM_OUTPUT_DIR, HLS_MASTER_PLAYLIST_NAME)

video_queue = deque()
played_today = set()
current_ffmpeg_process = None
stop_event = threading.Event()
stream_lock = threading.Lock()
currently_playing_url = None
default_video_path = None

app = Flask(__name__)
CORS(app)
app.secret_key = os.urandom(24)

os.makedirs(VIDEO_DIR, exist_ok=True)
os.makedirs(STREAM_OUTPUT_DIR, exist_ok=True)

def ensure_dropbox_raw_param(url):
    try:
        if not url or not (url.startswith('http://') or url.startswith('https://')):
            return url
        parsed_url = urlparse(url)
        if parsed_url.netloc.lower() == 'www.dropbox.com' or parsed_url.netloc.lower() == 'dropbox.com':
            query_params = parse_qs(parsed_url.query)
            if not ('raw' in query_params and query_params['raw'] == ['1']):
                query_params['raw'] = ['1']
                new_query = urlencode(query_params, doseq=True)
                modified_url = urlunparse((
                    parsed_url.scheme, parsed_url.netloc, parsed_url.path,
                    parsed_url.params, new_query, parsed_url.fragment
                ))
                return modified_url
            else:
                return url
        else:
            return url
    except Exception as e:
        return url

def get_safe_filename(url):
    try:
        parsed_url = urlparse(url)
        path_part = parsed_url.path
        base_name = os.path.basename(path_part)
        _, ext = os.path.splitext(base_name)
        hashed_url = hashlib.sha1(url.encode()).hexdigest()[:10]
        if not ext or len(ext) > 5:
             ext = '.mp4'
        if ext.lower() not in ['.mp4', '.ts', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.m3u8']:
             ext = '.mp4'
        return f"video_{hashed_url}{ext}"
    except Exception as e:
        hashed_url = hashlib.sha1(url.encode()).hexdigest()[:10]
        return f"video_{hashed_url}.mp4"

def download_video(url, output_filename):
    filepath = os.path.join(VIDEO_DIR, output_filename)
    try:
        if os.path.exists(filepath):
            try:
                if os.path.getsize(filepath) > 0:
                    return filepath
            except OSError as e:
                 pass
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, stream=True, timeout=60, headers=headers, allow_redirects=True)
        response.raise_for_status()
        content_type = response.headers.get('content-type', '').lower()
        problematic_types = ['text/html', 'application/json']
        is_likely_video = 'video' in content_type or 'mpegurl' in content_type or 'octet-stream' in content_type or not any(ptype in content_type for ptype in problematic_types)
        if not is_likely_video:
             if 'dropbox.com' in url and 'raw=1' not in url:
                 pass
        with open(filepath, "wb") as f:
            downloaded_size = 0
            for chunk in response.iter_content(chunk_size=8192 * 4):
                if stop_event.is_set():
                    if os.path.exists(filepath): os.remove(filepath)
                    return None
                if chunk:
                    f.write(chunk)
                    downloaded_size += len(chunk)
        if downloaded_size == 0:
             if os.path.exists(filepath): os.remove(filepath)
             return None
        return filepath
    except requests.exceptions.RequestException as e:
        if os.path.exists(filepath): os.remove(filepath)
        return None
    except Exception as e:
        if os.path.exists(filepath): os.remove(filepath)
        return None

def stop_ffmpeg_stream():
    global current_ffmpeg_process
    with stream_lock:
        process_to_stop = current_ffmpeg_process
        if process_to_stop and process_to_stop.poll() is None:
            try:
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/F', '/PID', str(process_to_stop.pid)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    process_to_stop.terminate()
                    try:
                        process_to_stop.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process_to_stop.kill()
                        process_to_stop.wait()
            except Exception as e:
                pass
        if current_ffmpeg_process == process_to_stop:
             current_ffmpeg_process = None

def clean_stream_output_directory():
    """Cleans the stream output directory including subdirectories."""
    if os.path.exists(STREAM_OUTPUT_DIR):
        for item in os.listdir(STREAM_OUTPUT_DIR):
            item_path = os.path.join(STREAM_OUTPUT_DIR, item)
            try:
                if os.path.isdir(item_path):
                    # Remove variant subdirectories (like stream_0, stream_1)
                    if item.startswith('stream_'):
                        shutil.rmtree(item_path)
                elif item.endswith('.ts') or item.endswith('.m3u8'):
                    # Remove master playlist and any stray segments
                    os.remove(item_path)
            except OSError as e:
                # Error logging removed
                pass
    else:
        os.makedirs(STREAM_OUTPUT_DIR, exist_ok=True)


def start_ffmpeg_stream(video_path):
    global current_ffmpeg_process

    abs_video_path = os.path.abspath(video_path)
    if not os.path.exists(abs_video_path):
        return None

    stop_ffmpeg_stream()
    clean_stream_output_directory() # Clean before starting new stream

    # --- ABR Settings ---
    # Resolutions (Width x Height)
    res_360p = "640x360"
    res_480p = "854x480"
    res_720p = "1280x720"

    # Video Bitrates (approximate)
    vb_360p = "800k"
    vb_480p = "1400k"
    vb_720p = "2800k"

    # Max Video Bitrates (slightly higher than target)
    max_vb_360p = "856k"
    max_vb_480p = "1498k"
    max_vb_720p = "2996k"

    # Buffer Sizes (CRF * 1.5 or 2)
    buf_360p = "1200k"
    buf_480p = "2100k"
    buf_720p = "4200k"

    # Audio Bitrate (consistent across variants)
    ab_all = "96k"

    # Encoder Preset (balance between speed and quality)
    preset = "veryfast"

    # HLS Segment Time
    hls_time = "4"
    hls_list_size = "6"

    # Create variant directories
    for i in range(3):
        os.makedirs(os.path.join(STREAM_OUTPUT_DIR, f"stream_{i}"), exist_ok=True)

    ffmpeg_command = [
        'ffmpeg', '-hide_banner', '-loglevel', 'error', # Suppress info messages
        '-re', '-i', abs_video_path,
        '-map', '0:v', '-map', '0:a', # Map input video and audio
        '-map', '0:v', '-map', '0:a',
        '-map', '0:v', '-map', '0:a',

        # --- 360p Output ---
        '-c:v:0', 'libx264', '-preset:v:0', preset, '-g:v:0', str(int(hls_time)*25), '-keyint_min:v:0', str(int(hls_time)*25), '-sc_threshold:v:0', '0', # Keyframe settings
        '-vf:0', f'scale=w={res_360p.split("x")[0]}:h={res_360p.split("x")[1]}:force_original_aspect_ratio=decrease,flags=bicubic', # Scaling
        '-b:v:0', vb_360p, '-maxrate:v:0', max_vb_360p, '-bufsize:v:0', buf_360p,
        '-c:a:0', 'aac', '-ar:a:0', '44100', '-b:a:0', ab_all,

        # --- 480p Output ---
        '-c:v:1', 'libx264', '-preset:v:1', preset, '-g:v:1', str(int(hls_time)*25), '-keyint_min:v:1', str(int(hls_time)*25), '-sc_threshold:v:1', '0',
        '-vf:1', f'scale=w={res_480p.split("x")[0]}:h={res_480p.split("x")[1]}:force_original_aspect_ratio=decrease,flags=bicubic',
        '-b:v:1', vb_480p, '-maxrate:v:1', max_vb_480p, '-bufsize:v:1', buf_480p,
        '-c:a:1', 'aac', '-ar:a:1', '44100', '-b:a:1', ab_all,

        # --- 720p Output ---
        '-c:v:2', 'libx264', '-preset:v:2', preset, '-g:v:2', str(int(hls_time)*25), '-keyint_min:v:2', str(int(hls_time)*25), '-sc_threshold:v:2', '0',
        '-vf:2', f'scale=w={res_720p.split("x")[0]}:h={res_720p.split("x")[1]}:force_original_aspect_ratio=decrease,flags=bicubic',
        '-b:v:2', vb_720p, '-maxrate:v:2', max_vb_720p, '-bufsize:v:2', buf_720p,
        '-c:a:2', 'aac', '-ar:a:2', '44100', '-b:a:2', ab_all,

        # --- HLS Settings ---
        '-f', 'hls',
        '-hls_time', hls_time,
        '-hls_list_size', hls_list_size,
        '-hls_flags', 'delete_segments+independent_segments+program_date_time',
        '-hls_segment_filename', os.path.join(STREAM_OUTPUT_DIR, 'stream_%v/segment%05d.ts'),
        '-master_pl_name', HLS_MASTER_PLAYLIST_NAME,
        # Map video streams (0, 2, 4) and audio streams (1, 3, 5) to variants 0, 1, 2
        '-var_stream_map', "v:0,a:1 v:2,a:3 v:4,a:5",
        HLS_MASTER_PLAYLIST_PATH # Output master playlist path
    ]

    try:
        process = subprocess.Popen(ffmpeg_command, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        with stream_lock:
            current_ffmpeg_process = process
        return process
    except FileNotFoundError:
        with stream_lock: current_ffmpeg_process = None
        return None
    except Exception as e:
        with stream_lock: current_ffmpeg_process = None
        return None

def stream_manager():
    global currently_playing_url, default_video_path, current_ffmpeg_process

    modified_default_url = ensure_dropbox_raw_param(DEFAULT_VIDEO_URL)
    temp_default_path = download_video(modified_default_url, DEFAULT_VIDEO_FILENAME)
    if temp_default_path:
         default_video_path = temp_default_path

    predownload_attempted_for_url = None

    while not stop_event.is_set():
        next_video_path = None
        play_url = None
        stop_default_and_process_queue = False

        try:
            with stream_lock:
                ffmpeg_is_running = current_ffmpeg_process and current_ffmpeg_process.poll() is None
                current_url_snapshot = currently_playing_url

                modified_default_url_snapshot = ensure_dropbox_raw_param(DEFAULT_VIDEO_URL)
                if ffmpeg_is_running:
                    if current_url_snapshot != modified_default_url_snapshot and video_queue:
                        next_url_in_queue_raw = video_queue[0]
                        next_url_in_queue_modified = ensure_dropbox_raw_param(next_url_in_queue_raw)
                        if next_url_in_queue_modified != predownload_attempted_for_url:
                            next_filename = get_safe_filename(next_url_in_queue_modified)
                            downloaded_path = download_video(next_url_in_queue_modified, next_filename)
                            predownload_attempted_for_url = next_url_in_queue_modified
                    elif current_url_snapshot == modified_default_url_snapshot and video_queue:
                        stop_default_and_process_queue = True
                        predownload_attempted_for_url = None
                    else:
                        if current_url_snapshot != modified_default_url_snapshot and not video_queue:
                            predownload_attempted_for_url = None
                        pass
                else:
                    predownload_attempted_for_url = None
                    if current_ffmpeg_process and current_ffmpeg_process.poll() is not None:
                        modified_default_url_snapshot = ensure_dropbox_raw_param(DEFAULT_VIDEO_URL)
                        if current_url_snapshot and current_url_snapshot != modified_default_url_snapshot:
                             played_today.add(current_url_snapshot)
                        current_ffmpeg_process = None
                        currently_playing_url = None

                    if video_queue:
                        raw_url_from_queue = video_queue.popleft()
                        play_url = ensure_dropbox_raw_param(raw_url_from_queue)
                        filename = get_safe_filename(play_url)
                        next_video_path = download_video(play_url, filename)
                        if not next_video_path:
                            play_url = None
                            currently_playing_url = None
                        else:
                             currently_playing_url = play_url
                    elif default_video_path:
                        modified_default_url_snapshot = ensure_dropbox_raw_param(DEFAULT_VIDEO_URL)
                        next_video_path = default_video_path
                        play_url = modified_default_url_snapshot
                        currently_playing_url = play_url
                    else:
                        currently_playing_url = None
                        pass

            if stop_default_and_process_queue:
                stop_ffmpeg_stream()
                time.sleep(0.5)
                continue

            if next_video_path and play_url:
                # Loop parameter removed, start_ffmpeg_stream now always does ABR
                started_process = start_ffmpeg_stream(next_video_path)
                if not started_process:
                     with stream_lock:
                         if currently_playing_url == play_url:
                             currently_playing_url = None

            if ffmpeg_is_running:
                 time.sleep(1)
            elif not next_video_path:
                 time.sleep(3)
            else:
                 time.sleep(0.5)
        except Exception as e:
             # Error logging removed
             try:
                 stop_ffmpeg_stream()
             except Exception as stop_err:
                  pass
             with stream_lock:
                 currently_playing_url = None
                 predownload_attempted_for_url = None
             time.sleep(5)

    stop_ffmpeg_stream()


@app.route('/')
def index():
    # Pass master playlist name to template if needed, or configure player directly
    return render_template('index.html', master_playlist_name=HLS_MASTER_PLAYLIST_NAME)

@app.route('/admin')
def admin_panel():
    with stream_lock:
        queue_snapshot = list(video_queue)
        played_snapshot = list(played_today)
        current_url_snapshot = currently_playing_url
        is_ffmpeg_running = current_ffmpeg_process and current_ffmpeg_process.poll() is None
        status_detail = ""
        if is_ffmpeg_running and video_queue:
            next_in_queue_raw = video_queue[0]
            status_detail = f" | এরপর কিউতে: {next_in_queue_raw[:50]}..."

    modified_default_url = ensure_dropbox_raw_param(DEFAULT_VIDEO_URL)
    if is_ffmpeg_running:
        # Mode indication changed to ABR
        mode = "[ABR 360p,480p,720p]"
        if current_url_snapshot == modified_default_url:
            current_status = f"ডিফল্ট ভিডিও চলছে {mode}{status_detail}"
        elif current_url_snapshot:
            current_status = f"চলছে: {current_url_snapshot[:80]}... {mode}{status_detail}"
        else:
            current_status = f"একটি ভিডিও চলছে (URL অজানা) {mode}"
    else:
        current_status = "⭕ কোনো ভিডিও চলছে না"
        if video_queue:
             current_status += f" | প্লে করার অপেক্ষায়: {video_queue[0][:50]}..."

    return render_template('admin.html',
                           queue=queue_snapshot,
                           current_status=current_status,
                           played=played_snapshot)

@app.route('/admin/add', methods=['POST'])
def add_video_form():
    url_from_form = request.form.get('video_url', '').strip()
    if url_from_form:
        if url_from_form.startswith('http://') or url_from_form.startswith('https://'):
            url_to_add = ensure_dropbox_raw_param(url_from_form)
            with stream_lock:
                if url_to_add in video_queue:
                     flash(f'"{url_to_add[:50]}..." এই URL টি ইতিমধ্যে কিউতে আছে (সম্ভবত raw=1 সহ)।', 'warning')
                else:
                    video_queue.append(url_to_add)
                    flash(f'"{url_to_add[:50]}..." সফলভাবে কিউতে যোগ করা হয়েছে।', 'success')
            return redirect(url_for('admin_panel'))
        else:
            flash('অবৈধ URL! অনুগ্রহ করে http:// বা https:// দিয়ে শুরু হওয়া একটি URL দিন।', 'error')
    else:
        flash('URL খালি রাখা যাবে না।', 'error')
    return redirect(url_for('admin_panel'))

@app.route('/admin/clear_queue', methods=['POST'])
def clear_queue_form():
    with stream_lock:
        if video_queue:
            video_queue.clear()
            flash('ভিডিও কিউ সফলভাবে খালি করা হয়েছে।', 'success')
        else:
             flash('ভিডিও কিউ আগে থেকেই খালি ছিল।', 'info')
    return redirect(url_for('admin_panel'))

@app.route('/admin/clear_played', methods=['POST'])
def clear_played_form():
    with stream_lock:
        if played_today:
            played_today.clear()
            flash("'আজকে চালানো হয়েছে' তালিকা খালি করা হয়েছে।", 'success')
        else:
             flash("'আজকে চালানো হয়েছে' তালিকা আগে থেকেই খালি ছিল।", 'info')
    return redirect(url_for('admin_panel'))


@app.route('/add', methods=['GET'])
def add_video_api():
    url_from_request = request.args.get('link', '').strip()
    if not url_from_request:
        return jsonify({'status': 'error', 'message': 'Missing "link" parameter.'}), 400
    if not (url_from_request.startswith('http://') or url_from_request.startswith('https://')):
        return jsonify({'status': 'error', 'message': 'Invalid URL format. Must start with http:// or https://', 'url': url_from_request}), 400
    url_to_add = ensure_dropbox_raw_param(url_from_request)
    with stream_lock:
        if url_to_add in video_queue:
            return jsonify({'status': 'warning', 'message': 'Video already in queue.', 'url': url_to_add, 'original_url': url_from_request}), 200
        else:
            video_queue.append(url_to_add)
            return jsonify({'status': 'success', 'message': 'Video added to queue.', 'url': url_to_add, 'original_url': url_from_request}), 200

@app.route('/delete', methods=['GET'])
def delete_video_api():
    link_param = request.args.get('link', '').strip()
    if not link_param:
        return jsonify({'status': 'error', 'message': 'Missing "link" parameter.'}), 400
    with stream_lock:
        if link_param.lower() == 'all':
            if video_queue:
                queue_len = len(video_queue)
                video_queue.clear()
                return jsonify({'status': 'success', 'message': f'Queue cleared. {queue_len} items removed.'}), 200
            else:
                return jsonify({'status': 'info', 'message': 'Queue was already empty.'}), 200
        else:
            url_from_request = link_param
            if not (url_from_request.startswith('http://') or url_from_request.startswith('https://')):
                 return jsonify({'status': 'error', 'message': 'Invalid URL format for deletion.', 'url': url_from_request}), 400
            url_to_delete = ensure_dropbox_raw_param(url_from_request)
            current_playing_modified = ensure_dropbox_raw_param(currently_playing_url) if currently_playing_url else None
            default_url_modified = ensure_dropbox_raw_param(DEFAULT_VIDEO_URL)
            if url_to_delete == current_playing_modified and url_to_delete != default_url_modified:
                 return jsonify({'status': 'error', 'message': 'Cannot delete the currently playing video.', 'url': url_to_delete, 'original_url': url_from_request}), 403
            try:
                video_queue.remove(url_to_delete)
                return jsonify({'status': 'success', 'message': 'Video removed from queue.', 'url': url_to_delete, 'original_url': url_from_request}), 200
            except ValueError:
                return jsonify({'status': 'error', 'message': 'Video not found in queue.', 'url': url_to_delete, 'original_url': url_from_request}), 404


@app.route('/stream/<path:filename>')
def stream(filename):
    # This route now needs to serve master.m3u8, stream_X/stream.m3u8 and stream_X/segmentY.ts
    stream_abs_path = os.path.abspath(STREAM_OUTPUT_DIR)
    safe_base = os.path.normpath(stream_abs_path)
    # Use safe_join to prevent escaping the directory, although send_from_directory handles it
    file_abs_path = os.path.normpath(os.path.join(safe_base, filename))

    # Security check: Ensure the requested path is within the STREAM_OUTPUT_DIR
    if not file_abs_path.startswith(safe_base):
        abort(403)

    # Check if the *directory* part of the path exists if it includes a subdirectory
    directory_part = os.path.dirname(file_abs_path)
    if not os.path.isdir(directory_part):
         abort(404) # Directory doesn't exist

    # Check if the *file* itself exists
    if not os.path.isfile(file_abs_path):
        abort(404)

    try:
        # send_from_directory works correctly with subdirectories in `filename`
        response = send_from_directory(safe_base, filename, conditional=True)
        # Crucial headers for live HLS
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response
    except FileNotFoundError:
         abort(404)
    except Exception as e:
        abort(500)

def signal_handler(sig, frame):
    if stop_event.is_set():
        return
    stop_event.set()
    stop_ffmpeg_stream()
    exit(0)

if __name__ == '__main__':
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    manager_thread = threading.Thread(target=stream_manager, name="StreamManager", daemon=True)
    manager_thread.start()

    host = '0.0.0.0'
    port = 5000

    try:
        # Note: Flask's development server is not ideal for high-load production.
        # Consider using a production server like Gunicorn + Nginx.
        app.run(host=host, port=port, threaded=True, use_reloader=False, debug=False)
    except Exception as e:
        pass
    finally:
        if not stop_event.is_set():
            stop_event.set()
        if manager_thread.is_alive():
            manager_thread.join(timeout=10)
        stop_ffmpeg_stream()

